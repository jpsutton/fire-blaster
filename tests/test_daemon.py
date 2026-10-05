import asyncio

from evdev import InputEvent, ecodes as e

from fireblaster import daemon
from fireblaster.config import Config, HoldAction
from fireblaster.controller import Controller
from fireblaster.profiles import ProfileSet
from fireblaster.state import State

from test_controller import PROFILES, FakeTx


class FakeDevice:
    def __init__(self, name, keys, events, path="/dev/input/event9"):
        self.name, self.path, self.phys = name, path, ""
        self._keys, self._events = keys, events
        self.grabbed = self.closed = False
        self.ff_effects_count = 0
        self.held = []  # successive active_keys() answers; [] once exhausted
        self.held_while_grabbing = None

    def capabilities(self):
        return {e.EV_KEY: self._keys}

    def active_keys(self):
        return self.held.pop(0) if self.held else []

    def grab(self):
        self.held_while_grabbing = self.active_keys()
        self.grabbed = True

    def close(self):
        self.closed = True

    async def async_read_loop(self):
        for ev in self._events:
            yield ev
            await asyncio.sleep(0)
        raise OSError(19, "No such device")  # remote went to sleep


class FakeUInput:
    instances = []

    def __init__(self, events=None, name="py-evdev-uinput", max_effects=96):
        self.name, self.events, self.closed = name, [], False
        self.capabilities = events or {}
        FakeUInput.instances.append(self)

    def write_event(self, ev):
        self.events.append((ev.type, ev.code, ev.value))

    def write(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.events.append((e.EV_SYN, e.SYN_REPORT, 0))

    def close(self):
        self.closed = True


def key(code, value):
    return InputEvent(0, 0, e.EV_KEY, code, value)


def syn():
    return InputEvent(0, 0, e.EV_SYN, e.SYN_REPORT, 0)


def make_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "UInput", FakeUInput)
    FakeUInput.instances.clear()
    cfg = Config(state_path=tmp_path / "state.json")
    tx = FakeTx()
    ctl = Controller(cfg, ProfileSet(PROFILES), State(cfg.state_path, "lg-a"), tx)
    return daemon.DeviceManager(cfg, ctl), tx


def test_matches(tmp_path, monkeypatch):
    mgr, _ = make_manager(tmp_path, monkeypatch)
    assert mgr.matches(FakeDevice("AR", [e.KEY_VOLUMEUP], []))
    assert mgr.matches(FakeDevice("Amazon Fire TV Remote Consumer Control", [e.KEY_MUTE], []))
    assert not mgr.matches(FakeDevice("Amazon Fire TV Remote", [e.KEY_LEFT], []))  # no intercepted keys
    assert not mgr.matches(FakeDevice("Logitech Keyboard", [e.KEY_VOLUMEUP], []))
    assert not mgr.matches(FakeDevice("AR" + daemon.UINPUT_SUFFIX, [e.KEY_VOLUMEUP], []))


def test_serve_filters_and_cleans_up(tmp_path, monkeypatch):
    mgr, tx = make_manager(tmp_path, monkeypatch)
    events = [
        key(e.KEY_VOLUMEUP, 1), syn(), key(e.KEY_VOLUMEUP, 0), syn(),
        key(e.KEY_LEFT, 1), syn(), key(e.KEY_LEFT, 0), syn(),
    ]
    dev = FakeDevice("AR", [e.KEY_VOLUMEUP, e.KEY_LEFT], events)
    mgr.tasks[dev.path] = None

    asyncio.run(mgr.serve(dev))

    assert dev.grabbed and dev.closed
    (ui,) = FakeUInput.instances
    assert ui.name == "AR (fire-blaster)" and ui.closed
    keys = [(c, v) for t, c, v in ui.events if t == e.EV_KEY]
    assert keys == [(e.KEY_LEFT, 1), (e.KEY_LEFT, 0)]
    assert tx.sent == [("lg-a", "VOLUME_UP", False)]
    assert dev.path not in mgr.tasks


def test_combo_claim_injects_release(tmp_path, monkeypatch):
    mgr, tx = make_manager(tmp_path, monkeypatch)
    mgr.ctl.inject = mgr.inject
    events = [key(e.KEY_BACK, 1), syn(), key(e.KEY_KPENTER, 1), syn(), key(e.KEY_BACK, 0), syn(), key(e.KEY_KPENTER, 0), syn()]
    dev = FakeDevice("AR Keyboard", [e.KEY_VOLUMEUP, e.KEY_BACK, e.KEY_KPENTER], events)

    asyncio.run(mgr.serve(dev))

    (ui,) = FakeUInput.instances
    keys = [(c, v) for t, c, v in ui.events if t == e.EV_KEY]
    assert keys == [(e.KEY_BACK, 1), (e.KEY_BACK, 0)]  # release injected at claim; OK never seen


def test_clone_adds_hold_keys_the_remote_lacks(tmp_path, monkeypatch):
    mgr, _ = make_manager(tmp_path, monkeypatch)
    mgr.cfg.hold = {e.KEY_HOMEPAGE: HoldAction(0.05, (e.KEY_LEFTMETA, e.KEY_HOMEPAGE))}
    mgr.ctl.inject = mgr.inject
    events = [key(e.KEY_HOMEPAGE, 1), syn(), key(e.KEY_HOMEPAGE, 0), syn()]
    dev = FakeDevice("AR Keyboard", [e.KEY_VOLUMEUP, e.KEY_HOMEPAGE], events)

    asyncio.run(mgr.serve(dev))

    (ui,) = FakeUInput.instances
    assert e.KEY_LEFTMETA in ui.capabilities[e.EV_KEY]
    keys = [(c, v) for t, c, v in ui.events if t == e.EV_KEY]
    assert keys == [(e.KEY_HOMEPAGE, 1), (e.KEY_HOMEPAGE, 0)]  # short press: a tap


def test_remap_rewrites_passed_keys(tmp_path, monkeypatch):
    mgr, _ = make_manager(tmp_path, monkeypatch)
    mgr.cfg.remap = {e.KEY_MENU: e.KEY_COMPOSE, e.KEY_KPENTER: e.KEY_ENTER}
    events = [key(e.KEY_MENU, 1), syn(), key(e.KEY_MENU, 0), syn(), key(e.KEY_KPENTER, 1), syn(), key(e.KEY_KPENTER, 0), syn()]
    # Only remap keys, no [keys] entries: the remote is still grabbed.
    mgr.cfg.keymap = {}
    dev = FakeDevice("AR Keyboard", [e.KEY_MENU, e.KEY_KPENTER], events)
    assert mgr.matches(dev)

    asyncio.run(mgr.serve(dev))

    (ui,) = FakeUInput.instances
    assert {e.KEY_COMPOSE, e.KEY_ENTER} <= ui.capabilities[e.EV_KEY]
    keys = [(c, v) for t, c, v in ui.events if t == e.EV_KEY]
    assert keys == [(e.KEY_COMPOSE, 1), (e.KEY_COMPOSE, 0), (e.KEY_ENTER, 1), (e.KEY_ENTER, 0)]


def test_hold_applies_to_the_remapped_key(tmp_path, monkeypatch):
    mgr, _ = make_manager(tmp_path, monkeypatch)
    mgr.cfg.remap = {e.KEY_MENU: e.KEY_COMPOSE}
    mgr.cfg.hold = {e.KEY_COMPOSE: HoldAction(0.05, (e.KEY_LEFTMETA, e.KEY_COMPOSE))}
    mgr.ctl.inject = mgr.inject

    class SlowDevice(FakeDevice):
        async def async_read_loop(self):
            yield key(e.KEY_MENU, 1)
            await asyncio.sleep(0.1)  # held past the hold time
            yield key(e.KEY_MENU, 0)
            raise OSError(19, "No such device")

    dev = SlowDevice("AR Keyboard", [e.KEY_VOLUMEUP, e.KEY_MENU], [])
    asyncio.run(mgr.serve(dev))

    (ui,) = FakeUInput.instances
    keys = [(c, v) for t, c, v in ui.events if t == e.EV_KEY]
    assert keys == [(e.KEY_LEFTMETA, 1), (e.KEY_COMPOSE, 1), (e.KEY_COMPOSE, 0), (e.KEY_LEFTMETA, 0)]


def test_remote_rule_names_are_grabbed_with_their_own_drop(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "UInput", FakeUInput)
    FakeUInput.instances.clear()
    cfg = Config(state_path=tmp_path / "state.json", device_names=["^AR"], keymap={})
    cfg.apply({"remote": [{"names": ["CIR transceiver$"], "drop": ["KEY_MUTE"]}]})
    ctl = Controller(cfg, ProfileSet(PROFILES), State(cfg.state_path, "lg-a"), FakeTx())
    mgr = daemon.DeviceManager(cfg, ctl)
    # Matched only by the [[remote]] names, and grabbed for its drop key alone.
    events = [key(e.KEY_MUTE, 1), syn(), key(e.KEY_MUTE, 0), syn(), key(e.KEY_UP, 1), syn(), key(e.KEY_UP, 0), syn()]
    dev = FakeDevice("ITE8713 CIR transceiver", [e.KEY_MUTE, e.KEY_UP], events)
    assert mgr.matches(dev)
    assert not mgr.matches(FakeDevice("Some Keyboard", [e.KEY_MUTE], []))

    asyncio.run(mgr.serve(dev))

    (ui,) = FakeUInput.instances
    keys = [(c, v) for t, c, v in ui.events if t == e.EV_KEY]
    assert keys == [(e.KEY_UP, 1), (e.KEY_UP, 0)]  # mute dropped


def test_grab_waits_for_held_keys(tmp_path, monkeypatch):
    """A key down when the remote is grabbed would stay down for the desktop
    (its release would come to us), so the grab waits for it to come up."""
    mgr, _ = make_manager(tmp_path, monkeypatch)
    dev = FakeDevice("AR", [e.KEY_VOLUMEUP, e.KEY_DOWN], [])
    dev.held = [[e.KEY_DOWN], [e.KEY_DOWN], [e.KEY_DOWN]]
    mgr.tasks[dev.path] = None

    asyncio.run(mgr.serve(dev))

    assert dev.grabbed and dev.held_while_grabbing == []


def test_grab_gives_up_on_a_stuck_key(tmp_path, monkeypatch, caplog):
    mgr, _ = make_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon, "GRAB_WAIT_SECONDS", 0.05)
    dev = FakeDevice("AR", [e.KEY_VOLUMEUP, e.KEY_DOWN], [])
    dev.held = [[e.KEY_DOWN]] * 1000
    mgr.tasks[dev.path] = None

    asyncio.run(mgr.serve(dev))

    assert dev.grabbed
    assert "grabbing with KEY_DOWN still held" in caplog.text
