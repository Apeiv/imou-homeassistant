// DB1C doorbell: the Vimar intercom card (same DOM, same CSS, same states and buttons) with a different
// "backend": no SIP and no vimar_intercom WebSocket, but
//   visitor video + voice at rest: go2rtc's own player (go2rtc/video-rtc.js, unmodified, the go2rtc version
//     inside Frigate) on the Frigate integration proxy (/api/frigate/<instance>/mse/api/ws?src=<stream>),
//     modes webrtc,mse,mjpeg (WebRTC at home, MSE through the tunnel, MJPEG as a last resort), URL signed
//     again on every (re)connection;
//   call (hear + talk): ONE signed WebSocket to dahua_talk, /api/dahua_talk/call_ws/<speaker>: the server says
//     {"type":"ready"} (busy = close 4409, 3-minute cap = close 4408), binary 0x01 + PCM16 8 kHz = visitor voice, the card sends
//     0x02 + PCM16 8 kHz = microphone (the Vimar card's protocol and capture, copied). While it is open the
//     <video> is muted;
//   ring: an "on" entity (input_boolean/binary_sensor, or event.*) + an optional input_datetime with the
//     time of the last ring (ring_time: survives a page reload and gives "rang HH:MM");
//   video: ALWAYS live, also at rest (always_live, default true; false = like the Vimar card);
//   history: ALL Frigate events of the camera in a bottom sheet (thumbnail, label, date, duration,
//     paged by `history`), a tap plays the clip in the video box; Frigate's notifications proxy needs no
//     auth (event id), so no expiring signatures;
//   startup: the camera's latest picture (entity_picture, ~0.2 s) until the first live frame arrives;
//   Open: a configured lock/button (none = no Open button);
//   camera only: without `ring` the card is a plain Frigate camera (live, Fit/Fill, history; Listen only if
//     the stream has audio), no talk, ring or Open.
// The states (idle/ringing/calling/in_call/offline) do not exist in HA: the card computes them and hands
// them to the Vimar card as if they were a sensor (wrapped hass, see _wrap).
//
// Needs the Vimar intercom card loaded (the vimar_intercom integration does it): this card extends it.
//
//   type: custom:db1c-doorbell-card
//   camera: camera.front_door                  (required: offline state, still picture)
//   stream: front_door                         (required: go2rtc stream inside Frigate)
//   frigate_camera: front_door                 (required: Frigate camera name for the event history)
//   ring: input_boolean.doorbell_ring          (required for the doorbell; without it: camera only)
//   speaker: media_player.front_door_speaker   (required with `ring`: dahua_talk entity of the call)
//   ring_time: input_datetime.doorbell_last_ring  (optional)
//   lock: lock.front_door                      (optional: Open = lock.open, with the latch; confirm_open: double tap)
//   layout: overlay                            (+ every Vimar card key: listen_on_ring, confirm_open...)
//   colors: { accent: ..., button: ... }       (optional: --db1c-* colours of the pill and the buttons, see README)
//   language: it                               (optional: default HA's language, then English)
//   frigate_instance / history_labels / history / ring_timeout / always_live / ear_buffer: see DEFAULTS

import { VideoRTC } from "./go2rtc/video-rtc.js";

const VIMAR = "vimar-intercom-card";
const TAG = "db1c-doorbell-card";
const VIDEO = "db1c-video-rtc";  // the player below
const STATUS = "sensor.__db1c_status";       // fake: they only exist in the hass the Vimar card sees
const LAST = "sensor.__db1c_last_ring";
const FIT_KEY = "db1c_doorbell_card_fit2";  // new key: the default moved from Fill to Fit
const AR_KEY = "db1c_doorbell_card_ar";     // { stream: width/height } of the last live video: the card's shape from the first paint
const CLIP_SIGN_S = 3600;  // iOS clips: HLS segments carry the playlist's signature, so it must outlive the playback
const RATE = 8000;  // call audio, both ways
const LAN = /^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|\[::1\])/.test(location.hostname);
const REQUIRED = ["camera", "stream", "frigate_camera"];  // + speaker with ring (the doorbell)
const DEFAULTS = {
  name: "Doorbell",
  frigate_instance: "frigate",        // client_id of the Frigate integration
  history_labels: null,               // e.g. [person] to show only some labels
  history: 50,                        // events per history page (scrolling loads the older ones)
  ring_time: null,                    // null = ring time from the ring entity only
  ring_timeout: 30,                   // s: a ring lasts at least this long even if the sensor goes "off" at once
  lock: null,                         // null = no Open button (overrides the Vimar default lock)
  confirm_open: true,                 // Open = two taps (the first arms for 3 s)
  layout: "overlay",                  // the video sits in the card: "popup" shows it only with the popup open
  always_live: true,                  // live video also at rest (false = only on ring/call)
  anchor: "doorbell",                 // URL hash that scrolls to the card; not the Vimar card's one
  ear_buffer: null,                   // s of visitor voice kept in hand; null = 0.12 on a LAN address, else 0.5
};
// History sheet: close animation (ms), drag slop and close distance (px), flick window (ms) and speed (px/ms).
const SHEET_MS = 220, DRAG_SLOP = 8, DRAG_CLOSE = 80, FLICK_MS = 80, FLICK_SPEED = 0.5;
const COLORS = ["accent", "warning", "on-warning", "glass", "ink", "button", "button-ink", "sheet", "sheet-ink"];  // colors: keys -> --db1c-<key>
// User-visible strings, picked by `language:` or HA's language; missing language or key = English.
// Frigate labels are l_<label>: unknown labels show capitalised.
const I18N = {
  en: {
    live: "live", rang: "rang", ringing: "ringing", on_call: "on call", connecting: "connecting…",
    locked: "Locked", unlocked: "Unlocked", open: "Open", locking: "Locking…", unlocking: "Unlocking…", opening: "Opening…",
    jammed: "Jammed", unavailable: "Unavailable", open_door: "Open door", confirm: "Confirm",
    open_aria: "Open the door", open_aria_twice: "Open the door, tap twice", mute: "Mute audio", listen: "Listen",
    talk_ring: "Talk to the visitor", ignore: "Ignore the ring", end: "End conversation",
    back: "Live", back_aria: "Back to live video", events: "Events", events_aria: "Doorbell events", cam_events_aria: "Camera events", close: "Close",
    today: "today", yesterday: "yesterday", ongoing: "ongoing", play: "play",
    no_events: "No events recorded", history_err: "History unavailable",
    answer: "Answer", mic_off: "Mute the microphone", mic_on: "Unmute the microphone", talk_na: "Talk not available",
    busy: "Busy: another phone is talking", call_cap: "Call closed after 3 minutes", call_end: "Call dropped",
    l_person: "Person", l_car: "Car", l_dog: "Dog", l_cat: "Cat", l_doorbell: "Doorbell",  // Frigate labels
  },
  it: {
    live: "dal vivo", rang: "squillo", ringing: "suonano", on_call: "in linea", connecting: "collegamento…",
    locked: "Chiusa", unlocked: "Aperta", open: "Aperta", locking: "Chiude…", unlocking: "Apre…", opening: "Apre…",
    jammed: "Bloccata", unavailable: "Non disponibile", open_door: "Apri porta", confirm: "Conferma",
    open_aria: "Apri la porta", open_aria_twice: "Apri la porta, tocca due volte", mute: "Silenzia l'audio", listen: "Ascolta l'audio",
    talk_ring: "Parla con il visitatore", ignore: "Ignora lo squillo", end: "Chiudi conversazione",
    back: "Dal vivo", back_aria: "Torna al video dal vivo", events: "Eventi", events_aria: "Eventi della porta", cam_events_aria: "Eventi della telecamera", close: "Chiudi",
    today: "oggi", yesterday: "ieri", ongoing: "in corso", play: "riproduci",
    no_events: "Nessun evento registrato", history_err: "Storico non disponibile",
    answer: "Rispondi", mic_off: "Silenzia il microfono", mic_on: "Riattiva il microfono", talk_na: "Parla non disponibile",
    busy: "Occupato: altro telefono in linea", call_cap: "Chiamata chiusa dopo 3 minuti", call_end: "Chiamata interrotta",
    l_person: "Persona", l_car: "Auto", l_dog: "Cane", l_cat: "Gatto", l_doorbell: "Campanello",  // Frigate labels
  },
};
// Static texts: data-t = textContent, data-ta = aria-label; filled by _applyLang (hass, hence the language, comes later).
const CLIP = `<video id="clipv" playsinline controls preload="none" hidden></video>
  <button id="back" data-ta="back_aria" hidden><ha-icon icon="mdi:arrow-left" aria-hidden="true"></ha-icon><span data-t="back"></span></button>`;
const SHEET = `<dialog class="sheet" data-ta="events_aria"><div class="grab"><div class="handle"></div>
  <header><span data-t="events"></span><button class="x" data-t="close"></button></header></div>
  <div class="evl"><div class="sent"></div></div><p class="evx"></p></dialog>`;
const EXTRA_CSS = `
  #video video { width: 100%; height: 100%; object-fit: cover; background: #000; }
  ha-card[data-fit="contain"] #video video { object-fit: contain; }
  /* Startup: the camera's latest picture instead of "waiting for video" until the first frame. */
  ha-card.live.wait .still[src] { display: block; }
  ha-card.live.wait .still[src] + .ph { display: none; }
  /* Fit (default) = whole picture, the card takes the stream's shape; Fill = a 260 px strip.
     Status pill top left, History then Fit/Fill in the corner, 44 px dark rounds bottom left, Open bottom right. */
  :host([layout="overlay"]) .live .media { aspect-ratio: var(--db1c-ar, 1); }
  :host([layout="overlay"]) .live[data-fit="cover"] .media { aspect-ratio: auto; height: 260px; }
  .drawer { display: none !important; }  /* the history is the .sheet */
  ha-card.clip :is(.row, .badge) { display: none !important; }
  #clipv { position: absolute; inset: 0; z-index: 2; width: 100%; height: 100%; object-fit: contain; background: #000; }
  /* Clip loading/buffering: a spinner over the poster. */
  ha-card.cload .media::after { content: ""; position: absolute; left: 50%; top: 50%; z-index: 3; width: 36px; height: 36px;
    margin: -18px 0 0 -18px; box-sizing: border-box; border-radius: 50%; border: 3px solid rgba(255,255,255,.3);
    border-top-color: #fff; animation: db1c-spin .9s linear infinite; pointer-events: none; }
  @keyframes db1c-spin { to { transform: rotate(360deg); } }
  #back { position: absolute; top: 12px; left: 12px; z-index: 4; height: 44px; padding: 0 14px 0 10px; gap: 6px;
    display: inline-flex; align-items: center; border-radius: 22px; font-size: 13px; color: #fff; background: rgba(0,0,0,.45);
    -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  #back[hidden] { display: none; }
  #back ha-icon { --mdc-icon-size: 20px; }
  /* History: bottom sheet like the dashboard's (Tapparelle, PIN): handle, drag down to close, backdrop closes. */
  dialog.sheet { --ink: var(--db1c-sheet-ink, var(--primary-text-color, #1b1b1f)); --dim: var(--secondary-text-color, #6f6a60);
    box-sizing: border-box; padding: 0; border: 0; margin: auto auto 0; width: min(720px, 100vw); max-width: 100vw;
    height: 82dvh; max-height: 82dvh; border-radius: 22px 22px 0 0; overflow: hidden; color: var(--ink);
    background: var(--db1c-sheet, var(--card-background-color, #fff)); transition: transform ${SHEET_MS}ms cubic-bezier(.2,.8,.2,1); }
  dialog.sheet[open] { display: flex; flex-direction: column; animation: db1c-up .28s cubic-bezier(.2,.8,.2,1); }
  @keyframes db1c-up { from { transform: translateY(100%); } }
  dialog.sheet::backdrop { background: rgba(0,0,0,.45); }
  .grab { flex: none; padding: 6px 16px 8px; touch-action: none; cursor: grab; -webkit-user-select: none; user-select: none; }
  .handle { width: 36px; height: 4px; border-radius: 2px; margin: 0 auto 6px; background: var(--dim); opacity: .6; }
  .sheet header { display: flex; align-items: center; justify-content: space-between; font-size: 17px; font-weight: 500; }
  .sheet .x { height: 44px; padding: 0 14px; border: 0; border-radius: 22px; font: inherit; font-size: 14px; color: inherit;
    cursor: pointer; background: color-mix(in srgb, var(--ink) 10%, transparent); }
  .evl { flex: 1; overflow-y: auto; overscroll-behavior: contain; -webkit-overflow-scrolling: touch; display: grid; align-content: start;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 8px; padding: 4px 12px max(16px, env(safe-area-inset-bottom)); }
  .ev { display: grid; grid-template-columns: 128px 1fr; gap: 12px; align-items: center; width: 100%; padding: 6px; text-align: left;
    color: inherit; font: inherit; background: none; cursor: pointer; border-radius: 14px;
    border: 1px solid color-mix(in srgb, var(--primary-color, #2a9d8f) 45%, transparent); }
  .ev:active { background: color-mix(in srgb, var(--ink) 7%, transparent); }
  .ev .th { position: relative; width: 128px; height: 96px; border-radius: 10px; overflow: hidden; background: #000; }
  .ev .th img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .ev .th ha-icon { position: absolute; left: 50%; top: 50%; transform: translate(-50%,-50%); color: #fff; --mdc-icon-size: 32px;
    filter: drop-shadow(0 1px 3px rgba(0,0,0,.6)); }
  .ev b { display: block; font-size: 16px; font-weight: 600; margin-bottom: 6px; }
  .ev small { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--dim); line-height: 22px; font-variant-numeric: tabular-nums; }
  .ev small ha-icon { --mdc-icon-size: 16px; }
  .sent { grid-column: 1 / -1; height: 1px; }
  .evx { margin: 0; padding: 24px; text-align: center; color: var(--dim); }
  .evx:empty { display: none; }
  :host([layout="overlay"]) ha-card.live[data-state="ringing"]::after { content: ""; position: absolute; inset: 0; z-index: 5;
    border-radius: inherit; box-shadow: inset 0 0 0 2px var(--db1c-warning, var(--vi-warn)); pointer-events: none; }
  :host([layout="overlay"]) .live .badge { height: 44px; box-sizing: border-box; max-width: 220px; padding: 0 14px 0 12px; gap: 7px;
    border-radius: 22px; font-size: 13px; font-weight: 400; white-space: nowrap; overflow: hidden; font-variant-numeric: tabular-nums;
    background: var(--db1c-glass, var(--vi-glass)); color: var(--db1c-ink, #fff); }
  :host([layout="overlay"]) .live .badge::before { background: var(--db1c-accent, var(--dot, var(--st))); }
  :host([layout="overlay"]) .live[data-state="ringing"] .badge { background: var(--db1c-warning, var(--vi-warn)); color: var(--db1c-on-warning, #fff); }
  :host([layout="overlay"]) .live[data-state="ringing"] .badge::before { background: currentColor; animation: blink 1s ease-in-out infinite; }
  :host([camera-only]) :is(#talk, #hangup, #view) { display: none !important; }
  :host([layout="overlay"]) .live #log { right: 64px; }
  :host([layout="overlay"]) .live #fit { right: 12px; }
  /* Light glass (like the status pill, more transparent) in light and dark theme: dark text, fixed colours. */
  :host([layout="overlay"]) .live { --g-bg: var(--db1c-button, rgba(255,253,247,.58)); --g-ink: var(--db1c-button-ink, #1b1812); --g-acc: #1f7a6f; --g-warn: #b45309; --g-bad: #b3261e; }
  :host([layout="overlay"]) .live :is(#log, #fit, .row button) { background: var(--g-bg); color: var(--g-ink);
    -webkit-backdrop-filter: blur(14px) saturate(1.2); backdrop-filter: blur(14px) saturate(1.2); }
  :host([layout="overlay"]) .live[data-drawer="true"] #log { background: var(--vi-primary); }
  :host([layout="overlay"]) .live .row { bottom: 6px; height: 56px; display: flex; gap: 8px; border: 0; border-radius: 0;
    background: none; -webkit-backdrop-filter: none; backdrop-filter: none; }
  :host([layout="overlay"]) .live .row button { flex: none; flex-direction: row; gap: 6px; width: 44px; height: 44px; min-height: 0;
    border-radius: 22px; font-size: 14px; }
  :host([layout="overlay"]) .live .row button:not(#open) .lbl { display: none; }
  :host([layout="overlay"]) .live .row #mute { position: static; order: 2; flex: none; flex-direction: row; gap: 0; width: 40px; height: 40px;
    margin: 0; padding: 0; align-self: center; justify-content: center; border-radius: 20px; background: var(--g-bg); }
  :host([layout="overlay"]) .live .row #mute::after { content: none; }
  :host([layout="overlay"]) .live .row #mute[hidden] { display: none; }
  :host([layout="overlay"]) .live .row #mute ha-icon { --mdc-icon-size: 20px; }
  :host([layout="overlay"]) .live .row #mute[aria-pressed="true"] { color: var(--g-acc); }
  :host([layout="overlay"]) .live .ic, :host([layout="overlay"]) .live .ic ha-icon { width: 20px; height: 20px; --mdc-icon-size: 20px; }
  :host([layout="overlay"]) .live #hangup { order: 0; color: var(--g-bad); }
  :host([layout="overlay"]) .live #talk { color: var(--g-ink); }
  :host([layout="overlay"]) .live #talk[aria-pressed="true"] { color: var(--g-acc); }
  :host([layout="overlay"]) .live[data-state="ringing"] #talk { animation: blink 1s ease-in-out infinite; }
  :host([layout="overlay"]) .live #open { margin-left: auto; width: auto; padding: 0 16px 0 12px; color: var(--g-ink); }
  /* real lock state (data-lk) or the tap feedback (class): neutral at rest, accent if open, warning/alert in transition or error */
  :host([layout="overlay"]) .live #open:is([data-lk="open"], .ok) { color: var(--g-acc); }
  :host([layout="overlay"]) .live #open:is([data-lk="warn"], .warn) { color: var(--g-warn); }
  :host([layout="overlay"]) .live #open:is([data-lk="bad"], .bad) { color: var(--g-bad); }`;
const mmss = (t) => { const s = Math.max(0, Math.floor((Date.now() - t) / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const ago = (t, days) => new Date(t).toDateString() === new Date(Date.now() - days * 864e5).toDateString();
// T = the I18N table in use (its language in T.lang, for the dates).
const dm = (t, T) => new Date(t).toLocaleDateString(T.lang, { day: "numeric", month: "short" });
const hm = (t, T, sec) => new Date(t).toLocaleTimeString(T.lang, { hour: "2-digit", minute: "2-digit", ...(sec && { second: "2-digit" }) });
const hhmm = (t, T) => ago(t, 0) ? hm(t, T) : `${dm(t, T)} ${hm(t, T)}`;  // "08:28" today, "8 Oct 08:28" other days
const day = (t, T) => ago(t, 0) ? T.today : ago(t, 1) ? T.yesterday : dm(t, T);
const LIVE = ["ringing", "calling", "in_call"];
const PAGE_EVENTS = ["visibilitychange", "pagehide", "pageshow"];  // listened on window, see _onPageState
// Page left (pagehide) or, on phones/tablets, app in the background (hidden). On a desktop a hidden tab keeps
// the call going: switching tabs must not hang up.
const TOUCH = matchMedia("(pointer: coarse)").matches;
const pageGone = (e) => e?.type === "pagehide" || (TOUCH && document.visibilityState === "hidden");
const dur = (e, T) => {
  if (!e.end_time) return T.ongoing;
  const s = Math.max(1, Math.round(e.end_time - e.start_time));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
};

customElements.whenDefined(VIMAR).then(() => {
  const Vimar = customElements.get(VIMAR);
  // This card leans on the Vimar card's internals: a missing hook means an incompatible version.
  const INCOMPATIBLE = ["_startTalk", "_openAudio"].some((m) => typeof Vimar.prototype[m] !== "function")
    && "db1c-doorbell-card: the loaded Vimar intercom card is too old/new: missing _startTalk/_openAudio";
  if (INCOMPATIBLE) console.error(INCOMPATIBLE);

  // go2rtc's player with HA's signed URL: a signature lasts 30 s, so every (re)connection signs again
  // (`sign` resolves to the ws(s):// URL). No controls, muted until _applyAudio says otherwise; page visibility is
  // the card's (_onPageState). Closed, the "empty src" error of its own ondisconnect is not logged (capture: before
  // the player's listener). play() = the player's, plus `onblocked` when an unmuted play is refused (iOS without a
  // tap). MJPEG has no video events: `onframe` on the first picture.
  customElements.get(VIDEO) || customElements.define(VIDEO, class extends VideoRTC {
    oninit() {
      this.visibilityCheck = false;
      super.oninit();
      this.video.controls = false;
      this.video.muted = true;
      this.addEventListener("error", (e) => !this.ws && !this.pc && e.stopPropagation(), true);
    }

    onconnect() {
      if (!this.isConnected || !this.sign || this.ws || this.pc || this.signing) return false;
      this.signing = this.sign()
        .then((url) => {
          this.wsURL = url;
          if (!document.hidden) super.onconnect();
        }, (e) => {
          console.warn("db1c-doorbell-card: signing failed:", e.message || e);
          this.reconnectTID = setTimeout(() => this.onconnect(), this.RECONNECT_TIMEOUT);
        })
        .finally(() => { this.signing = null; });
      return true;
    }

    ondisconnect() {
      if (this.video) super.ondisconnect();  // never connected: nothing to close
    }

    play() {
      this.video.play().catch((e) => {
        if (e?.name !== "NotAllowedError" || this.video.muted) return;  // AbortError at hangup: not a block
        this.video.muted = true;
        this.onblocked?.();
        this.video.play().catch(() => {});
      });
    }

    onmjpeg() {
      super.onmjpeg();
      const show = this.ondata;
      this.ondata = (d) => {
        show(d);
        this.onframe?.();
        this.onframe = null;
      };
    }
  });

  class Db1cDoorbellCard extends Vimar {
    static getConfigElement() { return undefined; }  // the editor is the Vimar one: YAML only here
    static getStubConfig() { return {}; }

    setConfig(config) {
      if (INCOMPATIBLE) throw new Error(INCOMPATIBLE);  // red error card in Lovelace
      const cam = !config?.ring;
      const missing = (cam ? REQUIRED : [...REQUIRED, "speaker"]).filter((k) => !config?.[k]);
      if (missing.length) throw new Error(`db1c-doorbell-card: missing required option(s): ${missing.join(", ")}`);
      // Camera only: always live in the card (no call to start it, nothing to pop up), no #doorbell anchor.
      super.setConfig({ ...DEFAULTS, ...(cam && { name: "Camera", anchor: null }), ...config,
        ...(cam && { always_live: true, layout: "overlay" }), status: STATUS, last_ring: LAST });
      this.toggleAttribute("camera-only", cam);  // CSS: no talk, hang up (Open: no lock, see _list)
      // colors: { accent: "var(--my-accent)" } -> --db1c-accent on the host (inherited by the shadow DOM)
      for (const k of COLORS) {
        const v = config.colors?.[k];
        if (v == null) this.style.removeProperty(`--db1c-${k}`);
        else this.style.setProperty(`--db1c-${k}`, String(v));
      }
    }

    get _camOnly() { return !this._cfg.ring; }

    set hass(h) {
      this._trackRing(h);
      super.hass = this._wrap(h);
    }

    // The Vimar card reads state and last ring from hass.states: answer them here without copying hass
    // (prototype + two getters, read on every _render).
    _wrap(h) {
      const states = Object.create(h.states, {
        [STATUS]: { get: () => ({ state: this._vstate(), attributes: {} }) },
        [LAST]: { get: () => ({ state: this._lastRing ? new Date(this._lastRing).toISOString() : "unknown", attributes: {} }) },
      });
      // "Open" = lock.open (e.g. Nuki: pulls the latch and the door opens), not the Vimar card's lock.unlock.
      // Only for the configured lock: other shortcuts stay as they are. Double tap: still the Vimar one.
      const callService = (d, s, data, ...r) =>
        h.callService(d, d === "lock" && s === "unlock" && data?.entity_id === this._cfg.lock ? "open" : s, data, ...r);
      return Object.create(h, { states: { value: states }, callService: { value: callService } });
    }

    // No lock and no shortcuts (camera only, doorbell without Open): nothing, not the Vimar card's [null].
    _list() {
      return this._cfg.lock || this._cfg.shortcuts ? super._list() : [];
    }

    _ent(key) {
      return key === "camera" ? this._cfg.camera : super._ent(key);  // never the Vimar camera found in the registry
    }

    _trackRing(h) {
      if (this._camOnly) return;  // a ring_time alone does not ring a camera
      const r = h.states[this._cfg.ring];
      this._ringOn = r?.state === "on";
      const t = h.states[this._cfg.ring_time]?.attributes?.timestamp;  // epoch s: Safari can't Date.parse("YYYY-MM-DD hh:mm")
      const at = t ? t * 1000 : !r ? null : r.entity_id.startsWith("event.") ? Date.parse(r.state)
        : this._ringOn ? Date.parse(r.last_changed) : null;
      if (!at || at === this._lastRing) return;
      this._lastRing = at;
      clearTimeout(this._ringT);  // at the end of the window it redraws itself (idle), without waiting for HA
      this._ringT = setTimeout(() => this._hass && this._render(), at + this._cfg.ring_timeout * 1000 - Date.now() + 50);
    }

    _vstate() {
      const cam = this._hass?.states[this._cfg.camera];
      if (!cam || cam.state === "unavailable") return "offline";
      if (this._session) return this._session;
      const r = this._lastRing, fresh = r && Date.now() - r < this._cfg.ring_timeout * 1000;
      return r && r !== this._declined && (this._ringOn || fresh) ? "ringing" : "idle";
    }

    // The vimar_intercom services become local states: the DB1C has no "call" to place.
    async _call(service) {
      // Hidden: no new session (a microphone permission that resolves after the app went to the background).
      if (this._hidden && (service === "call" || service === "answer")) return;
      this._err.textContent = this._hint;
      this._declined = this._lastRing;  // every action "consumes" the current ring
      if (service === "hangup" || service === "decline") {
        this._stopAudio();
        this._cancelled = this._state === "calling";
        this._session = null;
      } else {  // call / answer
        this._session = service === "answer" || this._connected || this._fellBack ? "in_call" : "calling";
      }
      this._render();
    }

    // The strings table: `language:` > HA's language > English (keys missing in a language: English).
    get _t() {
      const lang = String(this._cfg?.language || this._hass?.locale?.language || "en");  // full tag (en-GB) for the dates
      if (this._T?.req === lang) return this._T;
      let tag = "en";  // a malformed tag (it_IT) would make the date functions throw
      try { tag = Intl.getCanonicalLocales(lang.replace("_", "-"))[0] || tag; } catch { /* keep en */ }
      return (this._T = { ...I18N.en, ...I18N[tag.slice(0, 2).toLowerCase()], lang: tag, req: lang });
    }

    _applyLang() {
      if (!this._open) return;  // not built yet
      const T = this._t;
      this._root.querySelectorAll("[data-t]").forEach((el) => { el.textContent = T[el.dataset.t]; });
      this._root.querySelectorAll("[data-ta]").forEach((el) => el.setAttribute("aria-label", T[el.dataset.ta]));
      this._open.setAttribute("aria-label", this._cfg.confirm_open ? T.open_aria_twice : T.open_aria);
    }

    _render() {
      super._render();
      this._applyLang();
      if (this._state === "ringing" && this._card.classList.contains("clip")) this._stopClip();  // the ring wins over the clip
      // always_live: the Vimar card chose "live" only for ring/call; here it holds at rest too.
      this._card.classList.toggle("live", !!this._live);
      this._open.hidden = !this._list().length;  // no lock/shortcut configured: no Open button
      // Icon-only rounds (the label stays in the aria-label). On ring Talk stays the microphone, Decline = Ignore.
      const ring = this._state === "ringing";
      const call = this._ws;  // Talk = Answer; during the call it mutes the microphone
      this._icon(this._talk, !call ? "mdi:phone" : call.micOff ? "mdi:microphone-off" : "mdi:microphone");
      this._lockState();
      // Listen button (next to the microphone): crossed = muted, plain = audible. One state, _audible
      // (true = chosen by the user or talking, "auto" = listen_on_ring), also valid with the microphone open.
      const hearing = !!this._audible;
      const audio = this._v?.srcObject?.getAudioTracks?.().length || this._rtc?.mseCodecs?.match(/mp4a|opus|flac/);
      this._mute.hidden = !!call || !this._v || (this._camOnly && !audio);  // call: the voice is on; camera: only a stream with audio
      this._icon(this._mute, hearing ? "mdi:volume-high" : "mdi:volume-off");
      this._mute.setAttribute("aria-pressed", hearing);
      const T = this._t;
      this._mute.setAttribute("aria-label", hearing ? T.mute : T.listen);
      this._applyAudio();
      this._talk.setAttribute("aria-label", !call ? (ring ? T.talk_ring : T.answer) : call.micOff ? T.mic_on : T.mic_off);
      this._icon(this._hangup, ring ? "mdi:bell-off" : "mdi:phone-hangup");
      this._hangup.setAttribute("aria-label", ring ? T.ignore : T.end);
    }

    // "Open" shows the real lock state.
    _lockState() {
      const st = this._hass?.states[this._cfg.lock]?.state;
      const T = this._t, [label, icon, lk] = {
        locked: [T.locked, "mdi:lock", ""], unlocked: [T.unlocked, "mdi:lock-open-variant", "open"], open: [T.open, "mdi:door-open", "open"],
        locking: [T.locking, "mdi:lock-clock", "warn"], unlocking: [T.unlocking, "mdi:lock-clock", "warn"], opening: [T.opening, "mdi:lock-clock", "warn"],
        jammed: [T.jammed, "mdi:lock-alert", "bad"], unavailable: [T.unavailable, "mdi:lock-question", "bad"],
      }[st] || [T.open_door, "mdi:lock-question", ""];
      const b = this._open;
      b.dataset.label = label; b.dataset.icon = icon; b.dataset.lk = lk;
      if (!this._flash) { this._icon(b, icon); this._label(b, label); }
    }

    async _openDoor(...a) {
      const p = super._openDoor(...a);
      if (this._armed) this._label(this._armed, this._t.confirm);  // first tap = armed (synchronous, before any await)
      return p;
    }

    _build() {
      super._build();
      const talk = this._talk.onclick;  // the Vimar one starts the call; in the call: microphone mute, not hang up
      this._talk.onclick = () => {
        const c = this._ws;
        if (!c) return talk();
        c.micOff = !c.micOff;
        c.mic.getAudioTracks().forEach((t) => { t.enabled = !c.micOff; });  // silence keeps flowing: the channel stays
        this._render();
      };
      this._talk.after(this._mute);  // in the bottom row, right after the microphone
      this._mute.onclick = () => {  // real tap: the unmuted play() in _applyAudio passes on iOS too
        this._audible = !this._audible;
        this._audioBlocked = false;
        this._render();
      };
      this._root.querySelector("style").textContent += EXTRA_CSS;
      this._photo.disabled = false;  // the history loads only with the sheet open: the button does not wait for the list
      // Fit by default (the whole door); the choice is this card's, not the Vimar card's.
      try { this._cover = localStorage.getItem(FIT_KEY) === "cover"; } catch { this._cover = false; }
      this._applyFit();
      this._shape(this._ar()[this._cfg.stream] || 0, 1, false);  // last live shape, before any frame (no jump, no white band)
      this._still.addEventListener("load", () => !this._ar()[this._cfg.stream] && this._shape(this._still.naturalWidth, this._still.naturalHeight, false));
      this._fit.onclick = () => {
        this._cover = !this._cover;
        try { localStorage.setItem(FIT_KEY, this._cover ? "cover" : "contain"); } catch { /* this session only */ }
        this._applyFit();
      };
      this._root.querySelector(".media").insertAdjacentHTML("beforeend", CLIP);
      this._root.append(document.createRange().createContextualFragment(SHEET));  // ShadowRoot has no insertAdjacentHTML
      const $ = (s) => this._root.querySelector(s);
      this._clipV = $("#clipv");
      const load = (on) => () => this._card.classList.toggle("cload", on && !this._clipV.hidden);  // spinner while it buffers
      this._clipV.onwaiting = load(true);
      this._clipV.onplaying = this._clipV.oncanplay = this._clipV.onerror = this._clipV.onpause = load(false);
      this._back = $("#back");
      this._back.onclick = () => this._stopClip();
      this._sheet = $("dialog.sheet");
      if (this._camOnly) for (const el of [this._sheet, this._log]) el.dataset.ta = "cam_events_aria";
      this._evl = $(".evl");
      this._sent = $(".sent");
      this._evx = $(".evx");
      this._sheet.onclick = (e) => e.target === this._sheet && this._hideSheet();  // tap on the backdrop
      $(".sheet .x").onclick = () => this._hideSheet();
      this._sheet.onclose = () => {
        this._sheet.style.transform = this._sheet.style.transition = "";
        this._dragStop?.abort();  // a gesture whose pointerup never came does not block the next opening
        if (this._card.dataset.drawer === "true") super._setDrawer(false);
      };
      this._dragSheet();
      // At the bottom of the list: the previous page of events.
      new IntersectionObserver((en) => en[0].isIntersecting && this._sheet.open && this._loadHistory(true),
        { root: this._evl, rootMargin: "300px" }).observe(this._sent);
    }

    // The history button (#log) opens the sheet; the Vimar card closes it by itself on ring.
    _setDrawer(open) {
      super._setDrawer(open);
      const s = this._sheet;
      if (!s || open === s.open) return;
      if (!open) return s.close();
      clearTimeout(this._sheetT);
      s.style.transform = "";
      s.showModal();
      this._histN = (this._histN || 0) + 1;  // answers to earlier openings are dropped
      this._evs = [];
      this._evDone = false;
      this._evx.textContent = "";
      this._evl.replaceChildren(this._sent);
      this._loadHistory();
    }

    // Slides the sheet down, then closes it (the Vimar card's own close, e.g. on ring, is instant).
    _hideSheet() {
      const s = this._sheet;
      s.style.transform = "translateY(100%)";
      clearTimeout(this._sheetT);
      this._sheetT = setTimeout(() => s.close(), SHEET_MS);
    }

    // Drag down to close, like the dashboard's sheets: the handle/title drag at once, the list only from its top
    // and after DRAG_SLOP px down (more down than sideways); released past DRAG_CLOSE px or flicked, it closes,
    // else it goes back. Transform only, at most once per frame (smooth on iPhone).
    _dragSheet() {
      const sheet = this._sheet, list = this._evl;
      let drag = null;
      const move = (m) => {
        if (m.pointerId !== drag.id) return;
        const dy = m.clientY - drag.y0, dx = Math.abs(m.clientX - drag.x0);
        if (!drag.on) {
          if (Math.abs(dy) < DRAG_SLOP && dx < DRAG_SLOP) return;  // still a tap
          if (dy < DRAG_SLOP || dy < dx) return end(m);  // up or sideways: the list scrolls
          drag.on = true;
          sheet.style.transition = "none";
        }
        drag.dy = Math.max(0, dy);
        drag.pts.push([m.timeStamp, m.clientY]);
        if (drag.pts.length > 8) drag.pts.shift();
        drag.raf ||= requestAnimationFrame(() => { drag.raf = 0; sheet.style.transform = `translate3d(0, ${drag.dy}px, 0)`; });
      };
      const end = (m) => {
        if (m.pointerId !== drag.id) return;
        const { on, dy, pts, raf, stop } = drag;
        stop.abort();
        drag = null;
        if (!on) return;
        cancelAnimationFrame(raf);
        this._dragged = true;  // no click on the event under the finger
        setTimeout(() => { this._dragged = false; });
        const p0 = pts.find(([t]) => m.timeStamp - t <= FLICK_MS) || pts.at(-1);  // speed over the last FLICK_MS
        const speed = m.timeStamp > p0[0] ? (m.clientY - p0[1]) / (m.timeStamp - p0[0]) : 0;  // px/ms
        sheet.style.transition = "";
        if (m.type === "pointerup" && (dy > DRAG_CLOSE || (dy > 20 && speed > FLICK_SPEED))) this._hideSheet();
        else sheet.style.transform = "";
      };
      sheet.addEventListener("pointerdown", (e) => {
        if ((drag && !drag.stop.signal.aborted) || e.button > 0 || e.target === sheet) return;  // aborted: card left mid-drag
        const grab = !!e.target.closest(".grab") && !e.target.closest("button");
        if (!grab && (!list.contains(e.target) || list.scrollTop > 0)) return;
        const stop = this._dragStop = new AbortController();  // aborted on release or when the card goes away
        drag = { id: e.pointerId, x0: e.clientX, y0: e.clientY, on: grab, dy: 0, raf: 0, pts: [[e.timeStamp, e.clientY]], stop };
        sheet.getAnimations().forEach((a) => a.finish());  // still sliding up: the finger takes it from where it ends
        if (grab) sheet.style.transition = "none";
        const o = { passive: true, signal: stop.signal };
        window.addEventListener("pointermove", move, o);
        window.addEventListener("pointerup", end, o);
        window.addEventListener("pointercancel", end, o);
      });
      // iOS: the list must not scroll or bounce while the finger pulls the sheet down (non-passive on purpose)
      sheet.addEventListener("touchmove", (t) => {
        const p = t.touches[0];
        if (drag && p && (drag.on || (p.clientY > drag.y0 && p.clientY - drag.y0 >= Math.abs(p.clientX - drag.x0)))) t.preventDefault();
      }, { passive: false });
      sheet.addEventListener("click", (e) => this._dragged && e.stopImmediatePropagation(), true);
    }

    // Pill without the name: "rang 08:28" / "ringing · 0:05" / "on call · 0:24" (the Vimar card counts the ring);
    // a notice (the Vimar card's .err) takes its place: in the live overlay the .err row is hidden.
    _tickSetup() {
      super._tickSetup();
      const s = this._state;
      if (s === "in_call") {
        this._lineAt ||= Date.now();
        this._lineT ||= setInterval(() => this._tickSetup(), 1000);
      } else {
        clearInterval(this._lineT);
        this._lineT = this._lineAt = null;
      }
      const T = this._t;
      this._badge.textContent = this._err.textContent || (s === "ringing" ? `${T.ringing} · ${mmss(this._lastRing)}`
        : s === "in_call" ? `${T.on_call} · ${mmss(this._lineAt)}`
        : s === "calling" ? T.connecting
        : this._lastRing ? `${T.rang} ${hhmm(this._lastRing, T)}` : T.live);
    }

    // Live video: go2rtc's player (it plays, reconnects and picks the mode; the card only reads its <video>'s
    // events); at rest the Vimar card (picture-entity of the camera).
    // Never with the page hidden (_onPageState): iOS keeps WebRTC audio playing in the background.
    _setVideo(live) {
      const always = !!this._cfg.always_live && this._state !== "offline" && (!this._popup || this._pop.open);
      live = !this._hidden && (live || always);
      if (this._live === live) return;
      this._closePeer();
      this._connected = false;
      // Camera's latest picture (Frigate latest.jpg via HA's proxy, ~0.2 s): covers the wait for the first frame
      // and is the background at rest.
      const pic = this._hass?.states[this._cfg.camera]?.attributes?.entity_picture;
      if (pic) this._still.src = pic; else this._still.removeAttribute("src");
      if (!live) return super._setVideo(false);
      this._live = true;
      this._video = null;
      this._card.classList.add("wait");
      const el = (this._rtc = document.createElement(VIDEO));
      el.mode = "webrtc,mse,mjpeg";
      el.media = "video,audio";
      el.sign = () => this._hass.callWS({ type: "auth/sign_path", expires: 30,
        path: `/api/frigate/${this._cfg.frigate_instance}/mse/api/ws?src=${encodeURIComponent(this._cfg.stream)}` })
        .then(({ path }) => location.origin.replace(/^http/, "ws") + path);
      // First frame: "playing", or loadeddata/resize with a size (WebKit may never fire "playing" for a MediaStream).
      // Caught on the player (capture): its <video> exists only once the card is in the page. _v from then on.
      const first = (v) => { if (this._rtc === el && (v.videoWidth || v.poster)) { this._v = v; this._onVideo(); } };
      for (const t of ["playing", "loadeddata", "resize"]) el.addEventListener(t, (e) => first(e.target), true);
      el.onframe = () => first(el.video);
      el.onblocked = () => {  // unmuted without a real tap (iOS): back to muted, the speaker button asks for the tap
        if (this._rtc !== el) return;
        this._audible = false;
        this._audioBlocked = true;
        this._render();
      };
      this._videoBox.replaceChildren(el);
    }

    _ar() {
      try { return JSON.parse(localStorage.getItem(AR_KEY)) || {}; } catch { return {}; }
    }

    // --db1c-ar = w/h, only when it really changes (a resize every frame would make the card jump); save = remember it
    // for the next opening (the still's shape is only a first guess: Frigate's picture may come from the detect stream).
    _shape(w, h, save = true) {
      if (!w || !h || !this._card) return;
      const ar = w / h, cur = parseFloat(this._card.style.getPropertyValue("--db1c-ar"));
      if (Math.abs(ar - cur) < 0.01 * ar) return;
      this._card.style.setProperty("--db1c-ar", ar);
      if (save) try { localStorage.setItem(AR_KEY, JSON.stringify({ ...this._ar(), [this._cfg.stream]: ar })); } catch { /* this session only */ }
    }

    _onVideo() {
      this._shape(this._v?.videoWidth, this._v?.videoHeight);
      this._connected = true;
      this._card.classList.remove("wait");
      if (this._session === "calling") this._session = "in_call";
      this._render();
    }

    _closePeer() {
      this._rtc?.ondisconnect();  // now, not after VideoRTC's 5 s: iOS keeps a detached <video> playing
      this._rtc = this._v = null;
      this._endMic();
    }

    // listen_on_ring: the Vimar card calls it in the live states; here once per ring, so a mute
    // chosen during the ring sticks. The iOS check (real gesture) is the Vimar one, _unlockedContext.
    async _startListen() {
      if (this._hidden || this._audible || this._heardRing === this._lastRing) return;  // a ring while hidden stays silent
      this._heardRing = this._lastRing;
      this._listenStarting = true;
      try {
        const ctx = await this._unlockedContext();
        if (!ctx) return;
        ctx.close();
        if (this._hidden) return;
        this._audible ||= "auto";
        this._render();
      } finally {
        this._listenStarting = false;
      }
    }

    // The Vimar card calls it on every pass outside the live states: only automatic listening ends.
    _stopListen() {
      if (this._audible === "auto" && !LIVE.includes(this._state)) this._audible = false;
    }

    // The only place that writes _v.muted (play, src and srcObject are the player's). While talking the voice
    // comes from the call WebSocket: the <video> stays quiet. iOS pauses media elements when mic capture
    // starts/stops: the player's play() restarts it; an unmuted play it refuses comes back as onblocked.
    _applyAudio() {
      const v = this._v, ear = this._ws?.ear;
      if (ear) ear.gain.value = this._audible ? 1 : 0;
      const muted = !this._audible || !!ear;
      if (!v || (v.muted === muted && !v.paused)) return;
      v.muted = muted;
      this._rtc.play();
    }

    // Talk: the flow (HTTPS, permission, answer/call, errors in words) is the Vimar card's (_startTalk); here the
    // call WebSocket with the microphone and the AudioContext created in the gesture. Nothing is sent before the
    // server's "ready" (10 s at most); no "ready" (no endpoint, busy = close 4409) = no call, said in words, video
    // untouched. Hung up or gone to the background meanwhile (_endMic closed it): just the cleanup.
    async _openAudio(mic, ctx) {
      if (!this.isConnected) throw new Error("card closed");
      ctx.resume().catch(() => {});  // iOS: with the microphone the audio session changes and the context may stay suspended
      const T = this._t;
      const dial = (this._dialing = { close() { this.gone = true; } });  // hung up while signing: _endMic marks it
      const { path } = await this._hass.callWS({ type: "auth/sign_path", expires: 30,
        path: `/api/dahua_talk/call_ws/${encodeURIComponent(this._cfg.speaker)}` });
      if (dial.gone) {
        mic.getTracks().forEach((t) => t.stop());
        return ctx.close();
      }
      const ws = (this._dialing = new WebSocket(location.origin.replace(/^http/, "ws") + path));
      ws.binaryType = "arraybuffer";
      const late = setTimeout(() => ws.close(), 10000);
      const code = await new Promise((ok) => {
        ws.onmessage = (e) => typeof e.data === "string" && JSON.parse(e.data).type === "ready" && ok(0);
        ws.onclose = (e) => ok(e.code || 1006);
      });
      clearTimeout(late);
      const mine = this._dialing === ws;
      this._dialing = null;
      if (code || !mine) {
        ws.close();
        mic.getTracks().forEach((t) => t.stop());
        ctx.close();
        if (!mine) return;
        this._session = null;
        this._err.textContent = code === 4409 ? T.busy : T.talk_na;
        return this._render();
      }
      const ear = ctx.createGain();  // volume set by _applyAudio
      ear.connect(ctx.destination);
      const sink = this._earSink(ctx, ear, this._cfg.ear_buffer ?? (LAN ? 0.12 : 0.5));
      ws.onmessage = (e) => typeof e.data !== "string" && sink(e.data);
      ws.onclose = (e) => {
        if (this._ws?.sock !== ws) return;  // closed by us
        this._call("hangup");
        this._err.textContent = e.code === 4408 ? T.call_cap : T.call_end;
        this._tickSetup();
      };
      // Microphone -> 0x02 + PCM16 8 kHz (the Vimar card's capture: ScriptProcessor, plain average to resample).
      const source = ctx.createMediaStreamSource(mic);
      const proc = ctx.createScriptProcessor(2048, 1, 1);
      const step = ctx.sampleRate / RATE;
      proc.onaudioprocess = (ev) => {
        if (ws.readyState !== WebSocket.OPEN) return;
        const inp = ev.inputBuffer.getChannelData(0);
        const n = Math.floor(inp.length / step);
        const out = new Uint8Array(1 + n * 2);
        const view = new DataView(out.buffer);
        out[0] = 0x02;
        for (let i = 0; i < n; i++) {
          let sum = 0;
          const a = Math.floor(i * step), b = Math.floor((i + 1) * step);
          for (let j = a; j < b; j++) sum += inp[j];
          const s = Math.max(-1, Math.min(1, sum / (b - a)));
          view.setInt16(1 + i * 2, s * 32767, true);
        }
        ws.send(out);
      };
      source.connect(proc);
      proc.connect(ctx.destination);  // needed for onaudioprocess to run (outputs silence)
      // _ws = the call; the base card only checks it for truthiness. `audible` = the listen state before, restored by _endMic.
      this._ws = { sock: ws, mic, ctx, ear, audible: this._audible };
      this._audible = true;  // talking = hearing
      this._audioBlocked = false;
      this._render();
    }

    // Visitor voice: 0x01 + PCM16 8 kHz, resampled continuously to the context's rate and played `lead` s ahead
    // of the clock (the Vimar card's _pcmSink, copied, with the lead as a knob: ear_buffer). Each underrun adds
    // 40 ms, up to lead + 0.2 s. A 3.6 kHz low-pass removes the linear interpolation's images.
    _earSink(ctx, out, lead) {
      const step = RATE / ctx.sampleRate;
      const lp = ctx.createBiquadFilter();
      lp.type = "lowpass";
      lp.frequency.value = 3600;
      lp.connect(out);
      let playAt = 0, last = 0, pos = 0, ahead = lead, started = false;
      return (data) => {
        if (new Uint8Array(data, 0, 1)[0] !== 0x01) return;
        const pcm = new Int16Array(data.slice(1, 1 + ((data.byteLength - 1) & ~1))), n = pcm.length;
        if (!n) return;
        const at = (i) => (i < 0 ? last : pcm[i] / 32768);  // index -1 is the previous packet's last sample
        const count = Math.ceil((n - pos) / step);
        const buf = ctx.createBuffer(1, count, ctx.sampleRate);
        const ch = buf.getChannelData(0);
        let p = pos;
        for (let k = 0; k < count; k++, p += step) {
          const i = Math.floor(p), f = p - i;
          ch[k] = at(i - 1) + (at(i) - at(i - 1)) * f;
        }
        pos = p - n;
        last = pcm[n - 1] / 32768;
        const src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(lp);
        if (playAt < ctx.currentTime + 0.01) {
          if (started) ahead = Math.min(ahead + 0.04, lead + 0.2);  // ran dry: keep more in hand
          started = true;
          playAt = ctx.currentTime + ahead;
        }
        src.start(playAt);
        playAt += buf.duration;
      };
    }

    _endMic() {  // no redraw: _closePeer calls it from inside _render too (_stopAudio's redraw runs _applyAudio)
      this._dialing?.close();  // a call still waiting for "ready": _openAudio sees it and cleans up
      this._dialing = null;
      const a = this._ws;
      this._ws = null;
      if (!a) return false;
      if (this._audible === true) this._audible = a.audible;  // call over: listening back as before (unless muted meanwhile)
      a.mic.getTracks().forEach((t) => t.stop());
      a.sock.close();
      a.ctx.close();
      return true;
    }

    _stopAudio() {
      if (this._endMic() && this._root) this._render();
    }

    // History = Frigate events of the camera (all labels, or history_labels), newest first, paged.
    // Only with the sheet open (reset by _setDrawer): more = the page before the last listed (from the bottom);
    // without more, with the list already filled (the Vimar _render reloads on a live change), only new events
    // go on top and the scroll stays where it is.
    async _loadHistory(more = false) {
      if (!this._sheet?.open || (more && (this._evDone || this._evBusy || !this._evs.length))) return;
      const c = this._cfg, n = this._histN, last = more ? this._evs.at(-1) : null;
      if (more) this._evBusy = true;
      try {
        let ev = await this._hass.callWS({ type: "frigate/events/get", instance_id: c.frigate_instance, cameras: [c.frigate_camera],
          limit: c.history, ...(c.history_labels ? { labels: c.history_labels } : {}),
          ...(last ? { before: Math.ceil(last.start_time) } : {}) });  // integer: the last one comes back, filtered below
        if (typeof ev === "string") ev = JSON.parse(ev);  // the integration passes Frigate's JSON as is
        if (n !== this._histN) return;  // sheet closed and reopened meanwhile
        const seen = new Set(this._evs.map((e) => e.id)), fresh = ev.filter((e) => !seen.has(e.id));
        const items = fresh.map((e) => this._evItem(e));
        if (more || !this._evs.length) {
          this._evDone = ev.length < c.history || !fresh.length;
          this._evs.push(...fresh);
          this._sent.before(...items);
        } else {
          this._evs.unshift(...fresh);
          this._evl.prepend(...items);
        }
        this._evx.textContent = this._evs.length ? "" : this._t.no_events;
      } catch (e) {
        if (n === this._histN && !this._evs.length) this._evx.textContent = `${this._t.history_err}: ${e.message || e}`;
      } finally {
        if (more) this._evBusy = false;
      }
    }

    _evBase(e) {  // Frigate integration notifications proxy: no auth, by event id
      return `/api/frigate/${this._cfg.frigate_instance}/notifications/${encodeURIComponent(e.id)}/`;
    }

    _evItem(e) {
      const b = document.createElement("button"), t = e.start_time * 1000, T = this._t;
      const raw = String(e.label ?? ""), lbl = T[`l_${raw}`] || raw.charAt(0).toUpperCase() + raw.slice(1);
      b.className = "ev";
      b.innerHTML = `<span class="th"><img alt="" loading="lazy"></span><span><b></b>` +
        `<small><ha-icon icon="mdi:calendar-clock" aria-hidden="true"></ha-icon><span></span></small>` +
        `<small><ha-icon icon="mdi:timer-outline" aria-hidden="true"></ha-icon><span></span></small></span>`;
      const img = b.querySelector("img");
      img.draggable = false;  // desktop: a drag from the thumbnail moves the sheet, not the picture
      img.src = `${this._evBase(e)}thumbnail.jpg`;
      if (e.has_clip) img.insertAdjacentHTML("afterend", `<ha-icon icon="mdi:play-circle" aria-hidden="true"></ha-icon>`);
      const [when, len] = b.querySelectorAll("small span");
      b.querySelector("b").textContent = lbl;
      when.textContent = `${day(t, T)} ${hm(t, T, true)}`;
      len.textContent = dur(e, T);
      b.setAttribute("aria-label", `${lbl}, ${when.textContent}, ${len.textContent}${e.has_clip ? `, ${T.play}` : ""}`);
      b.onclick = () => this._playClip(e);
      return b;
    }

    // The clip in the video box (live stays connected underneath, to go back at once); no clip, the snapshot.
    // iOS can't play clip.mp4 (the proxy ignores Range requests): browsers with native HLS (iOS, Safari, recent
    // Chrome) get Frigate's HLS of the event, signed like the Frigate card does (a <video> can't send the token;
    // the segments inherit the playlist's signature). Signing failed: only the poster.
    async _playClip(e) {
      this._setDrawer(false);
      const v = this._clipV, base = this._evBase(e), n = ++this._clipN;
      v.poster = e.has_snapshot ? `${base}snapshot.jpg` : "";
      v.pause();
      v.removeAttribute("src");
      v.load();  // the previous clip stops now, not when the new one arrives
      v.muted = false;
      v.hidden = this._back.hidden = false;
      this._card.classList.add("clip");
      this._card.classList.toggle("cload", !!e.has_clip);
      this.scrollIntoView({ block: "nearest", behavior: "smooth" });
      if (!e.has_clip) return;
      let src = `${base}clip.mp4`;
      if (v.canPlayType("application/vnd.apple.mpegurl")) {
        try {
          src = (await this._hass.callWS({ type: "auth/sign_path", expires: CLIP_SIGN_S,
            path: `/api/frigate/${this._cfg.frigate_instance}/vod/event/${encodeURIComponent(e.id)}/index.m3u8` })).path;
        } catch (err) {
          console.warn("db1c-doorbell-card: clip signing failed:", err.message || err);
          if (n === this._clipN) this._card.classList.remove("cload");
          return;
        }
        if (n !== this._clipN) return;  // back to live, or another clip, meanwhile
      }
      v.src = src;
      // Unmuted play() after an await is no longer the user's tap: iOS refuses it, so start muted (controls unmute).
      const stuck = () => n === this._clipN && this._card.classList.remove("cload");  // refused: no canplay will come
      v.play().catch((err) => {
        if (n !== this._clipN) return;
        if (err.name !== "NotAllowedError") return stuck();
        v.muted = true;
        v.play().catch(stuck);
      });
    }

    _clipN = 0;  // the clip being opened: a later tap or Back drops an earlier one still signing

    _stopClip() {
      const v = this._clipV;
      if (!v) return;
      this._clipN++;
      this._card.classList.remove("cload");
      v.pause();
      v.removeAttribute("src");
      v.removeAttribute("poster");
      v.load();
      v.hidden = this._back.hidden = true;
      this._card.classList.remove("clip");
    }

    _hidden = pageGone();  // no live, no audio (see pageGone)

    // On window: pagehide/pageshow fire there, visibilitychange bubbles up from document. pageshow = back from
    // the iOS back-forward cache, where no visibilitychange may follow.
    // App closed or in the background (iOS keeps WebRTC and AudioContext audio playing there): talk over, live
    // closed, the clip stopped; nothing restarts until the page is back, then live comes back muted.
    _onPageState = (e) => {
      const hidden = pageGone(e);
      if (hidden === this._hidden) return;
      this._hidden = hidden;
      if (hidden) this._goQuiet();
      if (this._root && this._hass) this._render();  // hidden: _setVideo closes the live; visible: reconnects it
    };

    _goQuiet() {
      this._stopClip();
      this._closePeer();  // the player stops at once, + the call (_endMic): mic, WebSocket, AudioContext
      this._session = null;
      this._audible = this._audioBlocked = false;
    }

    connectedCallback() {
      for (const e of PAGE_EVENTS) window.addEventListener(e, this._onPageState);
      this._hidden = pageGone();
      super.connectedCallback();
    }

    disconnectedCallback() {
      for (const e of PAGE_EVENTS) window.removeEventListener(e, this._onPageState);
      super.disconnectedCallback();
      if (this._sheet?.open) this._sheet.close();
      this._dragStop?.abort();  // a drag in progress leaves no window listeners behind
      clearTimeout(this._sheetT);
      clearTimeout(this._ringT);
      clearInterval(this._lineT);
      this._lineT = this._lineAt = null;
      this._goQuiet();
      this._live = undefined;  // recreated on return
    }
  }

  customElements.get(TAG) || customElements.define(TAG, Db1cDoorbellCard);
});

window.customCards = window.customCards || [];
window.customCards.push({ type: TAG, name: "DB1C Doorbell",
  description: "The Vimar intercom card for the EZVIZ DB1C: video and voice from go2rtc/Frigate, talk, open, Frigate history." });
