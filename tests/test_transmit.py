import logging
import os
import struct
import types

import pytest

from fireblaster import transmit
from fireblaster.pronto import IrSignal
from fireblaster.transmit import (
    LIRC_CAN_SEND_PULSE,
    LIRC_CAN_SET_SEND_CARRIER,
    LIRC_GET_FEATURES,
    LIRC_MODE_PULSE,
    LIRC_SET_SEND_CARRIER,
    LIRC_SET_SEND_MODE,
    LircEmitter,
    Transmission,
    lirc_nodes,
    lirc_pulses,
)

SIGNAL = IrSignal(38000, (3400, 1600, 500, 400, 500, 40000))


def test_ioctl_numbers_match_linux_lirc_h():
    # The values the old service.irblaster add-on got from a C program.
    assert LIRC_GET_FEATURES == 2147772672
    assert LIRC_SET_SEND_MODE == 1074030865
    assert LIRC_SET_SEND_CARRIER == 1074030867


def test_pulses_end_on_a_pulse_and_repeat_count_times():
    pulses, trailing = lirc_pulses(Transmission("x", SIGNAL))
    assert pulses == [3400, 1600, 500, 400, 500] and trailing == 40000
    # blast_count 2: the frames are joined by the first one's trailing space.
    pulses, trailing = lirc_pulses(Transmission("x", SIGNAL, count=2))
    assert pulses == [3400, 1600, 500, 400, 500, 40000, 3400, 1600, 500, 400, 500] and trailing == 40000
    # A held-key repeat uses the repeat sequence when there is one.
    held = IrSignal(38000, (9000, 4500, 560, 40000), (9000, 2250, 560, 96000))
    assert lirc_pulses(Transmission("x", held, repeat=True)) == ([9000, 2250, 560], 96000)


def make_rc(sys_rc, n, driver, name, usb, lirc=True):
    """A /sys/class/rc/rcN entry, as a symlink into a fake device tree."""
    bus = "pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0" if usb else "pnp0/00:00"
    real = sys_rc.parent / "devices" / bus / "rc" / f"rc{n}"
    real.mkdir(parents=True)
    (real / "uevent").write_text(f"MAJOR=0\nDEV_NAME={name}\nDRV_NAME={driver}\nNAME=rc-rc6-mce\n")
    if lirc:
        (real / f"lirc{n}").mkdir()
    sys_rc.mkdir(exist_ok=True)
    (sys_rc / f"rc{n}").symlink_to(real)


@pytest.fixture
def sys_rc(tmp_path):
    root = tmp_path / "class" / "rc"
    make_rc(root, 0, "ite-cir", "ITE8713 CIR transceiver", usb=False)
    make_rc(root, 1, "mceusb", "Media Center Ed. eHome Infrared Remote Transceiver (1784:0006)", usb=True)
    make_rc(root, 2, "rc-loopback", "no lirc node", usb=False, lirc=False)
    return root


def test_lirc_nodes_put_usb_transmitters_first(sys_rc):
    nodes = lirc_nodes(sys_rc)
    assert [(str(n.path), n.driver, n.usb) for n in nodes] == [
        ("/dev/lirc1", "mceusb", True),
        ("/dev/lirc0", "ite-cir", False),
    ]


class FakeLirc:
    """Stands in for os/fcntl/time inside fireblaster.transmit."""

    def __init__(self, devices):
        self.devices = devices  # path -> features (None: open fails)
        self.fds = {}
        self.calls = []
        self.writes = []
        self.slept = []
        self.fail_write = False
        self.os = types.SimpleNamespace(
            O_RDWR=os.O_RDWR, O_CLOEXEC=os.O_CLOEXEC, open=self.open, write=self.write, close=self.close
        )
        self.fcntl = types.SimpleNamespace(ioctl=self.ioctl)
        self.time = types.SimpleNamespace(sleep=self.slept.append)

    def open(self, path, flags):
        if self.devices.get(str(path)) is None:
            raise PermissionError(13, "Permission denied")
        fd = 100 + len(self.fds)
        self.fds[fd] = str(path)
        return fd

    def close(self, fd):
        self.calls.append(("close", self.fds.pop(fd)))

    def ioctl(self, fd, request, arg):
        if request == LIRC_GET_FEATURES:
            return struct.pack("=I", self.devices[self.fds[fd]])
        self.calls.append((self.fds[fd], request, struct.unpack("=I", arg)[0]))
        return arg

    def write(self, fd, data):
        if self.fail_write:
            raise OSError(19, "No such device")
        self.writes.append((self.fds[fd], list(struct.unpack(f"={len(data) // 4}I", data))))
        return len(data)


@pytest.fixture
def lirc(monkeypatch):
    def install(devices):
        fake = FakeLirc(devices)
        monkeypatch.setattr(transmit, "os", fake.os)
        monkeypatch.setattr(transmit, "fcntl", fake.fcntl)
        monkeypatch.setattr(transmit, "time", fake.time)
        return fake

    return install


SENDS = LIRC_CAN_SEND_PULSE | LIRC_CAN_SET_SEND_CARRIER


def test_emitter_sets_mode_and_carrier_then_writes(sys_rc, lirc):
    fake = lirc({"/dev/lirc1": SENDS, "/dev/lirc0": SENDS})
    emitter = LircEmitter(sys_rc=sys_rc)
    emitter.send(Transmission("VOLUME_UP", SIGNAL))
    emitter.send(Transmission("VOLUME_UP", SIGNAL, repeat=True))
    # The USB transceiver is picked. Mode at open, then mode and carrier
    # before every write, as ir-ctl and the old service.irblaster did.
    per_send = [
        ("/dev/lirc1", LIRC_SET_SEND_MODE, LIRC_MODE_PULSE),
        ("/dev/lirc1", LIRC_SET_SEND_CARRIER, 38000),
    ]
    assert fake.calls == [("/dev/lirc1", LIRC_SET_SEND_MODE, LIRC_MODE_PULSE)] + per_send * 2
    assert fake.writes == [("/dev/lirc1", [3400, 1600, 500, 400, 500])] * 2
    assert fake.slept == [0.04, 0.04]


def test_emitter_by_driver_and_by_path(sys_rc, lirc):
    fake = lirc({"/dev/lirc1": SENDS, "/dev/lirc0": SENDS})
    LircEmitter(driver="ite-cir", sys_rc=sys_rc).send(Transmission("x", SIGNAL))
    LircEmitter(device="/dev/lirc0").send(Transmission("x", SIGNAL))
    assert [w[0] for w in fake.writes] == ["/dev/lirc0", "/dev/lirc0"]


def test_emitter_skips_devices_that_cannot_send(sys_rc, lirc):
    # The USB device can't be opened and the other one is receive-only.
    fake = lirc({"/dev/lirc1": None, "/dev/lirc0": 0})
    LircEmitter(sys_rc=sys_rc).send(Transmission("x", SIGNAL))
    assert fake.writes == []
    assert ("close", "/dev/lirc0") in fake.calls


def test_emitter_without_carrier_control_does_not_set_it(sys_rc, lirc):
    fake = lirc({"/dev/lirc1": LIRC_CAN_SEND_PULSE})
    LircEmitter(driver="mceusb", sys_rc=sys_rc).send(Transmission("x", SIGNAL))
    assert [c[1] for c in fake.calls] == [LIRC_SET_SEND_MODE, LIRC_SET_SEND_MODE]
    assert len(fake.writes) == 1


def test_emitter_reopens_after_a_failed_write(sys_rc, lirc):
    fake = lirc({"/dev/lirc1": SENDS})
    emitter = LircEmitter(sys_rc=sys_rc)
    fake.fail_write = True
    with pytest.raises(OSError):
        emitter.send(Transmission("x", SIGNAL))  # the Transmitter logs it
    assert ("close", "/dev/lirc1") in fake.calls
    fake.fail_write = False
    emitter.send(Transmission("x", SIGNAL))
    assert fake.writes == [("/dev/lirc1", [3400, 1600, 500, 400, 500])]
    # Reopened: mode at open, then mode and carrier for each write.
    assert [c[1] for c in fake.calls if c[0] == "/dev/lirc1"].count(LIRC_SET_SEND_CARRIER) == 2


def test_no_transmitter_warns_once(tmp_path, lirc, caplog):
    lirc({})
    emitter = LircEmitter(sys_rc=tmp_path / "none")
    with caplog.at_level(logging.WARNING):
        emitter.send(Transmission("x", SIGNAL))
        emitter.send(Transmission("x", SIGNAL))
    assert [r.message for r in caplog.records] == ["IR not sent: no IR transmitter found"]
