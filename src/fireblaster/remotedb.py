"""Recognize a remote from the codes it sends, using the profile database.

Every code in every profile is decoded once (irdecode) into an index from
code to (profile, function). A remote's button press is then looked up
there: a random LG TV remote's Up button sends the code the LG profiles
list as DPAD_UP. Several profiles often share a code, so a RemoteMatch
narrows the candidates with each press: the remote's code set is the one
that has every code heard so far.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .irdecode import IrCode, decode_all
from .profiles import Profile, ProfileSet

log = logging.getLogger(__name__)

# A profile names a heard button only if it scores at least this share of
# the top score: a code set that merely shares the remote's address (common
# with NEC) mustn't name the buttons the real one lacks.
NAMING_SHARE = 0.75


class RemoteIndex:
    def __init__(self, profiles: ProfileSet):
        start = time.monotonic()
        self.profiles = profiles
        self._codes: dict[IrCode, list[tuple[str, str]]] = defaultdict(list)
        for profile in profiles.by_id.values():
            for function in profile.codes:
                for code in _codes_of(profile, function):
                    entry = (profile.id, function)
                    if entry not in self._codes[code]:
                        self._codes[code].append(entry)
        log.info("remote index: %d codes from %d profiles in %.1fs", len(self._codes), len(profiles), time.monotonic() - start)

    def codes_for(self, profile_id: str) -> dict[IrCode, list[str]]:
        """Code -> functions for one profile; a code set may list one code
        under several names (LG: DPAD_OK and NUM_ENTER)."""
        profile = self.profiles.by_id[profile_id]
        table: dict[IrCode, list[str]] = defaultdict(list)
        for function in profile.codes:
            for code in _codes_of(profile, function):
                table[code].append(function)
        return dict(table)

    def lookup(self, code: IrCode) -> list[tuple[str, str]]:
        """(profile id, function) for every profile that has this code."""
        return self._codes.get(code, [])


def _codes_of(profile: Profile, function: str) -> set[IrCode]:
    codes = set()
    for signal in profile.variants(function):
        # The first decodable frame; a once sequence may hold several.
        for frame in (signal.once, signal.repeat):
            found = [c for c in decode_all(frame) if isinstance(c, IrCode)]
            if found:
                codes.add(found[0])
                break
    return codes


@dataclass
class Heard:
    code: IrCode
    function: str | None  # what the remaining candidates call it, if known


@dataclass
class RemoteMatch:
    """Narrows down which code set a remote belongs to, press by press.

    Each profile scores a point for every distinct code heard that it has;
    the candidates are the top scorers. Profiles list only some functions,
    so one missing code mustn't rule a code set out, as an intersection
    would.
    """

    index: RemoteIndex
    heard: list[Heard] = field(default_factory=list)
    scores: Counter[str] = field(default_factory=Counter)
    unknown: int = 0  # presses whose code no profile has
    _seen: set[IrCode] = field(default_factory=set)

    @property
    def candidates(self) -> set[str] | None:
        """Profile ids with the top score; None before any known code."""
        if not self.scores:
            return None
        top = max(self.scores.values())
        return {pid for pid, n in self.scores.items() if n == top}

    def add(self, code: IrCode) -> Heard:
        entries = self.index.lookup(code)
        if not entries:
            self.unknown += 1
        elif code not in self._seen:
            self.scores.update({pid for pid, _ in entries})
        self._seen.add(code)
        heard = Heard(code, self._function(entries))
        self.heard.append(heard)
        return heard

    def _function(self, entries: list[tuple[str, str]]) -> str | None:
        """What the best-scoring profiles that have this code call it, or
        None if no profile close to the top has it."""
        if not self.scores:
            return None
        floor = NAMING_SHARE * max(self.scores.values())
        best: dict[str, tuple[int, int]] = {}  # name -> (top score, profiles)
        for pid, name in entries:
            if self.scores[pid] < floor:
                continue
            score, count = best.get(name, (0, 0))
            best[name] = (max(score, self.scores[pid]), count + 1)
        return max(best, key=best.__getitem__) if best else None

    def best_profile(self) -> Profile | None:
        """The likeliest code set: top score, then the database's own
        confidence, then the most complete."""
        if not self.candidates:
            return None
        profiles = [self.index.profiles.by_id[pid] for pid in self.candidates]
        return max(profiles, key=lambda p: (p.confidence, len(p.codes), p.id))

    def brands(self) -> list[tuple[str, int]]:
        """Candidate brands with their code set counts, most first."""
        if not self.candidates:
            return []
        counts = Counter(self.index.profiles.by_id[pid].brand for pid in self.candidates)
        return counts.most_common()

    def summary(self) -> dict:
        last = self.heard[-1] if self.heard else None
        return {
            "presses": len(self.heard),
            "unknown": self.unknown,
            "last": {"code": str(last.code), "function": last.function} if last else None,
            "brands": [{"brand": b, "codesets": n} for b, n in self.brands()[:5]],
            "best": {"id": best.id, "brand": best.brand, "name": best.name} if (best := self.best_profile()) else None,
            "candidates": len(self.candidates) if self.candidates is not None else None,
        }
