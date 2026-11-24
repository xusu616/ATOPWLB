#!/usr/bin/env python3
"""Run the module's shell logic against temporary files, never real hardware."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
SHELL = os.environ.get("ATOPWLB_TEST_SHELL", "/bin/sh")


class ModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="atopwlb-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.sysfs = self.directory / "power_supply"
        self.sysfs.mkdir()
        self.config = self.directory / "config.conf"

    def supply(self, name: str, **attributes: str | int) -> Path:
        directory = self.sysfs / name
        directory.mkdir(exist_ok=True)
        for key, value in attributes.items():
            (directory / key).write_text(f"{value}\n", encoding="utf-8")
        return directory

    def battery(self, level: str | int = 20, status: str = "Discharging") -> Path:
        return self.supply(
            "battery", type="Battery", present=1, capacity=level, status=status
        )

    def write_config(self, text: str | None = None) -> None:
        if text is None:
            text = "ENABLED=1\nTHRESHOLD=20\nINTERVAL=30\n"
        self.config.write_text(text, encoding="utf-8")

    def run_shell(self, body: str) -> subprocess.CompletedProcess[str]:
        script = (
            f". {shlex.quote(str(REPOSITORY / 'common.sh'))}\n"
            f"SYSFS={shlex.quote(str(self.sysfs))}\n"
            f"CONFIG={shlex.quote(str(self.config))}\n"
            + body
        )
        return subprocess.run(
            [SHELL, "-c", script],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )

    def assert_shell_success(self, body: str) -> str:
        result = self.run_shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def sample(self) -> tuple[int, int]:
        output = self.assert_shell_success(
            'atop_detect_battery "$SYSFS" || { echo "$ATOP_ERROR"; exit 1; }\n'
            'atop_read_sample "$SYSFS" || { echo "$ATOP_ERROR"; exit 1; }\n'
            'printf "%s %s\\n" "$ATOP_LEVEL" "$ATOP_PLUGGED"\n'
        )
        level, plugged = output.split()
        return int(level), int(plugged)

    def should_shutdown(self, threshold: int) -> bool:
        self.write_config(f"ENABLED=1\nTHRESHOLD={threshold}\nINTERVAL=30\n")
        output = self.assert_shell_success(
            'atop_load_config "$CONFIG" || exit 1\n'
            'atop_detect_battery "$SYSFS" || exit 1\n'
            'atop_read_sample "$SYSFS" || exit 1\n'
            'if atop_should_shutdown; then echo yes; else echo no; fi\n'
        )
        return output == "yes"

    def test_prefers_canonical_battery_over_bms(self) -> None:
        self.battery(19)
        self.supply("bms", type="BMS", capacity=21)
        output = self.assert_shell_success(
            'atop_detect_battery "$SYSFS" || exit 1\n'
            'printf "%s\\n" "$ATOP_CAPACITY_FILE"\n'
        )
        self.assertEqual(output, str(self.sysfs / "battery" / "capacity"))

    def test_discovers_noncanonical_battery_name(self) -> None:
        self.supply("main_pack", type="Battery", present=1, capacity=17, status="Discharging")
        self.assertEqual(self.sample(), (17, 0))

    def test_bms_fallback_keeps_main_battery_status(self) -> None:
        self.supply("battery", type="Battery", present=1, status="Charging")
        self.supply("bms", type="BMS", capacity=9)
        self.assertEqual(self.sample(), (9, 1))

    def test_bms_alone_is_insufficient_for_charging_detection(self) -> None:
        self.supply("bms", type="BMS", capacity=9)
        output = self.assert_shell_success(
            'if atop_detect_battery "$SYSFS"; then exit 1; fi\n'
            '[ -n "$ATOP_ERROR" ] || exit 1\n'
            'echo rejected\n'
        )
        self.assertEqual(output, "rejected")

    def test_threshold_equality_triggers_for_both_choices(self) -> None:
        for threshold in (10, 20):
            with self.subTest(threshold=threshold):
                self.battery(threshold)
                self.assertTrue(self.should_shutdown(threshold))

    def test_above_threshold_does_not_trigger(self) -> None:
        for threshold in (10, 20):
            with self.subTest(threshold=threshold):
                self.battery(threshold + 1)
                self.assertFalse(self.should_shutdown(threshold))

    def test_zero_is_a_real_percentage_not_missing_data(self) -> None:
        self.battery(0)
        self.assertTrue(self.should_shutdown(10))

    def test_charging_status_blocks_shutdown(self) -> None:
        self.battery(5, "Charging")
        self.assertFalse(self.should_shutdown(10))

    def test_full_status_blocks_shutdown(self) -> None:
        self.battery(5, "Full")
        self.assertFalse(self.should_shutdown(10))

    def test_online_usb_blocks_shutdown_despite_discharging_status(self) -> None:
        self.battery(5)
        self.supply("usb", type="USB_CDP", online=1)
        self.assertFalse(self.should_shutdown(10))

    def test_online_supply_blocks_shutdown_while_charging_is_paused(self) -> None:
        self.battery(5, "Not charging")
        self.supply("wireless", type="Wireless", online=1)
        self.assertFalse(self.should_shutdown(10))

    def test_present_usb_blocks_shutdown_when_online_is_zero(self) -> None:
        self.supply("usb", type="USB_CDP", online=0, present=1)
        for status in ("Discharging", "Not charging"):
            with self.subTest(status=status):
                self.battery(5, status)
                self.assertEqual(self.sample(), (5, 1))
                self.assertFalse(self.should_shutdown(10))

    def test_present_only_supply_blocks_shutdown(self) -> None:
        self.supply("usb", type="USB_CDP", present=1)
        for status in ("Discharging", "Not charging"):
            with self.subTest(status=status):
                self.battery(5, status)
                self.assertEqual(self.sample(), (5, 1))
                self.assertFalse(self.should_shutdown(10))

    def test_paused_charging_without_external_state_source_is_rejected(self) -> None:
        self.battery(5, "Not charging")
        self.assert_shell_success(
            'atop_detect_battery "$SYSFS" || exit 1\n'
            'if atop_read_sample "$SYSFS"; then exit 1; fi\n'
        )

    def test_offline_supply_allows_shutdown_when_discharging(self) -> None:
        self.battery(5)
        self.supply("usb", type="USB_CDP", online=0)
        self.assertTrue(self.should_shutdown(10))

    def test_offline_absent_supply_allows_shutdown(self) -> None:
        self.supply("usb", type="USB_CDP", online=0, present=0)
        for status in ("Discharging", "Not charging"):
            with self.subTest(status=status):
                self.battery(5, status)
                self.assertEqual(self.sample(), (5, 0))
                self.assertTrue(self.should_shutdown(10))

    def test_bms_online_and_present_are_not_mistaken_for_external_power(self) -> None:
        self.battery(5)
        self.supply("bms", type="BMS", present=1, capacity=5)
        self.supply("fg_adc", type="BMS", online=1, present=1)
        self.assertTrue(self.should_shutdown(10))

    def test_invalid_battery_capacity_is_rejected(self) -> None:
        for value in ("", "-1", "101", "foo", "10.0", "10 20", "10\n20"):
            with self.subTest(value=value):
                self.battery(value)
                self.assert_shell_success(
                    'if atop_detect_battery "$SYSFS" && atop_read_sample "$SYSFS"; '
                    'then exit 1; fi\n'
                    '[ -n "$ATOP_ERROR" ] || exit 1\n'
                )

    def test_missing_capacity_is_rejected(self) -> None:
        self.supply("battery", type="Battery", status="Discharging")
        self.assert_shell_success('if atop_detect_battery "$SYSFS"; then exit 1; fi\n')

    def test_unknown_status_is_rejected(self) -> None:
        self.battery(5, "Unknown")
        self.assert_shell_success(
            'if atop_detect_battery "$SYSFS" && atop_read_sample "$SYSFS"; '
            'then exit 1; fi\n'
        )

    def test_absent_battery_is_rejected(self) -> None:
        directory = self.battery(5)
        (directory / "present").write_text("0\n", encoding="utf-8")
        self.assert_shell_success('if atop_detect_battery "$SYSFS"; then exit 1; fi\n')

    def test_online_failure_is_not_assumed_unplugged(self) -> None:
        self.battery(5)
        self.supply("usb", type="USB_CDP", online="invalid")
        self.assert_shell_success(
            'atop_detect_battery "$SYSFS" || exit 1\n'
            'if atop_read_sample "$SYSFS"; then exit 1; fi\n'
            '[ -n "$ATOP_ERROR" ] || exit 1\n'
        )

    def test_invalid_charger_present_stops_sampling_even_when_online_is_one(self) -> None:
        self.battery(5)
        for online in (0, 1):
            for present in ("", "invalid", "-1", "2", "0\n1"):
                with self.subTest(online=online, present=present):
                    self.supply("usb", type="USB_CDP", online=online, present=present)
                    self.assert_shell_success(
                        'atop_detect_battery "$SYSFS" || exit 1\n'
                        'if atop_read_sample "$SYSFS"; then exit 1; fi\n'
                        '[ -n "$ATOP_ERROR" ] || exit 1\n'
                    )

    def test_runtime_capacity_failure_does_not_reuse_old_sample(self) -> None:
        self.battery(5)
        self.assert_shell_success(
            'atop_detect_battery "$SYSFS" || exit 1\n'
            'atop_read_sample "$SYSFS" || exit 1\n'
            'printf "broken\\n" > "$ATOP_CAPACITY_FILE"\n'
            'if atop_read_sample "$SYSFS"; then exit 1; fi\n'
            '[ -n "$ATOP_ERROR" ] || exit 1\n'
        )

    def test_valid_config(self) -> None:
        self.write_config()
        output = self.assert_shell_success(
            'atop_load_config "$CONFIG" || { echo "$ATOP_ERROR"; exit 1; }\n'
            'printf "%s %s %s\\n" "$ATOP_ENABLED" "$ATOP_THRESHOLD" "$ATOP_INTERVAL"\n'
        )
        self.assertEqual(output, "1 20 30")

    def test_config_accepts_crlf_from_windows_editors(self) -> None:
        self.config.write_bytes(b"ENABLED=1\r\nTHRESHOLD=10\r\nINTERVAL=30\r\n")
        output = self.assert_shell_success(
            'atop_load_config "$CONFIG" || exit 1\n'
            'printf "%s\\n" "$ATOP_THRESHOLD"\n'
        )
        self.assertEqual(output, "10")

    def test_interval_accepts_documented_boundaries(self) -> None:
        for interval in (1, 3600):
            with self.subTest(interval=interval):
                self.write_config(f"ENABLED=1\nTHRESHOLD=20\nINTERVAL={interval}\n")
                self.assert_shell_success('atop_load_config "$CONFIG" || exit 1\n')

    def test_disabled_config_blocks_shutdown(self) -> None:
        self.battery(5)
        self.write_config("ENABLED=0\nTHRESHOLD=10\nINTERVAL=30\n")
        self.assert_shell_success(
            'atop_load_config "$CONFIG" || exit 1\n'
            'atop_detect_battery "$SYSFS" || exit 1\n'
            'atop_read_sample "$SYSFS" || exit 1\n'
            'if atop_should_shutdown; then exit 1; fi\n'
        )

    def test_invalid_configs_are_rejected(self) -> None:
        for content in (
            "",
            "ENABLED=1\nINTERVAL=30\n",
            "ENABLED=1\nTHRESHOLD=21\nINTERVAL=30\n",
            "ENABLED=2\nTHRESHOLD=20\nINTERVAL=30\n",
            "ENABLED=1\nTHRESHOLD=20\nINTERVAL=0\n",
            "ENABLED=1\nTHRESHOLD=20\nINTERVAL=3601\n",
            "ENABLED=1\nTHRESHOLD=20\nINTERVAL=030\n",
            "ENABLED=1\nTHRESHOLD=20\nINTERVAL=abc\n",
            "ENABLED=1\nTHRESHOLD=20\nTHRESHOLD=10\nINTERVAL=30\n",
            "ENABLED=1\nTHRESHOLD=20\nINTERVAL=30\nUNKNOWN=1\n",
        ):
            with self.subTest(content=content):
                self.write_config(content)
                self.assert_shell_success(
                    'if atop_load_config "$CONFIG"; then exit 1; fi\n'
                    '[ -n "$ATOP_ERROR" ] || exit 1\n'
                )

    def test_config_is_data_and_never_executed(self) -> None:
        marker = self.directory / "executed"
        self.write_config(
            f"ENABLED=1\nTHRESHOLD=$(touch {shlex.quote(str(marker))})\nINTERVAL=30\n"
        )
        self.assert_shell_success('if atop_load_config "$CONFIG"; then exit 1; fi\n')
        self.assertFalse(marker.exists())

    def test_probe_only_reads_svc_help(self) -> None:
        marker = self.directory / "arguments"
        svc = self.directory / "svc"
        svc.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(marker))}\n"
            "echo 'svc power shutdown'\n"
            "exit 1\n",
            encoding="utf-8",
        )
        svc.chmod(0o755)
        self.assert_shell_success(f"atop_probe_shutdown {shlex.quote(str(svc))}\n")
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["power"])

    def test_probe_rejects_svc_without_shutdown_support(self) -> None:
        svc = self.directory / "svc"
        svc.write_text("#!/bin/sh\necho 'svc power stayon'\n", encoding="utf-8")
        svc.chmod(0o755)
        self.assert_shell_success(
            f"if atop_probe_shutdown {shlex.quote(str(svc))}; then exit 1; fi\n"
        )

    def test_probe_rejects_abnormal_exit_despite_help_output(self) -> None:
        svc = self.directory / "svc"
        for exit_code in (2, 124, 127):
            with self.subTest(exit_code=exit_code):
                svc.write_text(
                    f"#!/bin/sh\necho 'svc power shutdown'\nexit {exit_code}\n",
                    encoding="utf-8",
                )
                svc.chmod(0o755)
                self.assert_shell_success(
                    f"if atop_probe_shutdown {shlex.quote(str(svc))}; then exit 1; fi\n"
                    '[ -n "$ATOP_ERROR" ] || exit 1\n'
                )

    def test_request_sends_one_framework_shutdown_without_fallback(self) -> None:
        marker = self.directory / "arguments"
        svc = self.directory / "svc"
        svc.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(marker))}\n"
            "exit 7\n",
            encoding="utf-8",
        )
        svc.chmod(0o755)
        result = self.run_shell(f"atop_request_shutdown {shlex.quote(str(svc))}\n")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["power shutdown"])

    def run_monitor(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [SHELL, str(REPOSITORY / "monitor.sh"), *arguments],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )

    def mock_svc(self) -> tuple[Path, Path]:
        marker = self.directory / "svc-arguments"
        svc = self.directory / "mock-svc"
        svc.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(marker))}\n"
            "echo 'svc power shutdown'\n"
            "exit 1\n",
            encoding="utf-8",
        )
        svc.chmod(0o755)
        return svc, marker

    def test_monitor_dry_run_never_requests_shutdown(self) -> None:
        self.battery(5)
        self.write_config("ENABLED=1\nTHRESHOLD=10\nINTERVAL=30\n")
        svc, marker = self.mock_svc()
        result = self.run_monitor(
            "--once", "--dry-run", "--sysfs-root", str(self.sysfs),
            "--config", str(self.config), "--svc", str(svc),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("power shutdown", marker.read_text(encoding="utf-8").splitlines())
        self.assertIn("low_battery", result.stdout)

    def test_monitor_check_never_requests_shutdown(self) -> None:
        self.battery(5)
        self.write_config("ENABLED=1\nTHRESHOLD=10\nINTERVAL=30\n")
        svc, marker = self.mock_svc()
        result = self.run_monitor(
            "--check", "--sysfs-root", str(self.sysfs),
            "--config", str(self.config), "--svc", str(svc),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("power shutdown", marker.read_text(encoding="utf-8").splitlines())

    def test_monitor_custom_paths_cannot_activate_real_shutdown(self) -> None:
        self.battery(5)
        self.write_config("ENABLED=1\nTHRESHOLD=10\nINTERVAL=30\n")
        svc, marker = self.mock_svc()
        result = self.run_monitor(
            "--once", "--sysfs-root", str(self.sysfs),
            "--config", str(self.config), "--svc", str(svc),
        )
        self.assertNotEqual(result.returncode, 0)
        if marker.exists():
            self.assertNotIn("power shutdown", marker.read_text(encoding="utf-8").splitlines())

    def test_monitor_dry_run_invalid_sample_cannot_request_shutdown(self) -> None:
        self.battery("broken")
        self.write_config()
        svc, marker = self.mock_svc()
        result = self.run_monitor(
            "--once", "--dry-run", "--sysfs-root", str(self.sysfs),
            "--config", str(self.config), "--svc", str(svc),
        )
        self.assertNotEqual(result.returncode, 0)
        if marker.exists():
            self.assertNotIn("power shutdown", marker.read_text(encoding="utf-8").splitlines())

    def test_monitor_dry_run_reports_charging_and_does_not_shutdown(self) -> None:
        self.battery(5, "Charging")
        self.write_config("ENABLED=1\nTHRESHOLD=10\nINTERVAL=30\n")
        svc, marker = self.mock_svc()
        result = self.run_monitor(
            "--dry-run", "--sysfs-root", str(self.sysfs),
            "--config", str(self.config), "--svc", str(svc),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DECISION=charging\n", result.stdout)
        self.assertIn("WOULD_SHUTDOWN=0\n", result.stdout)
        self.assertNotIn("power shutdown", marker.read_text(encoding="utf-8").splitlines())

    def test_monitor_dry_run_reports_disabled_and_does_not_shutdown(self) -> None:
        self.battery(5)
        self.write_config("ENABLED=0\nTHRESHOLD=10\nINTERVAL=30\n")
        svc, marker = self.mock_svc()
        result = self.run_monitor(
            "--dry-run", "--sysfs-root", str(self.sysfs),
            "--config", str(self.config), "--svc", str(svc),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DECISION=disabled\n", result.stdout)
        self.assertIn("WOULD_SHUTDOWN=0\n", result.stdout)
        self.assertNotIn("power shutdown", marker.read_text(encoding="utf-8").splitlines())

    def test_monitor_rereads_external_power_after_command_probe(self) -> None:
        self.battery(5)
        usb = self.supply("usb", type="USB_CDP", online=0)
        self.write_config("ENABLED=1\nTHRESHOLD=10\nINTERVAL=30\n")
        svc, marker = self.mock_svc()
        svc.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(marker))}\n"
            f"printf '1\\n' > {shlex.quote(str(usb / 'online'))}\n"
            "echo 'svc power shutdown'\nexit 1\n",
            encoding="utf-8",
        )
        result = self.run_monitor(
            "--dry-run", "--sysfs-root", str(self.sysfs),
            "--config", str(self.config), "--svc", str(svc),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DECISION=charging\n", result.stdout)
        self.assertIn("WOULD_SHUTDOWN=0\n", result.stdout)
        self.assertNotIn("power shutdown", marker.read_text(encoding="utf-8").splitlines())

    def run_daemon_fixture(
        self, *, corrupt_after_probe: bool = False
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        """Run the daemon with all device and persistent paths redirected."""
        module = self.directory / "daemon-module"
        module.mkdir()
        state = self.directory / "daemon-state"
        commands = self.directory / "commands"
        commands.mkdir()
        marker = self.directory / "daemon-svc-arguments"
        svc = commands / "svc"
        corruption = (
            f"printf 'broken\\n' > {shlex.quote(str(self.sysfs / 'battery' / 'capacity'))}\n"
            if corrupt_after_probe else ""
        )
        svc.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(marker))}\n"
            "if [ \"$*\" = 'power shutdown' ]; then\n"
            "  echo 'Simulated Binder permission denial' >&2\n  exit 7\nfi\n"
            + corruption + "echo 'svc power shutdown'\nexit 1\n",
            encoding="utf-8",
        )
        svc.chmod(0o755)
        for name, output in (("id", "0"), ("getprop", "1")):
            command = commands / name
            command.write_text(f"#!/bin/sh\nprintf '{output}\\n'\n", encoding="utf-8")
            command.chmod(0o755)
        (module / "common.sh").write_text(
            (REPOSITORY / "common.sh").read_text(encoding="utf-8"), encoding="utf-8"
        )
        monitor = (REPOSITORY / "monitor.sh").read_text(encoding="utf-8")
        monitor = monitor.replace("/sys/class/power_supply", str(self.sysfs))
        monitor = monitor.replace("/data/adb/atopwlb/config.conf", str(self.config))
        monitor = monitor.replace("/data/adb/atopwlb", str(state))
        monitor = monitor.replace("/system/bin/svc", str(svc))
        monitor = monitor.replace("/system/bin/sh", SHELL)
        script = module / "monitor.sh"
        script.write_text(monitor, encoding="utf-8")
        environment = os.environ.copy()
        environment["PATH"] = str(commands) + os.pathsep + environment.get("PATH", "")
        result = subprocess.run(
            [SHELL, str(script)], text=True, encoding="utf-8", env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        return result, state, marker

    def test_daemon_shutdown_denial_stops_after_exactly_one_request(self) -> None:
        self.battery(5)
        self.write_config("ENABLED=1\nTHRESHOLD=10\nINTERVAL=30\n")
        result, state, marker = self.run_daemon_fixture()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(marker.read_text().splitlines(), ["power", "power shutdown"])
        self.assertIn("STATE=inactive\n", (state / "status").read_text())
        self.assertIn("exit=7", (state / "monitor.log").read_text())
        self.assertIn("permission denial", (state / "shutdown.out").read_text())

    def test_daemon_runtime_data_failure_stops_without_shutdown_request(self) -> None:
        self.battery(5)
        self.write_config("ENABLED=1\nTHRESHOLD=10\nINTERVAL=30\n")
        result, state, marker = self.run_daemon_fixture(corrupt_after_probe=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(marker.read_text().splitlines(), ["power"])
        self.assertIn("STATE=inactive\n", (state / "status").read_text())
        self.assertFalse((state / "shutdown.out").exists())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--shell", default=SHELL)
    arguments, remaining = parser.parse_known_args()
    SHELL = arguments.shell
    unittest.main(argv=[sys.argv[0], *remaining])
