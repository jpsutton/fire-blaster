"""IR receiver: raw frames from a kernel LIRC device, decoded with irdecode.

The kernel decodes only the protocols its keymap enables (RC6 for a Media
Center receiver), so an unknown remote's buttons never become key events.
Reading the LIRC device in raw (mode2) mode gets every frame whatever the
protocol, and each open file has its own mode, so this runs alongside the
transmitter and the kernel's own decoding of the same device.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import struct
from collections.abc import Callable
from pathlib import Path

from .irdecode import FRAME_GAP_US, IrCode, Repeat, decode
from .transmit import SYS_RC, _lirc_ioctl, lirc_nodes, LIRC_GET_FEATURES

log = logging.getLogger(__name__)

LIRC_SET_REC_MODE = _lirc_ioctl(1, 0x12)
LIRC_GET_MIN_TIMEOUT = _lirc_ioctl(2, 0x08)
LIRC_SET_REC_TIMEOUT = _lirc_ioctl(1, 0x18)
LIRC_GET_REC_TIMEOUT = _lirc_ioctl(2, 0x24)
LIRC_CAN_SET_REC_TIMEOUT = 0x10000000
LIRC_MODE_MODE2 = 0x00000004
LIRC_CAN_REC_MODE2 = LIRC_MODE_MODE2 << 16  # LIRC_MODE2REC(LIRC_MODE_MODE2)

# linux/lirc.h mode2 samples: type in the top byte, microseconds below.
LIRC_MODE2_MASK = 0xFF000000
LIRC_VALUE_MASK = 0x00FFFFFF
LIRC_MODE2_SPACE = 0x00000000
LIRC_MODE2_PULSE = 0x01000000
LIRC_MODE2_TIMEOUT = 0x03000000
LIRC_MODE2_OVERFLOW = 0x04000000

RETRY_SECONDS = 5.0
# The receiver reports the space after a frame's last pulse only when it
# times out, so its timeout is the delay before a button press registers.
# Drivers default to ~125 ms; frames have no space longer than ~10 ms
# inside them. This is a device-wide setting, kept until the driver reloads.
REC_TIMEOUT_US = 25_000
# Ends a frame when the receiver goes quiet without reporting a timeout.
QUIET_SECONDS = 0.06
MAX_FRAME = 512  # durations; longer is noise or a stuck receiver


class FrameAssembler:
    """Turns mode2 samples into frames: pulse/space durations that start
    and end with a pulse, split at long spaces and receiver timeouts."""

    def __init__(self, on_frame: Callable[[tuple[int, ...]], None]):
        self.on_frame = on_frame
        self._durations: list[int] = []

    def feed(self, sample: int) -> None:
        kind, value = sample & LIRC_MODE2_MASK, sample & LIRC_VALUE_MASK
        if kind == LIRC_MODE2_PULSE:
            if len(self._durations) % 2 == 1:  # two pulses in a row: merge
                self._durations[-1] += value
            else:
                self._durations.append(value)
            if len(self._durations) > MAX_FRAME:
                self._durations.clear()
        elif kind == LIRC_MODE2_SPACE:
            if not self._durations:
                return  # idle time before a frame
            if value >= FRAME_GAP_US:
                self.flush()
            elif len(self._durations) % 2 == 0:
                self._durations[-1] += value
            else:
                self._durations.append(value)
        elif kind in (LIRC_MODE2_TIMEOUT, LIRC_MODE2_OVERFLOW):
            self.flush()

    def flush(self) -> None:
        frame, self._durations = self._durations, []
        if len(frame) % 2 == 0 and frame:
            frame.pop()
        if frame:
            self.on_frame(tuple(frame))


def _get(fd: int, request: int) -> int:
    return struct.unpack("=I", fcntl.ioctl(fd, request, struct.pack("=I", 0)))[0]


def _shorten_timeout(fd: int, path: Path) -> None:
    try:
        current = _get(fd, LIRC_GET_REC_TIMEOUT)
        if current <= REC_TIMEOUT_US:
            return
        timeout = max(REC_TIMEOUT_US, _get(fd, LIRC_GET_MIN_TIMEOUT))
        fcntl.ioctl(fd, LIRC_SET_REC_TIMEOUT, struct.pack("=I", timeout))
        log.info("IR receiver %s: timeout %d -> %d us", path, current, timeout)
    except OSError as e:
        log.debug("IR receiver %s: can't shorten its timeout: %s", path, e.strerror)


class LircReceiver:
    """Reads raw IR from the first LIRC device that can receive (or
    `device`), and calls `on_code` with each decoded frame. Frames that
    don't decode are passed as None, so setup can say it heard something."""

    def __init__(
        self,
        on_code: Callable[[IrCode | Repeat | None], None],
        device: Path | str | None = None,
        sys_rc: Path = SYS_RC,
    ):
        self.on_code = on_code
        self.device = Path(device) if device else None
        self.sys_rc = sys_rc
        self._assembler = FrameAssembler(self._frame)

    def _frame(self, frame: tuple[int, ...]) -> None:
        code = decode(frame)
        log.debug("IR RX %s (%d edges)", code if code is not None else "undecoded", len(frame))
        self.on_code(code)

    def _open(self) -> tuple[int, Path] | None:
        paths = [self.device] if self.device else [n.path for n in lirc_nodes(self.sys_rc)]
        for path in paths:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError as e:
                log.debug("IR receiver %s: %s", path, e.strerror)
                continue
            try:
                features = struct.unpack("=I", fcntl.ioctl(fd, LIRC_GET_FEATURES, struct.pack("=I", 0)))[0]
                if not features & LIRC_CAN_REC_MODE2:
                    raise OSError(0, "can't receive raw IR")
                fcntl.ioctl(fd, LIRC_SET_REC_MODE, struct.pack("=I", LIRC_MODE_MODE2))
            except OSError as e:
                os.close(fd)
                log.debug("IR receiver %s: %s", path, e.strerror)
                continue
            if features & LIRC_CAN_SET_REC_TIMEOUT:
                _shorten_timeout(fd, path)
            return fd, path
        return None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        said_missing = False
        while True:
            opened = self._open()
            if opened is None:
                if not said_missing:
                    log.info("no IR receiver found; remote setup can't hear remotes until one appears")
                    said_missing = True
                await asyncio.sleep(RETRY_SECONDS)
                continue
            said_missing = False
            fd, path = opened
            log.info("IR receiver: %s", path)
            gone = loop.create_future()
            quiet: asyncio.TimerHandle | None = None

            def readable() -> None:
                nonlocal quiet
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    return
                except OSError as e:
                    if not gone.done():
                        gone.set_result(e)
                    return
                if not data:
                    if not gone.done():
                        gone.set_result(None)
                    return
                for (sample,) in struct.iter_unpack("=I", data[: len(data) // 4 * 4]):
                    self._assembler.feed(sample)
                if quiet:
                    quiet.cancel()
                quiet = loop.call_later(QUIET_SECONDS, self._assembler.flush)

            loop.add_reader(fd, readable)
            try:
                error = await gone
                log.info("IR receiver %s gone: %s", path, getattr(error, "strerror", None) or "closed")
            finally:
                loop.remove_reader(fd)
                if quiet:
                    quiet.cancel()
                os.close(fd)
                self._assembler.flush()
            await asyncio.sleep(RETRY_SECONDS)
