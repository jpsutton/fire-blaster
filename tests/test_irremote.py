import asyncio

from evdev import ecodes as e

from fireblaster.irdecode import REPEAT, IrCode
from fireblaster.irremote import IrKeys, LearnedRemote, RemoteWizard
from fireblaster.profiles import ProfileSet
from fireblaster.remotedb import RemoteIndex
from fireblaster.state import State

from test_irdecode import lg_profile

LG = {"DPAD_UP": 0x20DF02FD, "DPAD_DOWN": 0x20DF827D, "DPAD_LEFT": 0x20DFE01F, "DPAD_RIGHT": 0x20DF609F,
      "DPAD_OK": 0x20DF22DD, "MENU_MAIN": 0x20DFC23D, "VOLUME_UP": 0x20DF40BF}
BACK = IrCode("pd", 32, 0x20DF14EB)


def nec(v):
    return IrCode("pd", 32, v)


def wizard():
    return RemoteWizard(RemoteIndex(ProfileSet([lg_profile("lg", "LG", LG)])))


def test_wizard_asks_for_missing_buttons_then_saves():
    w = wizard()
    assert w.step == "listen"
    for f in ("DPAD_OK", "DPAD_UP", "DPAD_LEFT"):  # OK before the match is trusted: just a press
        assert w.add(nec(LG[f])) == "heard"
    assert w.step == "confirm"
    assert w.add(nec(LG["DPAD_OK"])) == "taught" and w.teaching == "BACK"
    assert w.add(nec(LG["DPAD_UP"])) == "ignored"  # arrows are never reassigned
    assert w.add(BACK) == "taught" and w.teaching == "HOME"
    assert w.add(nec(LG["DPAD_OK"])) == "save"  # OK skips Home
    remote = w.remote()
    assert remote.name == "LG remote" and remote.profile == "lg"
    assert remote.key_for(BACK) == e.KEY_BACK
    assert remote.key_for(nec(LG["DPAD_OK"])) == e.KEY_ENTER
    assert remote.key_for(nec(LG["VOLUME_UP"])) is None  # the TV's, not the PC's


def test_learned_remote_round_trips_through_state(tmp_path):
    remote = LearnedRemote("LG remote", "lg", {nec(LG["DPAD_UP"]): "DPAD_UP", BACK: "BACK"})
    State(tmp_path / "s.json", "lg", [remote.to_json()]).save()
    (data,) = State.load(tmp_path / "s.json").remotes
    assert LearnedRemote.from_json(data) == remote
    assert IrCode.parse("pd32:20DF14EB") == BACK


def test_ir_keys_press_hold_release():
    async def body():
        events = []
        keys = IrKeys(lambda k, v: events.append((k, v)))
        keys.remotes = [LearnedRemote("LG", None, {nec(LG["DPAD_UP"]): "DPAD_UP", nec(LG["DPAD_OK"]): "DPAD_OK"})]
        keys.frame(nec(LG["DPAD_UP"]))
        for _ in range(3):
            await asyncio.sleep(0.11)
            keys.frame(REPEAT)  # held
        assert events == [(e.KEY_UP, 1)]
        keys.frame(nec(LG["DPAD_OK"]))  # another button: Up comes up first
        keys.frame(nec(0x12345678))  # unknown: ignored
        await asyncio.sleep(0.3)
        assert events == [(e.KEY_UP, 1), (e.KEY_UP, 0), (e.KEY_ENTER, 1), (e.KEY_ENTER, 0)]
        keys.frame(REPEAT)  # a stray repeat presses nothing
        assert len(events) == 4

    asyncio.run(body())
