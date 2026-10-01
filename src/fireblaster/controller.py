"""Key handling: decides which remote keys become IR and runs setup mode.

The controller is fed EV_KEY events from every grabbed remote device and
answers, per event, whether the daemon should pass it on to the virtual
(uinput) device that the media apps read.

Normal mode
    Keys in the keymap (volume, mute, power) are swallowed and blasted with the
    active profile. Volume keys repeat while held. Everything else passes
    through untouched.

Setup mode (hold the `setup_combo` keys, Back+OK by default, for
`setup_hold_seconds`)
    Every key is swallowed. The remote picks a code set for the TV in front of
    you (the setup UI shows brands as a horizontal strip and the current
    brand's code sets as a vertical list, matching these keys):
        Right / Left   next / previous brand, starting at its first code set
        Down / Up      next / previous code set within the brand (wraps)
                       (every move auto-blasts the test function)
        Vol, Mute, Pwr blast that function with the candidate code set
        OK             save the candidate and leave setup mode
        Back / Home    leave setup mode without changing anything
    Setup mode also ends, unchanged, after `setup_timeout_seconds` idle.

The combo keys usually pass through to apps. The first one is passed
normally, since it may just be a keypress; once the whole combo is held, the
controller "claims" it: already-passed combo keys get a synthetic release on
uinput (via `inject`) and everything after is swallowed.

The Alexa Voice Remote reports volume, mute and power as instant taps (press
and release in the same millisecond, however long the key is held), so those
keys can't be part of a hold combo and don't produce held-key IR repeats.

Listeners (the control socket, and through it the setup UI) get a state
snapshot after every change, plus transient events ("tx", "saved",
"cancelled"). The same setup actions the remote drives are available as
methods, so the UI can drive setup with a keyboard or mouse too.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Hashable
from typing import Any

from evdev import ecodes

from .config import Config
from .profiles import Profile, ProfileSet
from .state import State
from .transmit import Transmitter

log = logging.getLogger(__name__)

KEYS_NEXT_BRAND = {ecodes.KEY_RIGHT}
KEYS_PREV_BRAND = {ecodes.KEY_LEFT}
KEYS_NEXT_CODE = {ecodes.KEY_DOWN}
KEYS_PREV_CODE = {ecodes.KEY_UP}
KEYS_ACCEPT = {ecodes.KEY_SELECT, ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.KEY_OK}
KEYS_CANCEL = {ecodes.KEY_BACK, ecodes.KEY_ESC, ecodes.KEY_HOMEPAGE, ecodes.KEY_EXIT}

KEY_DOWN, KEY_UP, KEY_HOLD = 1, 0, 2

# next/prev walk the whole candidate list; *_brand and *_code are what the keys use.
SETUP_ACTIONS = ("next", "prev", "next_brand", "prev_brand", "next_code", "prev_code", "accept", "cancel")

Event = dict[str, Any]


def key_name(code: int) -> str:
    name = ecodes.KEY.get(code) or ecodes.BTN.get(code) or str(code)
    if isinstance(name, (list, tuple)):
        # Skip range markers such as KEY_MIN_INTERESTING (an alias of KEY_MUTE).
        name = next((n for n in name if "_MIN_" not in n and "_MAX" not in n), name[0])
    return name


class SetupSession:
    """Position in the candidate list, which is grouped into one run per brand."""

    def __init__(self, candidates: list[Profile], index: int = 0):
        self.candidates = candidates
        self.index = index
        self.brands: list[str] = []
        self._runs: list[tuple[int, int]] = []  # (start, end exclusive) per brand
        for i, p in enumerate(candidates):
            if self.brands and self.brands[-1].casefold() == p.brand.casefold():
                self._runs[-1] = (self._runs[-1][0], i + 1)
            else:
                self.brands.append(p.brand)
                self._runs.append((i, i + 1))

    @property
    def current(self) -> Profile:
        return self.candidates[self.index]

    @property
    def brand_index(self) -> int:
        return next(b for b, (start, end) in enumerate(self._runs) if start <= self.index < end)

    def brand_members(self) -> list[Profile]:
        start, end = self._runs[self.brand_index]
        return self.candidates[start:end]

    def brand_position(self) -> tuple[int, int, int]:
        """(brand index, position within brand, code sets in brand) for the current candidate."""
        b = self.brand_index
        start, end = self._runs[b]
        return b, self.index - start, end - start

    def step(self, delta: int) -> None:
        self.index = (self.index + delta) % len(self.candidates)

    def step_brand(self, delta: int) -> None:
        """First code set of the next/previous brand, wrapping."""
        b = (self.brand_index + delta) % len(self._runs)
        self.index = self._runs[b][0]

    def step_code(self, delta: int) -> None:
        """Next/previous code set within the current brand, wrapping."""
        start, end = self._runs[self.brand_index]
        self.index = start + (self.index - start + delta) % (end - start)


class Controller:
    def __init__(
        self,
        cfg: Config,
        profiles: ProfileSet,
        state: State,
        tx: Transmitter,
        inject: Callable[[Hashable, int, int], None] | None = None,
    ):
        self.cfg = cfg
        self.profiles = profiles
        self.state = state
        self.tx = tx
        self.active: Profile | None = profiles.get(state.profile)
        self.setup: SetupSession | None = None
        # Writes a key event to a source's uinput clone; set by the daemon.
        self.inject = inject or (lambda source, code, value: None)
        self.listeners: list[Callable[[Event], None]] = []
        self._combo = frozenset(cfg.setup_combo)
        # source -> (keymap, drop keys) for its remote ([[remote]] or global)
        self._sources: dict[Hashable, tuple[dict[int, str], set[int] | frozenset[int]]] = {}

        self._down: dict[int, Hashable] = {}  # intercepted key code -> source device
        self._passed: set[tuple[Hashable, int]] = set()  # (source, code) presses sent to uinput
        self._repeat_timer: asyncio.TimerHandle | None = None
        self._repeat_key: int | None = None
        # (source, code) of a [hold] key being held -> its timer, or None once
        # the hold action has fired.
        self._holding: dict[tuple[Hashable, int], asyncio.TimerHandle | None] = {}
        self._combo_down: dict[int, Hashable] = {}  # combo key code -> source device
        self._combo_timer: asyncio.TimerHandle | None = None
        self._setup_timer: asyncio.TimerHandle | None = None

        if state.profile and self.active is None:
            log.warning("saved profile %r not found; run setup mode", state.profile)
        elif self.active:
            log.info("active profile: %s", self.active.label)

    @property
    def intercepted_codes(self) -> set[int]:
        """Keys fire-blaster acts on for any remote (as the remote sends them)."""
        codes = set(self.cfg.keymap) | set(self.cfg.hold) | set(self.cfg.remap) | self.cfg.drop
        for rule in self.cfg.remotes:
            codes |= set(rule.keymap or ()) | set(rule.drop or ())
        return codes

    def intercepted_codes_for(self, device_name: str) -> set[int]:
        """Keys fire-blaster acts on for one remote; a device that has any of
        them is grabbed."""
        return (
            set(self.cfg.keymap_for(device_name))
            | set(self.cfg.hold)
            | set(self.cfg.remap)
            | set(self.cfg.drop_for(device_name))
        )

    def attach(self, source: Hashable, device_name: str) -> None:
        """A remote device was grabbed: use its [[remote]] settings."""
        self._sources[source] = (self.cfg.keymap_for(device_name), self.cfg.drop_for(device_name))

    def _settings(self, source: Hashable) -> tuple[dict[int, str], set[int] | frozenset[int]]:
        return self._sources.get(source) or (self.cfg.keymap, self.cfg.drop)

    @property
    def injected_codes(self) -> set[int]:
        """Keys that may reach a uinput clone without the remote having them:
        hold keys and chords, and remap targets."""
        codes = set(self.cfg.hold) | set(self.cfg.remap.values())
        for action in self.cfg.hold.values():
            codes.update(action.send)
        return codes

    def remapped(self, code: int) -> int:
        """The key code to use for one the remote sent ([remap])."""
        return self.cfg.remap.get(code, code)

    # -- listeners and state ------------------------------------------------

    def add_listener(self, listener: Callable[[Event], None]) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener: Callable[[Event], None]) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    def _emit(self, event: Event) -> None:
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                log.exception("listener failed")

    def _notify(self) -> None:
        self._emit(self.snapshot())

    def snapshot(self) -> Event:
        """Everything a UI needs to draw the current state."""
        now = asyncio.get_running_loop().time()

        def remaining(timer: asyncio.TimerHandle | None) -> float | None:
            return max(0.0, round(timer.when() - now, 2)) if timer else None

        setup = None
        if self.setup:
            brand_index, in_brand, brand_count = self.setup.brand_position()
            setup = {
                "index": self.setup.index,
                "count": len(self.setup.candidates),
                "candidate": _profile_info(self.setup.current),
                "brands": self.setup.brands,
                "brand_index": brand_index,
                "brand_position": in_brand,
                "brand_count": brand_count,
                "brand_codesets": [p.name for p in self.setup.brand_members()],
                "idle_timeout": remaining(self._setup_timer),
                "test_function": self.cfg.setup_test_function,
            }
        return {
            "event": "state",
            "mode": "setup" if self.setup else "normal",
            "active": _profile_info(self.active),
            "combo": {
                "keys": [key_name(c) for c in self.cfg.setup_combo],
                "hold_seconds": self.cfg.setup_hold_seconds,
                "remaining": remaining(self._combo_timer),
            },
            "functions": sorted(
                set(self.cfg.keymap.values()).union(*(r.keymap.values() for r in self.cfg.remotes if r.keymap))
            ),
            "setup": setup,
        }

    # -- commands (control socket / UI) ---------------------------------------

    def start_setup(self) -> bool:
        if self.setup is None:
            self._enter_setup()
        return self.setup is not None

    def setup_action(self, action: str) -> bool:
        """Run a setup action by name (see SETUP_ACTIONS); False if not in setup."""
        if self.setup is None or action not in SETUP_ACTIONS:
            return False
        self._arm_setup_timeout()
        if action == "accept":
            self._exit_setup(save=True)
        elif action == "cancel":
            self._exit_setup(save=False)
        else:
            step, delta = {
                "next": (self.setup.step, 1),
                "prev": (self.setup.step, -1),
                "next_brand": (self.setup.step_brand, 1),
                "prev_brand": (self.setup.step_brand, -1),
                "next_code": (self.setup.step_code, 1),
                "prev_code": (self.setup.step_code, -1),
            }[action]
            step(delta)
            self._announce_candidate()
        return True

    def test(self, function: str) -> bool:
        """Blast one function with the setup candidate, or the active profile."""
        if self.setup:
            self._arm_setup_timeout()
        profile = self.setup.current if self.setup else self.active
        if profile is None:
            return False
        sent = self._send(profile, function)
        if self.setup:
            self._notify()  # idle timeout was reset
        return sent

    def _send(self, profile: Profile, function: str, repeat: bool = False) -> bool:
        sent = self.tx.send(profile, function, repeat=repeat)
        if sent and not repeat:
            self._emit({"event": "tx", "function": function, "profile": _profile_info(profile)})
        return sent

    # -- event entry points -------------------------------------------------

    def handle(self, source: Hashable, code: int, value: int) -> bool:
        """Process one EV_KEY event. Returns True to pass it through to uinput."""
        keymap, drop = self._settings(source)
        if code in drop:
            return False
        if code in self._combo:
            if value == KEY_DOWN:
                self._combo_down[code] = source
                if self.setup is None and self._combo_complete():
                    self._claim_combo()
                    return False
            elif value == KEY_UP:
                self._combo_down.pop(code, None)
                self._check_combo_broken()
        # Setup mode keeps its own meaning for keys (Home = cancel), but a hold
        # already in progress still finishes as a hold.
        if (source, code) in self._holding or (code in self.cfg.hold and self.setup is None):
            return self._hold_key(source, code, value)
        function = keymap.get(code)
        if function is None:
            return self._other_key(source, code, value)
        if value == KEY_DOWN:
            self._press(source, code, function)
        elif value == KEY_UP:
            self._release(code)
        # Kernel autorepeat (value 2) is ignored; IR repeat runs on its own timer.
        return False

    def source_gone(self, source: Hashable) -> None:
        """A remote device disappeared (sleep, disconnect): forget its keys."""
        self._sources.pop(source, None)
        self._passed = {k for k in self._passed if k[0] != source}
        for code in [c for c, s in self._down.items() if s == source]:
            self._release(code)
        self._combo_down = {c: s for c, s in self._combo_down.items() if s != source}
        self._check_combo_broken()
        # A hold cut off by a disconnect does nothing: no tap, no chord.
        for key in [k for k in self._holding if k[0] == source]:
            if timer := self._holding.pop(key):
                timer.cancel()

    def shutdown(self) -> None:
        for timer in (self._repeat_timer, self._combo_timer, self._setup_timer, *self._holding.values()):
            if timer:
                timer.cancel()
        self._holding.clear()

    # -- [hold] keys --------------------------------------------------------

    def _hold_key(self, source: Hashable, code: int, value: int) -> bool:
        """A key with a hold action. Nothing passes through directly: a short
        press is re-sent as a tap on release, a long one as the hold chord."""
        key = (source, code)
        if value == KEY_DOWN:
            action = self.cfg.hold[code]
            loop = asyncio.get_running_loop()
            self._holding[key] = loop.call_later(action.seconds, self._hold_fired, source, code)
        elif value == KEY_UP and key in self._holding:
            timer = self._holding.pop(key)
            if timer is not None:  # released before the hold fired
                timer.cancel()
                self.inject(source, code, KEY_DOWN)
                self.inject(source, code, KEY_UP)
        # Kernel autorepeat while held is dropped.
        return False

    def _hold_fired(self, source: Hashable, code: int) -> None:
        key = (source, code)
        if key not in self._holding:
            return
        self._holding[key] = None
        send = self.cfg.hold[code].send
        log.info("%s held: sending %s", key_name(code), "+".join(key_name(k) for k in send))
        for k in send:
            self.inject(source, k, KEY_DOWN)
        for k in reversed(send):
            self.inject(source, k, KEY_UP)

    # -- intercepted keys ---------------------------------------------------

    def _press(self, source: Hashable, code: int, function: str) -> None:
        self._down[code] = source

        if self.setup:
            self._arm_setup_timeout()
            profile = self.setup.current
        else:
            profile = self.active
        if profile is None:
            log.warning(
                "%s pressed but no TV profile is selected; hold %s for %gs to run setup",
                key_name(code),
                self._combo_label(),
                self.cfg.setup_hold_seconds,
            )
            return

        self._stop_repeat()
        self._send(profile, function)
        if function in self.cfg.repeat_functions:
            self._start_repeat(code, function, profile)
        if self.setup:
            self._notify()  # idle timeout was reset

    def _release(self, code: int) -> None:
        self._down.pop(code, None)
        if self._repeat_key == code:
            self._stop_repeat()

    def _start_repeat(self, code: int, function: str, profile: Profile) -> None:
        loop = asyncio.get_running_loop()

        def fire() -> None:
            self._send(profile, function, repeat=True)
            self._repeat_timer = loop.call_later(self.cfg.repeat_interval, fire)

        self._repeat_key = code
        self._repeat_timer = loop.call_later(self.cfg.repeat_delay, fire)

    def _stop_repeat(self) -> None:
        if self._repeat_timer:
            self._repeat_timer.cancel()
        self._repeat_timer = None
        self._repeat_key = None

    # -- setup combo --------------------------------------------------------

    def _combo_complete(self) -> bool:
        return self._combo <= self._combo_down.keys()

    def _combo_label(self) -> str:
        return "+".join(key_name(c) for c in self.cfg.setup_combo)

    def _claim_combo(self) -> None:
        """Take the held combo keys away from apps and start the hold timer."""
        for code, source in self._combo_down.items():
            if (source, code) in self._passed:
                self._passed.discard((source, code))
                self.inject(source, code, KEY_UP)
            if code == self._repeat_key:
                self._stop_repeat()
            self._down.pop(code, None)
        if self._combo_timer:
            return
        log.info("%s held; setup mode in %gs", self._combo_label(), self.cfg.setup_hold_seconds)
        self._combo_timer = asyncio.get_running_loop().call_later(self.cfg.setup_hold_seconds, self._combo_fired)
        self._notify()

    def _check_combo_broken(self) -> None:
        if self._combo_timer and not self._combo_complete():
            self._combo_timer.cancel()
            self._combo_timer = None
            log.info("setup combo released early")
            self._notify()

    def _combo_fired(self) -> None:
        self._combo_timer = None
        self._enter_setup()
        if self.setup is None:
            self._notify()  # no candidates: clear the UI's countdown

    # -- other keys ---------------------------------------------------------

    def _other_key(self, source: Hashable, code: int, value: int) -> bool:
        key = (source, code)
        if value == KEY_DOWN:
            if self.setup:
                self._setup_nav(code)
                return False
            self._passed.add(key)
            return True
        if value == KEY_HOLD:
            return key in self._passed
        # Release: pass it only if the press was passed, so apps never see a
        # stray key-up, and keys held across a mode change never get stuck.
        if key in self._passed:
            self._passed.discard(key)
            return True
        return False

    # -- setup mode ---------------------------------------------------------

    def _enter_setup(self) -> None:
        candidates = self.profiles.candidates(
            self.cfg.setup_device_types,
            self.cfg.setup_brands,
            self.cfg.setup_include_other_brands,
            self.cfg.setup_test_function,
        )
        if not candidates:
            log.error(
                "setup mode: no candidate profiles (types=%s, brands=%s); check profile dirs %s",
                self.cfg.setup_device_types,
                self.cfg.setup_brands,
                ", ".join(map(str, self.cfg.profile_dirs)),
            )
            return

        start = candidates.index(self.active) if self.active in candidates else 0
        self.setup = SetupSession(candidates, start)
        log.info(
            "setup mode: %d candidates. Right/Left = next/prev brand, Down/Up = next/prev code set, "
            "vol/mute/power = test, OK = save, Back = cancel",
            len(candidates),
        )
        self._arm_setup_timeout()
        self._announce_candidate()

    def _setup_nav(self, code: int) -> None:
        for keys, action in (
            (KEYS_NEXT_BRAND, "next_brand"),
            (KEYS_PREV_BRAND, "prev_brand"),
            (KEYS_NEXT_CODE, "next_code"),
            (KEYS_PREV_CODE, "prev_code"),
            (KEYS_ACCEPT, "accept"),
            (KEYS_CANCEL, "cancel"),
        ):
            if code in keys:
                self.setup_action(action)
                return
        log.debug("setup mode: ignoring %s", key_name(code))
        self._arm_setup_timeout()

    def _announce_candidate(self) -> None:
        assert self.setup is not None
        profile = self.setup.current
        log.info("setup mode: candidate %d/%d: %s", self.setup.index + 1, len(self.setup.candidates), profile.label)
        self._stop_repeat()
        # Notify before the test blast so a UI shows the candidate first.
        self._notify()
        if self.cfg.setup_test_function:
            self._send(profile, self.cfg.setup_test_function)

    def _arm_setup_timeout(self) -> None:
        if self._setup_timer:
            self._setup_timer.cancel()
        self._setup_timer = asyncio.get_running_loop().call_later(self.cfg.setup_timeout_seconds, self._setup_timed_out)

    def _setup_timed_out(self) -> None:
        self._setup_timer = None
        log.info("setup mode: idle for %gs", self.cfg.setup_timeout_seconds)
        self._exit_setup(save=False)

    def _exit_setup(self, save: bool) -> None:
        session, self.setup = self.setup, None
        if session is None:
            return
        if self._setup_timer:
            self._setup_timer.cancel()
            self._setup_timer = None
        self._stop_repeat()
        if save:
            self.active = session.current
            self.state.profile = self.active.id
            self.state.save()
            log.info("setup mode: saved %s", self.active.label)
            self._emit({"event": "saved", "profile": _profile_info(self.active)})
        else:
            log.info("setup mode: cancelled; keeping %s", self.active.label if self.active else "no profile")
            self._emit({"event": "cancelled", "profile": _profile_info(self.active)})
        self._notify()


def _profile_info(profile: Profile | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {"id": profile.id, "brand": profile.brand, "name": profile.name}
