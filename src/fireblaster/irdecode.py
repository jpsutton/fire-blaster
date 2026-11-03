"""Decode IR frames into protocol-neutral codes, to recognize remotes.

The same decoder reads the code sets in the profiles and the frames the IR
receiver hears, so a received frame can be matched against the database
without knowing each protocol's scancode layout. A frame is
alternating pulse/space durations in microseconds, starting with a pulse.

Two bit encodings cover most TV remotes:

pulse distance  pulses all the same length; a short space is 0, a long one
                1. NEC and its variants (LG, most TVs), Samsung, Sharp.
pulse width     spaces all the same length; a short pulse is 0, a long one
                1. Sony SIRC.

An optional header (a pulse and space much longer than the bits) and a stop
pulse are skipped. Bi-phase protocols (RC5, RC6: Philips, Media Center
remotes) are not decoded yet, so those frames return None.
"""

from __future__ import annotations

from dataclasses import dataclass

# A frame needs this many bits to be told apart from noise.
MIN_BITS = 8
# Durations within this ratio of each other are the same symbol.
SAME = 1.6
# Two symbol lengths must differ by at least this ratio to carry bits.
DISTINCT = 1.5
# A space at least this long ends a frame (the gap between repeats).
FRAME_GAP_US = 10_000


@dataclass(frozen=True)
class IrCode:
    """A decoded frame: the encoding, its bit count, and the bits (first
    received bit is the most significant)."""

    encoding: str  # "pd" (pulse distance) or "pw" (pulse width)
    bits: int
    value: int

    def __str__(self) -> str:
        return f"{self.encoding}{self.bits}:{self.value:0{(self.bits + 3) // 4}X}"

    @classmethod
    def parse(cls, text: str) -> IrCode:
        """The inverse of str(): "pd32:20DF02FD"."""
        head, _, value = text.partition(":")
        return cls(head[:2], int(head[2:]), int(value, 16))


@dataclass(frozen=True)
class Repeat:
    """A NEC-style repeat frame: a header and one pulse, sent while held."""


REPEAT = Repeat()


def split_frames(durations: list[int] | tuple[int, ...]) -> list[tuple[int, ...]]:
    """Split pulse/space durations at long gaps into frames. Each frame
    starts with a pulse and ends with a pulse (its trailing gap dropped)."""
    frames: list[tuple[int, ...]] = []
    current: list[int] = []
    for i, d in enumerate(durations):
        is_space = i % 2 == 1
        if is_space and d >= FRAME_GAP_US:
            if current:
                frames.append(tuple(current))
            current = []
            continue
        if not current and is_space:
            continue
        current.append(d)
    if len(current) % 2 == 0 and current:
        current.pop()  # a trailing space too short to be a gap
    if current:
        frames.append(tuple(current))
    return frames


def decode(frame: tuple[int, ...] | list[int]) -> IrCode | Repeat | None:
    """Decode one frame (from split_frames, or a Pronto sequence)."""
    frame = list(frame)
    if len(frame) % 2 == 0:
        frame.pop()  # trailing gap
    if len(frame) < 3:
        return None
    unit = sorted(frame)[len(frame) // 4]  # a short symbol, robust to outliers

    # Header: a pulse well above the bit symbols, then its space.
    body = frame
    if frame[0] > 2.5 * unit and len(frame) >= 3:
        body = frame[2:]
        if len(body) == 1 and frame[1] < frame[0] * 0.75:
            return REPEAT  # e.g. NEC: 9 ms, 2.25 ms, stop pulse
    if len(body) < 2 * MIN_BITS + 1:
        return None

    pulses = body[0::2]
    spaces = body[1::2]  # one fewer: the last pulse is the stop bit
    if _one_length(pulses):
        return _bits("pd", spaces)
    if _one_length(spaces):
        # Pulse width: the last pulse is a data bit too, there is no stop bit.
        return _bits("pw", pulses)
    return None


def decode_all(durations: list[int] | tuple[int, ...]) -> list[IrCode | Repeat]:
    """Every decodable frame in a pulse/space sequence."""
    return [c for f in split_frames(durations) if (c := decode(f)) is not None]


def _one_length(values: list[int]) -> bool:
    return max(values) <= SAME * min(values)


def _bits(encoding: str, symbols: list[int]) -> IrCode | None:
    short, long = min(symbols), max(symbols)
    if long < DISTINCT * short:
        return None
    threshold = (short + long) / 2
    # Two lengths only: anything far from both is not this encoding.
    if any(not (s <= SAME * short or s * SAME >= long) for s in symbols):
        return None
    value = 0
    for s in symbols:
        value = (value << 1) | (s > threshold)
    if len(symbols) < MIN_BITS:
        return None
    return IrCode(encoding, len(symbols), value)
