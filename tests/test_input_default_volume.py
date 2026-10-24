"""Configured volume presets follow confirmed transitions, never reconnections."""
import json
from pathlib import Path
import runpy
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import test_volume_controls as controls

R = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'YamahaRemote.pyw'), run_name='input_defaults_tests')

class ConfigurationTests(unittest.TestCase):
    def config(self, **changes):
        raw = R['_default_config_copy']()
        raw.update(changes)
        return R['validate_config'](raw, allow_incomplete=True)

    def test_raw_db_inheritance_and_explicit_opt_out(self):
        config = self.config(GlobalDefaultVolume=96, Inputs={'tv': {'DefaultVolume': '-51.5 dB'},
                                                           'pc': {'DefaultVolume': False}, 'other': {}})
        self.assertEqual(R['_default_volume_raw'](config['Inputs']['tv']['DefaultVolume'], config), 91)
        self.assertIsNone(R['_default_volume_raw'](False, config))
        self.assertEqual(config['Inputs']['other']['DefaultVolume'], '')
        self.assertFalse(config['StartupDefaultVolume'])
        self.assertEqual(R['_default_volume_raw']('-51.6dB', config), 91)
        config['VolumeDbStepSize'] = 0.1
        self.assertEqual(R['_default_volume_raw']('-51.55dB', config), 455)

    def test_invalid_values_and_explicit_db_conversion_settings(self):
        for value in (True, -1, 1.5, '96', 'bad', [], None, '-1000dB', 'NaNdB'):
            with self.subTest(value=value), self.assertRaises(R['ConfigError']):
                self.config(GlobalDefaultVolume=value)
        for key in ('VolumeDbAtZero', 'VolumeDbStepSize'):
            raw = R['_default_config_copy']()
            raw['Inputs'] = {'tv': {'DefaultVolume': '-51.5dB'}}
            del raw[key]
            with self.assertRaisesRegex(R['ConfigError'], key):
                R['validate_config'](raw, allow_incomplete=True)
        with self.assertRaises(R['ConfigError']):
            self.config(StartupDefaultVolume='true')

    def test_api_limit_rejects_without_clamping(self):
        config = self.config(GlobalDefaultVolume=155)
        with self.assertRaisesRegex(R['ConfigError'], 'above the API maximum'):
            R['_validate_default_volumes'](config, 154)

    def test_load_adds_missing_settings_and_retains_preferences(self):
        raw = {'Host': 'device.local', 'Inputs': {'tv': {'Name': 'TV', 'Favorite': True}}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'YamahaRemote.json'
            path.write_text(json.dumps(raw))
            with patch.dict(R['load_config'].__globals__, {'CONFIG_PATH': path}):
                R['load_config']()
            saved = json.loads(path.read_text())
            self.assertEqual(saved['GlobalDefaultVolume'], '')
            self.assertFalse(saved['StartupDefaultVolume'])
            self.assertEqual(saved['Inputs']['tv']['DefaultVolume'], '')
            self.assertTrue(saved['Inputs']['tv']['Favorite'])

    def test_optional_recheck_suffix_and_raw_string_without_db_settings(self):
        for value, target, delay in (('93 +2', 93, 2.0), ('-51.5 dB +1.5', 91, 1.5),
                                      ('-51.5dB+.25', 91, .25), ('93 +0', 93, 0.0)):
            config = self.config(GlobalDefaultVolume=value)
            self.assertEqual(R['_default_volume_raw'](value, config), target)
            self.assertEqual(R['_default_volume_parts'](value)[1], delay)
        config = R['validate_config']({'GlobalDefaultVolume': '93 +2'}, allow_incomplete=True)
        self.assertEqual(R['_default_volume_raw']('93 +2', config), 93)
        for value in ('93 +-2', '-51dB +NaN', '93 +1 +2', '93 +999999999999', '-1 +2', '93.5 +2'):
            with self.subTest(value=value), self.assertRaises(R['ConfigError']):
                self.config(GlobalDefaultVolume=value)

class TransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        controls.VolumeControlTests.setUpClass()

    def setUp(self):
        fixture = controls.VolumeControlTests()
        self.addCleanup(fixture.doCleanups)
        self.w = fixture.window()
        self.w.config['GlobalDefaultVolume'] = 96
        self.w.config['Inputs'] = {'tv': {'DefaultVolume': 80}, 'disabled': {'DefaultVolume': False}}
        self.w.run_action = Mock(return_value=True)
        self.w.api.set_volume = Mock()
        self.w.show_error = Mock()

    def observe(self, power='on', input='tv', request_started_at=None, **changes):
        status = dict(power=power, input=input, volume=20, max_volume=154, mute=True)
        status.update(changes)
        self.w._apply_default_volume(status, request_started_at=request_started_at)
        return status

    def assert_target(self, target):
        self.w.run_action.assert_called_once()
        self.w.run_action.call_args.args[0]()
        self.w.api.set_volume.assert_called_once_with(target)
        self.assertEqual(self.w.run_action.call_args.kwargs['refresh_groups'], {'main'})

    def test_initial_connection_then_external_input_transition_once(self):
        self.observe(input='pc')
        self.w.run_action.assert_not_called()
        self.observe(input='tv')
        self.observe(input='tv')
        self.assert_target(80)

    def test_global_fallback_opt_out_and_already_correct_volume(self):
        self.observe()
        self.observe(input='disabled')
        self.w.run_action.assert_not_called()
        self.observe(input='pc', volume=96)
        self.w.run_action.assert_not_called()
        self.observe(input='other')
        self.assert_target(96)

    def test_startup_requires_confirmed_standby_and_toggle(self):
        self.observe('standby', input=None, max_volume=None)
        self.observe()
        self.w.run_action.assert_not_called()
        self.w.config['StartupDefaultVolume'] = True
        self.observe('standby', input=None, max_volume=None)
        self.observe()
        self.observe()
        self.assert_target(80)

    def test_reconnect_and_unconfirmed_status_do_not_apply_startup(self):
        self.w.config['StartupDefaultVolume'] = True
        self.observe('standby')
        self.w.set_offline()
        self.observe()
        self.w.run_action.assert_not_called()
        self.observe('standby')
        self.observe('unknown')
        self.observe()
        self.w.run_action.assert_not_called()
        self.observe('standby')
        self.observe(input=None)
        self.observe()
        self.w.run_action.assert_not_called()

    def test_calibration_does_not_apply_or_defer_defaults(self):
        self.observe(input='pc')
        self.w.calibration_active = True
        self.observe()
        self.w.calibration_active = False
        self.observe()
        self.w.run_action.assert_not_called()

    def test_bad_device_limit_warns_once_without_command(self):
        self.w.config['GlobalDefaultVolume'] = 155
        self.observe(input='pc')
        self.observe()
        self.observe()
        self.w.show_error.assert_called_once()
        self.w.run_action.assert_not_called()

    def test_invalid_edit_is_rejected_before_restart_using_known_range(self):
        raw = R['_default_config_copy']()
        raw.update(Host='device.local', GlobalDefaultVolume=155)
        self.w.connected = True
        self.w.last_status = {'max_volume': 154}
        self.w._config_digest = b'old'
        self.w._watch_config_paths = Mock()
        self.w._restart_for_config_change = Mock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(raw))
            globals_ = type(self.w).__init__.__globals__
            with patch.dict(globals_, {'CONFIG_PATH': path}), patch.object(globals_['QMessageBox'], 'warning') as warning:
                self.w._check_external_config_change()
            self.w._restart_for_config_change.assert_not_called()
            warning.assert_called_once()

    def test_status_poll_drives_transition_and_failure_breaks_continuity(self):
        self.w.initialized = True
        self.w._speaker_outputs = ()
        self.w.discovery.tick = Mock()
        self.w.polling.allowed = Mock(return_value=True)
        self.w.polling.busy = Mock(return_value=False)
        self.w.handle_request_error = Mock()
        def worker(function, on_result=None, on_error=None, on_finished=None, **kwargs):
            try:
                result = function()
            except Exception as error:
                on_error(error)
            else:
                on_result(result)
        self.w.run_worker = worker
        def status(input):
            return dict(power='on', input=input, volume=20, max_volume=154, mute=True)
        self.w.api.get_status = Mock(return_value=status('pc'))
        self.w.poll_status(groups={'main'})
        self.w.api.get_status.return_value = status('tv')
        self.w.poll_status(groups={'main'})
        self.assert_target(80)
        self.w.run_action.reset_mock()
        self.w.api.get_status.side_effect = R['DeviceResponseError']('invalid status')
        self.w.poll_status(groups={'main'})
        self.w.api.get_status.side_effect = None
        self.w.api.get_status.return_value = status('pc')
        self.w.poll_status(groups={'main'})
        self.w.run_action.assert_not_called()

    def arm_recheck(self, value='80 +2'):
        self.w.config['Inputs']['tv']['DefaultVolume'] = value
        self.observe(input='pc')
        self.w.run_action.reset_mock()
        self.observe(input='tv')
        return self.w._default_volume_recheck['deadline']

    def test_immediate_then_one_changed_volume_reapply_from_fresh_request(self):
        deadline = self.arm_recheck()
        self.assertEqual(self.w.run_action.call_count, 1)
        self.observe(volume=60, request_started_at=deadline - .1)
        self.assertEqual(self.w.run_action.call_count, 1)
        self.observe(volume=60, request_started_at=deadline)
        self.assertEqual(self.w.run_action.call_count, 2)
        self.w.run_action.call_args.args[0]()
        self.w.api.set_volume.assert_called_once_with(80)
        self.assertIsNone(self.w._default_volume_recheck)
        self.assertFalse(self.w.default_volume_timer.isActive())
        self.observe(volume=60, request_started_at=deadline + 1)
        self.assertEqual(self.w.run_action.call_count, 2)

    def test_unchanged_volume_needs_no_second_command(self):
        deadline = self.arm_recheck()
        self.observe(volume=80, request_started_at=deadline)
        self.assertEqual(self.w.run_action.call_count, 1)
        self.assertIsNone(self.w._default_volume_recheck)

    def test_already_matching_initial_volume_still_gets_one_delayed_check(self):
        self.w.config['Inputs']['tv']['DefaultVolume'] = '80 +2'
        self.observe(input='pc')
        self.observe(volume=80)
        self.w.run_action.assert_not_called()
        deadline = self.w._default_volume_recheck['deadline']
        self.observe(volume=60, request_started_at=deadline)
        self.assert_target(80)

    def test_timer_requests_fresh_main_status_once(self):
        from PySide6.QtTest import QTest
        self.w.connected = True
        self.w.last_status = dict(power='on', input='tv', volume=80, max_volume=154, mute=False)
        self.w._request_poll = Mock()
        self.w.polling.allowed = Mock(return_value=True)
        self.arm_recheck('80 +0.03')
        QTest.qWait(80)
        self.w._request_poll.assert_called_once_with(groups={'main'})
        self.observe(volume=60, request_started_at=time.monotonic())
        self.assertEqual(self.w.run_action.call_count, 2)

    def test_explicit_controls_cancel_recheck(self):
        for command in (lambda: self.w.adjust_volume(1), lambda: self.w.toggle_mute()):
            self.arm_recheck()
            self.w.last_status = dict(power='on', input='tv', volume=80, max_volume=154, mute=False)
            command()
            self.assertIsNone(self.w._default_volume_recheck)
            self.assertFalse(self.w.default_volume_timer.isActive())

    def test_queued_native_command_cancels_without_touching_timer_on_hook_thread(self):
        self.arm_recheck()
        self.w._pending_hotkeys.append('volume_up')
        self.w._request_poll = Mock()
        self.w._request_default_volume_recheck()
        self.w._request_poll.assert_not_called()
        self.assertIsNone(self.w._default_volume_recheck)

    def test_accepted_ipc_volume_command_cancels_before_queue_drains(self):
        self.arm_recheck()
        self.w._volume_hotkeys_eligible = Mock(return_value=True)
        self.w.last_status = {'input': 'tv'}
        self.assertTrue(self.w._accept_ipc_command('volume_up', 'tv')[0])
        self.assertIsNone(self.w._default_volume_recheck)

    def test_state_change_and_calibration_cancel_pending_recheck(self):
        self.arm_recheck()
        self.observe('standby')
        self.assertIsNone(self.w._default_volume_recheck)
        self.arm_recheck()
        self.observe(input='disabled')
        self.assertIsNone(self.w._default_volume_recheck)
        self.arm_recheck()
        self.w.set_offline()
        self.assertIsNone(self.w._default_volume_recheck)
        self.arm_recheck()
        self.w._calibration_changed(True)
        self.assertIsNone(self.w._default_volume_recheck)

    def test_failed_immediate_command_cancels_followup(self):
        self.arm_recheck()
        failed = self.w.run_action.call_args.kwargs['on_error']
        self.w.handle_request_error = Mock()
        failed(R['DeviceResponseError']('failed'))
        self.assertIsNone(self.w._default_volume_recheck)
        self.w.handle_request_error.assert_called_once()

    def test_plain_default_does_not_schedule_recheck(self):
        self.observe(input='pc')
        self.observe(input='tv')
        self.assertIsNone(self.w._default_volume_recheck)
        self.assertFalse(self.w.default_volume_timer.isActive())

    def test_native_intent_cancels_even_if_queue_has_already_been_discarded(self):
        self.arm_recheck()
        with self.w._hotkey_lock:
            self.w._enqueue_hotkey_locked('volume_up')
            self.w._pending_hotkeys.clear()
        self.w._request_poll = Mock()
        self.w._request_default_volume_recheck()
        self.w._request_poll.assert_not_called()
        self.assertIsNone(self.w._default_volume_recheck)

    def test_suspended_polling_cancels_instead_of_enforcing_hours_later(self):
        self.arm_recheck()
        self.w.connected = True
        self.w.last_status = dict(power='on', input='tv')
        self.w.polling.allowed = Mock(return_value=False)
        self.w._request_poll = Mock()
        self.w._request_default_volume_recheck()
        self.assertIsNone(self.w._default_volume_recheck)
        self.w._request_poll.assert_not_called()
