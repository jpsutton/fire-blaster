"""fireblasterd: grab the remote's input devices, blast IR, pass the rest through.

For each input device whose name matches the config and which can emit an
intercepted key, the daemon:

1. creates a uinput clone named "<name> (fire-blaster)",
2. grabs the real device (EVIOCGRAB) so no other program sees its events,
3. forwards every event to the clone except the keys the controller swallows.

Media apps therefore only ever see navigation keys, while volume/mute/power
become IR. The remote sleeps and reconnects often, so devices are rescanned
every second.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import signal
import sys
from pathlib import Path

import evdev
from evdev import InputDevice, UInput, ecodes

from .config import Config, ConfigError
from .control import ControlServer
from .controller import Controller, key_name
from .profiles import ProfileSet
from .state import State
from .transmit import LircEmitter, LogEmitter, Transmitter, lirc_nodes

log = logging.getLogger("fireblasterd")

UINPUT_SUFFIX = " (fire-blaster)"
SCAN_INTERVAL = 1.0


def _clone(dev: InputDevice, extra_keys: set[int]) -> UInput:
    """A uinput device with dev's capabilities plus extra_keys, which the
    controller may send (a hold chord can use keys the remote lacks)."""
    caps: dict[int, set] = {}
    for ev_type, codes in dev.capabilities().items():
        if ev_type not in (ecodes.EV_SYN, ecodes.EV_FF):
            caps[ev_type] = set(codes)
    caps.setdefault(ecodes.EV_KEY, set()).update(extra_keys)
    return UInput(events=caps, name=dev.name + UINPUT_SUFFIX, max_effects=dev.ff_effects_count)


def _node_signature(path: str) -> tuple[int, int] | None:
    """Identity of a device node, so a recreated node at the same path is rescanned."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_rdev, st.st_ctime_ns)


class DeviceManager:
    def __init__(self, cfg: Config, controller: Controller, grab: bool = True):
        self.cfg = cfg
        self.ctl = controller
        self.grab = grab
        self.patterns = [re.compile(p) for p in cfg.all_device_names]
        self.tasks: dict[str, asyncio.Task] = {}
        self.uinputs: dict[str, UInput] = {}
        self.ignored: dict[str, tuple[int, int]] = {}

    def inject(self, source: str, code: int, value: int) -> None:
        """Write a synthetic key event to a device's uinput clone."""
        if ui := self.uinputs.get(source):
            ui.write(ecodes.EV_KEY, code, value)
            ui.syn()

    def matches(self, dev: InputDevice) -> bool:
        if dev.name.endswith(UINPUT_SUFFIX):
            return False
        if not any(p.search(dev.name) for p in self.patterns):
            return False
        keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
        return bool(keys & self.ctl.intercepted_codes_for(dev.name))

    async def run(self) -> None:
        while True:
            self.scan()
            await asyncio.sleep(SCAN_INTERVAL)

    def scan(self) -> None:
        for path in evdev.list_devices():
            if path in self.tasks:
                continue
            sig = _node_signature(path)
            if sig is None or self.ignored.get(path) == sig:
                continue
            try:
                dev = InputDevice(path)
            except PermissionError:
                log.warning("no permission to open %s; is this user in the 'input' group?", path)
                self.ignored[path] = sig
                continue
            except OSError as e:
                log.debug("cannot open %s: %s", path, e)
                continue
            if not self.matches(dev):
                dev.close()
                self.ignored[path] = sig
                continue
            self.ignored.pop(path, None)
            self.tasks[path] = asyncio.create_task(self.serve(dev), name=f"serve {path}")

    async def serve(self, dev: InputDevice) -> None:
        path, name = dev.path, dev.name
        ui: UInput | None = None
        self.ctl.attach(path, name)
        try:
            if self.grab:
                try:
                    ui = _clone(dev, self.ctl.injected_codes)
                except (OSError, evdev.UInputError) as e:
                    log.error("cannot create uinput clone for %s (%s); leaving it ungrabbed, apps will also see volume/power", name, e)
                else:
                    dev.grab()
                    self.uinputs[path] = ui
            log.info("remote connected: %s (%s)%s", name, path, "" if ui else " [not grabbed]")

            async for event in dev.async_read_loop():
                if event.type == ecodes.EV_KEY:
                    # [remap] first, so holds, the setup combo and apps all see
                    # the remapped key.
                    code = self.ctl.remapped(event.code)
                    passed = self.ctl.handle(path, code, event.value)
                    if event.value != 2:  # skip kernel autorepeat, ~30/s while held
                        log.debug("%s %s %s", key_name(code), "down" if event.value else "up", "pass" if passed else "eat")
                    if not passed:
                        continue
                    if ui and code != event.code:
                        ui.write(ecodes.EV_KEY, code, event.value)
                        continue
                if ui:
                    ui.write_event(event)
        except OSError as e:
            log.info("remote gone: %s (%s): %s", name, path, e.strerror or e)
        finally:
            self.ctl.source_gone(path)
            self.uinputs.pop(path, None)
            if ui:
                ui.close()
            try:
                dev.close()
            except OSError:
                pass
            self.tasks.pop(path, None)


def list_devices(cfg: Config) -> int:
    patterns = [re.compile(p) for p in cfg.all_device_names]
    paths = sorted(evdev.list_devices(), key=lambda p: int(re.sub(r"\D", "", p) or 0))
    if not paths:
        print("no readable input devices (need root or the 'input' group?)")
    for path in paths:
        try:
            dev = InputDevice(path)
        except OSError as e:
            print(f"{path}: {e.strerror}")
            continue
        keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
        intercepted = set(cfg.keymap_for(dev.name)) | set(cfg.hold) | set(cfg.remap) | set(cfg.drop_for(dev.name))
        hit = keys & intercepted
        name_ok = any(p.search(dev.name) for p in patterns) and not dev.name.endswith(UINPUT_SUFFIX)
        mark = "*" if name_ok and hit else " "
        print(f"{mark} {path}: {dev.name!r} phys={dev.phys!r}")
        if hit:
            print(f"      intercepted keys: {', '.join(sorted(key_name(k) for k in hit))}")
        dev.close()
    print("\n* = would be grabbed with the current [device] names")
    nodes = lirc_nodes()
    print("\nIR transmitters (LIRC devices; [ir] device = \"auto\" tries them in this order):")
    for node in nodes:
        print(f"  {node.path}: {node.name!r} driver={node.driver}{' (USB)' if node.usb else ''}")
    if not nodes:
        print("  none")
    return 0


async def run(cfg: Config, grab: bool) -> None:
    profiles = ProfileSet.load(cfg.profile_dirs)
    state = State.load(cfg.state_path)
    if cfg.ir_device == "log":
        emitter = LogEmitter()
    elif cfg.ir_device == "auto":
        emitter = LircEmitter(driver=cfg.ir_driver)
    else:
        emitter = LircEmitter(device=cfg.ir_device)
    tx = Transmitter(emitter)
    controller = Controller(cfg, profiles, state, tx)
    devices = DeviceManager(cfg, controller, grab=grab)
    controller.inject = devices.inject
    control = ControlServer(controller, cfg.control_socket)
    try:
        await control.start()
    except OSError as e:
        log.error("control socket %s unavailable (%s); setup UI won't connect", cfg.control_socket, e)
        control = None

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = [asyncio.create_task(tx.run(), name="transmitter"), asyncio.create_task(devices.run(), name="scanner")]
    log.info("watching for remotes matching %s", cfg.all_device_names)
    try:
        await stop.wait()
    finally:
        controller.shutdown()
        for task in tasks + list(devices.tasks.values()):
            task.cancel()
        await asyncio.gather(*tasks, *devices.tasks.values(), return_exceptions=True)
        if control:
            await control.close()
        if isinstance(emitter, LircEmitter):
            emitter.close()
        log.info("stopped")


def _stderr_is_journal() -> bool:
    # JOURNAL_STREAM is inherited by child processes, so compare it with stderr
    # itself (the check sd_journal documents) instead of trusting it.
    try:
        st = os.fstat(sys.stderr.fileno())
    except (OSError, ValueError):
        return False
    return os.environ.get("JOURNAL_STREAM") == f"{st.st_dev}:{st.st_ino}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fireblasterd", description=__doc__.splitlines()[0])
    parser.add_argument("-c", "--config", type=Path, help="config file (default /etc/fire-blaster/config.toml if present)")
    parser.add_argument("-p", "--profiles", type=Path, action="append", help="profile directory; repeatable, replaces the configured list")
    parser.add_argument("--state", type=Path, help="state file path")
    parser.add_argument("--socket", type=Path, help="control socket path for the setup UI")
    parser.add_argument("--list-devices", action="store_true", help="show input devices and which would be grabbed, then exit")
    parser.add_argument("--no-grab", action="store_true", help="observe keys without grabbing (apps also see them)")
    parser.add_argument("--ir", metavar="DEVICE", help='IR transmitter: "auto", "log" (only log), or a path like /dev/lirc1')
    parser.add_argument("-v", "--verbose", action="count", default=0, help="-v for debug logging (per-key events, IR data)")
    args = parser.parse_args(argv)

    # journald adds its own timestamps.
    fmt = "%(levelname)s %(name)s: %(message)s" if _stderr_is_journal() else "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=fmt, stream=sys.stderr)

    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        log.error("config: %s", e)
        return 2
    if args.profiles:
        cfg.profile_dirs = args.profiles
    if args.state:
        cfg.state_path = args.state
    if args.socket:
        cfg.control_socket = args.socket
    if args.ir:
        cfg.ir_device = args.ir

    if args.list_devices:
        return list_devices(cfg)
    asyncio.run(run(cfg, grab=not args.no_grab))
    return 0


if __name__ == "__main__":
    sys.exit(main())
