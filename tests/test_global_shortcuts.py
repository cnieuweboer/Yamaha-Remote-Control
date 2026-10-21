"""Configurable native ownership without a Windows desktop or real amplifier."""
import copy
import ctypes
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import Qt
import test_configurable_shortcuts as fixture

R = fixture.REMOTE
G = R['validate_config'].__globals__


class GlobalConfigurationTests(unittest.TestCase):
    def config(self, bindings=None, **extra):
        raw = R['_default_config_copy']()
        raw['Host'] = 'amplifier.local'
        if bindings is not None:
            raw['GlobalShortcuts'] = bindings
        raw.update(extra)
        return R['validate_config'](raw)

    def test_default_global_bindings_disabled_independently_of_focused(self):
        config = self.config()
        self.assertEqual(config['GlobalShortcuts'], dict(VolumeUp=[], VolumeDown=[], Mute=[]))
        self.assertTrue(config['Shortcuts']['VolumeUp'])
        self.assertNotIn('GlobalVolumeHotkeys', config)

    def test_partial_empty_and_independent_profiles(self):
        config = self.config({'VolumeUp':['Ctrl+Alt+Up'], 'Mute':''})
        self.assertEqual(config['GlobalShortcuts']['Mute'], [])
        self.assertEqual(config['GlobalShortcuts']['VolumeDown'], [])
        config = self.config({'VolumeUp':['Volume Up']})
        self.assertEqual(config['GlobalShortcuts']['VolumeUp'], config['Shortcuts']['VolumeUp'])

    def test_invalid_global_bindings_rejected_before_restart(self):
        for bindings in ({'Play':['A']}, {'Mute':['A'], 'VolumeUp':['A']},
                         {'VolumeUp':['F35']}, {'Mute':['Ctrl+K, Ctrl+C']},
                         {'Mute':['Shift']}, {'VolumeUp':'A'}, {'Mute':['Num+F1']}):
            with self.subTest(bindings=bindings), self.assertRaises(R['ConfigError']):
                self.config(bindings)

    def test_native_alias_collision_rejected_during_validation(self):
        with self.assertRaises(R['ConfigError']):
            self.config({'VolumeUp':['Num+Enter'], 'VolumeDown':['Enter']})

    def test_retired_toggle_migrates_once_and_explicit_bindings_win(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.json'
            raw = R['_default_config_copy']()
            raw.pop('GlobalShortcuts')
            raw.update(Host='amp.local', GlobalVolumeHotkeys=True)
            path.write_text(json.dumps(raw))
            with patch.dict(G, CONFIG_PATH=path):
                config = R['load_config']()
            self.assertEqual(config['GlobalShortcuts'], R['DEFAULT_SHORTCUTS'])
            saved = json.loads(path.read_text())
            self.assertNotIn('GlobalVolumeHotkeys', saved)
            self.assertEqual(saved['GlobalShortcuts'], config['GlobalShortcuts'])
        self.assertFalse(any(self.config({}, GlobalVolumeHotkeys=True)['GlobalShortcuts'].values()))

    def test_native_modifier_and_keypad_translation(self):
        native = R['_native_binding']
        self.assertEqual(native(Qt.Key_A.value | Qt.ControlModifier.value), (65, Qt.ControlModifier.value))
        self.assertEqual(native(Qt.Key_7.value | Qt.KeypadModifier.value), (0x67, 0))
        self.assertEqual(native(Qt.Key_Enter.value | Qt.KeypadModifier.value), (13, Qt.KeypadModifier.value))
        self.assertEqual(native(Qt.Key_MediaNext.value), (0xB0, 0))


class GlobalOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.FocusedBindingTests.setUpClass()

    def setUp(self):
        self.fixture = fixture.FocusedBindingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.w = self.fixture.window
        self.g = self.fixture.g
        self.app = self.fixture.app
        self.configure(VolumeUp=['A'], VolumeDown=['Ctrl+Alt+Down'], Mute=['F9'])
        self.w._hotkey_state = (True, 123, time.monotonic(), False)

    def configure(self, **bindings):
        config = copy.deepcopy(self.w.config)
        config['GlobalShortcuts'] = bindings
        config = self.g['validate_config'](config, allow_incomplete=True)
        self.w._global_bindings = self.g['_compile_shortcuts'](config, 'GlobalShortcuts')
        self.w._global_native_bindings = {self.g['_native_binding'](code): command
                                        for code, command in self.w._global_bindings.items()}

    def test_global_binding_wins_over_focused_and_works_outside_window(self):
        self.fixture.configure(Mute=['A'])
        command = self.w._resolve_native_volume(65, 0)
        self.assertEqual(command[-1], 'volume_up')
        self.assertTrue(self.w._accept_native_hotkey(command, 456))
        self.app.processEvents()
        self.w.adjust_volume.assert_called_once_with(1, background=True, batch_volume=True)
        self.w.toggle_mute.assert_not_called()

    def test_focused_physical_binding_remains_available_without_global_match(self):
        command = self.w._resolve_native_volume(0xAF, 0)
        self.assertEqual(command, 'volume_up')
        self.assertFalse(self.w._accept_native_hotkey(command, 456))
        self.assertTrue(self.w._accept_native_hotkey(command, 123))

    def test_global_disabled_offline_stale_modal_or_standby(self):
        for state in ((False, 123, time.monotonic(), False),
                      (True, 123, time.monotonic()-7, False),
                      (True, 123, time.monotonic(), True)):
            self.w._hotkey_state = state
            self.assertIsNone(self.w._resolve_native_volume(65, 0))
        self.w.last_status['power'] = 'standby'
        self.w._publish_hotkey_state()
        self.assertIsNone(self.w._resolve_native_volume(65, 0))

    def test_global_transport_binding_remains_owned_without_tv_permission(self):
        self.configure(VolumeUp=['Media Next'], VolumeDown=['Volume Down'])
        self.assertEqual(self.w._resolve_native_volume(0xB0, 0)[-1], 'volume_up')
        self.assertEqual(self.w._resolve_native_volume(0xAE, 0)[-1], 'volume_down')

    def test_state_publishes_only_active_bindings(self):
        self.configure(VolumeUp=['Media Next'], Mute=['F9'])
        self.w._native_volume_hook = SimpleNamespace(installed=True, stop=Mock())
        self.assertEqual(self.w._active_global_codes(), [Qt.Key_MediaNext.value, Qt.Key_F9.value])
        self.w.last_status['power'] = 'standby'
        self.assertEqual(self.w._active_global_codes(), [])

    def test_actual_hook_handles_custom_key_repeat_release_and_overflow(self):
        hook = self.g['_WindowsVolumeHook'](self.w._accept_native_hotkey, resolve=self.w._resolve_native_volume)
        hook._user32 = SimpleNamespace(GetForegroundWindow=lambda:456,
            GetAsyncKeyState=lambda key:0, CallNextHookEx=Mock(return_value=17))
        hook._notify = Mock()
        data = self.g['_LowLevelKey'](65,0,0,0,0)
        send = lambda message: hook._handle_key(0,message,ctypes.addressof(data))
        for _ in range(100):
            self.assertEqual(send(0x100),1)
        self.assertEqual(len(self.w._pending_hotkeys), self.g['MAX_PENDING_HOTKEYS'])
        self.w._hotkey_state = (False,123,None,False)
        self.assertEqual(send(0x101),1)
        self.assertEqual(send(0x100),17)
        self.assertEqual(send(0x101),17)
        hook._notify.assert_not_called()

    def test_global_mute_repeat_keeps_press_ownership(self):
        state = self.g['_VolumeKeyState']()
        command = self.w._resolve_native_volume(0x78,0)
        accepted = Mock(return_value=True)
        state.press(0x78,command,accepted)
        state.press(0x78,command,accepted)
        accepted.assert_called_once()
        self.assertTrue(state.release(0x78))
    def test_real_cross_repository_state_ownership_and_disconnect(self):
        import runpy
        import uuid
        other = Path(__file__).resolve().parents[2]/'Sony-Remote-Control/SonyRemote.pyw'
        if not other.exists():
            self.skipTest('Optional cross-repository check requires the TV checkout beside this repo')
        consumer = runpy.run_path(str(other),run_name='cross_repository_client')
        client_type = consumer['AmplifierClient']
        client_globals = client_type.__init__.__globals__
        name = 'remote-control.test.'+uuid.uuid4().hex
        self.configure(VolumeUp=['Media Next'], Mute=['F9'])
        self.w._native_volume_hook = SimpleNamespace(installed=True,stop=Mock())
        with patch.dict(self.g,IPC_SERVER_NAME=name), patch.dict(client_globals,IPC_SERVER_NAME=name), \
             patch.object(self.w,'_update_polling_state'):
            self.w._setup_ipc()
            server = self.w.amplifier_server
            self.assertTrue(server.available)
            self.addCleanup(server.close)
            client = client_type()
            self.addCleanup(client.stop)
            def until(condition):
                deadline = time.monotonic()+2
                while not condition() and time.monotonic()<deadline:
                    self.app.processEvents()
                    time.sleep(.002)
                self.assertTrue(condition())
            client.start()
            until(lambda: client.state is not None)
            self.assertEqual(client.state['global_shortcuts'],[Qt.Key_MediaNext.value,Qt.Key_F9.value])
            self.assertIsNotNone(self.w._resolve_native_volume(0xB0,0))
            self.w.last_status['input'] = 'pc'
            self.w._publish_hotkey_state()
            client.set_focused_volume(456,[Qt.Key_MediaNext.value],'tv',time.monotonic())
            until(lambda: bool(self.w._focused_tv_overrides))
            command = self.w._resolve_native_volume(0xB0,0)
            self.assertFalse(self.w._accept_native_hotkey(command,456))
            self.assertTrue(self.w._accept_native_hotkey(command,789))
            self.w._pending_hotkeys.clear()
            client.set_focused_volume(0,[],'tv',time.monotonic())
            until(lambda: not self.w._focused_tv_overrides)
            self.assertTrue(self.w._accept_native_hotkey(command,456))
            self.w._pending_hotkeys.clear()
            client.set_focused_volume(456,[Qt.Key_MediaNext.value],'tv',time.monotonic())
            until(lambda: bool(self.w._focused_tv_overrides))
            self.w.last_status['power'] = 'standby'
            server.publish()
            until(lambda: client.state is not None and not client.state['global_shortcuts'])
            self.w.last_status['power'] = 'on'
            server.publish()
            until(lambda: client.state is not None and bool(client.state['global_shortcuts']))
            client.stop()
            until(lambda: not server.clients)
            self.assertFalse(self.w._focused_tv_overrides)
            self.assertEqual(self.w._resolve_native_volume(0xB0,0)[-1],'volume_up')
            client.start()
            until(lambda: client.state is not None)
            server.close()
            until(lambda: client.state is None)
