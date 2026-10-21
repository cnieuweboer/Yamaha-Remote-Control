"""Authoritative, leased TV focus exceptions to amplifier global ownership."""
import ctypes
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import Qt
import test_global_shortcuts as fixture


class FocusedOverrideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.GlobalOwnershipTests.setUpClass()

    def setUp(self):
        self.fixture = fixture.GlobalOwnershipTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.w, self.g, self.app = self.fixture.w, self.fixture.g, self.fixture.app
        self.w._native_volume_hook = SimpleNamespace(installed=True,stop=Mock())
        self.w.last_status['input'] = 'pc'
        self.context = (456,(Qt.Key_A.value,), 'tv', time.monotonic()+6)
        self.w.amplifier_server = SimpleNamespace(focused_volume={'peer':self.context},publish=Mock())
        self.w._publish_hotkey_state()

    def test_matching_global_key_yields_only_to_actual_tv_foreground(self):
        grant = self.w._focused_tv_grant(self.context)
        self.assertEqual(grant['shortcuts'],[Qt.Key_A.value])
        command = self.w._resolve_native_volume(65,0)
        self.assertFalse(self.w._accept_native_hotkey(command,456))
        self.assertFalse(self.w._pending_hotkeys)
        self.assertTrue(self.w._accept_native_hotkey(command,789))
        self.assertEqual(list(self.w._pending_hotkeys),['volume_up'])

    def test_other_global_bindings_stay_owned(self):
        command = self.w._resolve_native_volume(0x78,0)
        self.assertEqual(command[-1],'mute')
        self.assertTrue(self.w._accept_native_hotkey(command,456))

    def test_tv_input_standby_offline_calibration_or_expiry_never_grants(self):
        for changes in ({'input':'tv'},{'input':None},{'power':'standby'}):
            with self.subTest(changes=changes),patch.dict(self.w.last_status,changes):
                self.assertIsNone(self.w._focused_tv_grant(self.context))
        self.w.connected = False
        self.assertIsNone(self.w._focused_tv_grant(self.context))
        self.w.connected = True
        self.w.calibration_active = True
        self.assertIsNone(self.w._focused_tv_grant(self.context))
        self.w.calibration_active = False
        self.assertIsNone(self.w._focused_tv_grant((*self.context[:3],time.monotonic()-1)))

    def test_native_deadline_is_enforced_without_any_new_message(self):
        command = self.w._resolve_native_volume(65,0)
        self.w._focused_tv_overrides = ((456,time.monotonic()-1,frozenset(((65,0),))),)
        self.assertTrue(self.w._accept_native_hotkey(command,456))

    def test_withdrawal_and_input_change_revoke_native_exception(self):
        command = self.w._resolve_native_volume(65,0)
        self.w.amplifier_server.focused_volume.clear()
        self.w._publish_hotkey_state()
        self.assertTrue(self.w._accept_native_hotkey(command,456))
        self.w.amplifier_server.focused_volume['peer'] = self.context
        self.w._publish_hotkey_state()
        self.assertFalse(self.w._accept_native_hotkey(command,456))
        self.w.last_status['input'] = 'tv'
        self.w._publish_hotkey_state()
        self.assertTrue(self.w._accept_native_hotkey(command,456))

    def test_actual_hook_does_not_acquire_a_yielded_held_key_after_focus_changes(self):
        foreground = [456]
        hook = self.g['_WindowsVolumeHook'](self.w._accept_native_hotkey,resolve=self.w._resolve_native_volume)
        hook._user32 = SimpleNamespace(GetForegroundWindow=lambda:foreground[0],
            GetAsyncKeyState=lambda key:0,CallNextHookEx=Mock(return_value=17))
        hook._notify = Mock()
        data = self.g['_LowLevelKey'](65,0,0,0,0)
        send = lambda kind:hook._handle_key(0,kind,ctypes.addressof(data))
        self.assertEqual(send(0x100),17)
        foreground[0] = 789
        self.assertEqual(send(0x100),17)
        self.assertEqual(send(0x101),17)
        self.assertEqual(send(0x100),1)
        foreground[0] = 456
        self.assertEqual(send(0x100),1)
        self.assertEqual(send(0x101),1)
        hook._notify.assert_not_called()

    def test_native_aliases_and_unsupported_focused_keys_are_safe(self):
        self.fixture.configure(VolumeUp=['Ctrl++'])
        with patch.dict(self.g,sys=SimpleNamespace(platform='linux')):
            grant = self.w._focused_tv_grant((456,(Qt.Key_Plus.value|Qt.ControlModifier.value,
                                                  Qt.Key_F35.value),'tv',time.monotonic()+6))
        self.assertEqual(grant['shortcuts'],[Qt.Key_Plus.value|Qt.ControlModifier.value])

    def test_context_parser_accepts_withdrawals_and_rejects_invalid_values(self):
        import test_amplifier_protocol as protocol
        base = {'version':1,'type':'focused_volume','window':456,
                'shortcuts':[Qt.Key_A.value],'input':'tv','valid_for_ms':6000}
        for changes in ({},{'window':0,'shortcuts':[]}, {'window':True},
                        {'valid_for_ms':6001},{'valid_for_ms':True},
                        {'shortcuts':[True]},{'shortcuts':[Qt.Key_A.value]*2},
                        {'input':None}):
            with self.subTest(changes=changes):
                socket = protocol.MemorySocket(dict(base,**changes))
                server = SimpleNamespace(clients={socket:bytearray()},focused_volume={},changed=Mock())
                self.g['AmplifierServer']._read(server,socket)
                if not changes or changes.get('window') == 0:
                    socket.abort.assert_not_called()
                    server.changed.assert_called_once()
                    self.assertEqual(bool(server.focused_volume),not bool(changes))
                else:
                    socket.abort.assert_called_once()

    def test_disconnect_removes_context_and_republishes_policy(self):
        socket = Mock()
        server = SimpleNamespace(clients={socket:bytearray()},focused_volume={socket:self.context},changed=Mock())
        self.g['AmplifierServer']._remove(server,socket)
        self.assertFalse(server.focused_volume)
        server.changed.assert_called_once()
