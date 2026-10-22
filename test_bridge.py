"""Preflight and native startup contract tests; no attached hardware is changed."""

import subprocess
import socket
import ctypes
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bridge
import control


class BridgeTests(unittest.TestCase):
    def test_second_runtime_is_rejected_before_any_bus_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bridge, "runtime_lock_path", return_value=Path(directory) / "lock"):
                with bridge.acquire_runtime_lock():
                    with patch.object(bridge, "configure_interface") as configure:
                        self.assertEqual(bridge.main(["--interface", "can0", "--configure"]), 1)
                        configure.assert_not_called()

    def test_unknown_native_abi_is_rejected_before_loading_libraries(self):
        with patch.object(bridge.importlib.metadata, "distribution", return_value=SimpleNamespace(version="26.4.0")):
            with patch.object(bridge.ctypes, "CDLL") as loader:
                with self.assertRaises(bridge.BridgeError):
                    bridge.PhoenixRuntime()
                loader.assert_not_called()

    def test_pinned_native_abis_have_distinct_signatures(self):
        for version, count in [("26.3.0", 7), ("26.70.0a2", 2)]:
            with self.subTest(version=version):
                dist = SimpleNamespace(version=version, locate_file=lambda _: "/not-loaded")
                with patch.object(bridge.importlib.metadata, "distribution", return_value=dist):
                    with patch.object(bridge.ctypes, "CDLL", side_effect=lambda *_a, **_kw: Mock()):
                        runtime = bridge.PhoenixRuntime()
                        call = runtime.tools.c_ctre_phoenix6_platform_canbus_get_status
                        self.assertEqual(len(call.argtypes), count)
                        runtime.tools.c_ctre_phoenix6_unmanaged_get_transmit_enable.return_value = False
                        runtime.tools.c_ctre_phoenix6_encode_device.return_value = 0
                        runtime.start("can0")
                        self.assertEqual(len(call.call_args.args), count)
                        if version == "26.3.0":
                            self.assertEqual(call.call_args.args[-2:], (b"can0", True))
                        else:
                            self.assertEqual(call.argtypes[0], ctypes.POINTER(bridge.CANBusStatus))

    @patch.object(bridge.subprocess, "run")
    def test_healthy_running_interface_is_not_reset(self, run):
        info = {"flags": ["UP"], "mtu": 16, "linkinfo": {"info_kind": "can", "info_data": {
            "state": "ERROR-ACTIVE", "bittiming": {"bitrate": 1_000_000}}}}
        with patch.object(bridge, "interface_info", return_value=info):
            bridge.configure_interface("can0", 1_000_000)
        run.assert_not_called()

    @patch.object(bridge.subprocess, "run")
    def test_running_interface_with_other_settings_is_not_reset(self, run):
        info = {"flags": ["UP"], "mtu": 72, "linkinfo": {"info_kind": "can"}}
        with patch.object(bridge, "interface_info", return_value=info):
            with self.assertRaises(bridge.BridgeError):
                bridge.configure_interface("can0", 1_000_000)
        run.assert_not_called()

    def test_stale_or_reused_pid_is_not_adopted(self):
        with patch.object(control, "process_token", return_value="new"):
            self.assertFalse(control.is_alive({"pid": 123, "token": "old"}))
        self.assertFalse(control.is_alive({}))

    def test_clean_http_shutdown_does_not_block_restart(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen()
            with socket.create_connection(("127.0.0.1", port)) as client:
                with listener.accept()[0] as connection:
                    connection.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b"")
                    client.shutdown(socket.SHUT_WR)
                    self.assertEqual(connection.recv(1), b"")
        with patch.object(bridge, "PORT", port):
            bridge.require_free_port()

    def test_rejects_an_existing_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with patch.object(bridge, "PORT", listener.getsockname()[1]):
                with self.assertRaises(bridge.BridgeError):
                    bridge.require_free_port()

    def test_rejects_non_can_down_and_bus_off(self):
        for info in [
            {"linkinfo": {"info_kind": "ether"}, "flags": ["UP"]},
            {"linkinfo": {"info_kind": "can"}, "flags": []},
            {"linkinfo": {"info_kind": "can", "info_data": {"state": "BUS-OFF"}}, "flags": ["UP"]},
        ]:
            with self.subTest(info=info), self.assertRaises(bridge.BridgeError):
                bridge.check_interface(info)

    def test_virtual_interface_requires_explicit_opt_in(self):
        info = {"linkinfo": {"info_kind": "vcan"}, "flags": ["UP"]}
        with self.assertRaises(bridge.BridgeError):
            bridge.check_interface(info)
        bridge.check_interface(info, allow_virtual=True)

    @patch.object(bridge, "interface_info", return_value={"linkinfo": {"info_kind": "can"}})
    @patch.object(bridge.subprocess, "run")
    def test_failed_bitrate_does_not_bring_interface_up(self, run, _info):
        run.side_effect = [None, subprocess.CalledProcessError(2, ["ip"])]
        with self.assertRaises(bridge.BridgeError):
            bridge.configure_interface("can0", 1_000_000)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(any(call.args[0][-1] == "up" for call in run.call_args_list))

    def test_start_disables_enable_and_only_registers_device_identity(self):
        tools = SimpleNamespace(
            c_ctre_phoenix6_unmanaged_set_transmit_enable=Mock(),
            c_ctre_phoenix6_unmanaged_get_transmit_enable=Mock(return_value=False),
            c_Phoenix_Diagnostics_SetSecondsToStart=Mock(),
            c_ctre_phoenix6_unmanaged_load_phoenix=Mock(),
            c_ctre_phoenix6_encode_device=Mock(return_value=0),
            c_ctre_phoenix6_platform_canbus_get_status=Mock(return_value=-1),
        )
        runtime = object.__new__(bridge.PhoenixRuntime)
        runtime.version = "26.70.0a2"
        runtime.tools = tools
        runtime.start("can0")
        tools.c_ctre_phoenix6_unmanaged_set_transmit_enable.assert_called_once_with(False)
        tools.c_Phoenix_Diagnostics_SetSecondsToStart.assert_called_once_with(0.0)
        self.assertEqual(tools.c_ctre_phoenix6_encode_device.call_args.args[:3], (0, b"talon fx", b"can0"))
        self.assertEqual(tools.c_ctre_phoenix6_platform_canbus_get_status.call_args.args[1], b"can0")
        runtime.stop()
        tools.c_Phoenix_Diagnostics_SetSecondsToStart.assert_called_with(-1.0)

    def test_start_refuses_an_enabled_native_runtime(self):
        runtime = object.__new__(bridge.PhoenixRuntime)
        runtime.version = "26.70.0a2"
        runtime.tools = Mock()
        runtime.tools.c_ctre_phoenix6_unmanaged_get_transmit_enable.return_value = True
        with self.assertRaises(bridge.BridgeError):
            runtime.start("can0")
        runtime.tools.c_ctre_phoenix6_unmanaged_load_phoenix.assert_not_called()


if __name__ == "__main__":
    unittest.main()
