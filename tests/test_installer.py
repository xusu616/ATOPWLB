#!/usr/bin/env python3
"""Exercise installation against temporary hardware, tools, and manager stubs."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
SHELL = os.environ.get("ATOPWLB_TEST_SHELL", "/bin/sh")
ORIGINAL_CONFIG = "old configuration must survive an unsuccessful install\n"


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="atopwlb-install-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.module = self.directory / "module"
        self.module.mkdir()
        self.state = self.directory / "state"
        self.state.mkdir()
        self.config = self.state / "config.conf"
        self.config.write_text(ORIGINAL_CONFIG, encoding="utf-8")
        self.sysfs = self.directory / "power_supply"
        self.battery = self.sysfs / "battery"
        self.battery.mkdir(parents=True)
        for attribute, value in {
            "type": "Battery", "present": "1", "capacity": "50",
            "status": "Discharging",
        }.items():
            (self.battery / attribute).write_text(value + "\n", encoding="utf-8")
        self.events = self.directory / "events"
        self.events.touch()
        self.reader_pid = self.directory / "reader.pid"
        self.svc_calls = self.directory / "svc-calls"
        self.getevent = self.directory / "getevent"
        self.getevent.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-pl" ]; then\n'
            '  echo KEY_VOLUMEUP\n'
            '  [ "$MOCK_CAPS" = up_only ] || echo KEY_VOLUMEDOWN\n'
            "  exit 0\n"
            "fi\n"
            'printf \'%s\\n\' "$$" > "$MOCK_READER_PID"\n'
            'cat "$MOCK_EVENTS"\n'
            "exec /bin/sleep 3600\n",
            encoding="utf-8",
        )
        self.getevent.chmod(0o755)
        self.svc = self.directory / "svc"
        self.svc.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$MOCK_SVC_CALLS"\n'
            'if [ "$MOCK_SHUTDOWN" != unsupported ]; then\n'
            "  echo 'usage: svc power shutdown'\n"
            "else\n"
            "  echo 'usage: svc power stayon'\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        self.svc.chmod(0o755)

        # Substitute only absolute device paths. All detection and installation
        # control flow remains the actual code shipped in the module.
        common = (REPOSITORY / "common.sh").read_text(encoding="utf-8")
        common = common.replace("/sys/class/power_supply", str(self.sysfs))
        common = common.replace("/system/bin/svc", str(self.svc))
        (self.module / "common.sh").write_text(common, encoding="utf-8")
        installer = (REPOSITORY / "customize.sh").read_text(encoding="utf-8")
        installer = installer.replace("/system/bin/sh", "/bin/sh")
        installer = installer.replace("/system/bin/getevent", str(self.getevent))
        installer = installer.replace("/data/adb/atopwlb", str(self.state))
        (self.module / "customize.sh").write_text(installer, encoding="utf-8")
        for name in ("service.sh", "monitor.sh", "module.prop"):
            (self.module / name).touch()

    def run_installer(
        self, events: str = "", *, bootmode: str = "true", uid: str = "0",
        caps: str = "both", shutdown: str = "supported",
    ) -> subprocess.CompletedProcess[str]:
        self.events.write_text(events, encoding="utf-8")
        harness = (
            "ui_print() { printf '%s\\n' \"$*\"; }\n"
            "abort() { printf '%s\\n' \"$*\"; exit 77; }\n"
            "set_perm_recursive() { return 0; }\n"
            "set_perm() { return 0; }\n"
            "chown() { return 0; }\n"
            "id() { printf '%s\\n' \"$MOCK_UID\"; }\n"
            # Accelerate the timer at its I/O boundary without replacing the
            # actual sixty-iteration timeout or any key selection logic.
            "sleep() { /bin/sleep 0.01; }\n"
            f". {shlex.quote(str(self.module / 'customize.sh'))}\n"
        )
        environment = os.environ | {
            "MODPATH": str(self.module), "BOOTMODE": bootmode,
            "MOCK_UID": uid, "MOCK_CAPS": caps, "MOCK_SHUTDOWN": shutdown,
            "MOCK_EVENTS": str(self.events), "MOCK_READER_PID": str(self.reader_pid),
            "MOCK_SVC_CALLS": str(self.svc_calls),
        }
        result = subprocess.run(
            [SHELL, "-c", harness], env=environment, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        self.assertEqual(list(self.module.glob(".volume-*")), [])
        if self.reader_pid.exists() and sys.platform.startswith("linux"):
            self.assertFalse((Path("/proc") / self.reader_pid.read_text().strip()).exists())
        if self.svc_calls.exists():
            self.assertEqual(self.svc_calls.read_text(), "power\n")
        return result

    def assert_install_success(self, result: subprocess.CompletedProcess[str], threshold: int) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn(f"THRESHOLD={threshold}\n", text)
        self.assertIn("ENABLED=1\n", text)
        self.assertIn(str(self.config), result.stdout)
        self.assertIn("安装不执行真实关机测试", result.stdout)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertTrue(self.reader_pid.exists())

    def assert_install_rejected(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 77, result.stdout + result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), ORIGINAL_CONFIG)

    def test_volume_up_selects_twenty(self) -> None:
        result = self.run_installer("/dev/input/event6: EV_KEY KEY_VOLUMEUP DOWN\n")
        self.assert_install_success(result, 20)

    def test_volume_down_selects_ten(self) -> None:
        result = self.run_installer("/dev/input/event0: EV_KEY KEY_VOLUMEDOWN DOWN\n")
        self.assert_install_success(result, 10)

    def test_key_release_is_ignored(self) -> None:
        result = self.run_installer(
            "/dev/input/event0: EV_KEY KEY_VOLUMEDOWN UP\n"
            "/dev/input/event6: EV_KEY KEY_VOLUMEUP DOWN\n"
        )
        self.assert_install_success(result, 20)

    def test_key_repeat_is_ignored(self) -> None:
        result = self.run_installer(
            "/dev/input/event6: EV_KEY KEY_VOLUMEUP REPEAT\n"
            "/dev/input/event0: EV_KEY KEY_VOLUMEDOWN DOWN\n"
        )
        self.assert_install_success(result, 10)

    def test_timeout_rejects_without_default(self) -> None:
        result = self.run_installer()
        self.assert_install_rejected(result)
        self.assertIn("60 秒内没有收到音量键选择", result.stdout)
        self.assertTrue(self.reader_pid.exists())

    def test_battery_detection_failure_preserves_config(self) -> None:
        (self.battery / "capacity").write_text("broken\n", encoding="utf-8")
        result = self.run_installer()
        self.assert_install_rejected(result)
        self.assertFalse(self.reader_pid.exists())
        self.assertFalse(self.svc_calls.exists())

    def test_battery_detection_failure_does_not_create_config(self) -> None:
        self.config.unlink()
        (self.battery / "capacity").write_text("broken\n", encoding="utf-8")
        result = self.run_installer()
        self.assertEqual(result.returncode, 77, result.stdout + result.stderr)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.reader_pid.exists())

    def test_unsupported_shutdown_rejects(self) -> None:
        result = self.run_installer(shutdown="unsupported")
        self.assert_install_rejected(result)
        self.assertFalse(self.reader_pid.exists())

    def test_missing_volume_down_rejects(self) -> None:
        result = self.run_installer(caps="up_only")
        self.assert_install_rejected(result)
        self.assertFalse(self.reader_pid.exists())

    def test_recovery_install_rejects(self) -> None:
        result = self.run_installer(bootmode="false")
        self.assert_install_rejected(result)
        self.assertFalse(self.reader_pid.exists())
        self.assertFalse(self.svc_calls.exists())

    def test_unprivileged_install_rejects(self) -> None:
        result = self.run_installer(uid="2000")
        self.assert_install_rejected(result)
        self.assertFalse(self.reader_pid.exists())
        self.assertFalse(self.svc_calls.exists())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--shell", default=SHELL)
    arguments, remaining = parser.parse_known_args()
    SHELL = arguments.shell
    unittest.main(argv=[sys.argv[0], *remaining])
