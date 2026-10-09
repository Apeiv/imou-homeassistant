/*
 * hik_noi — nói ra loa camera Hikvision / EZVIZ qua HCNetSDK (cổng thiết bị 8000, hoặc 8443 = SDK qua TLS).
 *
 * Vì sao có chương trình C này: container Home Assistant (cả HA OS) chạy Alpine (musl), không nạp
 * được HCNetSDK (dựng cho glibc). Chương trình này dựng với glibc và được chạy qua trình nạp
 * glibc mang theo:  ld-linux-x86-64.so.2 --library-path <glibc>:<sdk> hik_noi IP CỔNG TÀI_KHOẢN
 * Mã nguồn đi kèm để ai cũng dựng lại được:  gcc -O2 -o hik_noi hik_noi.c -ldl
 *
 * Giao thức với tích hợp:
 *   env HIK_LIB = thư mục lib của HCNetSDK, HIK_MK = mật khẩu (mã xác minh EZVIZ),
 *       HIK_NGHI = ngồi yên (kênh đóng) ngần này giây thì đăng xuất và thoát (mặc định 60)
 *       HIK_NGHE_FD = fd nhận tiếng MIC CAMERA mà SDK đưa về trong lúc kênh đàm thoại mở (khung mã hoá
 *       nguyên như camera gửi: AAC ADTS hay G.711, cùng mã với chiều nói); không đặt thì bỏ tiếng ấy
 *   kênh báo (fd trong HIK_BAO_FD, mặc định 3), mỗi dòng một tin:
 *       "SAN <mã> <tần_số>" đăng nhập xong  |  "OK" kênh đã mở  |  "DONG" kênh đã đóng
 *       "LOI <mã> <thông điệp>" hỏng — <mã> = mã lỗi HCNetSDK (NET_DVR_GetLastError), 0 nếu không phải lỗi SDK;
 *       tích hợp đọc số này, không đọc chữ. (sau LOI lúc đăng nhập thì chương trình thoát; LOI GIỮA LƯỢT — gửi tiếng hỏng
 *       hay SDK báo EXCEPTION_AUDIOEXCHANGE — thì kênh đã đóng, vẫn giữ đăng nhập cho lượt sau)
 *   stdin: mỗi mục = 4 byte độ dài (big-endian) + dữ liệu
 *       0xFFFFFFFF = mở kênh đàm thoại; n > 0 = một khung tiếng; 0 = phát nốt rồi đóng kênh
 *       hết stdin = đóng kênh (nếu đang mở), đăng xuất, thoát
 *   Mã: AAC (khung ADTS 1024 mẫu) hoặc G711U / G711A (khung 160 byte = 20 ms ở 8 kHz)
 *
 * Vì sao giữ đăng nhập giữa các lượt nói: đo 29/09/2026 trên H6C, đăng nhập 0,6–1,3 s và đăng
 * xuất 0,5 s, còn mở kênh chỉ 0,02–0,27 s. Mỗi lượt bộ đàm mà đăng nhập lại thì tiếng dồn hàng
 * đợi suốt lúc ấy và cả câu phát trễ theo — chủ máy nghe "chậm hơn Imou".
 *
 * Đo thật 28/09/2026, EZVIZ H6C: đăng nhập cổng 8000 được, camera báo AAC 16 kHz, gửi khung
 * ADTS mỗi 64 ms thì loa phát (chủ máy nghe xác nhận). SDK tự in rác ra stdout — nên kênh báo
 * là fd 3 riêng.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

typedef struct { char sPath[256]; unsigned char byRes[128]; } SdkPath;
typedef struct {
    char sDeviceAddress[129]; unsigned char byUseTransport; unsigned short wPort;
    char sUserName[64]; char sPassword[64]; void *cbLoginResult; void *pUser; int bUseAsynLogin;
    unsigned char byProxyType, byUseUTCTime, byLoginMode, byHttps; int iProxyID;
    unsigned char byVerifyMode; unsigned char byRes3[119];
} LoginInfo;
typedef struct {
    unsigned char byAudioEncType, byAudioSamplingRate, byAudioBitRate, byres[4], bySupport;
} AudioComp;

typedef int (*f_int)(void);
typedef int (*f_setcfg)(int, void *);
typedef int (*f_conn)(unsigned, unsigned);
typedef int (*f_login)(LoginInfo *, unsigned char *);
typedef int (*f_id)(int);
typedef unsigned (*f_err)(void);
typedef int (*f_comp)(int, AudioComp *);
typedef void (*cb_t)(int, char *, unsigned, unsigned char, void *);
typedef int (*f_start)(int, unsigned, cb_t, void *);
typedef int (*f_send)(int, char *, unsigned);
typedef void (*cb_ngoai_le)(unsigned, int, int, void *);
typedef int (*f_exc)(unsigned, void *, cb_ngoai_le, void *);

static FILE *bao;
/* Issue #2 (30/09/2026): camera rớt mạng giữa bài mà chương trình vẫn gửi tiếp 44 phút — không xem kết quả
 * NET_DVR_VoiceComSendData, không nghe ngoại lệ đàm thoại. SDK gọi hàm này từ luồng riêng; vòng chính đọc cờ. */
#define EXCEPTION_AUDIOEXCHANGE 0x8001
static volatile int kenh_hong = -1;                        /* lHandle kênh đàm thoại SDK báo hỏng */

static void ngoai_le(unsigned loai, int uid, int h, void *u) {
    (void)uid; (void)u;
    if (loai == EXCEPTION_AUDIOEXCHANGE) kenh_hong = h;
}

/* Tiếng mic camera về trong lúc kênh mở (đàm thoại hai chiều): chép ra HIK_NGHE_FD. fd không chặn —
 * bên đọc chậm thì mất khung (hay khung bị cắt dở: ffmpeg tự bắt lại đầu khung ADTS) chứ luồng SDK không treo. */
static int nghe_fd = -1;
static void nghe_mic(int h, char *b, unsigned n, unsigned char f, void *u) {
    (void)h; (void)u;
    if (f != 1) return;                                    /* byAudioFlag: 0 = tiếng máy này, 1 = tiếng camera */
    if (nghe_fd >= 0 && n) { ssize_t r = write(nghe_fd, b, n); (void)r; }
}

static int doc_du(unsigned char *buf, size_t n) {
    size_t da = 0;
    while (da < n) {
        ssize_t r = read(0, buf + da, n - da);
        if (r <= 0) return 0;
        da += (size_t)r;
    }
    return 1;
}

static double bay_gio(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

static void ngu(double giay) {
    if (giay <= 0) return;
    struct timespec t = { (time_t)giay, (long)((giay - (time_t)giay) * 1e9) };
    nanosleep(&t, NULL);
}

#define SYM(kieu, ten) kieu ten = (kieu)dlsym(sdk, #ten); if (!ten) { fprintf(bao, "LOI 0 thiếu hàm " #ten "\n"); return 2; }

int main(int argc, char **argv) {
    const char *fd_bao = getenv("HIK_BAO_FD");            /* số fd kênh báo, mặc định 3 */
    bao = fdopen(fd_bao ? atoi(fd_bao) : 3, "w");
    if (!bao) return 9;
    setvbuf(bao, NULL, _IOLBF, 0);
    signal(SIGPIPE, SIG_IGN);                            /* ffmpeg nghe chết thì write() trả EPIPE, không giết ta */
    if (argc < 4) { fprintf(bao, "LOI 0 thiếu tham số\n"); return 1; }
    const char *lib = getenv("HIK_LIB"), *mk = getenv("HIK_MK");
    if (!lib || !mk) { fprintf(bao, "LOI 0 thiếu HIK_LIB / HIK_MK\n"); return 1; }
    const char *fd_nghe = getenv("HIK_NGHE_FD");
    if (fd_nghe) { nghe_fd = atoi(fd_nghe); fcntl(nghe_fd, F_SETFL, fcntl(nghe_fd, F_GETFL) | O_NONBLOCK); }
    char duong[1024];
    const char *truoc[] = {"libcrypto.so.1.1", "libssl.so.1.1", "libhpr.so", "libHCCore.so"};
    for (int i = 0; i < 4; i++) {
        snprintf(duong, sizeof duong, "%s/%s", lib, truoc[i]);
        if (access(duong, R_OK) == 0) dlopen(duong, RTLD_NOW | RTLD_GLOBAL);
    }
    snprintf(duong, sizeof duong, "%s/libhcnetsdk.so", lib);
    void *sdk = dlopen(duong, RTLD_NOW | RTLD_GLOBAL);
    if (!sdk) { fprintf(bao, "LOI 0 không nạp được HCNetSDK (%s)\n", dlerror()); return 2; }
    SYM(f_setcfg, NET_DVR_SetSDKInitCfg) SYM(f_int, NET_DVR_Init) SYM(f_conn, NET_DVR_SetConnectTime)
    SYM(f_login, NET_DVR_Login_V40) SYM(f_err, NET_DVR_GetLastError) SYM(f_comp, NET_DVR_GetCurrentAudioCompress)
    SYM(f_start, NET_DVR_StartVoiceCom_MR_V30) SYM(f_send, NET_DVR_VoiceComSendData)
    SYM(f_id, NET_DVR_StopVoiceCom) SYM(f_id, NET_DVR_Logout) SYM(f_int, NET_DVR_Cleanup)
    f_exc dat_ngoai_le = (f_exc)dlsym(sdk, "NET_DVR_SetExceptionCallBack_V30");   /* không có thì chỉ dựa kết quả gửi */

    SdkPath p; memset(&p, 0, sizeof p); snprintf(p.sPath, sizeof p.sPath, "%s/", lib);
    NET_DVR_SetSDKInitCfg(2, &p);
    NET_DVR_Init();
    if (dat_ngoai_le) dat_ngoai_le(0, NULL, ngoai_le, NULL);
    NET_DVR_SetConnectTime(5000, 1);
    LoginInfo li; memset(&li, 0, sizeof li);
    snprintf(li.sDeviceAddress, sizeof li.sDeviceAddress, "%s", argv[1]);
    li.wPort = (unsigned short)atoi(argv[2]);
    li.byHttps = li.wPort == 8443;                       /* SDK over TLS, no CA check (byVerifyMode 0) */
    snprintf(li.sUserName, sizeof li.sUserName, "%s", argv[3]);
    snprintf(li.sPassword, sizeof li.sPassword, "%s", mk);
    /* Sống nhiều ngày: xoá mật khẩu khỏi vùng env (đọc được qua /proc/<pid>/environ) ngay khi chép xong. */
    memset((char *)mk, 0, strlen(mk));
    unsetenv("HIK_MK");
    unsigned char dev[1024];
    int uid = NET_DVR_Login_V40(&li, dev);
    if (uid < 0) {
        fprintf(bao, "LOI %u đăng nhập cổng %s không được\n", NET_DVR_GetLastError(), argv[2]);
        NET_DVR_Cleanup(); return 3;
    }
    AudioComp ac; memset(&ac, 0, sizeof ac);
    NET_DVR_GetCurrentAudioCompress(uid, &ac);
    const char *ma = ac.byAudioEncType == 1 ? "G711U" : ac.byAudioEncType == 2 ? "G711A"
                   : (ac.byAudioEncType == 7 || ac.byAudioEncType == 12) ? "AAC" : NULL;
    int tan_so_bang[] = {16000, 16000, 32000, 48000, 44100, 8000};
    int tan_so = ac.byAudioSamplingRate < 6 ? tan_so_bang[ac.byAudioSamplingRate] : 16000;
    if (ma && strcmp(ma, "AAC")) tan_so = 8000;
    if (!ma) {
        fprintf(bao, "LOI 0 camera đòi mã đàm thoại chưa hỗ trợ (%u)\n", ac.byAudioEncType);
        NET_DVR_Logout(uid); NET_DVR_Cleanup(); return 4;
    }
    fprintf(bao, "SAN %s %d\n", ma, tan_so);
    const char *nghi_s = getenv("HIK_NGHI");
    int nghi_ms = (nghi_s ? atoi(nghi_s) : 60) * 1000;
    int aac = !strcmp(ma, "AAC");
    static unsigned char khung[65536];
    int h = -1;
    double t0 = -1, da_phat = 0;
    for (;;) {
        if (h < 0) {                                       /* kênh đóng: chờ lệnh, quá lâu thì thôi */
            struct pollfd pf = { 0, POLLIN, 0 };
            if (poll(&pf, 1, nghi_ms) <= 0) break;
        }
        unsigned char dau[4];
        if (!doc_du(dau, 4)) break;
        uint32_t n = ((uint32_t)dau[0] << 24) | ((uint32_t)dau[1] << 16) | ((uint32_t)dau[2] << 8) | dau[3];
        if (n == 0xFFFFFFFFu) {                            /* mở kênh */
            if (h < 0) { kenh_hong = -1; h = NET_DVR_StartVoiceCom_MR_V30(uid, 1, nghe_mic, NULL); }
            if (h < 0) fprintf(bao, "LOI %u camera không mở kênh đàm thoại\n", NET_DVR_GetLastError());
            else fprintf(bao, "OK\n");
            t0 = -1; da_phat = 0;
            continue;
        }
        if (n == 0) {                                      /* phát nốt phần đệm rồi đóng kênh */
            if (h >= 0) {
                if (t0 >= 0) ngu(t0 + da_phat - bay_gio() + 0.5);
                NET_DVR_StopVoiceCom(h);
                h = -1;
            }
            fprintf(bao, "DONG\n");
            continue;
        }
        if (n > sizeof khung || !doc_du(khung, n)) break;
        if (h < 0) continue;                               /* khung lạc khi kênh đóng: bỏ */
        if (t0 < 0) t0 = bay_gio();
        ngu(t0 + da_phat - bay_gio());
        int gui_duoc = NET_DVR_VoiceComSendData(h, (char *)khung, n);
        if (!gui_duoc || kenh_hong == h) {
            if (!gui_duoc) fprintf(bao, "LOI %u gửi tiếng hỏng\n", NET_DVR_GetLastError());
            else fprintf(bao, "LOI 0 camera rớt kênh đàm thoại (ngoại lệ 0x%x)\n", EXCEPTION_AUDIOEXCHANGE);
            NET_DVR_StopVoiceCom(h);
            h = -1;                                        /* khung còn lại của lượt này bị bỏ; vẫn giữ đăng nhập */
            continue;
        }
        da_phat += aac ? 1024.0 / tan_so : n / 8000.0;
    }
    if (h >= 0) {
        if (t0 >= 0) ngu(t0 + da_phat - bay_gio() + 0.5);
        NET_DVR_StopVoiceCom(h);
    }
    NET_DVR_Logout(uid);
    NET_DVR_Cleanup();
    return 0;
}
