"""Development-only regressions; the standalone remote does not import tests."""
import hashlib
from pathlib import Path
import runpy
import unittest

ROOT = Path(__file__).resolve().parents[1]
REMOTE = runpy.run_path(str(ROOT / "YamahaRemote.pyw"), run_name="test_remote")


class ConfigurationTests(unittest.TestCase):
    def test_missing_optional_settings_use_defaults(self):
        self.assertEqual(REMOTE["validate_config"]({}, allow_incomplete=True), REMOTE["DEFAULT_CONFIG"])

    def test_all_boolean_settings_reject_nonbooleans(self):
        self.assertEqual(set(REMOTE["BOOLEAN_CONFIG_KEYS"]),
                         {key for key, value in REMOTE["DEFAULT_CONFIG"].items() if type(value) is bool})
        for key in REMOTE["BOOLEAN_CONFIG_KEYS"]:
            for invalid in (0, 1, "true", None):
                with self.subTest(key=key, invalid=invalid):
                    with self.assertRaises(REMOTE["ConfigError"]):
                        REMOTE["validate_config"]({key: invalid}, allow_incomplete=True)

    def test_unknown_settings_and_missing_host_are_rejected(self):
        with self.assertRaisesRegex(REMOTE["ConfigError"], "Unknown configuration"):
            REMOTE["validate_config"]({"UnknownSetting": False}, allow_incomplete=True)
        with self.assertRaises(REMOTE["ConfigError"]):
            REMOTE["validate_config"]({})

    def test_db_settings_remain_explicit_and_numeric(self):
        for document in ({"VolumeDb": True}, {"VolumeDb": True, "VolumeDbAtZero": -97.0},
                         {"VolumeDb": True, "VolumeDbStepSize": 0.5},
                         {"VolumeDbStepSize": 0}, {"VolumeDbStepSize": -1},
                         {"VolumeDbAtZero": True}, {"VolumeDbStepSize": float('nan')}):
            with self.subTest(document=document):
                with self.assertRaises(REMOTE["ConfigError"]):
                    REMOTE["validate_config"](document, allow_incomplete=True)
        result = REMOTE["validate_config"]({"VolumeDb": True, "VolumeDbAtZero": -97, "VolumeDbStepSize": 0.5}, allow_incomplete=True)
        api = REMOTE["YamahaApi"]("127.0.0.1", result)
        self.assertEqual(api.format_volume(100), "-47.0 dB")

    def test_tray_dependency_and_ip_validation_unchanged(self):
        for document in ({"CloseToTray": True, "SystemTray": False},
                         {"Host": "192.168.1.1000"}, {"Host": "300.1.1.1"}):
            with self.subTest(document=document):
                with self.assertRaises(REMOTE["ConfigError"]):
                    REMOTE["validate_config"](document, allow_incomplete=True)
        for host in ("192.168.1.100", "::1", "receiver.local"):
            self.assertEqual(REMOTE["validate_config"]({"Host": host})["Host"], host)


class EndpointTests(unittest.TestCase):
    def test_generic_endpoint_names_match_protocol(self):
        suffix = hashlib.sha256(str(Path.home()).encode("utf-8")).hexdigest()[:16]
        for role in ("tv", "amplifier"):
            for calibration in (False, True):
                expected = f"remote-control.{role}{'.calibration' if calibration else ''}.v1.{suffix}"
                self.assertEqual(REMOTE["ipc_server_name"](role, calibration=calibration), expected)
        self.assertEqual(REMOTE["IPC_SERVER_NAME"], REMOTE["ipc_server_name"]("amplifier"))


if __name__ == "__main__":
    unittest.main()
