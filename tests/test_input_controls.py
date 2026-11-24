"""Input selector and favorite availability across amplifier power states."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
import test_volume_controls as fixture


class InputControlAvailabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.VolumeControlTests.setUpClass()

    def setUp(self):
        self.f = fixture.VolumeControlTests()
        self.addCleanup(self.f.doCleanups)
        self.w = self.f.window()
        entry = lambda label: dict(Name=label, NameOverride='', Hidden=False, Favorite=True)
        self.w.input_settings.update({'hdmi1': entry('TV'), 'optical1': entry('Other')})
        self.w._build_favorites_from_config()
        self.f.app.processEvents()
        self.assertEqual(len(self.w.favorite_buttons), 2)

    def reported(self, power):
        # Only the reported amplifier power changes between these checks.
        self.w.connected = True
        self.w.last_status = {'power': power, 'volume': 100, 'max_volume': 154,
                              'mute': False, 'input': 'hdmi1'}
        self.w.update_control_states()
        self.f.app.processEvents()

    def controls(self):
        return {'power': self.w.power_button, 'input selector': self.w.input_combo,
                'volume down': self.w.volume_down, 'volume up': self.w.volume_up,
                'mute': self.w.mute_button,
                **{f'favorite {name}': button for name, button in self.w.favorite_buttons.items()}}

    def test_standby_disables_the_input_selector_and_favorites(self):
        self.reported('on')
        for name in ('power', 'input selector', 'favorite hdmi1', 'favorite optical1'):
            with self.subTest(control=name):
                self.assertTrue(self.controls()[name].isEnabled())
        self.reported('standby')
        for name in ('input selector', 'favorite hdmi1', 'favorite optical1',
                     'volume down', 'volume up', 'mute'):
            with self.subTest(control=name):
                self.assertFalse(self.controls()[name].isEnabled())
        # Waking the amplifier stays available; it is the only way out of standby.
        self.assertTrue(self.controls()['power'].isEnabled())

    def test_waking_the_amplifier_enables_the_input_controls_again(self):
        self.reported('standby')
        self.reported('on')
        for name in ('input selector', 'favorite hdmi1', 'favorite optical1'):
            with self.subTest(control=name):
                self.assertTrue(self.controls()[name].isEnabled())

    def test_rebuilt_input_controls_follow_the_reported_state(self):
        # Input discovery may rebuild the selector and favorites at any time.
        self.reported('standby')
        self.w._build_favorites_from_config()
        self.w.update_control_states()
        self.f.app.processEvents()
        self.assertFalse(self.w.input_combo.isEnabled())
        self.assertTrue(all(not button.isEnabled() for button in self.w.favorite_buttons.values()))

    def test_offline_or_unknown_status_disables_everything(self):
        self.reported('on')
        self.w.connected = False
        self.w.last_status = None
        self.w.update_control_states()
        for name, widget in self.controls().items():
            with self.subTest(control=name):
                self.assertFalse(widget.isEnabled())

    def test_standby_input_controls_cannot_reach_the_amplifier(self):
        self.reported('standby')
        with patch.object(self.w, 'run_action') as run:
            for button in self.w.favorite_buttons.values():
                QTest.mousePress(button, Qt.LeftButton)
                QTest.mouseRelease(button, Qt.LeftButton)
            QTest.mousePress(self.w.input_combo, Qt.LeftButton)
            QTest.mouseRelease(self.w.input_combo, Qt.LeftButton)
            self.assertFalse(self.w.input_combo.view().isVisible())
            run.assert_not_called()

    def test_input_controls_still_reach_the_amplifier_while_it_is_on(self):
        self.reported('on')
        with patch.object(self.w, 'run_action') as run:
            self.w.favorite_buttons['optical1'].click()
            run.assert_called_once()
