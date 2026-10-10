# DB1C doorbell card

A Lovelace card for an EZVIZ DB1C (or another HCNetSDK doorbell):

- live video and audio from Frigate's go2rtc with go2rtc's own player (WebRTC, MSE or MJPEG, whichever
  connects), through the Frigate integration's proxy;
- a call (hear and talk at once) on one Home Assistant WebSocket, `/api/dahua_talk/call_ws/<speaker entity>`,
  at home and away from home alike;
- the camera's Frigate event history in a bottom sheet (drag it down to close), with clips (Frigate's HLS where the browser plays it natively, as on iOS);
- an optional Open button for a lock;
- a camera-only mode for any other Frigate camera (see [Camera only](#camera-only)).

It extends `vimar-intercom-card`, so it has the same layout, buttons and states. The camera's latest picture
shows until the first live frame arrives.

## Requirements

- The Vimar intercom card loaded as a resource (`vimar-intercom-card`). This card is a subclass of it, and it
  shows an error card if the loaded version lacks the hooks it needs.
- The Frigate integration, with the doorbell as a Frigate camera and its go2rtc stream.
- dahua_talk with an HCNetSDK entry for the doorbell and its `call_ws` view. Without it the card works and
  the call says "Talk not available".

## Install

1. Copy the `db1c-doorbell-card` folder (the card and its `go2rtc/` folder) to `/config/www/`.
2. Add the resource `/local/db1c-doorbell-card/db1c-doorbell-card.js` with type `module` (Settings > Dashboards > Resources).
3. Add the card in YAML (there is no visual editor).

```yaml
type: custom:db1c-doorbell-card
name: Front door
camera: camera.front_door
stream: front_door
frigate_camera: front_door
ring: input_boolean.doorbell_ring
ring_time: input_datetime.doorbell_last_ring
lock: lock.front_door
speaker: media_player.front_door_speaker
```

## Options

| Key | Required | Default | Description |
|---|---|---|---|
| `camera` | yes | | Camera entity: offline state, still picture. Example `camera.front_door` |
| `stream` | yes | | go2rtc stream name inside Frigate. Example `front_door` |
| `frigate_camera` | yes | | Frigate camera name for the event history. Example `front_door` |
| `ring` | no | | Without it the card is [camera only](#camera-only). Entity that is `on` while ringing (input_boolean, binary_sensor) or an `event.*` entity. Example `input_boolean.doorbell_ring` |
| `speaker` | no | | Required with `ring`. dahua_talk media_player of the doorbell (the call). Example `media_player.front_door_speaker` |
| `ring_time` | no | none | input_datetime with the last ring time: survives page reloads. Example `input_datetime.doorbell_last_ring` |
| `lock` | no | none | Lock opened with `lock.open`. Without `lock` (and without `shortcuts`) there is no Open button. Example `lock.front_door` |
| `name` | no | `Doorbell` (`Camera` without `ring`) | Card name |
| `frigate_instance` | no | `frigate` | client_id of the Frigate integration |
| `history` | no | `50` | Events per history page |
| `history_labels` | no | all | Only these Frigate labels, e.g. `[person]` |
| `ring_timeout` | no | `30` | Seconds a ring lasts even if `ring` turns off sooner |
| `confirm_open` | no | `true` | Open needs a second tap within 3 s |
| `layout` | no | `overlay` | `overlay`, `sotto` or `popup` (Vimar card layouts) |
| `always_live` | no | `true` | Live video at rest too; `false` = only on ring or call |
| `anchor` | no | `doorbell` (none without `ring`) | URL hash (`#doorbell`) that scrolls the card into view |
| `listen_on_ring` | no | `false` | Hear the visitor as soon as it rings (Vimar card option) |
| `ear_buffer` | no | `0.12` on a LAN address, else `0.5` | Seconds of visitor voice kept in hand during a call: more = fewer gaps, more delay |
| `language` | no | HA language | `en` or `it`; other languages fall back to English |
| `colors` | no | theme | See [Colors](#colors) |

Other Vimar card options (such as `shortcuts`) pass through.

## Camera only

Leave out `ring` (and `speaker`) and the card shows any Frigate camera with the same look: the latest picture
at once, then live video, Fit/Fill, the event history with clips. There is no talk, ring or Open button; the
Listen button appears only if the stream that plays has audio. It is always
live in the card: `always_live` and `layout` are ignored.

```yaml
type: custom:db1c-doorbell-card
name: Living room
camera: camera.living_room
stream: living_room
frigate_camera: living_room
```

## Colors

The status pill, the ring outline, the round buttons and the history sheet use the card's own CSS variables. Unset, the pill
follows the Home Assistant theme (through the Vimar card: `--warning-color`, `--primary-color`, its dark glass)
and the buttons are light glass with dark icons.

| Key | Variable | Used for | Default |
|---|---|---|---|
| `accent` | `--db1c-accent` | status dot | state colour (`--primary-color` at rest) |
| `warning` | `--db1c-warning` | ringing pill and outline | `--warning-color` |
| `on-warning` | `--db1c-on-warning` | text on the ringing pill | `#fff` |
| `glass` | `--db1c-glass` | pill background | dark glass |
| `ink` | `--db1c-ink` | pill text | `#fff` |
| `button` | `--db1c-button` | round buttons' glass | light glass `rgba(255,253,247,.58)` |
| `button-ink` | `--db1c-button-ink` | round buttons' icons | `#1b1812` |
| `sheet` | `--db1c-sheet` | history sheet background | `--card-background-color` |
| `sheet-ink` | `--db1c-sheet-ink` | history sheet text | `--primary-text-color` |

Set the variables in your theme, or per card with `colors:` (it wins over the theme). Values are any CSS
colour, including `var(...)`. `accent` replaces the per-state dot colours with one colour:

```yaml
colors:
  accent: var(--accent-color)
  warning: "#e69500"
  glass: rgba(255, 255, 255, 0.85)
  ink: var(--primary-text-color)
```

## How it works

- **Video:** `go2rtc/video-rtc.js` from go2rtc, unmodified, in modes `webrtc,mse,mjpeg` on
  `/api/frigate/<frigate_instance>/mse/api/ws?src=<stream>`. A 10-line subclass (`db1c-video-rtc`) signs the
  URL again (`auth/sign_path`, 30 s) on every connection and reconnection. The card never calls `play()` or sets
  `src`/`srcObject` on the player's video: it reads its events and sets `muted`.
- **Call:** Answer opens the microphone (echo cancellation, noise suppression) and one signed WebSocket to
  `/api/dahua_talk/call_ws/<speaker>`. The server sends `{"type":"ready"}`; busy = close code 4409, the 180 s cap =
  close code 4408. Binary `0x01` + PCM16 LE mono 8 kHz is the visitor's voice, the card sends `0x02` + the
  same for the microphone, only after `ready`. During the call the video is muted, Talk mutes the microphone and
  Hang up ends the call. On a phone the call ends when the app goes to the background.

## go2rtc player file

`go2rtc/video-rtc.js` is go2rtc's web player, unmodified, from go2rtc **1.9.14** (the go2rtc inside Frigate 0.18),
sha256 `d48ce627baf7c341a92c0f5844a3c546431f9db873ff21489671aba2ecfe64fb`, MIT licence (`go2rtc/LICENSE`).
Take it from the same version as Frigate's go2rtc when Frigate is updated.

The card imports `./go2rtc/video-rtc.js` relative to itself: serve it from its folder
(`/local/db1c-doorbell-card/db1c-doorbell-card.js` with `go2rtc/` next to it), not as a lone file in `/config/www/`.

## Notes

- dahua_talk closes the call after 3 minutes.
- On a PC use headphones: Chrome doesn't echo-cancel audio played through WebAudio, so the visitor may hear
  themselves.
- The video starts muted: tap the speaker button to hear it.
