"""fireblaster-setup: 10-foot setup overlay for fireblasterd (PySide6).

Runs in the desktop session and talks to the daemon over the control socket
(see control.py). It stays hidden until setup is happening:

- while the setup combo is held, it shows a countdown;
- in setup mode, it shows the candidate code set, the brand list, and what
  was last sent to the TV, following the remote as you press keys;
- when setup ends, it shows "saved" / "cancelled" briefly and hides again.

The remote is read by the daemon, not by this window, so remote keys work
whether or not the window has focus. A keyboard or mouse works too, when
the window has focus: arrows, Enter = save, Esc/Backspace = cancel,
+/-/M/P = test volume/mute/power.

    fireblaster-setup            persistent overlay (autostart it in the session)
    fireblaster-setup --start    start setup now (e.g. from a Bigscreen tile)
    fireblaster-setup --demo     scripted walkthrough, no daemon needed

Only one instance runs per user; a second one hands its --start to the first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .control import client_socket_paths

RESULT_SECONDS = 2.5
STATUS_FADE_SECONDS = 3.0
RETRY_MS = 2000
BRAND_STRIP = 7  # brands shown around the current one (horizontal, Left/Right)
CODESET_ROWS = 5  # code sets shown around the current one (vertical, Up/Down)

FUNCTION_NAMES = {
    "VOLUME_UP": "Volume Up",
    "VOLUME_DOWN": "Volume Down",
    "MUTE_TOGGLE": "Mute",
    "POWER_TOGGLE": "Power",
    "POWER_ON": "Power On",
    "POWER_OFF": "Power Off",
}
KEY_NAMES = {"KEY_BACK": "Back", "KEY_KPENTER": "OK", "KEY_ENTER": "OK", "KEY_SELECT": "OK", "KEY_HOMEPAGE": "Home"}

COLORS = {"bg": "#0f1317", "fg": "#eceff1", "muted": "#8e979f", "chip": "#1f262d", "accent": "#4f9cf9", "ok": "#3ccf6e", "warn": "#ffb020"}


def function_name(function: str) -> str:
    return FUNCTION_NAMES.get(function, function.replace("_", " ").title())


def key_label(key: str) -> str:
    return KEY_NAMES.get(key, key.removeprefix("KEY_").replace("_", " ").title())


def profile_label(profile: dict | None) -> str:
    if not profile:
        return "no TV selected"
    return profile["brand"] if profile["name"] == profile["brand"] else f"{profile['brand']} / {profile['name']}"


# -- daemon connection ------------------------------------------------------


class ControlClient(QObject):
    """Line-delimited JSON over QLocalSocket, reconnecting until the daemon appears."""

    message = Signal(dict)
    connection_changed = Signal(bool)

    def __init__(self, paths: list[Path]):
        super().__init__()
        # QLocalSocket treats names without a leading "/" as names under /tmp.
        self.paths = [p.absolute() for p in paths]
        self._try = 0
        self._buffer = b""
        self._pending: list[dict] = []
        self.connected = False
        self.socket = QLocalSocket(self)
        self.socket.connected.connect(self._on_connected)
        self.socket.disconnected.connect(self._on_disconnected)
        self.socket.errorOccurred.connect(self._on_error)
        self.socket.readyRead.connect(self._on_ready_read)
        self._retry = QTimer(self, singleShot=True, interval=RETRY_MS, timeout=self._connect)

    def start(self) -> None:
        self._connect()

    def send(self, cmd: str, **fields) -> None:
        msg = {"cmd": cmd, **fields}
        if self.connected:
            self.socket.write((json.dumps(msg) + "\n").encode())
        else:
            self._pending.append(msg)  # e.g. --start before the daemon is up

    def _connect(self) -> None:
        # Try each candidate path in turn; a missing path fails immediately.
        path = self.paths[self._try % len(self.paths)]
        self._try += 1
        self.socket.abort()
        self.socket.connectToServer(str(path))

    def _on_connected(self) -> None:
        self.connected = True
        self._try -= 1  # stick with this path next time
        self.connection_changed.emit(True)
        for msg in self._pending:
            self.socket.write((json.dumps(msg) + "\n").encode())
        self._pending.clear()

    def _on_disconnected(self) -> None:
        if self.connected:
            self.connected = False
            self._buffer = b""
            self.connection_changed.emit(False)
        self._retry.start()

    def _on_error(self, _error) -> None:
        if not self.connected:
            # Cycle quickly through paths, then back off.
            if self._try % len(self.paths):
                QTimer.singleShot(0, self._connect)
            else:
                self._retry.start()

    def _on_ready_read(self) -> None:
        self._buffer += bytes(self.socket.readAll())
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            try:
                self.message.emit(json.loads(line))
            except ValueError:
                pass


# -- window -----------------------------------------------------------------


def _label(text: str = "", size: int = 32, color: str = "fg", bold: bool = False, align=Qt.AlignmentFlag.AlignCenter) -> QLabel:
    label = QLabel(text)
    label.setAlignment(align)
    label.setWordWrap(True)
    label.setProperty("fb_size", size)
    label.setProperty("fb_color", color)
    label.setProperty("fb_bold", bold)
    return label


class SetupWindow(QWidget):
    command = Signal(str, dict)  # cmd, extra fields

    def __init__(self, scale: float = 1.0):
        super().__init__()
        self.scale = scale
        self.setWindowTitle("TV remote setup")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)

        self.state: dict | None = None
        self.connected = False
        self.wait_for_daemon = False  # --start: show "waiting" instead of staying hidden
        self.done_callback = None  # called when the window hides after a setup session
        self._seen_setup = False
        self._combo_deadline: float | None = None
        self._combo_total = 5.0
        self._idle_deadline: float | None = None
        self._status_at = 0.0
        self._result_until = 0.0

        self.pages = QStackedWidget()
        self.page_waiting = self._build_waiting()
        self.page_combo = self._build_combo()
        self.page_setup = self._build_setup()
        self.page_result = self._build_result()
        for page in (self.page_waiting, self.page_combo, self.page_setup, self.page_result):
            self.pages.addWidget(page)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*(int(64 * scale),) * 4)
        outer.addWidget(self.pages)

        self._tick = QTimer(self, interval=100, timeout=self._on_tick)
        self._tick.start()
        self._restyle()

    # -- pages --

    def _build_waiting(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.addStretch()
        box.addWidget(_label("Waiting for the fire-blaster service…", 56, bold=True))
        self.waiting_detail = _label("", 28, "muted")
        box.addWidget(self.waiting_detail)
        box.addStretch()
        return page

    def _build_combo(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.addStretch()
        self.combo_title = _label("Keep holding", 56, bold=True)
        box.addWidget(self.combo_title)
        self.combo_count = _label("5", 180, "accent", bold=True)
        box.addWidget(self.combo_count)
        self.combo_bar = QProgressBar(textVisible=False, maximum=1000)
        self.combo_bar.setFixedWidth(int(900 * self.scale))
        box.addWidget(self.combo_bar, alignment=Qt.AlignmentFlag.AlignHCenter)
        box.addWidget(_label("to set up TV control", 32, "muted"))
        box.addStretch()
        return page

    def _build_setup(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)

        header = QHBoxLayout()
        header.addWidget(_label("TV remote setup", 30, "muted", align=Qt.AlignmentFlag.AlignLeft))
        self.idle_label = _label("", 30, "muted", align=Qt.AlignmentFlag.AlignRight)
        header.addWidget(self.idle_label)
        box.addLayout(header)
        box.addStretch(2)

        # Brands run left to right (Left/Right keys) ...
        strip = QHBoxLayout()
        strip.setSpacing(int(16 * self.scale))
        strip.addStretch()
        self.brand_left = _label("◀", 40, "muted")
        strip.addWidget(self.brand_left)
        self.brand_chips = [_label("", 44) for _ in range(BRAND_STRIP)]
        for chip in self.brand_chips:
            chip.setWordWrap(False)
            strip.addWidget(chip)
        self.brand_right = _label("▶", 40, "muted")
        strip.addWidget(self.brand_right)
        strip.addStretch()
        box.addLayout(strip)
        box.addSpacing(int(48 * self.scale))

        # ... and the current brand's code sets run top to bottom (Up/Down).
        self.code_up = _label("▲", 28, "muted")
        box.addWidget(self.code_up)
        self.code_rows = [_label("", 40) for _ in range(CODESET_ROWS)]
        for row in self.code_rows:
            row.setWordWrap(False)
            row.setMinimumWidth(int(760 * self.scale))
            box.addWidget(row, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.code_down = _label("▼", 28, "muted")
        box.addWidget(self.code_down)
        box.addSpacing(int(16 * self.scale))
        self.position_label = _label("", 28, "muted")
        box.addWidget(self.position_label)
        box.addStretch(3)

        self.status_label = _label("", 36, "accent")
        box.addWidget(self.status_label)
        box.addSpacing(int(32 * self.scale))

        hints = QHBoxLayout()
        hints.setSpacing(int(16 * self.scale))
        hints.addStretch()
        for text, cmd, fields in (
            ("◀  ▶   Brand", "next_brand", {}),
            ("▲  ▼   Code set", "next_code", {}),
            ("Vol · Mute · Power   Test", "test", {"function": "VOLUME_UP"}),
            ("OK   Save", "accept", {}),
            ("Back   Cancel", "cancel", {}),
        ):
            button = QPushButton(text)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _=False, c=cmd, f=fields: self.command.emit(c, f))
            hints.addWidget(button)
        hints.addStretch()
        box.addLayout(hints)
        box.addSpacing(int(16 * self.scale))
        self.current_label = _label("", 26, "muted")
        box.addWidget(self.current_label)
        return page

    def _build_result(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.addStretch()
        self.result_title = _label("", 96, "ok", bold=True)
        self.result_detail = _label("", 44)
        box.addWidget(self.result_title)
        box.addWidget(self.result_detail)
        box.addStretch()
        return page

    def _restyle(self) -> None:
        s = self.scale
        c = COLORS
        sheet = [
            f"SetupWindow {{ background: {c['bg']}; }}",
            f"QLabel {{ color: {c['fg']}; }}",
            f"QPushButton {{ color: {c['fg']}; background: {c['chip']}; border: none; border-radius: {int(12 * s)}px;"
            f" padding: {int(14 * s)}px {int(28 * s)}px; font-size: {int(26 * s)}px; }}",
            f"QPushButton:hover {{ background: {c['accent']}; }}",
            f"QProgressBar {{ background: {c['chip']}; border: none; border-radius: {int(8 * s)}px; max-height: {int(16 * s)}px; }}",
            f"QProgressBar::chunk {{ background: {c['accent']}; border-radius: {int(8 * s)}px; }}",
        ]
        for label in self.findChildren(QLabel):
            label.setStyleSheet(self._label_css(label))
        self.setStyleSheet("\n".join(sheet))

    def _label_css(self, label: QLabel, color: str | None = None, background: str | None = None) -> str:
        size = int(label.property("fb_size") * self.scale)
        css = f"font-size: {size}px; color: {COLORS[color or label.property('fb_color')]};"
        if label.property("fb_bold"):
            css += " font-weight: 700;"
        if background:
            css += f" background: {COLORS[background]}; border-radius: {int(10 * self.scale)}px; padding: {int(8 * self.scale)}px {int(20 * self.scale)}px;"
        return css

    # -- daemon messages --

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        if not connected:
            self.state = None
            self._combo_deadline = self._idle_deadline = None
        self._update_visibility()

    def on_message(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "state":
            self.apply_state(msg)
        elif event == "tx":
            self.status_label.setText(f"Sent {function_name(msg['function'])}. Did the TV react?")
            self._status_at = time.monotonic()
            self.status_label.setStyleSheet(self._label_css(self.status_label, "accent"))
        elif event in ("saved", "cancelled"):
            self.show_result(event, msg.get("profile"))
        elif event == "error":
            self.status_label.setText(msg.get("message", "error"))
            self._status_at = time.monotonic()
            self.status_label.setStyleSheet(self._label_css(self.status_label, "warn"))
        self._update_visibility()

    def apply_state(self, state: dict) -> None:
        self.state = state
        now = time.monotonic()
        combo = state["combo"]
        self._combo_total = combo["hold_seconds"]
        self._combo_deadline = now + combo["remaining"] if combo["remaining"] is not None else None
        self.combo_title.setText("Keep holding " + " + ".join(key_label(k) for k in combo["keys"]))
        self.current_label.setText(f"Current: {profile_label(state['active'])}")

        setup = state.get("setup")
        if setup:
            self._seen_setup = True
            cand = setup["candidate"]
            self.position_label.setText(
                f"Code set {setup['brand_position'] + 1} of {setup['brand_count']} for {cand['brand']}"
                f"  ·  brand {setup['brand_index'] + 1} of {len(setup['brands'])}"
            )
            self._fill_brand_strip(setup["brands"], setup["brand_index"])
            self._fill_codesets(setup["brand_codesets"], setup["brand_position"])
            timeout = setup["idle_timeout"]
            self._idle_deadline = now + timeout if timeout is not None else None
        else:
            self._idle_deadline = None
        self._on_tick()

    def _window(self, count: int, current: int, size: int) -> int:
        """First index of a `size`-long window over `count` items, centred on `current`."""
        return max(0, min(current - size // 2, count - size))

    def _fill_brand_strip(self, brands: list[str], current: int) -> None:
        start = self._window(len(brands), current, BRAND_STRIP)
        for i, chip in enumerate(self.brand_chips):
            j = start + i
            chip.setVisible(j < len(brands))
            if j < len(brands):
                chip.setText(brands[j])
                selected = j == current
                chip.setStyleSheet(self._label_css(chip, "bg" if selected else "muted", "accent" if selected else "bg"))
        # Left/Right wrap, so the arrows always apply when there's more than one brand.
        for arrow in (self.brand_left, self.brand_right):
            arrow.setVisible(len(brands) > 1)

    def _fill_codesets(self, names: list[str], current: int) -> None:
        start = self._window(len(names), current, CODESET_ROWS)
        for i, row in enumerate(self.code_rows):
            j = start + i
            row.setVisible(j < len(names))
            if j < len(names):
                row.setText(names[j])
                selected = j == current
                row.setStyleSheet(self._label_css(row, "bg" if selected else "fg", "accent" if selected else "chip"))
        for arrow in (self.code_up, self.code_down):
            # Keep the space so the layout doesn't jump between brands.
            arrow.setText(("▲" if arrow is self.code_up else "▼") if len(names) > 1 else " ")

    def show_result(self, event: str, profile: dict | None) -> None:
        if event == "saved":
            self.result_title.setText("✓  Saved")
            self.result_title.setStyleSheet(self._label_css(self.result_title, "ok"))
            self.result_detail.setText(f"The remote now controls: {profile_label(profile)}")
        else:
            self.result_title.setText("Setup cancelled")
            self.result_title.setStyleSheet(self._label_css(self.result_title, "muted"))
            self.result_detail.setText(f"Keeping: {profile_label(profile)}")
        self._result_until = time.monotonic() + RESULT_SECONDS
        self.status_label.clear()

    # -- timers and visibility --

    def _on_tick(self) -> None:
        now = time.monotonic()
        if self._combo_deadline is not None:
            left = max(0.0, self._combo_deadline - now)
            self.combo_count.setText(str(int(left) + 1) if left > 0 else "0")
            self.combo_bar.setValue(int(1000 * (1 - left / self._combo_total)) if self._combo_total else 1000)
        if self._idle_deadline is not None:
            left = max(0, int(self._idle_deadline - now + 0.999))
            self.idle_label.setText(f"Closes in {left} s")
        if self._status_at and now - self._status_at > STATUS_FADE_SECONDS:
            self.status_label.setStyleSheet(self._label_css(self.status_label, "muted"))
            self._status_at = 0.0
        if self._result_until and now >= self._result_until:
            self._result_until = 0.0
            self._update_visibility()

    def _update_visibility(self) -> None:
        now = time.monotonic()
        state = self.state
        if self._result_until > now:
            page = self.page_result
        elif state and state["mode"] == "setup":
            page = self.page_setup
        elif state and self._combo_deadline is not None:
            page = self.page_combo
        elif not self.connected and self.wait_for_daemon:
            page = self.page_waiting
        else:
            page = None

        if page is None:
            if self.isVisible():
                self.hide()
            if self._seen_setup and self.done_callback:
                self.done_callback()
            return
        self.pages.setCurrentWidget(page)
        if not self.isVisible():
            self.showFullScreen()
            self.raise_()
            self.activateWindow()

    # -- keyboard (when focused) --

    def keyPressEvent(self, event: QKeyEvent) -> None:
        k = Qt.Key
        mapping = {
            k.Key_Right: ("next_brand", {}),
            k.Key_Left: ("prev_brand", {}),
            k.Key_Down: ("next_code", {}),
            k.Key_Up: ("prev_code", {}),
            k.Key_Return: ("accept", {}),
            k.Key_Enter: ("accept", {}),
            k.Key_Escape: ("cancel", {}),
            k.Key_Backspace: ("cancel", {}),
            k.Key_Plus: ("test", {"function": "VOLUME_UP"}),
            k.Key_Equal: ("test", {"function": "VOLUME_UP"}),
            k.Key_Minus: ("test", {"function": "VOLUME_DOWN"}),
            k.Key_M: ("test", {"function": "MUTE_TOGGLE"}),
            k.Key_P: ("test", {"function": "POWER_TOGGLE"}),
        }
        if event.key() in mapping:
            cmd, fields = mapping[event.key()]
            self.command.emit(cmd, fields)
        else:
            super().keyPressEvent(event)


# -- demo -------------------------------------------------------------------


def demo_state(mode: str = "setup", index: int = 2, combo_remaining: float | None = None) -> dict:
    """A plausible daemon snapshot, for --demo and screenshots."""
    candidates = [
        ("amazon-4495", "LG", "LG TV"),
        ("amazon-4496", "LG", "Code Group 2"),
        ("amazon-4497", "LG", "Code Group 3"),
        ("amazon-5513", "LG", "JP Code Group 1"),
        ("amazon-744", "Samsung", "Samsung"),
        ("amazon-4517", "Sony", "Older models"),
        ("amazon-4518", "Sony", "Bravia 2015+"),
        ("amazon-4801", "Vizio", "Vizio"),
        ("amazon-4900", "TCL", "Roku TV"),
        ("amazon-4901", "Hisense", "Hisense"),
        ("amazon-4902", "Insignia", "Fire TV Edition"),
    ]
    brands = list(dict.fromkeys(b for _, b, _ in candidates))
    pid, brand, name = candidates[index]
    in_brand = [c for c in candidates if c[1] == brand]
    setup = {
        "index": index,
        "count": 118,
        "candidate": {"id": pid, "brand": brand, "name": name},
        "brands": brands,
        "brand_index": brands.index(brand),
        "brand_position": [c[0] for c in in_brand].index(pid),
        "brand_count": len(in_brand),
        "brand_codesets": [c[2] for c in in_brand],
        "idle_timeout": 42.0,
        "test_function": "VOLUME_UP",
    }
    return {
        "event": "state",
        "mode": mode,
        "active": {"id": "amazon-4495", "brand": "LG", "name": "LG TV"},
        "combo": {"keys": ["KEY_BACK", "KEY_KPENTER"], "hold_seconds": 5.0, "remaining": combo_remaining},
        "functions": ["MUTE_TOGGLE", "POWER_TOGGLE", "VOLUME_DOWN", "VOLUME_UP"],
        "setup": setup if mode == "setup" else None,
    }


def run_demo(window: SetupWindow, app: QApplication) -> None:
    window.set_connected(True)
    steps = [(0.0, lambda: window.on_message(demo_state("normal", combo_remaining=5.0)))]
    t = 5.0
    for i in range(0, 5):
        steps.append((t, lambda i=i: window.on_message(demo_state("setup", i))))
        steps.append((t + 0.1, lambda: window.on_message({"event": "tx", "function": "VOLUME_UP", "profile": None})))
        t += 1.5
    steps.append((t, lambda: window.on_message({"event": "saved", "profile": {"id": "x", "brand": "Sony", "name": "Bravia 2015+"}})))
    steps.append((t + 0.01, lambda: window.on_message(demo_state("normal"))))
    steps.append((t + RESULT_SECONDS + 0.5, app.quit))
    for at, fn in steps:
        QTimer.singleShot(int(at * 1000), fn)


# -- main -------------------------------------------------------------------


def _instance_name() -> str:
    return f"fire-blaster-setup-{os.getuid()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fireblaster-setup", description=__doc__.splitlines()[0])
    parser.add_argument("--start", action="store_true", help="ask the daemon to start setup mode now")
    parser.add_argument("--socket", type=Path, help="daemon control socket (default: search the usual places)")
    parser.add_argument("--demo", action="store_true", help="scripted walkthrough without a daemon")
    parser.add_argument("--windowed", action="store_true", help="normal window instead of fullscreen (debugging)")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("fireblaster-setup")
    app.setDesktopFileName("fireblaster-setup")

    if not args.demo:
        # Single instance: hand --start to a running overlay and exit.
        probe = QLocalSocket()
        probe.connectToServer(_instance_name())
        if probe.waitForConnected(300):
            probe.write(b"start\n" if args.start else b"ping\n")
            probe.waitForBytesWritten(300)
            probe.disconnectFromServer()
            return 0

    screen = QGuiApplication.primaryScreen()
    scale = max(0.5, screen.geometry().height() / 1080) if screen else 1.0
    window = SetupWindow(scale)
    if args.windowed:
        window.showFullScreen = window.show  # type: ignore[method-assign]
        window.resize(int(1600 * scale / 1.5), int(900 * scale / 1.5))

    if args.demo:
        run_demo(window, app)
        return app.exec()

    client = ControlClient([args.socket] if args.socket else client_socket_paths())
    client.message.connect(window.on_message)
    client.connection_changed.connect(window.set_connected)
    window.command.connect(lambda cmd, fields: client.send(cmd, **fields))

    def request_start() -> None:
        window.wait_for_daemon = True
        window._update_visibility()
        client.send("start_setup")

    def stop_waiting() -> None:
        window.wait_for_daemon = False

    window.done_callback = stop_waiting

    server = QLocalServer()
    QLocalServer.removeServer(_instance_name())  # stale name from a crash
    server.listen(_instance_name())

    def on_instance() -> None:
        conn = server.nextPendingConnection()
        conn.readyRead.connect(lambda: request_start() if b"start" in bytes(conn.readAll()) else None)

    server.newConnection.connect(on_instance)

    client.start()
    if args.start:
        request_start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
