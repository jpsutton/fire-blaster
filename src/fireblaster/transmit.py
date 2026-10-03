"""IR transmission queue and emitters.

`LircEmitter` blasts through a kernel LIRC device (/dev/lircN), as the old
service.irblaster did: raw pulse/space durations written to the device after
setting the carrier. Any transmitter with a kernel rc driver works; the
Media Center USB transceiver (mceusb) is the one tested. `LogEmitter` only
logs what would be sent.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .pronto import IrSignal, format_pulses
from .profiles import Profile

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Transmission:
    label: str
    signal: IrSignal
    repeat: bool = False
    count: int = 1


class Emitter(Protocol):
    def send(self, tx: Transmission) -> None:
        """Blocking send; called from a worker thread."""


class LogEmitter:
    """Stand-in emitter: logs what would be blasted."""

    def send(self, tx: Transmission) -> None:
        frame = tx.signal.frame(tx.repeat)
        log.info(
            "IR TX %s%s: carrier=%dHz, %d edges, %.1fms x%d",
            tx.label,
            " (repeat)" if tx.repeat else "",
            tx.signal.carrier,
            len(frame),
            sum(frame) / 1000,
            tx.count,
        )
        log.debug("IR TX data: %s", format_pulses(frame))


# linux/lirc.h: _IOR('i', 0x00, __u32) and _IOW('i', 0x11/0x13, __u32).
def _lirc_ioctl(direction: int, nr: int) -> int:
    return (direction << 30) | (4 << 16) | (ord("i") << 8) | nr


LIRC_GET_FEATURES = _lirc_ioctl(2, 0x00)
LIRC_SET_SEND_MODE = _lirc_ioctl(1, 0x11)
LIRC_SET_SEND_CARRIER = _lirc_ioctl(1, 0x13)
LIRC_MODE_PULSE = 0x00000002
LIRC_CAN_SEND_PULSE = LIRC_MODE_PULSE  # LIRC_MODE2SEND(LIRC_MODE_PULSE)
LIRC_CAN_SET_SEND_CARRIER = 0x00000100

SYS_RC = Path("/sys/class/rc")
# The trailing space of a frame is waited out after the write (the device
# takes only pulse-ended data), so the next frame keeps its distance. Capped:
# some learned codes end with a very long gap.
MAX_TRAILING_GAP_US = 200_000


class EmitterUnavailable(Exception):
    """No usable IR transmitter right now (none plugged in, or no access)."""


@dataclass(frozen=True)
class LircNode:
    path: Path  # /dev/lircN
    driver: str  # kernel rc driver, e.g. "mceusb" or "ite-cir"
    name: str  # rc device name, e.g. "Media Center Ed. eHome Infrared Remote Transceiver (1784:0006)"
    usb: bool


def lirc_nodes(sys_rc: Path = SYS_RC, dev: Path = Path("/dev")) -> list[LircNode]:
    """Every LIRC device the kernel's rc devices have, USB ones first: a
    plugged-in blaster wins over a built-in CIR port, which often has a
    transmitter in the chip but no emitter wired to it."""
    nodes = []
    for rc in sorted(sys_rc.glob("rc*"), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        try:
            fields = dict(line.split("=", 1) for line in (rc / "uevent").read_text().splitlines() if "=" in line)
            usb = "/usb" in str(rc.resolve())
        except OSError:
            continue
        for lirc in sorted(rc.glob("lirc*")):
            nodes.append(LircNode(dev / lirc.name, fields.get("DRV_NAME", ""), fields.get("DEV_NAME", ""), usb))
    return sorted(nodes, key=lambda n: not n.usb)


def lirc_pulses(tx: Transmission) -> tuple[list[int], int]:
    """The durations to write for a transmission, and the trailing space to
    wait out afterwards. Frames are pulse/space pairs; a LIRC device takes an
    odd count that ends on a pulse, so the last space is split off."""
    durations = [d for d in tx.signal.frame(tx.repeat) if d > 0] * max(1, tx.count)
    trailing = durations.pop() if len(durations) % 2 == 0 and durations else 0
    return durations, trailing


class LircEmitter:
    """Blasts through a kernel LIRC device.

    `device` pins a path (/dev/lirc1); otherwise the device is found by
    `driver` ("mceusb"), or else it is the first one that can send, USB
    first. The device is opened on the first send and found again after an
    error, so a blaster plugged in or replugged later is picked up. With no
    transmitter, sends are logged and dropped.
    """

    def __init__(self, device: Path | str | None = None, driver: str | None = None, sys_rc: Path = SYS_RC):
        self.device = Path(device) if device else None
        self.driver = driver
        self.sys_rc = sys_rc
        self._fd: int | None = None
        self._path: Path | None = None
        self._can_set_carrier = False
        self._carrier: int | None = None
        self._unavailable: str | None = None  # last reason logged, so it's said once

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = None
        self._path = None
        self._carrier = None

    def _candidates(self) -> list[Path]:
        if self.device:
            return [self.device]
        nodes = lirc_nodes(self.sys_rc)
        if self.driver:
            nodes = [n for n in nodes if n.driver.lower() == self.driver.lower()]
        return [n.path for n in nodes]

    def _open(self) -> None:
        candidates = self._candidates()
        if not candidates:
            raise EmitterUnavailable(
                f"no IR transmitter{f' with driver {self.driver}' if self.driver else ''} found"
            )
        reasons = []
        for path in candidates:
            try:
                fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
            except OSError as e:
                reasons.append(f"{path}: {e.strerror}")
                continue
            try:
                features = struct.unpack("=I", fcntl.ioctl(fd, LIRC_GET_FEATURES, struct.pack("=I", 0)))[0]
                if not features & LIRC_CAN_SEND_PULSE:
                    raise OSError(0, "can't send IR")
                fcntl.ioctl(fd, LIRC_SET_SEND_MODE, struct.pack("=I", LIRC_MODE_PULSE))
            except OSError as e:
                os.close(fd)
                reasons.append(f"{path}: {e.strerror}")
                continue
            self._fd, self._path = fd, path
            self._can_set_carrier = bool(features & LIRC_CAN_SET_SEND_CARRIER)
            self._carrier = None
            log.info("IR transmitter: %s", path)
            return
        raise EmitterUnavailable("; ".join(reasons))

    def send(self, tx: Transmission) -> None:
        if self._fd is None:
            try:
                self._open()
            except EmitterUnavailable as e:
                if str(e) != self._unavailable:
                    log.warning("IR not sent: %s", e)
                    self._unavailable = str(e)
                log.debug("dropped IR %s", tx.label)
                return
            self._unavailable = None
        pulses, trailing = lirc_pulses(tx)
        if not pulses:
            return
        carrier = tx.signal.carrier
        try:
            if carrier and self._can_set_carrier and carrier != self._carrier:
                fcntl.ioctl(self._fd, LIRC_SET_SEND_CARRIER, struct.pack("=I", carrier))
                self._carrier = carrier
            os.write(self._fd, struct.pack(f"={len(pulses)}I", *pulses))
        except OSError:
            # Unplugged, or the device went away: find it again next time.
            self.close()
            raise
        log.debug("IR TX %s via %s: %s", tx.label, self._path, format_pulses(pulses))
        time.sleep(min(trailing, MAX_TRAILING_GAP_US) / 1_000_000)


class Transmitter:
    """Serializes transmissions onto one emitter.

    Held-key repeats are dropped rather than queued when the emitter falls
    behind, so releasing a volume key stops the TV promptly.
    """

    def __init__(self, emitter: Emitter, queue_size: int = 4):
        self.emitter = emitter
        self.queue: asyncio.Queue[Transmission] = asyncio.Queue(queue_size)
        self._variant: dict[tuple[str, str], int] = {}

    def send(self, profile: Profile, function: str, *, repeat: bool = False) -> bool:
        variants = profile.variants(function)
        if not variants:
            log.warning("%s has no %s code", profile.label, function)
            return False

        # Toggle-bit protocols (RC5/RC6) ship two variants; alternate them per
        # press, and keep the same one for repeats of a held key.
        key = (profile.id, function)
        if repeat:
            index = self._variant.get(key, 0)
        else:
            index = (self._variant.get(key, -1) + 1) % len(variants)
            self._variant[key] = index

        tx = Transmission(f"{function} via {profile.label}", variants[index], repeat, profile.blast_count)
        try:
            self.queue.put_nowait(tx)
        except asyncio.QueueFull:
            log.log(logging.DEBUG if repeat else logging.WARNING, "IR queue full; dropped %s", tx.label)
            return False
        return True

    async def run(self) -> None:
        while True:
            tx = await self.queue.get()
            try:
                await asyncio.to_thread(self.emitter.send, tx)
            except Exception:
                log.exception("IR send failed for %s", tx.label)
