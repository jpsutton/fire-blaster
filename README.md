# fire-blaster

Control a hotel TV with an Alexa Voice Remote that is paired to a Linux mini PC.

The remote's navigation keys drive the media apps on the PC (Kodi, Jellyfin
Desktop, and so on). Its volume, mute and power keys are intercepted and sent as
IR to the TV through a USB IR blaster. You pick the TV's code set with the
remote itself, so no Fire TV is needed and nothing has to be set up in advance.

This rewrites [service.irblaster](https://github.com/jpsutton/service.irblaster).
It runs as a standalone systemd service instead of a Kodi add-on, reads the
remote through evdev instead of eventlircd, and keeps IR codes in profile files
instead of in the source.

**Status:** IR goes out through any kernel LIRC transmitter (`/dev/lircN`).
The Media Center USB transceiver (`mceusb`) is the target hardware. See
[IR output](#ir-output).

## How it works

```
BT remote ──► /dev/input/eventN ──grab──► fireblasterd ──► uinput clone ──► Kodi / Jellyfin / Plasma
                                               │            (nav keys only)
                                               ▼
                                       vol/mute/power ──► IR emitter ──► TV
                                               │
                                     control socket (JSON) ◄──► fireblaster-setup (on-screen UI)
```

- Each input device whose name matches `[device] names` and that can send an
  intercepted key is grabbed (`EVIOCGRAB`). The daemon re-sends every event
  except the intercepted keys through a uinput clone named `<name> (fire-blaster)`.
  Apps never see volume or power, so you don't need `noop` keymaps and
  `KEY_POWER` can't shut down the PC.
- Volume keys re-blast every 120 ms while held, after a 350 ms delay, on remotes
  that report holds. The Alexa Voice Remote doesn't: it sends volume, mute and
  power as instant taps (press and release in the same millisecond, however
  long the key is held), so each press is one IR blast.
- Toggle-bit codes (RC5/RC6, stored as `code1`/`code2` in the Amazon data)
  alternate on each press.
- The remote sleeps and reconnects often, so input devices are rescanned every
  second.

## IR output

fireblasterd writes each code's pulse/space durations to a LIRC device after
setting its carrier, as `ir-ctl --send` does, but from the running daemon, so
there is no process to start per key press:

```toml
[ir]
device = "auto"     # default; or "/dev/lirc1", or "log" to only log
# driver = "mceusb" # with "auto": only devices of this kernel driver
```

With `"auto"`, the first LIRC device that can send is used, USB devices first:
a plugged-in blaster wins over a built-in CIR port, which often has a
transmitter in the chip but no emitter wired to it. The device is opened on
the first key press and found again after an error, so a blaster plugged in
or replugged later is picked up. With no transmitter, fireblasterd logs one
warning and drops the codes. `fireblasterd --list-devices` lists the
transmitters, and `--ir log` only logs what would be sent.

LIRC devices are root-only by default; `udev/70-fire-blaster.rules` opens
them to the `input` group, as it does `/dev/uinput`.

For a cheap transmitter, `firmware/pico/` turns a Raspberry Pi Pico or a
USB-A RP2040 board and an IR LED module into a USB blaster. The kernel's
`ir_toy` driver sees it as an IR Toy and registers it as a LIRC device, so
`"auto"` finds it. See [firmware/pico/README.md](firmware/pico/README.md).

## A remote for an AV receiver

The old service.irblaster add-on sent a Media Center remote's volume, mute and
number keys to a Denon AV receiver hidden in a cabinet, through the MCE
transceiver's blaster cable. A `[[remote]]` table with a `profile` does the
same: that remote's keys always blast that profile, whatever TV setup mode
picked.

```toml
[[remote]]
names = ["eHome Infrared"]   # the Media Center transceiver's input device
profile = "denon-avr"        # extra-profiles/avr/denon-avr.toml

[remote.keys]
KEY_VOLUMEUP = "VOLUME_UP"
KEY_VOLUMEDOWN = "VOLUME_DOWN"
KEY_MUTE = "MUTE_TOGGLE"
KEY_NUMERIC_1 = "INPUT_BD"
KEY_NUMERIC_2 = "INPUT_GAME"
KEY_NUMERIC_3 = "INPUT_DVD"
```

Unlike the add-on, volume repeats while held (the MCE remote reports holds),
and no Kodi `noop` keymap is needed, because grabbed keys never reach apps.
`extra-profiles/` holds hand-captured profiles like this one; the packaged
profile directory gets them next to the generated TV code sets.

## Remapped keys

A `[remap]` entry makes a key arrive as another key. It is applied before
anything else, so `[hold]`, the `[setup]` combo and apps all see the new key:

```toml
[remap]
KEY_KPENTER = "KEY_ENTER"   # many remotes send keypad Enter for OK
KEY_MENU = "KEY_COMPOSE"    # KEY_MENU maps to XF86MenuKB; KEY_COMPOSE is the Menu keysym
```

Remaps apply only to grabbed remotes. A remote with only `[remap]` keys is
still grabbed.

## Dropped keys

A `[drop]` entry swallows keys outright: no IR, and apps never see them. Use
it when the remote already handles a key itself, for example a remote that
was paired to the TV and sends volume and power to it over its own IR:

```toml
[drop]
keys = ["KEY_VOLUMEUP", "KEY_VOLUMEDOWN", "KEY_MUTE", "KEY_POWER"]
```

Without it, those keys would also reach the PC (an empty `[keys]` table) or
blast IR (the default `[keys]`), so each press would act twice. `[drop]` wins
over `[keys]` and `[hold]`, and applies after `[remap]`.

## Per-remote settings

With more than one kind of remote, a `[[remote]]` section gives the remotes
whose device name matches its `names` their own `keys` and/or `drop`; anything
it leaves out falls back to the global `[keys]` and `[drop]`. Its names are
grabbed like `[device]` names, and the first matching section wins. `[remap]`,
`[hold]` and `[setup]` stay global.

```toml
[[remote]]
names = ["^AR( Keyboard)?$"]   # controls the TV itself: drop what it already sent
drop = ["KEY_VOLUMEUP", "KEY_VOLUMEDOWN", "KEY_MUTE", "KEY_POWER"]

[[remote]]
names = ["CIR transceiver$", "eHome Infrared"]   # Media Center remotes
keys = { KEY_POWER2 = "POWER_TOGGLE" }           # once an IR blaster is fitted
```

This is how a key one remote must ignore can still work on another: dropped
on a remote that controls the TV, passed to the PC or blasted on one that
doesn't.

## Long-press keys

A `[hold]` entry gives a key a second action when held. The key is held back
from apps until fireblasterd knows which it is:

- Released before `seconds` (default 0.6): it is sent as a normal tap, on
  release.
- Held longer: the `send` keys are sent as one chord (pressed in order,
  released in reverse), and the key itself never reaches apps.

```toml
[hold.KEY_HOMEPAGE]
seconds = 0.6
send = ["KEY_LEFTMETA", "KEY_HOMEPAGE"]
```

With this, a short Home press goes to the focused app (Kodi and Jellyfin both
go to their own home screen), and a long press sends Meta+Home Page, which a
desktop shortcut can bind (couchbox uses it to show the Plasma Bigscreen
launcher). Only keys the remote reports as held work: on the Alexa Voice
Remote, Home does (down, kernel autorepeat, up), while volume, mute and power
arrive as instant taps. A remote with only `[hold]` keys and an empty `[keys]`
table is still grabbed, so the feature works without IR output.

## Setup mode: choosing the TV's code set

Hold **Back and OK** together for **5 seconds** (`[setup] combo`). Apps may
see one Back press: the first key passes through as normal until the second
one arrives, then the daemon releases it and swallows the rest. Then:

| Key | Action |
|---|---|
| Right / Left | next / previous brand (its first code set) |
| Down / Up | next / previous code set within the brand; wraps around |
| Vol+, Vol-, Mute, Power | send that function with the current candidate |
| OK | save the candidate and exit setup mode |
| Back / Home | exit without saving |

Every move sends VOLUME_UP as a test.

Setup mode also exits without saving after 60 seconds with no key presses. The
candidates are TV profiles from the brands in `[setup] brands`, in list order,
with higher-confidence code sets first. Code sets with identical
power/volume/mute codes appear only once. The saved choice goes to
`state.json`.

### On-screen setup UI

`fireblaster-setup` (PySide6; install with `pip install -e '.[ui]'`) is a
fullscreen 10-foot overlay for setup mode. It runs in the desktop session and
talks to the daemon over its control socket, because the daemon holds the
remote exclusively and the UI can't read remote keys itself. It stays hidden
until something happens:

- while Back+OK is held, it shows a countdown;
- in setup mode, it lays brands out left to right and the current brand's
  code sets top to bottom, the same way the keys move. It also shows what was
  last sent ("Sent Volume Up. Did the TV react?"), key hints and the idle
  timeout;
- when setup ends, it shows "Saved" or "Setup cancelled" for 2.5 s, then hides.

The remote works whatever has focus. When the window has focus, a keyboard
works too (arrows, Enter, Esc, `+`/`-`/`M`/`P` to test), and so do mouse
clicks on the hint buttons.

```sh
fireblaster-setup            # persistent overlay; see desktop/fireblaster-setup-autostart.desktop
fireblaster-setup --start    # start setup now: the Bigscreen tile, desktop/fireblaster-setup.desktop
fireblaster-setup --demo     # scripted walkthrough, no daemon needed
fireblaster-setup --windowed # normal window, for debugging
```

Only one instance runs per user. A second `--start` hands off to the running
overlay and exits. The UI looks for the socket at `$FIREBLASTER_SOCKET`, then
`/run/fire-blaster/control.sock` (the system service), then
`$XDG_RUNTIME_DIR/fire-blaster/control.sock` (a daemon you started yourself).

On Wayland, KWin may not keep a client window above a fullscreen player. If
Kodi covers the overlay, run the overlay under XWayland
(`QT_QPA_PLATFORM=xcb fireblaster-setup`) or add a KWin window rule that sets
"Keep above" for `fireblaster-setup`.

The control socket protocol is in `src/fireblaster/control.py`: line-delimited
JSON, with state snapshots and events from the daemon and commands from the
client.

## Install (development)

```sh
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest
```

Build profiles from the Amazon Fire TV IR dump
([shaikh-amaan-fm/fire_tv_remote_ir_db](https://github.com/shaikh-amaan-fm/fire_tv_remote_ir_db)):

```sh
curl -LO https://raw.githubusercontent.com/shaikh-amaan-fm/fire_tv_remote_ir_db/main/ir_profiles_db.json
.venv/bin/fireblaster-import-amazon ir_profiles_db.json -o profiles        # TVs only (1259)
.venv/bin/fireblaster-import-amazon ir_profiles_db.json -o profiles --all-types
```

Run it. It needs read access to `/dev/input/event*` and write access to
`/dev/uinput`, which means the `input` group plus `udev/70-fire-blaster.rules`,
or root:

```sh
.venv/bin/fireblasterd --list-devices          # find the remote; * = would be grabbed
.venv/bin/fireblasterd -p profiles -v          # -v logs every key and the IR pulse data
.venv/bin/fireblasterd -p profiles --no-grab   # watch only; apps still see every key
```

For a system install, see `systemd/fireblasterd.service` (DynamicUser, `input`
group, `StateDirectory`, `RuntimeDirectory` for the control socket),
`udev/70-fire-blaster.rules`, `desktop/*.desktop` and `config.example.toml`.

## Profiles

Each profile is one TOML file for one code set:

```toml
id = "amazon-4495"
brand = "LG"
name = "LG TV"
device_type = "tv"      # tv, projector, stb, avr, soundbar
confidence = 6          # optional; higher sorts first in setup mode
blast_count = 1         # optional

[codes]
VOLUME_UP = ["0000 006D 0022 0002 0157 00AC ..."]        # Pronto hex
POWER_TOGGLE = ["0000 0073 ...", "0000 0073 ..."]        # toggle-bit variants
MUTE_TOGGLE = "+3450 -1600 +500 -300 ..."                  # ir-ctl pulse text, uses `carrier`
```

Function names follow the Amazon database: `VOLUME_UP`, `VOLUME_DOWN`,
`MUTE_TOGGLE`, `POWER_TOGGLE`, `POWER_ON`, `POWER_OFF`, `INPUT_SCROLL`, `HDMI_1`,
and so on. You can capture codes from any remote with
`ir-ctl -d /dev/lirc0 --receive` and paste them in as pulse text.

`fireblaster-pronto` converts between the two formats:

```sh
fireblaster-pronto decode 0000 006D 0022 0002 0156 00AB ...   # ir-ctl text (for ir-ctl --send)
fireblaster-pronto encode --carrier 38000 "+9000 -4500 +560 ..."
```

## Notes

- **Remote IR profile.** If the remote was ever set up for TV control on a Fire
  TV, it may still blast its own stored codes as well as sending BT events. Use
  a factory-reset remote, and check that it stays dark by pointing a phone
  camera at its IR LED.
- **Alexa Voice Remote key codes** (2nd gen, "AR Keyboard"): D-pad =
  `KEY_UP/DOWN/LEFT/RIGHT`, OK = `KEY_KPENTER`, Back = `KEY_BACK`, Home =
  `KEY_HOMEPAGE`, Menu = `KEY_MENU`, volume/mute/power = `KEY_VOLUMEUP/DOWN`,
  `KEY_MUTE`, `KEY_POWER`. Holding Play/Pause made the remote drop its BT
  connection for about 7 s, so don't use it in a combo.
- **Device names.** The default patterns (`^AR( Keyboard)?$`, `Amazon`, `Fire ?TV`)
  cover the 2nd-gen Alexa Voice Remote ("AR Keyboard"). Confirm the real name with `--list-devices`.
