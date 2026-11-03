from fireblaster import irdecode
from fireblaster.irdecode import REPEAT, IrCode, decode, decode_all, split_frames
from fireblaster.pronto import encode
from fireblaster.profiles import Profile, ProfileSet
from fireblaster.receive import LIRC_MODE2_PULSE, LIRC_MODE2_SPACE, LIRC_MODE2_TIMEOUT, FrameAssembler
from fireblaster.remotedb import RemoteIndex, RemoteMatch


def nec(value, bits=32):
    """A NEC-style frame (header, pulse-distance bits, stop pulse), first
    bit most significant, with its trailing gap."""
    frame = [9000, 4500]
    for i in reversed(range(bits)):
        frame += [560, 1690 if value >> i & 1 else 560]
    return frame + [560, 40000]


def sony(value, bits=12):
    frame = [2400, 600]
    for i in reversed(range(bits)):
        frame += [1200 if value >> i & 1 else 600, 600]
    frame[-1] = 25000
    return frame


def test_nec_frame():
    assert decode(nec(0x20DF02FD)) == IrCode("pd", 32, 0x20DF02FD)
    assert str(IrCode("pd", 32, 0x20DF02FD)) == "pd32:20DF02FD"


def test_jittery_receiver_timings():
    frame = [d + (37 if i % 3 else -45) for i, d in enumerate(nec(0x20DF10EF))]
    assert decode(frame) == IrCode("pd", 32, 0x20DF10EF)


def test_nec_repeat_and_noise():
    assert decode([9000, 2250, 560]) is REPEAT
    assert decode([560, 560, 560]) is None
    assert decode([300, 9000, 1200, 50, 4000, 700, 560]) is None


def test_sony_pulse_width():
    assert decode(sony(0x490)) == IrCode("pw", 12, 0x490)


def test_split_frames_at_gaps():
    durations = nec(0x20DF40BF)[:-1] + [41000, 9000, 2250, 560, 96000] + nec(0x20DF40BF)
    codes = decode_all(durations)
    assert codes == [IrCode("pd", 32, 0x20DF40BF), REPEAT, IrCode("pd", 32, 0x20DF40BF)]
    assert split_frames([100, 200]) == [(100,)]


def test_frame_assembler():
    frames = []
    asm = FrameAssembler(frames.append)
    asm.feed(LIRC_MODE2_SPACE | 500000)  # idle before the frame
    for i, d in enumerate(nec(0x20DF02FD)[:-1]):
        asm.feed((LIRC_MODE2_SPACE if i % 2 else LIRC_MODE2_PULSE) | d)
    asm.feed(LIRC_MODE2_TIMEOUT | 125000)
    (frame,) = frames
    assert decode(frame) == IrCode("pd", 32, 0x20DF02FD)


def lg_profile(pid, brand, codes):
    return Profile(id=pid, brand=brand, name=pid, device_type="tv",
                   codes={f: (encode(38000, nec(v)),) for f, v in codes.items()})


def test_remote_match_narrows_by_press():
    profiles = ProfileSet([
        lg_profile("lg", "LG", {"DPAD_UP": 0x20DF02FD, "DPAD_OK": 0x20DF22DD, "SMART_TV_EXIT": 0x20DFDA25}),
        lg_profile("clone", "Haier", {"DPAD_UP": 0x20DF02FD, "DPAD_OK": 0x40BF22DD}),
        lg_profile("other", "Sony", {"DPAD_UP": 0x10EF02FD}),
    ])
    index = RemoteIndex(profiles)
    match = RemoteMatch(index)
    assert match.add(IrCode("pd", 32, 0x20DF02FD)).function == "DPAD_UP"
    assert match.candidates == {"lg", "clone"}
    assert match.add(IrCode("pd", 32, 0x20DF22DD)).function == "DPAD_OK"
    assert match.candidates == {"lg"} and match.brands() == [("LG", 1)]
    assert match.add(IrCode("pd", 32, 0x12345678)).function is None
    assert match.candidates == {"lg"} and match.unknown == 1
    summary = match.summary()
    assert summary["presses"] == 3 and summary["brands"] == [{"brand": "LG", "codesets": 1}]
    assert summary["best"] == {"id": "lg", "brand": "LG", "name": "lg"}


def test_far_off_code_sets_do_not_name_buttons():
    profiles = ProfileSet([
        lg_profile("lg", "LG", {"DPAD_UP": 0x20DF02FD, "DPAD_OK": 0x20DF22DD, "MUTE_TOGGLE": 0x20DF906F}),
        lg_profile("other", "Acme", {"DPAD_UP": 0x20DF02FD, "DIGIT_3": 0x20DFF00F}),
    ])
    match = RemoteMatch(RemoteIndex(profiles))
    for v in (0x20DF02FD, 0x20DF22DD, 0x20DF906F):
        match.add(IrCode("pd", 32, v))
    # Only "other" (1 of 3 codes) has it: not close enough to name it.
    assert match.add(IrCode("pd", 32, 0x20DFF00F)).function is None
