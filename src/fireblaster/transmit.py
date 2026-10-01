"""IR transmission queue and emitters.

Only a logging emitter exists for now. A LIRC emitter (raw pulse writes to
/dev/lircN, as the old service.irblaster did) slots in behind the same
`send(Transmission)` interface once there is hardware to test against.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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
