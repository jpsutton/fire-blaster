"""Persistent daemon state (currently just the selected profile)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
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

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable state file %s: %s", path, e)
            return cls(path)
        return cls(path, profile=data.get("profile"))

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"profile": self.profile}, indent=2) + "\n")
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("could not save state to %s: %s", self.path, e)
