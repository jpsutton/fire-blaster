"""Control socket: newline-delimited JSON over a Unix socket.

The daemon runs as a system service and holds the remote exclusively, so a
setup UI in the user's session can't see remote keys itself. Instead it
connects here.

Server -> client, one JSON object per line:
    {"event": "state", ...}       full snapshot; sent on connect and after every change
    {"event": "tx", "function": "VOLUME_UP", "profile": {...}}
    {"event": "saved" | "cancelled", "profile": {...}}
    {"event": "error", "message": "..."}

Client -> server:
    {"cmd": "state"}                                  resend the snapshot
    {"cmd": "start_setup"}
    {"cmd": "next" | "prev" | "next_brand" | "prev_brand" | "accept" | "cancel"}
    {"cmd": "test", "function": "MUTE_TOGGLE"}

The socket is world-connectable (0666): on a single-user HTPC, anyone local
may run TV setup. Nothing here reads or writes files beyond the daemon state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .controller import Controller

log = logging.getLogger(__name__)

SOCKET_NAME = "control.sock"
SYSTEM_SOCKET = Path("/run/fire-blaster") / SOCKET_NAME
MAX_LINE = 4096
MAX_BUFFERED = 256 * 1024  # drop clients that stop reading


def default_socket_path() -> Path:
    """Where the daemon listens: systemd RuntimeDirectory, else the user runtime dir."""
    if runtime := os.environ.get("RUNTIME_DIRECTORY"):
        return Path(runtime.split(":")[0]) / SOCKET_NAME
    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        return Path(xdg) / "fire-blaster" / SOCKET_NAME
    return SYSTEM_SOCKET


def client_socket_paths() -> list[Path]:
    """Where a client looks, in order: $FIREBLASTER_SOCKET, the system service, a user-run daemon."""
    paths = []
    if env := os.environ.get("FIREBLASTER_SOCKET"):
        paths.append(Path(env))
    paths.append(SYSTEM_SOCKET)
    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        paths.append(Path(xdg) / "fire-blaster" / SOCKET_NAME)
    return paths


class ControlServer:
    def __init__(self, controller: Controller, path: Path):
        self.ctl = controller
        self.path = path
        self.clients: set[asyncio.StreamWriter] = set()
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_socket():
            self.path.unlink()  # stale socket from a previous run
        self._server = await asyncio.start_unix_server(self._serve_client, path=self.path, limit=MAX_LINE)
        os.chmod(self.path, 0o666)
        self.ctl.add_listener(self.broadcast)
        log.info("control socket: %s", self.path)

    async def close(self) -> None:
        self.ctl.remove_listener(self.broadcast)
        for writer in list(self.clients):
            writer.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        try:
            self.path.unlink()
        except OSError:
            pass

    def broadcast(self, event: dict) -> None:
        line = (json.dumps(event) + "\n").encode()
        for writer in list(self.clients):
            if writer.transport.get_write_buffer_size() > MAX_BUFFERED:
                log.warning("dropping control client that stopped reading")
                self.clients.discard(writer)
                writer.close()
            else:
                writer.write(line)

    def _send(self, writer: asyncio.StreamWriter, event: dict) -> None:
        writer.write((json.dumps(event) + "\n").encode())

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients.add(writer)
        log.debug("control client connected")
        self._send(writer, self.ctl.snapshot())
        try:
            while line := await reader.readline():
                self._command(writer, line)
        except (ConnectionError, asyncio.LimitOverrunError, ValueError) as e:
            log.debug("control client error: %s", e)
        finally:
            self.clients.discard(writer)
            writer.close()
            log.debug("control client disconnected")

    def _command(self, writer: asyncio.StreamWriter, line: bytes) -> None:
        try:
            msg = json.loads(line)
            cmd = msg["cmd"]
        except (ValueError, KeyError, TypeError):
            self._send(writer, {"event": "error", "message": "expected {\"cmd\": ...}"})
            return

        if cmd == "state":
            self._send(writer, self.ctl.snapshot())
        elif cmd == "start_setup":
            if not self.ctl.start_setup():
                self._send(writer, {"event": "error", "message": "no setup candidates; check profile directories"})
        elif cmd == "test":
            function = msg.get("function")
            if not isinstance(function, str) or not self.ctl.test(function):
                self._send(writer, {"event": "error", "message": f"cannot send {function!r}"})
        elif not self.ctl.setup_action(cmd):
            self._send(writer, {"event": "error", "message": f"unknown command or not in setup: {cmd!r}"})
