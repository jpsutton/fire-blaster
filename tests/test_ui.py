import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fireblaster import ui  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    w = ui.SetupWindow(1.0)
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    w.set_connected(True)
    yield w
    w.close()


def test_hidden_until_something_happens(window):
    window.on_message(ui.demo_state("normal"))
    assert not window.isVisible()


def test_combo_countdown_page(window):
    window.on_message(ui.demo_state("normal", combo_remaining=2.4))
    assert window.isVisible() and window.pages.currentWidget() is window.page_combo
    assert window.combo_title.text() == "Keep holding Back + OK"
    assert window.combo_count.text() == "3"
    window.on_message(ui.demo_state("normal"))
    assert not window.isVisible()


def test_setup_page_follows_state(window):
    window.on_message(ui.demo_state("setup", 6))  # Sony / Bravia 2015+
    assert window.pages.currentWidget() is window.page_setup
    assert window.position_label.text().startswith("Code set 2 of 2 for Sony")
    shown = [c.text() for c in window.brand_chips if not c.isHidden()]
    assert "Sony" in shown and len(shown) == ui.BRAND_STRIP
    rows = [r.text() for r in window.code_rows if not r.isHidden()]
    assert rows == ["Older models", "Bravia 2015+"]
    assert window.code_up.text() == "▲"

    window.on_message(ui.demo_state("setup", 7))  # Vizio: one code set, no up/down arrows
    assert [r.text() for r in window.code_rows if not r.isHidden()] == ["Vizio"]
    assert window.code_up.text() == " "
    assert window.idle_label.text() == "Closes in 42 s"

    window.on_message({"event": "tx", "function": "MUTE_TOGGLE", "profile": None})
    assert window.status_label.text() == "Sent Mute. Did the TV react?"


def test_result_then_hide(window):
    window.on_message(ui.demo_state("setup", 0))
    window.on_message({"event": "saved", "profile": {"id": "a", "brand": "Sony", "name": "Sony"}})
    window.on_message(ui.demo_state("normal"))
    assert window.pages.currentWidget() is window.page_result
    assert window.result_detail.text() == "The remote now controls: Sony"
    window._result_until = 0.001  # expire
    window._on_tick()
    assert not window.isVisible()


def test_waiting_page_only_when_requested(window):
    window.set_connected(False)
    assert not window.isVisible()
    window.wait_for_daemon = True
    window._update_visibility()
    assert window.pages.currentWidget() is window.page_waiting


def test_keyboard_commands(window):
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent

    sent = []
    window.command.connect(lambda cmd, fields: sent.append((cmd, fields)))
    for key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_M, Qt.Key.Key_Return, Qt.Key.Key_Escape):
        window.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))
    assert sent == [
        ("next_brand", {}),
        ("next_code", {}),
        ("test", {"function": "MUTE_TOGGLE"}),
        ("accept", {}),
        ("cancel", {}),
    ]


def test_labels():
    assert ui.key_label("KEY_KPENTER") == "OK"
    assert ui.key_label("KEY_MENU") == "Menu"
    assert ui.function_name("INPUT_SCROLL") == "Input Scroll"
    assert ui.profile_label(None) == "no TV selected"
    assert ui.profile_label({"brand": "LG", "name": "LG TV"}) == "LG / LG TV"


def test_remote_setup_page_and_closed_result(window):
    state = ui.demo_state("normal")
    state.update(mode="remote_setup", remote_setup={"idle_timeout": 30.0})
    window.on_message(state)
    assert window.isVisible() and window.pages.currentWidget() is window.page_remote
    assert window.remote_idle_label.text() == "Closes in 30 s"
    window.on_message({"event": "remote_setup_closed", "reason": "timeout"})
    window.on_message(ui.demo_state("normal"))
    assert window.pages.currentWidget() is window.page_result
    assert window.result_title.text() == "Remote setup closed"


def test_remote_setup_shows_heard_button(window):
    state = ui.demo_state("normal")
    match = {"presses": 2, "unknown": 0, "last": {"code": "pd32:20DF02FD", "function": "DPAD_UP"},
             "brands": [{"brand": "LG", "codesets": 2}, {"brand": "Haier", "codesets": 1}], "candidates": 3,
             "best": {"id": "amazon-4495", "brand": "LG", "name": "LG TV"}}
    state.update(mode="remote_setup", remote_setup={"idle_timeout": 30.0, "index_ready": True, "match": match})
    window.on_message(state)
    assert window.remote_heard.text() == "Heard: Up"
    assert window.remote_brands.text() == "Looks like: LG / LG TV  (or 2 similar code sets)"


def test_remote_setup_prompts_follow_the_wizard(window):
    state = ui.demo_state("normal")
    match = {"presses": 3, "unknown": 0, "last": None, "brands": [], "candidates": None, "best": None,
             "step": "teach", "teach": "BACK"}
    state.update(mode="remote_setup", remote_setup={"idle_timeout": 30.0, "index_ready": True, "match": match})
    window.on_message(state)
    assert window.remote_prompt.text() == "Press the button you use for Back.  OK skips it."
    window.on_message({"event": "remote_setup_closed", "reason": "saved", "remote": "LG remote"})
    assert window.result_detail.text() == "The LG remote now works this PC."
