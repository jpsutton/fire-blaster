import asyncio

import pytest
from evdev import ecodes as e

from fireblaster.config import Config, ConfigError, HoldAction
from fireblaster.controller import Controller, SetupSession
from fireblaster.profiles import Profile, ProfileSet
from fireblaster.state import State

SRC = "/dev/input/event9"


def code(n):
    # Distinct, valid learned Pronto codes.
    return f"0000 006D 0001 0000 {n:04X} 0020"


def profile(pid, brand, n, device_type="tv", confidence=6):
    return Profile(
        id=pid,
        brand=brand,
        name=pid,
        device_type=device_type,
        confidence=confidence,
        codes={f: (code(n + i),) for i, f in enumerate(("VOLUME_UP", "VOLUME_DOWN", "MUTE_TOGGLE", "POWER_TOGGLE"))},
    )


PROFILES = [
    profile("lg-a", "LG", 0x10, confidence=6),
    profile("lg-b", "LG", 0x20, confidence=1),
    profile("lg-dup", "LG", 0x10, confidence=0),  # same codes as lg-a
    profile("samsung", "Samsung", 0x30),
    profile("sony", "Sony", 0x40),
    profile("cablebox", "LG", 0x50, device_type="stb"),
]


class FakeInject:
    def __init__(self):
        self.events = []

    def __call__(self, source, code, value):
        self.events.append((source, code, value))


class FakeTx:
    def __init__(self):
        self.sent = []

    def send(self, profile, function, *, repeat=False):
        self.sent.append((profile.id, function, repeat))
        return True


def make(tmp_path, active=None, **overrides):
    cfg = Config(
        repeat_delay=0.05,
        repeat_interval=0.02,
        setup_hold_seconds=0.1,
        setup_timeout_seconds=0.5,
        setup_brands=["LG", "Samsung", "Sony"],
        state_path=tmp_path / "state.json",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    state = State(cfg.state_path, profile=active)
    tx = FakeTx()
    ctl = Controller(cfg, ProfileSet(PROFILES), state, tx, inject=FakeInject())
    return ctl, tx, state


def run(coro):
    return asyncio.run(coro)


def test_passthrough_of_other_keys(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, "lg-a")
        assert ctl.handle(SRC, e.KEY_LEFT, 1) is True
        assert ctl.handle(SRC, e.KEY_LEFT, 2) is True
        assert ctl.handle(SRC, e.KEY_LEFT, 0) is True
        # A release with no passed press is swallowed.
        assert ctl.handle(SRC, e.KEY_RIGHT, 0) is False
        assert tx.sent == []

    run(body())


def test_volume_blasts_and_repeats_while_held(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, "lg-a")
        assert ctl.handle(SRC, e.KEY_VOLUMEUP, 1) is False
        assert tx.sent == [("lg-a", "VOLUME_UP", False)]
        assert ctl.handle(SRC, e.KEY_VOLUMEUP, 2) is False  # kernel autorepeat ignored
        await asyncio.sleep(0.12)
        assert ctl.handle(SRC, e.KEY_VOLUMEUP, 0) is False
        repeats = [s for s in tx.sent if s[2]]
        assert len(repeats) >= 2
        assert all(s == ("lg-a", "VOLUME_UP", True) for s in repeats)
        count = len(tx.sent)
        await asyncio.sleep(0.08)
        assert len(tx.sent) == count  # stopped on release

    run(body())


def test_mute_does_not_repeat(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, "lg-a")
        ctl.handle(SRC, e.KEY_MUTE, 1)
        await asyncio.sleep(0.12)
        ctl.handle(SRC, e.KEY_MUTE, 0)
        assert tx.sent == [("lg-a", "MUTE_TOGGLE", False)]

    run(body())


def test_no_profile_sends_nothing(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, None)
        ctl.handle(SRC, e.KEY_VOLUMEUP, 1)
        ctl.handle(SRC, e.KEY_VOLUMEUP, 0)
        assert tx.sent == []

    run(body())


def test_missing_saved_profile_is_ignored(tmp_path):
    async def body():
        ctl, _, _ = make(tmp_path, "gone")
        assert ctl.active is None

    run(body())


async def enter_setup(ctl):
    # The remote's OK key is KEY_KPENTER; default combo is Back+OK.
    ctl.handle(SRC, e.KEY_BACK, 1)
    ctl.handle(SRC, e.KEY_KPENTER, 1)
    await asyncio.sleep(0.15)
    assert ctl.setup is not None
    ctl.handle(SRC, e.KEY_KPENTER, 2)
    ctl.handle(SRC, e.KEY_BACK, 0)
    ctl.handle(SRC, e.KEY_KPENTER, 0)


def test_combo_enters_setup_and_ok_saves(tmp_path):
    async def body():
        ctl, tx, state = make(tmp_path, None)
        await enter_setup(ctl)
        # Entering setup blasts the test function on the first candidate.
        assert tx.sent == [("lg-a", "VOLUME_UP", False)]
        assert [p.id for p in ctl.setup.candidates] == ["lg-a", "lg-b", "samsung", "sony"]

        tx.sent.clear()
        assert ctl.handle(SRC, e.KEY_DOWN, 1) is False  # next code set within LG
        assert ctl.handle(SRC, e.KEY_DOWN, 0) is False
        assert tx.sent == [("lg-b", "VOLUME_UP", False)]

        # Test keys use the candidate.
        ctl.handle(SRC, e.KEY_MUTE, 1)
        ctl.handle(SRC, e.KEY_MUTE, 0)
        assert tx.sent[-1] == ("lg-b", "MUTE_TOGGLE", False)

        ctl.handle(SRC, e.KEY_KPENTER, 1)
        ctl.handle(SRC, e.KEY_KPENTER, 0)
        assert ctl.setup is None
        assert ctl.active.id == "lg-b"
        assert State.load(state.path).profile == "lg-b"

        # Back to normal mode with the saved profile.
        tx.sent.clear()
        ctl.handle(SRC, e.KEY_VOLUMEDOWN, 1)
        ctl.handle(SRC, e.KEY_VOLUMEDOWN, 0)
        assert tx.sent == [("lg-b", "VOLUME_DOWN", False)]
        assert ctl.handle(SRC, e.KEY_RIGHT, 1) is True

    run(body())


def test_combo_claims_passed_keys(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, "sony")
        assert ctl.handle(SRC, e.KEY_BACK, 1) is True  # might be a plain Back press
        assert ctl.handle(SRC, e.KEY_KPENTER, 1) is False  # combo complete: claimed
        assert ctl.inject.events == [(SRC, e.KEY_BACK, 0)]  # apps see Back released
        assert ctl.handle(SRC, e.KEY_BACK, 2) is False
        assert ctl.handle(SRC, e.KEY_KPENTER, 2) is False
        await asyncio.sleep(0.15)
        assert ctl.setup is not None
        # Setup starts on the active profile.
        assert ctl.setup.current.id == "sony"
        # Releases after claiming never reach apps (no second Back release).
        assert ctl.handle(SRC, e.KEY_BACK, 0) is False
        assert ctl.handle(SRC, e.KEY_KPENTER, 0) is False
        assert tx.sent == [("sony", "VOLUME_UP", False)]

    run(body())


def test_combo_of_intercepted_keys(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, "sony", setup_combo=[e.KEY_VOLUMEDOWN, e.KEY_VOLUMEUP])
        ctl.handle(SRC, e.KEY_VOLUMEDOWN, 1)
        ctl.handle(SRC, e.KEY_VOLUMEUP, 1)
        await asyncio.sleep(0.07)  # past repeat_delay: repeat must have been cancelled
        assert tx.sent == [("sony", "VOLUME_DOWN", False)]
        await asyncio.sleep(0.08)
        assert ctl.setup is not None
        assert ctl.inject.events == []

    run(body())


def test_combo_released_early(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, "lg-a")
        ctl.handle(SRC, e.KEY_BACK, 1)
        ctl.handle(SRC, e.KEY_KPENTER, 1)
        await asyncio.sleep(0.05)
        assert ctl.handle(SRC, e.KEY_KPENTER, 0) is False
        await asyncio.sleep(0.1)
        assert ctl.setup is None
        assert ctl.handle(SRC, e.KEY_BACK, 0) is False
        # A fresh Back press passes through normally again.
        assert ctl.handle(SRC, e.KEY_BACK, 1) is True

    run(body())


def test_setup_cancel_and_timeout(tmp_path):
    async def body():
        ctl, tx, state = make(tmp_path, "samsung")
        await enter_setup(ctl)
        ctl.handle(SRC, e.KEY_RIGHT, 1)
        ctl.handle(SRC, e.KEY_BACK, 1)
        assert ctl.setup is None and ctl.active.id == "samsung"
        assert not state.path.exists()

        await enter_setup(ctl)
        await asyncio.sleep(0.6)
        assert ctl.setup is None and ctl.active.id == "samsung"

    run(body())


def test_setup_brand_jumps(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, None)
        await enter_setup(ctl)
        ids = []
        for key in (e.KEY_RIGHT, e.KEY_RIGHT, e.KEY_RIGHT, e.KEY_LEFT, e.KEY_LEFT, e.KEY_UP, e.KEY_UP):
            ctl.handle(SRC, key, 1)
            ids.append(ctl.setup.current.id)
        # Left/Right move between brands; Up/Down wrap within LG's two code sets.
        assert ids == ["samsung", "sony", "lg-a", "sony", "samsung", "samsung", "samsung"]
        ctl.handle(SRC, e.KEY_LEFT, 1)
        ctl.handle(SRC, e.KEY_UP, 1)
        assert ctl.setup.current.id == "lg-b"

    run(body())


def test_key_held_across_setup_entry_is_released(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, "lg-a")
        assert ctl.handle(SRC, e.KEY_LEFT, 1) is True
        await enter_setup(ctl)
        assert ctl.handle(SRC, e.KEY_LEFT, 0) is True

    run(body())


def test_source_gone_cancels_combo(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, "lg-a")
        ctl.handle(SRC, e.KEY_BACK, 1)
        ctl.handle(SRC, e.KEY_KPENTER, 1)
        ctl.source_gone(SRC)
        await asyncio.sleep(0.15)
        assert ctl.setup is None

    run(body())


def test_no_candidates_stays_in_normal_mode(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, None, setup_brands=["Nonexistent"])
        ctl.handle(SRC, e.KEY_BACK, 1)
        ctl.handle(SRC, e.KEY_KPENTER, 1)
        await asyncio.sleep(0.15)
        assert ctl.setup is None

    run(body())


def test_include_other_brands(tmp_path):
    ps = ProfileSet(PROFILES)
    cands = ps.candidates(["tv"], ["Sony"], True, "VOLUME_UP")
    assert [p.id for p in cands] == ["sony", "lg-a", "lg-b", "samsung"]


CANDS = [p for p in PROFILES if p.id in ("lg-a", "lg-b", "samsung", "sony")]


@pytest.mark.parametrize(
    "start,delta,expected",
    [(0, 1, 2), (1, 1, 2), (2, 1, 3), (3, 1, 0), (1, -1, 3), (0, -1, 3), (3, -1, 2), (2, -1, 0)],
)
def test_session_step_brand(start, delta, expected):
    s = SetupSession(CANDS, start)
    s.step_brand(delta)
    assert s.index == expected


@pytest.mark.parametrize("start,delta,expected", [(0, 1, 1), (1, 1, 0), (0, -1, 1), (2, 1, 2), (3, -1, 3)])
def test_session_step_code(start, delta, expected):
    s = SetupSession(CANDS, start)
    s.step_code(delta)
    assert s.index == expected


def test_session_brand_info():
    s = SetupSession(CANDS, 1)
    assert s.brands == ["LG", "Samsung", "Sony"]
    assert s.brand_position() == (0, 1, 2)
    assert [p.id for p in s.brand_members()] == ["lg-a", "lg-b"]


# -- [hold] keys ---------------------------------------------------------------

HOME_HOLD = {e.KEY_HOMEPAGE: HoldAction(0.05, (e.KEY_LEFTMETA, e.KEY_HOMEPAGE))}


def keys_sent(ctl):
    return [(c, v) for _, c, v in ctl.inject.events]


def test_hold_key_short_press_is_a_tap_on_release(tmp_path):
    async def body():
        ctl, _, _ = make(tmp_path, "lg-a", hold=HOME_HOLD)
        assert ctl.handle(SRC, e.KEY_HOMEPAGE, 1) is False
        assert ctl.handle(SRC, e.KEY_HOMEPAGE, 2) is False
        assert keys_sent(ctl) == []  # nothing until we know it's short
        assert ctl.handle(SRC, e.KEY_HOMEPAGE, 0) is False
        assert keys_sent(ctl) == [(e.KEY_HOMEPAGE, 1), (e.KEY_HOMEPAGE, 0)]
        await asyncio.sleep(0.1)  # the cancelled hold timer never fires
        assert len(keys_sent(ctl)) == 2

    run(body())


def test_hold_key_long_press_sends_the_chord_only(tmp_path):
    async def body():
        ctl, _, _ = make(tmp_path, "lg-a", hold=HOME_HOLD)
        ctl.handle(SRC, e.KEY_HOMEPAGE, 1)
        await asyncio.sleep(0.1)
        chord = [(e.KEY_LEFTMETA, 1), (e.KEY_HOMEPAGE, 1), (e.KEY_HOMEPAGE, 0), (e.KEY_LEFTMETA, 0)]
        assert keys_sent(ctl) == chord
        assert ctl.handle(SRC, e.KEY_HOMEPAGE, 2) is False
        assert ctl.handle(SRC, e.KEY_HOMEPAGE, 0) is False
        assert keys_sent(ctl) == chord  # no tap after the hold fired

    run(body())


def test_hold_key_keeps_its_setup_meaning(tmp_path):
    async def body():
        ctl, _, _ = make(tmp_path, "samsung", hold=HOME_HOLD)
        await enter_setup(ctl)
        ctl.handle(SRC, e.KEY_HOMEPAGE, 1)  # Home cancels setup, as before
        assert ctl.setup is None and ctl.active.id == "samsung"
        await asyncio.sleep(0.1)
        # (entering setup injected Back's release; no Home tap or chord follows)
        assert not {c for c, _ in keys_sent(ctl)} & {e.KEY_HOMEPAGE, e.KEY_LEFTMETA}

    run(body())


def test_hold_cut_off_by_disconnect_sends_nothing(tmp_path):
    async def body():
        ctl, _, _ = make(tmp_path, "lg-a", hold=HOME_HOLD)
        ctl.handle(SRC, e.KEY_HOMEPAGE, 1)
        ctl.source_gone(SRC)
        await asyncio.sleep(0.1)
        assert keys_sent(ctl) == []
        assert ctl.handle(SRC, e.KEY_HOMEPAGE, 0) is False  # stray release

    run(body())


def test_hold_keys_are_intercepted_and_injected(tmp_path):
    ctl, _, _ = make(tmp_path, "lg-a", hold=HOME_HOLD)
    assert e.KEY_HOMEPAGE in ctl.intercepted_codes
    assert ctl.injected_codes == {e.KEY_HOMEPAGE, e.KEY_LEFTMETA}


def test_hold_config(tmp_path):
    cfg = Config()
    cfg.apply({"hold": {"KEY_HOMEPAGE": {"seconds": 0.8, "send": ["KEY_LEFTMETA", "KEY_HOMEPAGE"]}}})
    assert cfg.hold == {e.KEY_HOMEPAGE: HoldAction(0.8, (e.KEY_LEFTMETA, e.KEY_HOMEPAGE))}
    cfg.apply({"hold": {"KEY_HOMEPAGE": {"send": ["KEY_F13"]}}})
    assert cfg.hold[e.KEY_HOMEPAGE].seconds == 0.6  # default
    for bad in ({"KEY_HOMEPAGE": {"send": []}}, {"KEY_HOMEPAGE": {"send": ["NOPE"]}},
                {"KEY_HOMEPAGE": {"seconds": 0, "send": ["KEY_F13"]}}, {"KEY_HOMEPAGE": "KEY_F13"}):
        with pytest.raises(ConfigError):
            Config().apply({"hold": bad})


def test_remap_config():
    cfg = Config()
    cfg.apply({"remap": {"KEY_MENU": "KEY_COMPOSE"}})
    assert cfg.remap == {e.KEY_MENU: e.KEY_COMPOSE}
    for bad in ({"KEY_MENU": "NOPE"}, {"NOPE": "KEY_COMPOSE"}, {"KEY_MENU": ["KEY_COMPOSE"]}):
        with pytest.raises(ConfigError):
            Config().apply({"remap": bad})


def test_drop_config():
    cfg = Config()
    cfg.apply({"drop": {"keys": ["KEY_VOLUMEUP", "KEY_POWER"]}})
    assert cfg.drop == {e.KEY_VOLUMEUP, e.KEY_POWER}
    for bad in ({"keys": ["NOPE"]}, {"keys": "KEY_POWER"}, ["KEY_POWER"]):
        with pytest.raises(ConfigError):
            Config().apply({"drop": bad})


def test_dropped_keys_are_swallowed_without_ir(tmp_path):
    async def body():
        # KEY_VOLUMEUP is still in the default [keys]: [drop] wins.
        ctl, tx, _ = make(tmp_path, "lg-a", drop={e.KEY_VOLUMEUP, e.KEY_F13})
        assert {e.KEY_VOLUMEUP, e.KEY_F13} <= ctl.intercepted_codes
        for value in (1, 2, 0):
            assert ctl.handle(SRC, e.KEY_VOLUMEUP, value) is False
            assert ctl.handle(SRC, e.KEY_F13, value) is False
        await asyncio.sleep(0.1)  # no IR repeat either
        assert tx.sent == []
        assert ctl.handle(SRC, e.KEY_VOLUMEDOWN, 1) is False  # still blasts
        ctl.handle(SRC, e.KEY_VOLUMEDOWN, 0)
        assert tx.sent == [("lg-a", "VOLUME_DOWN", False)]

    run(body())


def test_remote_config():
    cfg = Config()
    cfg.apply({
        "drop": {"keys": ["KEY_MUTE"]},
        "remote": [
            {"names": ["^AR"], "drop": ["KEY_VOLUMEUP", "KEY_POWER"]},
            {"names": ["CIR transceiver$"], "keys": {"KEY_POWER": "POWER_TOGGLE"}},
        ],
    })
    ar, cir = cfg.remotes
    assert ar.drop == {e.KEY_VOLUMEUP, e.KEY_POWER} and ar.keymap is None
    assert cir.keymap == {e.KEY_POWER: "POWER_TOGGLE"} and cir.drop is None
    assert cfg.drop_for("AR Keyboard") == {e.KEY_VOLUMEUP, e.KEY_POWER}
    assert cfg.drop_for("ITE8713 CIR transceiver") == {e.KEY_MUTE}  # global
    assert cfg.keymap_for("AR Keyboard") == cfg.keymap  # global
    assert cfg.keymap_for("ITE8713 CIR transceiver") == {e.KEY_POWER: "POWER_TOGGLE"}
    assert "CIR transceiver$" in cfg.all_device_names
    for bad in ({"drop": ["KEY_POWER"]}, {"names": []}, {"names": ["("]}, {"names": ["x"], "drop": ["NOPE"]},
                {"names": ["x"], "keys": ["KEY_POWER"]}, {"names": ["x"], "volume": 1}):
        with pytest.raises(ConfigError):
            Config().apply({"remote": [bad]})
    with pytest.raises(ConfigError):
        Config().apply({"remote": {"names": ["x"]}})  # [remote], not [[remote]]


def test_drop_and_keys_per_remote(tmp_path):
    async def body():
        ctl, tx, _ = make(tmp_path, "lg-a")
        ctl.cfg.apply({"remote": [{"names": ["^AR"], "drop": ["KEY_VOLUMEUP"], "keys": {"KEY_MUTE": "MUTE_TOGGLE"}}]})
        ar, cir = "/dev/input/event1", "/dev/input/event2"
        ctl.attach(ar, "AR Keyboard")
        ctl.attach(cir, "ITE8713 CIR transceiver")
        # The Alexa remote's volume up is dropped: no IR, not passed.
        assert ctl.handle(ar, e.KEY_VOLUMEUP, 1) is False
        ctl.handle(ar, e.KEY_VOLUMEUP, 0)
        # Its own keymap has only mute, so volume down passes through.
        assert ctl.handle(ar, e.KEY_VOLUMEDOWN, 1) is True
        ctl.handle(ar, e.KEY_VOLUMEDOWN, 0)
        assert ctl.handle(ar, e.KEY_MUTE, 1) is False
        ctl.handle(ar, e.KEY_MUTE, 0)
        # The other remote keeps the global keymap: volume up blasts.
        assert ctl.handle(cir, e.KEY_VOLUMEUP, 1) is False
        ctl.handle(cir, e.KEY_VOLUMEUP, 0)
        assert tx.sent == [("lg-a", "MUTE_TOGGLE", False), ("lg-a", "VOLUME_UP", False)]
        assert ctl.intercepted_codes_for("AR Keyboard") >= {e.KEY_VOLUMEUP, e.KEY_MUTE}
        assert e.KEY_VOLUMEDOWN not in ctl.intercepted_codes_for("AR Keyboard")
        # Forgotten when the device goes: an unknown source uses the globals.
        ctl.source_gone(ar)
        assert ctl.handle(ar, e.KEY_VOLUMEDOWN, 1) is False  # global keymap blasts it
        ctl.handle(ar, e.KEY_VOLUMEDOWN, 0)

    run(body())
