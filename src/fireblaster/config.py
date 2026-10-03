"""Daemon configuration: built-in defaults, optionally overridden by a TOML file.

See config.example.toml for the file format.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from evdev import ecodes

from .control import default_socket_path
from .state import default_state_path

DEFAULT_CONFIG_PATH = Path("/etc/fire-blaster/config.toml")

# Back+OK: the Alexa Voice Remote reports volume/mute/power as instant taps,
# so only navigation keys can be detected as held.
DEFAULT_SETUP_COMBO = ["KEY_BACK", "KEY_KPENTER"]

DEFAULT_KEYMAP = {
    "KEY_VOLUMEUP": "VOLUME_UP",
    "KEY_VOLUMEDOWN": "VOLUME_DOWN",
    "KEY_MUTE": "MUTE_TOGGLE",
    "KEY_POWER": "POWER_TOGGLE",
}

# Common hotel TV brands, most likely first.
DEFAULT_SETUP_BRANDS = [
    "LG", "Samsung", "Sony", "Vizio", "TCL", "Hisense", "Insignia", "Philips",
    "Sharp", "Toshiba", "Panasonic", "RCA", "Westinghouse", "Element", "JVC",
    "Magnavox", "Sanyo", "Hitachi",
]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class HoldAction:
    """What a key does when held: after `seconds`, send the `send` keys as one
    chord (pressed in order, released in reverse) instead of the key itself.
    A shorter press passes the key through as a tap when it is released."""

    seconds: float
    send: tuple[int, ...]


@dataclass(frozen=True)
class RemoteRule:
    """Settings for the remotes whose device name matches one of `names`
    (regexes, re.search). Unset tables (None) fall back to the global ones."""

    names: tuple[str, ...]
    keymap: dict[int, str] | None = None  # this remote's [keys]
    drop: frozenset[int] | None = None  # this remote's [drop] keys
    # A profile id this remote's keys always blast (e.g. an AV receiver),
    # instead of the TV chosen in setup mode.
    profile: str | None = None

    def matches(self, device_name: str) -> bool:
        return any(re.search(p, device_name) for p in self.names)


def _default_profile_dirs() -> list[Path]:
    return [
        Path("/usr/share/fire-blaster/profiles"),
        Path("/etc/fire-blaster/profiles"),
        Path.home() / ".local" / "share" / "fire-blaster" / "profiles",
    ]


@dataclass
class Config:
    # Regexes (re.search) matched against input device names.
    device_names: list[str] = field(default_factory=lambda: [r"^AR( Keyboard)?$", r"Amazon", r"Fire ?TV"])
    # evdev key code -> IR function name.
    keymap: dict[int, str] = field(default_factory=lambda: {ecodes.ecodes[k]: f for k, f in DEFAULT_KEYMAP.items()})
    # evdev key code -> what holding it does. Empty by default.
    hold: dict[int, HoldAction] = field(default_factory=dict)
    # evdev key code the remote sends -> key code it becomes, before anything
    # else sees it. Empty by default.
    remap: dict[int, int] = field(default_factory=dict)
    # evdev key codes swallowed outright: no IR, no pass-through. Empty by
    # default.
    drop: set[int] = field(default_factory=set)
    # Per-remote overrides of [keys] and [drop], first match wins ([[remote]]).
    remotes: list[RemoteRule] = field(default_factory=list)
    repeat_functions: set[str] = field(default_factory=lambda: {"VOLUME_UP", "VOLUME_DOWN"})
    repeat_delay: float = 0.35
    repeat_interval: float = 0.12
    setup_combo: list[int] = field(default_factory=lambda: [ecodes.ecodes[k] for k in DEFAULT_SETUP_COMBO])
    setup_hold_seconds: float = 5.0
    setup_timeout_seconds: float = 60.0
    setup_test_function: str | None = "VOLUME_UP"
    setup_device_types: list[str] = field(default_factory=lambda: ["tv"])
    setup_brands: list[str] = field(default_factory=lambda: list(DEFAULT_SETUP_BRANDS))
    setup_include_other_brands: bool = False
    # IR transmitter: "auto" (first LIRC device that can send, USB first),
    # a device path such as "/dev/lirc1", or "log" to only log transmissions.
    ir_device: str = "auto"
    # With ir_device "auto": only use LIRC devices of this kernel driver,
    # e.g. "mceusb".
    ir_driver: str | None = None
    profile_dirs: list[Path] = field(default_factory=_default_profile_dirs)
    state_path: Path = field(default_factory=default_state_path)
    control_socket: Path = field(default_factory=default_socket_path)

    @classmethod
    def load(cls, path: Path | None) -> Config:
        cfg = cls()
        if path is None:
            if not DEFAULT_CONFIG_PATH.exists():
                return cfg
            path = DEFAULT_CONFIG_PATH
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"{path}: {e}") from None
        cfg.apply(data)
        return cfg

    def apply(self, data: dict) -> None:
        device = data.get("device", {})
        if "names" in device:
            self.device_names = list(device["names"])

        if "keys" in data:
            self.keymap = _keymap(data["keys"], "[keys]")

        if "remap" in data:
            self.remap = {}
            for name, target in data["remap"].items():
                if not isinstance(target, str):
                    raise ConfigError(f"[remap] {name}: expected a key name, e.g. \"KEY_ENTER\"")
                self.remap[_key_code(name, "[remap]")] = _key_code(target, f"[remap] {name}")

        if "drop" in data:
            keys = data["drop"].get("keys", []) if isinstance(data["drop"], dict) else None
            self.drop = set(_key_list(keys, "[drop] keys"))

        if "remote" in data:
            self.remotes = [_remote_rule(table, i) for i, table in enumerate(_tables(data["remote"], "[[remote]]"))]

        if "hold" in data:
            self.hold = {}
            for name, table in data["hold"].items():
                where = f"[hold.{name}]"
                if not isinstance(table, dict):
                    raise ConfigError(f"{where}: expected a table with 'seconds' and 'send'")
                send = tuple(_key_code(k, f"{where} send") for k in table.get("send", []))
                if not send:
                    raise ConfigError(f"{where}: 'send' needs at least one key")
                seconds = float(table.get("seconds", 0.6))
                if seconds <= 0:
                    raise ConfigError(f"{where}: 'seconds' must be positive")
                self.hold[_key_code(name, where)] = HoldAction(seconds, send)

        repeat = data.get("repeat", {})
        if "functions" in repeat:
            self.repeat_functions = set(repeat["functions"])
        self.repeat_delay = float(repeat.get("delay", self.repeat_delay))
        self.repeat_interval = float(repeat.get("interval", self.repeat_interval))

        setup = data.get("setup", {})
        if "combo" in setup:
            self.setup_combo = [_key_code(name, "[setup] combo") for name in setup["combo"]]
            if not self.setup_combo:
                raise ConfigError("[setup] combo needs at least one key")
        self.setup_hold_seconds = float(setup.get("hold_seconds", self.setup_hold_seconds))
        self.setup_timeout_seconds = float(setup.get("timeout_seconds", self.setup_timeout_seconds))
        if "test_function" in setup:
            self.setup_test_function = setup["test_function"] or None
        self.setup_device_types = list(setup.get("device_types", self.setup_device_types))
        self.setup_brands = list(setup.get("brands", self.setup_brands))
        self.setup_include_other_brands = bool(setup.get("include_other_brands", self.setup_include_other_brands))

        ir = data.get("ir", {})
        if not isinstance(ir, dict):
            raise ConfigError("[ir]: expected a table")
        unknown = set(ir) - {"device", "driver"}
        if unknown:
            raise ConfigError(f"[ir]: unknown setting(s) {', '.join(sorted(unknown))}")
        if "device" in ir:
            if not isinstance(ir["device"], str) or not ir["device"]:
                raise ConfigError('[ir] device: expected "auto", "log" or a device path')
            self.ir_device = ir["device"]
        if "driver" in ir:
            self.ir_driver = str(ir["driver"]) or None

        paths = data.get("paths", {})
        if "profiles" in paths:
            self.profile_dirs = [Path(p).expanduser() for p in paths["profiles"]]
        if "state" in paths:
            self.state_path = Path(paths["state"]).expanduser()
        if "socket" in paths:
            self.control_socket = Path(paths["socket"]).expanduser()


    @property
    def all_device_names(self) -> list[str]:
        """[device] names plus every [[remote]]'s: the devices to grab."""
        names = list(self.device_names)
        for rule in self.remotes:
            names += [n for n in rule.names if n not in names]
        return names

    def rule_for(self, device_name: str) -> RemoteRule | None:
        return next((rule for rule in self.remotes if rule.matches(device_name)), None)

    def keymap_for(self, device_name: str) -> dict[int, str]:
        rule = self.rule_for(device_name)
        return self.keymap if rule is None or rule.keymap is None else rule.keymap

    def drop_for(self, device_name: str) -> set[int] | frozenset[int]:
        rule = self.rule_for(device_name)
        return self.drop if rule is None or rule.drop is None else rule.drop

    def profile_for(self, device_name: str) -> str | None:
        """The profile a remote's keys always blast, or None for the TV
        chosen in setup mode."""
        rule = self.rule_for(device_name)
        return rule.profile if rule else None


def _tables(value, where: str) -> list[dict]:
    if not isinstance(value, list) or not all(isinstance(t, dict) for t in value):
        raise ConfigError(f"{where}: expected one or more [[remote]] tables")
    return value


def _keymap(table, where: str) -> dict[int, str]:
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: expected a table of KEY_... = \"FUNCTION\"")
    return {_key_code(name, where): function for name, function in table.items()}


def _key_list(keys, where: str) -> list[int]:
    if not isinstance(keys, list):
        raise ConfigError(f"{where}: expected a list, e.g. [\"KEY_VOLUMEUP\"]")
    return [_key_code(name, where) for name in keys]


def _remote_rule(table: dict, index: int) -> RemoteRule:
    where = f"[[remote]] #{index + 1}"
    names = table.get("names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise ConfigError(f"{where}: 'names' needs at least one device name regex")
    for n in names:
        try:
            re.compile(n)
        except re.error as e:
            raise ConfigError(f"{where}: bad regex {n!r}: {e}") from None
    unknown = set(table) - {"names", "keys", "drop", "profile"}
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
    keymap = _keymap(table["keys"], f"{where} keys") if "keys" in table else None
    drop = frozenset(_key_list(table["drop"], f"{where} drop")) if "drop" in table else None
    profile = table.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile):
        raise ConfigError(f"{where}: 'profile' expects a profile id")
    return RemoteRule(tuple(names), keymap, drop, profile)


def _key_code(name: str, where: str) -> int:
    code = ecodes.ecodes.get(name)
    if code is None or not name.startswith(("KEY_", "BTN_")):
        raise ConfigError(f"{where}: unknown key name {name!r}")
    return code
