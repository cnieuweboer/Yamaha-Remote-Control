"""Clicks must not enter native window movement; actual drags still can."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import test_volume_controls as fixture


class WindowDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.VolumeControlTests.setUpClass()

    def setUp(self):
        self.f = fixture.VolumeControlTests()
        self.addCleanup(self.f.doCleanups)
        self.w = self.f.window()
        self.w.activateWindow()
        self.f.app.processEvents()
        self.move_patch = patch.object(QWindow, 'startSystemMove', return_value=True)
        self.move = self.move_patch.start()
        self.addCleanup(self.move_patch.stop)

    def send_move(self, widget, point, buttons=Qt.LeftButton):
        event = QMouseEvent(QEvent.MouseMove, QPointF(point), QPointF(widget.mapToGlobal(point)),
                            Qt.NoButton, buttons, Qt.NoModifier)
        QApplication.sendEvent(widget, event)

    def test_clicks_on_both_drag_surfaces_preserve_button_hover(self):
        for widget, point in ((self.w, QPoint(1, 1)),
                              (self.w.volume_value, self.w.volume_value.rect().center())):
            with self.subTest(surface=type(widget).__name__):
                QTest.mouseClick(widget, Qt.LeftButton, pos=point)
                self.move.assert_not_called()
                self.w.volume_up.setEnabled(True)
                QTest.mouseMove(self.w.volume_up, self.w.volume_up.rect().center())
                self.f.app.processEvents()
                self.assertTrue(self.w.volume_up.underMouse())
                self.assertTrue(self.w.volume_up.toolTip())

    def test_only_movement_beyond_threshold_starts_a_drag(self):
        for widget, origin in ((self.w, QPoint(1, 1)),
                               (self.w.volume_value, self.w.volume_value.rect().center())):
            with self.subTest(surface=type(widget).__name__):
                self.move.reset_mock()
                QTest.mousePress(widget, Qt.LeftButton, pos=origin)
                self.move.assert_not_called()
                distance = QApplication.startDragDistance()
                self.send_move(widget, origin + QPoint(distance - 1, 0))
                self.move.assert_not_called()
                self.send_move(widget, origin + QPoint(distance, 0))
                self.move.assert_called_once()
                self.send_move(widget, origin + QPoint(distance + 2, 0))
                self.move.assert_called_once()
                QTest.mouseRelease(widget, Qt.LeftButton, pos=origin)

    def test_release_and_focus_loss_cancel_pending_drags(self):
        label = self.w.volume_value
        origin = label.rect().center()
        for ending in ('release', 'deactivate', 'hide'):
            with self.subTest(ending=ending):
                QTest.mousePress(label, Qt.LeftButton, pos=origin)
                if ending == 'release':
                    QTest.mouseRelease(label, Qt.LeftButton, pos=origin)
                else:
                    QApplication.sendEvent(label, QEvent(QEvent.WindowDeactivate if ending == 'deactivate' else QEvent.Hide))
                self.send_move(label, origin + QPoint(QApplication.startDragDistance(), 0))
                self.move.assert_not_called()
                QTest.mouseRelease(label, Qt.LeftButton, pos=origin)

    def test_buttons_and_group_background_do_not_start_window_drags(self):
        for widget in (self.w.volume_up, self.w.volume_value.parentWidget()):
            origin = widget.rect().center()
            QTest.mousePress(widget, Qt.LeftButton, pos=origin)
            self.send_move(widget, origin + QPoint(QApplication.startDragDistance(), 0))
            QTest.mouseRelease(widget, Qt.LeftButton, pos=origin)
        self.move.assert_not_called()

    def test_right_click_does_not_arm_a_drag(self):
        label = self.w.volume_value
        origin = label.rect().center()
        QTest.mousePress(label, Qt.RightButton, pos=origin)
        self.send_move(label, origin + QPoint(QApplication.startDragDistance(), 0), Qt.RightButton)
        QTest.mouseRelease(label, Qt.RightButton, pos=origin)
        self.move.assert_not_called()


if __name__ == '__main__':
    unittest.main()
