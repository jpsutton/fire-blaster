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
