#!/usr/bin/env python3
"""Run CTRE's diagnostic server on Linux with an ordinary SocketCAN adapter."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = 1250


def runtime_lock_path() -> Path:
    directory = Path.home() / ".local/state/usb-can-tuner"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / "runtime.lock"


def acquire_runtime_lock(inherited: int | None = None):
    if inherited is not None:
        if os.fstat(inherited).st_ino != runtime_lock_path().stat().st_ino:
            raise BridgeError("Invalid inherited runtime lock.")
        return os.fdopen(inherited, "a")
    lock = runtime_lock_path().open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise BridgeError("Another bridge is running. It will not be interrupted.") from exc
    return lock


class BridgeError(Exception):
    """A setup error that should be shown without a traceback."""


class CANBusStatus(ctypes.Structure):
    """CANBusStatus_t layout from the pinned Phoenix C ABI."""

    _fields_ = [
        ("bus_util", ctypes.c_float),
        ("bus_error", ctypes.c_uint32),
        ("arbitration_lost", ctypes.c_uint32),
        ("restarts", ctypes.c_uint32),
        ("bus_off", ctypes.c_uint32),
        ("tx_full", ctypes.c_uint32),
        ("rec", ctypes.c_uint16),
        ("tec", ctypes.c_uint16),
        ("can_state", ctypes.c_int),
    ]


def interface_info(name: str) -> dict:
    try:
        result = subprocess.run(
            ["ip", "-details", "-json", "link", "show", "dev", name],
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)[0]
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError) as exc:
        raise BridgeError(f"Cannot inspect {name}: attach a SocketCAN adapter first.") from exc


def check_interface(info: dict, allow_virtual: bool = False) -> None:
    kind = info.get("linkinfo", {}).get("info_kind")
    if kind == "vcan" and not allow_virtual:
        raise BridgeError("This is a virtual CAN interface; use --allow-virtual for a server smoke test.")
    if kind not in ("can", "vcan"):
        raise BridgeError("The selected interface is not a SocketCAN interface.")
    if "UP" not in info.get("flags", []):
        raise BridgeError("The CAN interface is down. Run with --configure to bring it up.")
    if info.get("linkinfo", {}).get("info_data", {}).get("state") == "BUS-OFF":
        raise BridgeError("The CAN controller is bus-off. Check wiring and termination, then reconfigure.")


def configure_interface(name: str, bitrate: int) -> None:
    info = interface_info(name)
    if info.get("linkinfo", {}).get("info_kind") != "can":
        raise BridgeError("--configure requires a physical CAN interface.")
    data = info.get("linkinfo", {}).get("info_data", {})
    if "UP" in info.get("flags", []):
        if data.get("bittiming", {}).get("bitrate") == bitrate and info.get("mtu") == 16:
            check_interface(info)
            return
        raise BridgeError("The interface is already up with different settings. It will not be reset automatically.")
    commands = [
        ["ip", "link", "set", "dev", name, "down"],
        ["ip", "link", "set", "dev", name, "type", "can", "bitrate", str(bitrate), "fd", "off"],
        ["ip", "link", "set", "dev", name, "txqueuelen", "1000"],
        ["ip", "link", "set", "dev", name, "up"],
    ]
    for command in commands:
        try:
            # Elevate link configuration only; the diagnostic service runs as the user.
            subprocess.run(command if os.geteuid() == 0 else ["sudo", "-n", *command], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise BridgeError(f"CAN setup failed: {' '.join(command)}") from exc


def require_free_port() -> None:
    try:
        with socket.socket() as probe:
            # The native server uses SO_REUSEADDR. Match it so a clean restart
            # is not rejected solely because old HTTP connections are TIME_WAIT.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", PORT))
    except OSError as exc:
        raise BridgeError(f"TCP {PORT} is already in use. Stop the other diagnostic server first.") from exc


class PhoenixRuntime:
    """Use the hardware C ABI shipped in CTRE's installed Python distribution.

    These signatures match phoenix6/phoenix_native.py in the pinned wheel.
    Loading the native libraries directly avoids the alpha wheel's top-level
    import failure when its optional WPILib telemetry package is absent. No
    vendor files, platform identities, firmware, or protocol messages are patched.
    """

    def __init__(self) -> None:
        try:
            dist = importlib.metadata.distribution("phoenix6")
        except importlib.metadata.PackageNotFoundError as exc:
            raise BridgeError("Install requirements.txt in a Python virtual environment first.") from exc
        self.version = dist.version
        if self.version not in ("26.3.0", "26.70.0a2"):
            raise BridgeError(f"Unsupported Phoenix C ABI version {self.version}; use the stable or alpha requirements file.")
        library_dir = Path(dist.locate_file("phoenix6/lib"))
        try:
            self.tools = ctypes.CDLL(str(library_dir / "libCTRE_PhoenixTools.so"), mode=ctypes.RTLD_GLOBAL)
            self.api = ctypes.CDLL(str(library_dir / "libCTRE_Phoenix6.so"), mode=ctypes.RTLD_GLOBAL)
            signatures = {
                "c_ctre_phoenix6_unmanaged_set_transmit_enable": ([ctypes.c_bool], None),
                "c_ctre_phoenix6_unmanaged_get_transmit_enable": ([], ctypes.c_bool),
                "c_ctre_phoenix6_unmanaged_load_phoenix": ([], None),
                "c_Phoenix_Diagnostics_SetSecondsToStart": ([ctypes.c_double], None),
                "c_ctre_phoenix6_encode_device": (
                    [ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32)],
                    ctypes.c_int,
                ),
            }
            if self.version == "26.3.0":
                # Stable uses separate output pointers; Alpha 7 replaced them
                # with a struct. Calling one ABI with the other signature crashes.
                bus_status_args = [ctypes.POINTER(ctypes.c_float)] + [ctypes.POINTER(ctypes.c_uint32)] * 4 + [ctypes.c_char_p, ctypes.c_bool]
            else:
                bus_status_args = [ctypes.POINTER(CANBusStatus), ctypes.c_char_p]
            signatures["c_ctre_phoenix6_platform_canbus_get_status"] = (bus_status_args, ctypes.c_int32)
            for name, (args, result) in signatures.items():
                function = getattr(self.tools, name)
                function.argtypes = args
                function.restype = result
        except (OSError, AttributeError) as exc:
            raise BridgeError(f"Cannot load the Phoenix hardware libraries: {exc}") from exc

    def start(self, interface: str) -> None:
        self.tools.c_ctre_phoenix6_unmanaged_set_transmit_enable(False)
        if self.tools.c_ctre_phoenix6_unmanaged_get_transmit_enable():
            raise BridgeError("Phoenix enable transmission did not turn off.")
        self.tools.c_Phoenix_Diagnostics_SetSecondsToStart(0.0)
        self.tools.c_ctre_phoenix6_unmanaged_load_phoenix()
        # Requesting bus status opens the platform's SocketCAN connection. The
        # first result can be unavailable while the native worker initializes.
        if self.version == "26.3.0":
            utilization = ctypes.c_float()
            counters = [ctypes.c_uint32() for _ in range(4)]
            self.tools.c_ctre_phoenix6_platform_canbus_get_status(
                ctypes.byref(utilization), *(ctypes.byref(counter) for counter in counters),
                interface.encode("ascii"), True,
            )
        else:
            bus_status = CANBusStatus()
            self.tools.c_ctre_phoenix6_platform_canbus_get_status(
                ctypes.byref(bus_status), interface.encode("ascii"),
            )
        # Register the selected bus as device construction normally would. An
        # absent device ID is valid for diagnostic startup. This encodes a device
        # identifier; it does not configure a device or command an actuator.
        device_hash = ctypes.c_uint32()
        status = self.tools.c_ctre_phoenix6_encode_device(
            0, b"talon fx", interface.encode("ascii"), ctypes.byref(device_hash),
        )
        if status != 0:
            raise BridgeError(f"Phoenix could not register {interface}: status {status}")

    def stop(self) -> None:
        self.tools.c_ctre_phoenix6_unmanaged_set_transmit_enable(False)
        self.tools.c_Phoenix_Diagnostics_SetSecondsToStart(-1.0)


def diagnostic_request(action: str, interface: str = "") -> dict:
    query = urllib.parse.urlencode({"action": action, "canbus": interface})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{PORT}/?{query}", timeout=2) as response:
        return json.load(response)


def wait_ready(timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            version = diagnostic_request("getversion")
            if version.get("GeneralReturn", {}).get("Error") == 0:
                return version
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(0.2)
    raise BridgeError("The native diagnostic server did not become ready on TCP 1250.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="can0", help="SocketCAN interface (default: can0)")
    parser.add_argument("--configure", action="store_true", help="Use sudo to configure classic CAN")
    parser.add_argument("--bitrate", type=int, default=1_000_000)
    parser.add_argument("--allow-virtual", action="store_true", help="Allow vcan for server smoke tests")
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    runtime = None
    lock = None
    try:
        if sys.platform != "linux":
            raise BridgeError("Run this backend on Linux or WSL2; use start-windows.ps1 from Windows.")
        if not args.interface.isascii() or not args.interface or len(args.interface) > 15:
            raise BridgeError("Invalid Linux interface name.")
        if args.bitrate <= 0:
            raise BridgeError("Bitrate must be positive.")
        lock = acquire_runtime_lock(args.lock_fd)
        require_free_port()
        if args.configure:
            # USB/IP attach can return before the kernel creates its CAN netdev.
            deadline = time.monotonic() + 10
            while True:
                try:
                    interface_info(args.interface)
                    break
                except BridgeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            configure_interface(args.interface, args.bitrate)
        info = interface_info(args.interface)
        check_interface(info, args.allow_virtual)
        data = info.get("linkinfo", {}).get("info_data", {})
        bitrate = data.get("bittiming", {}).get("bitrate")
        print(f"Using {args.interface}: state={data.get('state', 'virtual')}, bitrate={bitrate}", flush=True)
        if data.get("state") == "ERROR-PASSIVE":
            print("CAN is error-passive: check powered nodes, CAN wiring, and termination.", flush=True)
        stopped = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stopped.set())
        runtime = PhoenixRuntime()
        runtime.start(args.interface)
        version = wait_ready()
        print(f"Phoenix {version.get('Version')} — {version.get('System')}", flush=True)
        print("Tuner X: target localhost, port 1250, CANivore USB toggle OFF.", flush=True)
        print(f"Select bus {args.interface}. The service does not feed an enable heartbeat.", flush=True)
        if args.allow_virtual:
            print("Virtual CAN test: no physical devices or firmware flash are being tested.", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        stopped.wait()
        return 0
    except (BridgeError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if runtime is not None:
            runtime.stop()
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
