// DB1C doorbell: the Vimar intercom card (same DOM, same CSS, same states and buttons) with a different
// "backend": no SIP and no vimar_intercom WebSocket, but
//   visitor video + voice: a receive-only RTCPeerConnection to Frigate's go2rtc, through the Frigate
//     integration proxy (/api/frigate/<instance>/webrtc/api/ws?src=<stream>, signed);
//   microphone: ONLY while talking, a second send-only PeerConnection (like the Frigate UI) -> go2rtc ->
//     dahua_talk's #backchannel=1 source (rtsp://...:8557/<entry>/<key>, added to the stream in Frigate)
//     -> the DB1C speaker. At rest nobody holds the talk channel;
//   visitor voice WHILE talking (both directions at once): with the talk channel open the DB1C sends its
//     microphone on the same channel (HCNetSDK), dahua_talk decodes it and serves it on
//     /api/dahua_talk/listen/<speaker> (PCM 8 kHz); the card plays it on an AudioContext (the Vimar card's
//     player) and, as soon as it flows, mutes the <video>'s RTSP audio so it is not heard twice;
//   ring: an "on" entity (input_boolean/binary_sensor, or event.*) + an optional input_datetime with the
//     time of the last ring (ring_time: survives a page reload and gives "rang HH:MM");
//   video: ALWAYS live, also at rest (always_live, default true; false = like the Vimar card);
//   history: ALL Frigate events of the camera in a full-screen sheet (thumbnail, label, date, duration,
//     paged by `history`), a tap plays the clip in the video box; Frigate's notifications proxy needs no
//     auth (event id), so no expiring signatures;
//   startup: the camera's latest picture (entity_picture, ~0.2 s) until the first live frame arrives;
//   Open: a configured lock/button (none = no Open button).
// The states (idle/ringing/calling/in_call/offline) do not exist in HA: the card computes them and hands
// them to the Vimar card as if they were a sensor (wrapped hass, see _wrap).
//
// Needs the Vimar intercom card loaded (the vimar_intercom integration does it): this card extends it.
//
//   type: custom:db1c-doorbell-card
//   camera: camera.front_door                  (required: offline state, still picture, video fallback)
//   stream: front_door                         (required: go2rtc stream inside Frigate)
//   frigate_camera: front_door                 (required: Frigate camera name for the event history)
//   ring: input_boolean.doorbell_ring          (required)
//   speaker: media_player.front_door_speaker   (required: dahua_talk entity, visitor voice while talking)
//   ring_time: input_datetime.doorbell_last_ring  (optional)
//   lock: lock.front_door                      (optional: Open = lock.open, with the latch; confirm_open: double tap)
//   layout: overlay                            (+ every Vimar card key: listen_on_ring, confirm_open...)
//   colors: { accent: ..., button: ... }       (optional: --db1c-* colours of the pill and the buttons, see README)
//   language: it                               (optional: default HA's language, then English)
//   frigate_instance / history_labels / history / ring_timeout / always_live: see DEFAULTS

const VIMAR = "vimar-intercom-card";
const TAG = "db1c-doorbell-card";
const STATUS = "sensor.__db1c_status";       // fake: they only exist in the hass the Vimar card sees
const LAST = "sensor.__db1c_last_ring";
const FIT_KEY = "db1c_doorbell_card_fit2";  // new key: the default moved from Fill to Fit
const CONNECT_MS = 8000;  // WebRTC not connected within this (away from home: 8555 is LAN only) -> HA stream, video only
const PROBE_MS = 60000;   // while on the HA stream, try WebRTC again this often (tab visible only)
// Ear buffer: 0.4 s of 8 kHz PCM16 silence in front of _pcmSink, on every underrun. On the bench chunks
// arrive in 64 ms pairs with gaps up to 200 ms even on LAN; the player's 120 ms lead is not enough.
const EAR_RATE = 8000;
const EAR_PRE_BYTES = 0.4 * EAR_RATE * 2;
const REQUIRED = ["camera", "stream", "frigate_camera", "ring", "speaker"];
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
};
const COLORS = ["accent", "warning", "on-warning", "glass", "ink", "button", "button-ink"];  // colors: keys -> --db1c-<key>
// User-visible strings, picked by `language:` or HA's language; missing language or key = English.
// Frigate labels are l_<label>: unknown labels show capitalised.
const I18N = {
  en: {
    live: "live", rang: "rang", ringing: "ringing", on_call: "on call", connecting: "connecting…",
    locked: "Locked", unlocked: "Unlocked", open: "Open", locking: "Locking…", unlocking: "Unlocking…", opening: "Opening…",
    jammed: "Jammed", unavailable: "Unavailable", open_door: "Open door", confirm: "Confirm",
    open_aria: "Open the door", open_aria_twice: "Open the door, tap twice", mute: "Mute audio", listen: "Listen",
    talk_ring: "Talk to the visitor", ignore: "Ignore the ring", end: "End conversation",
    back: "Live", back_aria: "Back to live video", events: "Events", events_aria: "Doorbell events", close: "Close",
    today: "today", yesterday: "yesterday", ongoing: "ongoing", play: "play",
    no_events: "No events recorded", history_err: "History unavailable", mic_err: "Microphone not connected",
    video_only: "Video only: voice and microphone work on the home network.",
    away: "video only away from home", not_live: "live stream not connected",
    l_person: "Person", l_car: "Car", l_dog: "Dog", l_cat: "Cat", l_doorbell: "Doorbell",  // Frigate labels
  },
  it: {
    live: "dal vivo", rang: "squillo", ringing: "suonano", on_call: "in linea", connecting: "collegamento…",
    locked: "Chiusa", unlocked: "Aperta", open: "Aperta", locking: "Chiude…", unlocking: "Apre…", opening: "Apre…",
    jammed: "Bloccata", unavailable: "Non disponibile", open_door: "Apri porta", confirm: "Conferma",
    open_aria: "Apri la porta", open_aria_twice: "Apri la porta, tocca due volte", mute: "Silenzia l'audio", listen: "Ascolta l'audio",
    talk_ring: "Parla con il visitatore", ignore: "Ignora lo squillo", end: "Chiudi conversazione",
    back: "Dal vivo", back_aria: "Torna al video dal vivo", events: "Eventi", events_aria: "Eventi della porta", close: "Chiudi",
    today: "oggi", yesterday: "ieri", ongoing: "in corso", play: "riproduci",
    no_events: "Nessun evento registrato", history_err: "Storico non disponibile", mic_err: "Microfono non collegato",
    video_only: "Solo video: voce e microfono funzionano in casa.",
    away: "solo video fuori casa", not_live: "diretta non collegata",
    l_person: "Persona", l_car: "Auto", l_dog: "Cane", l_cat: "Gatto", l_doorbell: "Campanello",  // Frigate labels
  },
};
// Static texts: data-t = textContent, data-ta = aria-label; filled by _applyLang (hass, hence the language, comes later).
const CLIP = `<video id="clipv" playsinline controls preload="none" hidden></video>
  <button id="back" data-ta="back_aria" hidden><ha-icon icon="mdi:arrow-left" aria-hidden="true"></ha-icon><span data-t="back"></span></button>`;
const SHEET = `<dialog class="sheet" data-ta="events_aria"><header><span data-t="events"></span>
  <button class="x" data-ta="close"><ha-icon icon="mdi:close" aria-hidden="true"></ha-icon></button></header>
  <div class="evl"><div class="sent"></div></div><p class="evx"></p></dialog>`;
const EXTRA_CSS = `
  #video > video { width: 100%; height: 100%; object-fit: cover; background: #000; }
  ha-card[data-fit="contain"] #video > video { object-fit: contain; }
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
  #back { position: absolute; top: 12px; left: 12px; z-index: 4; height: 44px; padding: 0 14px 0 10px; gap: 6px;
    display: inline-flex; align-items: center; border-radius: 22px; font-size: 13px; color: #fff; background: rgba(0,0,0,.45);
    -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  #back[hidden] { display: none; }
  #back ha-icon { --mdc-icon-size: 20px; }
  /* History like Frigate: full-screen sheet on phones, wide window on desktop. */
  dialog.sheet { --ink: var(--primary-text-color, #1b1b1f); --dim: var(--secondary-text-color, #6f6a60);
    box-sizing: border-box; padding: 0; border: 0; width: min(720px, 100vw); height: min(900px, 92dvh); max-width: 100vw;
    max-height: 100dvh; border-radius: 20px; color: var(--ink); background: var(--card-background-color, #fff); }
  dialog.sheet[open] { display: flex; flex-direction: column; }
  dialog.sheet::backdrop { background: rgba(0,0,0,.6); }
  @media (max-width: 600px) { dialog.sheet { width: 100vw; height: 100dvh; border-radius: 0; } }
  .sheet header { display: flex; align-items: center; justify-content: space-between; padding: 8px 8px 8px 16px;
    padding-top: max(8px, env(safe-area-inset-top)); font-size: 18px; font-weight: 600; }
  .sheet .x { width: 44px; height: 44px; border-radius: 22px; display: grid; place-items: center; color: inherit; background: var(--secondary-background-color, rgba(127,127,127,.15)); }
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
const hangUp = (pc) => { pc?.close(); pc?.sock?.close(); };  // PeerConnection + its go2rtc WebSocket
const dur = (e, T) => {
  if (!e.end_time) return T.ongoing;
  const s = Math.max(1, Math.round(e.end_time - e.start_time));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
};

customElements.whenDefined(VIMAR).then(() => {
  const Vimar = customElements.get(VIMAR);
  // This card leans on the Vimar card's internals: a missing hook means an incompatible version.
  const INCOMPATIBLE = ["_openAudio", "_pcmSink"].some((m) => typeof Vimar.prototype[m] !== "function")
    && "db1c-doorbell-card: the loaded Vimar intercom card is too old/new: missing _openAudio/_pcmSink";
  if (INCOMPATIBLE) console.error(INCOMPATIBLE);

  class Db1cDoorbellCard extends Vimar {
    static getConfigElement() { return undefined; }  // the editor is the Vimar one: YAML only here
    static getStubConfig() { return {}; }

    setConfig(config) {
      if (INCOMPATIBLE) throw new Error(INCOMPATIBLE);  // red error card in Lovelace
      const missing = REQUIRED.filter((k) => !config?.[k]);
      if (missing.length) throw new Error(`db1c-doorbell-card: missing required option(s): ${missing.join(", ")}`);
      super.setConfig({ ...DEFAULTS, ...config, status: STATUS, last_ring: LAST });
      // colors: { accent: "var(--my-accent)" } -> --db1c-accent on the host (inherited by the shadow DOM)
      for (const k of COLORS) {
        const v = config.colors?.[k];
        if (v == null) this.style.removeProperty(`--db1c-${k}`);
        else this.style.setProperty(`--db1c-${k}`, String(v));
      }
    }

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

    _ent(key) {
      return key === "camera" ? this._cfg.camera : super._ent(key);  // never the Vimar camera found in the registry
    }

    _trackRing(h) {
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

    // The vimar_intercom services become local states: the DB1C has no "call", WebRTC is either there or not.
    async _call(service) {
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
      this._icon(this._talk, this._ws ? "mdi:microphone" : "mdi:microphone-off");
      this._lockState();
      // Listen button (next to the microphone): crossed = muted, plain = audible. One state, _audible
      // (true = chosen by the user or talking, "auto" = listen_on_ring), also valid with the microphone open.
      const hearing = !!this._audible;
      this._mute.hidden = !this._v;
      this._icon(this._mute, hearing ? "mdi:volume-high" : "mdi:volume-off");
      this._mute.setAttribute("aria-pressed", hearing);
      const T = this._t;
      this._mute.setAttribute("aria-label", hearing ? T.mute : T.listen);
      this._applyAudio();
      this._talk.setAttribute("aria-label", ring ? T.talk_ring : this._talk.querySelector(".lbl").textContent);
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
      this._fit.onclick = () => {
        this._cover = !this._cover;
        try { localStorage.setItem(FIT_KEY, this._cover ? "cover" : "contain"); } catch { /* this session only */ }
        this._applyFit();
        if (this._fellBack) this._setPicture(true);  // HA's card reads fit_mode only when created
      };
      this._root.querySelector(".media").insertAdjacentHTML("beforeend", CLIP);
      this._root.append(document.createRange().createContextualFragment(SHEET));  // ShadowRoot has no insertAdjacentHTML
      const $ = (s) => this._root.querySelector(s);
      this._clipV = $("#clipv");
      this._back = $("#back");
      this._back.onclick = () => this._stopClip();
      this._sheet = $("dialog.sheet");
      this._evl = $(".evl");
      this._sent = $(".sent");
      this._evx = $(".evx");
      this._sheet.onclick = (e) => e.target === this._sheet && this._sheet.close();  // tap on the backdrop
      $(".sheet .x").onclick = () => this._sheet.close();
      this._sheet.onclose = () => this._card.dataset.drawer === "true" && super._setDrawer(false);
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
      s.showModal();
      this._histN = (this._histN || 0) + 1;  // answers to earlier openings are dropped
      this._evs = [];
      this._evDone = false;
      this._evx.textContent = "";
      this._evl.replaceChildren(this._sent);
      this._loadHistory();
    }

    // Pill without the name: "rang 08:28" / "ringing · 0:05" / "on call · 0:24" (the Vimar card counts the ring).
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
      this._badge.textContent = s === "ringing" ? `${T.ringing} · ${mmss(this._lastRing)}`
        : s === "in_call" ? `${T.on_call} · ${mmss(this._lineAt)}`
        : s === "calling" ? T.connecting
        : this._lastRing ? `${T.rang} ${hhmm(this._lastRing, T)}` : T.live;
    }

    // Live video: a <video> on the PeerConnection; at rest the Vimar card (picture-entity of the camera).
    // ponytail: at rest the PeerConnection stays open with the tab hidden too; close it on
    // visibilitychange if it weighs on battery/go2rtc.
    _setVideo(live) {
      live ||= !!this._cfg.always_live && this._state !== "offline" && (!this._popup || this._pop.open);
      if (this._live === live) return;
      this._closePeer();
      this._connected = this._fellBack = false;
      // Camera's latest picture (Frigate latest.jpg via HA's proxy, ~0.2 s): covers the WebRTC wait (~2 s, waits
      // for the key frame) and is the background at rest.
      const pic = this._hass?.states[this._cfg.camera]?.attributes?.entity_picture;
      if (pic) this._still.src = pic; else this._still.removeAttribute("src");
      if (!live) return super._setVideo(false);
      this._live = true;
      this._video = null;
      this._card.classList.add("wait");
      const v = (this._v = this._newVideo());
      this._videoBox.replaceChildren(v);
      this._connect(v);
    }

    _newVideo() {
      const v = document.createElement("video");
      v.autoplay = v.playsInline = v.muted = true;
      v.onplaying = () => this._v === v && this._onVideo();  // not a background retry still detached
      v.onresize = () => v.videoWidth && this._card.style.setProperty("--db1c-ar", v.videoWidth / v.videoHeight);  // Fit: the stream's shape
      return v;
    }

    _onVideo() {
      this._connected = true;
      this._retry = 0;
      this._card.classList.remove("wait");
      if (this._session === "calling") this._session = "in_call";
      this._render();
    }

    // Live: video + visitor voice, receive only (no talk channel at rest, see _micLeg).
    _connect(v) {
      const pc = (this._pc = new RTCPeerConnection());
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.addTransceiver("audio", { direction: "recvonly" });
      const ms = new MediaStream();
      pc.ontrack = (e) => { ms.addTrack(e.track); v.srcObject = ms; };
      const fail = (why) => this._pc === pc && (this._connected ? this._drop(why) : this._fallback(why));
      pc.onconnectionstatechange = () => {
        if (pc.connectionState === "failed") fail("ICE failed");
        else if (pc.connectionState === "connected" && this._fellBack && this._pc === pc) this._leaveFallback(v);
      };
      this._connT = setTimeout(() => !this._connected && fail("timeout"), CONNECT_MS);
      this._signal(pc, fail);
    }

    // go2rtc WebSocket protocol (same as Frigate and video-rtc.js): offer -> answer, candidates.
    // Messages for an already closed PeerConnection (reconnect, fallback) are ignored. A WebSocket closing
    // after negotiation does not count: media flows on its own, a real drop is reported by "failed".
    async _signal(pc, fail) {
      const stale = () => pc.signalingState === "closed";
      try {
        const { path } = await this._hass.callWS({ type: "auth/sign_path",
          path: `/api/frigate/${this._cfg.frigate_instance}/webrtc/api/ws?src=${encodeURIComponent(this._cfg.stream)}` });
        if (stale()) return;
        const ws = (pc.sock = new WebSocket(location.origin.replace(/^http/, "ws") + path));  // closed by hangUp
        const send = (type, value) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type, value }));
        ws.onopen = async () => {
          pc.onicecandidate = (e) => send("webrtc/candidate", e.candidate ? e.candidate.candidate : "");
          await pc.setLocalDescription(await pc.createOffer());
          send("webrtc/offer", pc.localDescription.sdp);
        };
        ws.onmessage = (ev) => {
          if (stale()) return ws.close();
          const m = JSON.parse(ev.data);
          if (m.type === "webrtc/answer") pc.setRemoteDescription({ type: "answer", sdp: m.value }).catch((e) => !stale() && fail(e.message));
          else if (m.type === "webrtc/candidate") pc.addIceCandidate({ candidate: m.value, sdpMid: "0" }).catch(() => {});
          else if (m.type === "error") fail(m.value);
        };
        ws.onclose = () => !stale() && pc.connectionState !== "connected" && fail("WebSocket closed");
      } catch (e) {
        if (!stale()) fail(e.message || e);
      }
    }

    // WebRTC unreachable (away from home: 8555 is LAN only): HA's stream, like the Vimar card without
    // WebCodecs. Video only, no voice or microphone. If it was already connected (at home) retry a few times first.
    // While on HA's stream WebRTC is retried in the background (_probe); a failed retry just closes quietly.
    _fallback(why) {
      if (!this._live || this._connected) return;
      if (this._fellBack) {  // background retry failed: stay on HA's stream
        clearTimeout(this._connT);
        hangUp(this._pc);
        this._pc = null;
        return;
      }
      if (this._retry && this._retry < 4) return this._drop(why);
      console.warn("db1c-doorbell-card: WebRTC unavailable:", why);
      this._closePeer();
      this._retry = 0;
      this._fellBack = true;
      this._card.classList.remove("wait");
      this._err.textContent = this._t.video_only;
      this._setPicture(true);
      if (this._session === "calling") this._session = "in_call";
      this._probeT = setInterval(this._probe, PROBE_MS);
      document.addEventListener("visibilitychange", this._probe);
      this._render();
    }

    // On HA's stream: try WebRTC again in the background; HA's stream stays until it connects.
    _probe = () => {
      if (this._fellBack && !this._pc && !document.hidden) this._connect(this._newVideo());
    };

    // A background retry got through: from HA's stream back to WebRTC (onplaying -> _onVideo does the rest).
    _leaveFallback(v) {
      this._fellBack = false;
      clearInterval(this._probeT);
      document.removeEventListener("visibilitychange", this._probe);
      this._v = v;
      this._video = null;
      this._err.textContent = this._hint;
      this._card.classList.add("wait");
      this._videoBox.replaceChildren(v);
      v.play().catch(() => {});
    }

    // Live dropped after connecting (HA/Frigate restart, network change): retried after 2, 4, 8 s;
    // the fourth failed attempt switches to HA's stream (_fallback).
    _drop(why) {
      console.warn("db1c-doorbell-card: WebRTC dropped, retrying:", why);
      this._closePeer();
      this._connected = false;
      this._retry = (this._retry || 0) + 1;
      this._card.classList.add("wait");
      this._retryT = setTimeout(() => { this._live = undefined; if (this._hass) this._render(); }, 1000 * 2 ** this._retry);
      this._render();  // microphone button off at once
    }

    _closePeer() {
      clearTimeout(this._connT);
      clearTimeout(this._retryT);
      clearInterval(this._probeT);
      document.removeEventListener("visibilitychange", this._probe);
      const pc = this._pc;
      this._pc = this._v = null;
      hangUp(pc);
      this._endMic();
    }

    // listen_on_ring: the Vimar card calls it in the live states; here once per ring, so a mute
    // chosen during the ring sticks. The iOS check (real gesture) is the Vimar one, _unlockedContext.
    async _startListen() {
      if (this._audible || this._heardRing === this._lastRing) return;
      this._heardRing = this._lastRing;
      this._listenStarting = true;
      try {
        const ctx = await this._unlockedContext();
        if (!ctx) return;
        ctx.close();
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

    // The only place that writes _v.muted. Unmuted without a real gesture (iOS) play() fails: back to muted.
    // A paused <video> is restarted too: iOS pauses media elements when mic capture starts/stops.
    // While talking the voice comes from the talk channel (_earLeg): the <video> goes quiet as soon as it flows.
    _applyAudio() {
      const v = this._v, ear = this._ws?.ear;
      if (ear) ear.gain.gain.value = this._audible ? 1 : 0;
      const muted = !this._audible || !!ear?.live;
      if (!v || (v.muted === muted && !v.paused)) return;
      v.muted = muted;
      v.play().catch((e) => {
        if (this._v !== v || e.name !== "NotAllowedError") return;  // AbortError: srcObject arriving, not a block
        this._audible = false;
        this._audioBlocked = true;
        v.muted = true;
        v.play().catch(() => {});
        this._render();
      });
    }

    // Talk: the flow (HTTPS, permission, answer/call, errors in words) is the Vimar card's (_startTalk);
    // here only what to do with the microphone and the AudioContext created in the gesture: talk channel and ear.
    // If it throws, the Vimar card stops the microphone, closes the context and writes why.
    async _openAudio(mic, ctx) {
      if (this._fellBack || !this._pc) throw new Error(this._fellBack ? this._t.away : this._t.not_live);
      ctx.resume().catch(() => {});  // iOS: with the microphone the audio session changes and the context may stay suspended
      // _ws = the Talk session object; the base card only checks it for truthiness. Synchronous from the check above: no races.
      // `audible` = the listen state before Talk, restored by _endMic.
      this._ws = { mic, pc: this._micLeg(mic.getAudioTracks()[0]), ctx, ear: this._earLeg(ctx), audible: this._audible };
      this._audible = true;  // talking = hearing
      this._audioBlocked = false;
      this._render();
    }

    // Visitor voice while talking: 8 kHz PCM from dahua_talk (the doorbell mic on the talk channel), played
    // by the Vimar card's player (_pcmSink: 8 kHz, 0x01 prefix, anti-jitter buffer). On the first chunk
    // `live` = true and _applyAudio mutes the <video>'s RTSP audio. No data (a doorbell that does not send
    // its mic back) changes nothing: the previous audio stays.
    _earLeg(ctx) {
      const ac = new AbortController(), gain = ctx.createGain(), ear = { ac, gain, live: false };
      gain.connect(ctx.destination);  // volume set by _applyAudio
      // The sink's play position, spied from its src.start(): when it is behind the clock the sink realigns,
      // and the voice gets the EAR_PRE_BYTES silence in front again.
      let end = 0;
      const spy = new Proxy(ctx, { get: (t, k) => k === "createBufferSource" ? () => {
        const s = t.createBufferSource(), start = s.start.bind(s);
        s.start = (at) => { end = at + s.buffer.duration; start(at); };
        return s;
      } : typeof t[k] === "function" ? t[k].bind(t) : t[k] });
      const sink = this._pcmSink(spy, gain);
      const pre = new Uint8Array(1 + EAR_PRE_BYTES);
      pre[0] = 1;
      (async () => {
        const r = await this._hass.fetchWithAuth(`/api/dahua_talk/listen/${encodeURIComponent(this._cfg.speaker)}`, { signal: ac.signal });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const reader = r.body.getReader();
        let rest = new Uint8Array(0);
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          const u = new Uint8Array(1 + rest.length + value.length);  // 0x01 + PCM16, the odd byte next time
          u[0] = 1; u.set(rest, 1); u.set(value, 1 + rest.length);
          const n = (u.length - 1) & ~1;
          rest = u.slice(1 + n);
          if (!n) continue;
          if (!ear.live) {
            ear.live = true;
            this._applyAudio();
          }
          if (end < ctx.currentTime + 0.01) sink({ data: pre.buffer });  // same test as _pcmSink's realign
          sink({ data: u.buffer.slice(0, 1 + n) });
        }
      })().catch((e) => { if (!ac.signal.aborted) console.warn("db1c-doorbell-card: visitor voice (talk):", e.message || e); })
        .finally(() => {  // stream over (HA restart, channel dropped): the RTSP audio comes back
          if (ear.live) { ear.live = false; if (this._ws?.ear === ear) this._applyAudio(); }
        });
      return ear;
    }

    // The doorbell's talk channel: a separate PeerConnection, mic only (sendonly), opened on the Talk tap and
    // closed on the second tap (like the Frigate UI). The live stream stays as it was: the visitor voice is not
    // interrupted, and at rest nobody holds 8557.
    _micLeg(track) {
      const pc = new RTCPeerConnection();
      const fail = (why) => {
        if (this._ws?.pc !== pc) return;
        this._err.textContent = `${this._t.mic_err}: ${why}`;
        this._stopAudio();
      };
      pc.addTransceiver(track, { direction: "sendonly" });
      pc.onconnectionstatechange = () => pc.connectionState === "failed" && fail("ICE failed");
      this._signal(pc, fail);
      return pc;
    }

    _endMic() {  // no redraw: _closePeer calls it from inside _render too (_stopAudio's redraw runs _applyAudio)
      const a = this._ws;
      this._ws = null;
      if (!a) return false;
      if (this._audible === true) this._audible = a.audible;  // Talk over: listening back as before (unless muted meanwhile)
      a.mic.getTracks().forEach((t) => t.stop());
      hangUp(a.pc);
      a.ear.ac.abort();
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
    _playClip(e) {
      this._setDrawer(false);
      const v = this._clipV, base = this._evBase(e);
      v.poster = e.has_snapshot ? `${base}snapshot.jpg` : "";
      if (e.has_clip) v.src = `${base}clip.mp4`; else v.removeAttribute("src");
      v.hidden = this._back.hidden = false;
      this._card.classList.add("clip");
      if (e.has_clip) v.play().catch(() => {});  // iOS may want a tap on the controls
      this.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    _stopClip() {
      const v = this._clipV;
      if (!v) return;
      v.pause();
      v.removeAttribute("src");
      v.removeAttribute("poster");
      v.load();
      v.hidden = this._back.hidden = true;
      this._card.classList.remove("clip");
    }

    disconnectedCallback() {
      super.disconnectedCallback();
      this._stopClip();
      if (this._sheet?.open) this._sheet.close();
      clearTimeout(this._ringT);
      clearInterval(this._lineT);
      this._lineT = this._lineAt = null;
      this._closePeer();
      this._session = null;
      this._retry = 0;
      this._live = undefined;  // recreated on return
    }
  }

  customElements.get(TAG) || customElements.define(TAG, Db1cDoorbellCard);
});

window.customCards = window.customCards || [];
window.customCards.push({ type: TAG, name: "DB1C Doorbell",
  description: "The Vimar intercom card for the EZVIZ DB1C: video and voice from go2rtc/Frigate, talk, open, Frigate history." });
