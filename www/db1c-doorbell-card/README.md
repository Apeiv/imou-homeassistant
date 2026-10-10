# DB1C doorbell card

A Lovelace card for an EZVIZ DB1C (or another HCNetSDK doorbell):

- live video and audio from Frigate's go2rtc, through the Frigate integration's WebRTC proxy;
- two-way talk: the microphone goes to dahua_talk's RTSP backchannel source on port 8557, and the visitor's
  voice comes back from `/api/dahua_talk/listen/<speaker entity>` while you talk;
- the camera's Frigate event history in a bottom sheet (drag it down to close), with clips;
- an optional Open button for a lock;
- a camera-only mode for any other Frigate camera (see [Camera only](#camera-only)).

It extends `vimar-intercom-card`, so it has the same layout, buttons and states. Away from home, when WebRTC
can't connect, it falls back to Home Assistant's camera stream (video only) and keeps retrying WebRTC every minute.

## Requirements

- The Vimar intercom card loaded as a resource (`vimar-intercom-card`). This card is a subclass of it, and it
  shows an error card if the loaded version lacks the hooks it needs.
- The Frigate integration, with the doorbell as a Frigate camera and its go2rtc stream.
- dahua_talk with an HCNetSDK entry for the doorbell. Add its backchannel source
  (`rtsp://<home assistant>:8557/...#backchannel=1`) to the go2rtc stream in Frigate.

## Install

1. Copy `db1c-doorbell-card.js` to `/config/www/`.
2. Add the resource `/local/db1c-doorbell-card.js` with type `module` (Settings > Dashboards > Resources).
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
| `camera` | yes | | Camera entity: offline state, still picture, video fallback. Example `camera.front_door` |
| `stream` | yes | | go2rtc stream name inside Frigate. Example `front_door` |
| `frigate_camera` | yes | | Frigate camera name for the event history. Example `front_door` |
| `ring` | no | | Without it the card is [camera only](#camera-only). Entity that is `on` while ringing (input_boolean, binary_sensor) or an `event.*` entity. Example `input_boolean.doorbell_ring` |
| `speaker` | no | | Required with `ring`. dahua_talk media_player of the doorbell (visitor voice while talking). Example `media_player.front_door_speaker` |
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
| `language` | no | HA language | `en` or `it`; other languages fall back to English |
| `colors` | no | theme | See [Colors](#colors) |

Other Vimar card options (such as `shortcuts`) pass through.

## Camera only

Leave out `ring` (and `speaker`) and the card shows any Frigate camera with the same look: the latest picture
at once, then live WebRTC, Fit/Fill, the event history with clips. There is no talk, ring or Open button; the
Listen button appears only if the go2rtc stream sends audio WebRTC can play (Opus/PCM, not AAC). It is always
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

## Notes

- dahua_talk closes the talk channel after 3 minutes of each Talk session.
- On a PC use headphones: Chrome doesn't echo-cancel audio played through WebAudio, so the visitor may hear
  themselves.
- iOS starts the video muted: tap the speaker button to hear it.
