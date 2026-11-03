"""IR remotes taught in remote setup, and their buttons as key presses.

RemoteWizard runs remote setup: it matches the buttons heard against the
profile database (remotedb), asks for the essential buttons (Back, Home),
and builds a LearnedRemote: a table of codes and the functions they mean
(Amazon database names, plus BACK and HOME for taught buttons).

IrKeys then turns each frame a learned remote sends into a key press for
apps. Volume, mute and power have no key: the remote already sends those to
its TV.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from evdev import ecodes as e

from .irdecode import IrCode, Repeat
from .remotedb import RemoteIndex, RemoteMatch

FUNCTION_KEYS = {
    "DPAD_UP": e.KEY_UP,
    "DPAD_DOWN": e.KEY_DOWN,
    "DPAD_LEFT": e.KEY_LEFT,
    "DPAD_RIGHT": e.KEY_RIGHT,
    "DPAD_OK": e.KEY_ENTER,
    "BACK": e.KEY_BACK,
    "EXIT": e.KEY_BACK,
    "SMART_TV_EXIT": e.KEY_BACK,
    "HOME": e.KEY_HOMEPAGE,
    "SMART_TV": e.KEY_HOMEPAGE,
    "MENU_MAIN": e.KEY_MENU,
    "CHANNEL_UP": e.KEY_CHANNELUP,
    "CHANNEL_DOWN": e.KEY_CHANNELDOWN,
    "PLAY": e.KEY_PLAY,
    "PAUSE": e.KEY_PAUSE,
    "PLAY_PAUSE": e.KEY_PLAYPAUSE,
    "STOP": e.KEY_STOPCD,
    "FORWARD": e.KEY_FASTFORWARD,
    "REVERSE": e.KEY_REWIND,
    "REWIND": e.KEY_REWIND,
    "RECORD": e.KEY_RECORD,
    "CC": e.KEY_SUBTITLE,
    "DISPLAY": e.KEY_INFO,
    "FUNCTION_RED": e.KEY_RED,
    "FUNCTION_GREEN": e.KEY_GREEN,
    "FUNCTION_YELLOW": e.KEY_YELLOW,
    "FUNCTION_BLUE": e.KEY_BLUE,
    **{f"DIGIT_{n}": getattr(e, f"KEY_{n}") for n in range(10)},
}

NAVIGATION = ("DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_OK")
# Buttons setup always asks for, after OK: code sets often lack them or
# give the name to another button (LG's Exit, not its Back, is
# SMART_TV_EXIT).
ESSENTIALS = ("BACK", "HOME")
# Distinct known buttons heard before setup trusts the match.
MIN_KNOWN = 3
# A held button sends repeat frames about every 110 ms; release after this
# much silence.
RELEASE_SECONDS = 0.2


@dataclass
class LearnedRemote:
    name: str
    profile: str | None
    codes: dict[IrCode, str]

    def key_for(self, code: IrCode) -> int | None:
        function = self.codes.get(code)
        return FUNCTION_KEYS.get(function) if function else None

    def to_json(self) -> dict:
        return {"name": self.name, "profile": self.profile, "codes": {str(c): f for c, f in self.codes.items()}}

    @classmethod
    def from_json(cls, data: dict) -> LearnedRemote:
        return cls(data["name"], data.get("profile"), {IrCode.parse(c): f for c, f in data["codes"].items()})


@dataclass
class RemoteWizard:
    """Remote setup's progress. `add` takes each decoded frame and returns
    what happened: "heard" (a button pressed while listening), "taught" (a
    missing button assigned or skipped), "ignored" (an arrow while asking
    for a button) or "save" (ready to save)."""

    index: RemoteIndex
    match: RemoteMatch = field(init=False)
    taught: dict[IrCode, str] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)
    teaching: str | None = None

    def __post_init__(self) -> None:
        self.match = RemoteMatch(self.index)

    def table(self) -> dict[IrCode, str]:
        """Code -> function: the best code set, then buttons named from
        the near-top code sets, then taught buttons."""
        table: dict[IrCode, str] = {}
        if best := self.match.best_profile():
            for code, functions in self.index.codes_for(best.id).items():
                table[code] = min(functions, key=_preference)
        for heard in self.match.heard:
            if heard.function and heard.code not in table:
                table[heard.code] = heard.function
        for code, function in self.taught.items():
            for other in [c for c, f in table.items() if f == function]:
                del table[other]
            table[code] = function
        return table

    @property
    def ready(self) -> bool:
        known = {h.code for h in self.match.heard if h.function}
        functions = set(self.table().values())
        return len(known) >= MIN_KNOWN and all(f in functions for f in NAVIGATION)

    @property
    def step(self) -> str:
        return "teach" if self.teaching else "confirm" if self.ready else "listen"

    def missing(self) -> list[str]:
        """Essential buttons not yet asked for."""
        taught = set(self.taught.values())
        return [name for name in ESSENTIALS if name not in self.skipped and name not in taught]

    def add(self, code: IrCode) -> str:
        if self.teaching:
            if code in self.taught:
                return "ignored"  # already the answer to an earlier question
            function = self.table().get(code)
            if function == "DPAD_OK":
                self.skipped.add(self.teaching)
            elif function in NAVIGATION:
                return "ignored"  # an arrow: never reassign navigation
            else:
                self.taught[code] = self.teaching
            missing = self.missing()
            self.teaching = missing[0] if missing else None
            return "taught" if self.teaching else "save"
        was_ready = self.ready
        self.match.add(code)
        if was_ready and self.table().get(code) == "DPAD_OK":
            missing = self.missing()
            if missing:
                self.teaching = missing[0]
                return "taught"
            return "save"
        return "heard"

    def remote(self) -> LearnedRemote:
        best = self.match.best_profile()
        name = f"{best.brand} remote" if best else "IR remote"
        return LearnedRemote(name, best.id if best else None, self.table())

    def summary(self) -> dict:
        return {**self.match.summary(), "step": self.step, "teach": self.teaching}


def _preference(function: str) -> tuple[bool, bool]:
    """Sort key for a code's names: navigation first, then ones with keys."""
    return (function not in NAVIGATION, function not in FUNCTION_KEYS)


class IrKeys:
    """Key presses from learned remotes' frames. A full frame presses its
    key (releasing any other); repeat frames keep it held; silence releases
    it. `emit(key, value)` gets 1 for press and 0 for release."""

    def __init__(self, emit: Callable[[int, int], None]):
        self.emit = emit
        self.remotes: list[LearnedRemote] = []
        self._down: int | None = None
        self._timer: asyncio.TimerHandle | None = None

    def frame(self, code: IrCode | Repeat | None) -> None:
        if isinstance(code, Repeat):
            if self._down is not None:
                self._arm()
            return
        if code is None:
            return
        key = next((k for r in self.remotes if (k := r.key_for(code)) is not None), None)
        if key is None:
            return
        self.release()
        self._down = key
        self.emit(key, 1)
        self._arm()

    def release(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._down is not None:
            key, self._down = self._down, None
            self.emit(key, 0)

    def _arm(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().call_later(RELEASE_SECONDS, self.release)
