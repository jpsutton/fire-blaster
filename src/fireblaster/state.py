"""Persistent daemon state: the selected TV profile and the IR remotes
taught in remote setup (irremote.LearnedRemote.to_json)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def default_state_path() -> Path:
    # systemd sets STATE_DIRECTORY when the unit uses StateDirectory=.
    if state_dir := os.environ.get("STATE_DIRECTORY"):
        return Path(state_dir.split(":")[0]) / "state.json"
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "fire-blaster" / "state.json"


@dataclass
class State:
    path: Path
    profile: str | None = None
    remotes: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable state file %s: %s", path, e)
            return cls(path)
        return cls(path, profile=data.get("profile"), remotes=list(data.get("remotes", [])))

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"profile": self.profile, "remotes": self.remotes}, indent=2) + "\n")
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("could not save state to %s: %s", self.path, e)
