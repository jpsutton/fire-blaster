import pytest

from fireblaster import pronto
from fireblaster.pronto import ProntoError

# NEC "Channel Down" from the Amazon database (Avera, code group 3).
NEC = (
    "0000 006D 0022 0002 0156 00AB 0015 0015 0015 0015 0015 0015 0015 0015 0015 0015 0015 0015 "
    "0015 0015 0015 0015 0015 003F 0015 003F 0015 003F 0015 003F 0015 003F 0015 003F 0015 003F "
    "0015 0015 0015 0015 0015 0015 0015 003F 0015 003F 0015 003F 0015 0015 0015 003F 0015 0015 "
    "0015 003F 0015 003F 0015 0015 0015 0015 0015 0015 0015 003F 0015 0015 0015 003F 0015 0629 "
    "0156 0055 0015 0E51"
)


def test_decode_nec():
    sig = pronto.decode(NEC)
    assert sig.carrier == 38029
    assert len(sig.once) == 2 * 0x22
    assert len(sig.repeat) == 2 * 2
    # 9ms leader / 4.5ms space, then 560us bits
    assert sig.once[:3] == (8993, 4497, 552)
    # NEC repeat code: 9ms / 2.25ms / 560us
    assert sig.repeat[:3] == (8993, 2235, 552)


def test_frame_selection():
    sig = pronto.decode(NEC)
    assert sig.frame() == sig.once
    assert sig.frame(repeat=True) == sig.repeat
    only_repeat = pronto.IrSignal(38000, (), (100, 200))
    assert only_repeat.frame() == (100, 200)
    only_once = pronto.IrSignal(38000, (100, 200))
    assert only_once.frame(repeat=True) == (100, 200)


def test_round_trip():
    sig = pronto.decode(NEC)
    again = pronto.decode(pronto.encode(sig.carrier, sig.once, sig.repeat))
    assert again == sig


def test_unmodulated():
    sig = pronto.decode("0100 006D 0001 0000 0010 0020")
    assert sig.carrier == 0
    assert sig.once == (421, 841)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0000 006D",
        "zzzz 006D 0001 0000 0010 0020",
        "5000 0073 0000 0001 0001 0001",  # predefined RC5, not supported
        "0000 0000 0001 0000 0010 0020",
        "0000 006D 0000 0000",
        "0000 006D 0002 0000 0010 0020",  # truncated
    ],
)
def test_decode_rejects(text):
    with pytest.raises(ProntoError):
        pronto.decode(text)


def test_parse_pulses():
    assert pronto.parse_pulses("carrier 38000\n+9000 -4500\n+560 # tail\n") == [9000, 4500, 560]
    with pytest.raises(ProntoError):
        pronto.parse_pulses("+9000 +4500")
    with pytest.raises(ProntoError):
        pronto.parse_pulses("9000 -4500")


def test_format_pulses():
    assert pronto.format_pulses([9000, 4500, 560]) == "+9000 -4500 +560"


def test_encode_rejects_odd():
    with pytest.raises(ProntoError):
        pronto.encode(38000, [9000, 4500, 560])


def test_cli_decode_encode(capsys):
    assert pronto.main(["decode", NEC]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "carrier 38029"
    assert out[1].startswith("+8993 -4497 +552")
    assert out[2].startswith("# gap ")

    assert pronto.main(["encode", "--carrier", "38029", "+8993 -4497 +552"]) == 0
    code = capsys.readouterr().out.strip()
    assert code.startswith("0000 006D 0002 0000 0156 00AB 0015")
