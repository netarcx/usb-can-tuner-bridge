#!/usr/bin/env python3
"""Windows launcher's WSL helper. No stop, reset, control, or flash operation."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time

import bridge

VERSIONS = {"stable": "26.3.0", "alpha": "26.70.0a2"}


def matching_interfaces(serial: str) -> list[dict]:
    found = []
    for interface in Path("/sys/class/net").iterdir():
        device = interface / "device"
        if not device.exists():
            continue
        for parent in device.resolve().parents:
            serial_file = parent / "serial"
            if serial_file.is_file() and serial_file.read_text().strip().casefold() == serial.casefold():
                info = bridge.interface_info(interface.name)
                if info.get("linkinfo", {}).get("info_kind") == "can":
                    found.append(info)
                break
    return found


def process_token(pid: int) -> str:
    # comm can contain spaces or parentheses; fields after the last ')' start at 3.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def is_alive(record: dict) -> bool:
    try:
        pid = int(record["pid"])
        return process_token(pid) == record["token"] and b"bridge.py" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError, KeyError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "start", "status", "watch"])
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--serial", default="")
    parser.add_argument("--profile", choices=VERSIONS, default="stable")
    args = parser.parse_args()
    if os.geteuid() != 0 or args.uid == 0:
        raise bridge.BridgeError("The helper needs WSL root and a non-root backend user.")
    # wsl.exe --exec inherits a non-login PATH that can omit /sbin entirely.
    os.environ["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin:" + os.environ.get("PATH", "")
    user = pwd.getpwuid(args.uid)
    home = Path(user.pw_dir)
    state = home / ".local/state/usb-can-tuner"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(state, user.pw_uid, user.pw_gid)
    record_file = state / "process.json"
    try:
        record = json.loads(record_file.read_text())
    except (OSError, ValueError):
        record = {}
    if args.action == "watch":
        # An attached wsl.exe session keeps the distro active independently of
        # the GUI. One watcher per user; it exits when this exact backend exits.
        with (state / "watcher.lock").open("a") as watcher_lock:
            os.chown(state / "watcher.lock", user.pw_uid, user.pw_gid)
            try:
                fcntl.flock(watcher_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            os.setgroups([])
            os.setgid(user.pw_gid)
            os.setuid(user.pw_uid)
            while is_alive(record):
                time.sleep(2)
        return
    if args.action == "status":
        print(json.dumps({"running": is_alive(record), "record": record,
                          "interfaces": matching_interfaces(args.serial) if args.serial else []}))
        return
    version = VERSIONS[args.profile]
    venv = home / ".local/share/usb-can-tuner/venvs" / version
    as_user = ["runuser", "-u", user.pw_name, "--", "env", f"HOME={home}", "PYTHONNOUSERSITE=1"]
    if args.action == "prepare":
        # A versioned environment is never upgraded in place while a bridge runs.
        lock_path = state / f"install-{version}.lock"
        with lock_path.open("a") as install_lock:
            fcntl.flock(install_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not (venv / "bin/python").exists():
                subprocess.run(as_user + ["python3", "-m", "venv", str(venv)], check=True)
            python = str(venv / "bin/python")
            installed = subprocess.run(as_user + [python, "-c", "import importlib.metadata as m; print(m.version('phoenix6'))"], capture_output=True, text=True)
            if installed.stdout.strip() != version:
                with (state / "runtime.lock").open("a") as runtime_lock:
                    os.chown(state / "runtime.lock", user.pw_uid, user.pw_gid)
                    try:
                        fcntl.flock(runtime_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise bridge.BridgeError("A bridge is running; dependencies will not be changed.") from exc
                    requirements = Path(__file__).with_name("requirements-alpha.txt" if args.profile == "alpha" else "requirements.txt")
                    subprocess.run(as_user + [python, "-m", "pip", "install", "--disable-pip-version-check",
                        "--index-url", "https://pypi.org/simple", "-r", str(requirements)], check=True)
        print(json.dumps({"ready": True, "version": version}))
        return
    if is_alive(record):
        if record.get("serial") != args.serial or record.get("profile") != args.profile:
            raise bridge.BridgeError("A bridge is already running for another adapter or profile. It will not be interrupted.")
        print(json.dumps({"running": True, "record": record, "reused": True}))
        return
    if not args.serial:
        raise bridge.BridgeError("An adapter serial number is required; no interface is guessed.")
    lock = (state / "runtime.lock").open("a")
    os.chown(state / "runtime.lock", user.pw_uid, user.pw_gid)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise bridge.BridgeError("A bridge already holds the runtime lock. It will not be interrupted.") from exc
    bridge.require_free_port()
    python = venv / "bin/python"
    if not python.exists():
        raise bridge.BridgeError("Prepare this backend before connecting the adapter.")
    subprocess.run(["modprobe", "gs_usb"], check=True, capture_output=True)
    deadline = time.monotonic() + 15
    while True:
        interfaces = matching_interfaces(args.serial)
        if interfaces or time.monotonic() >= deadline:
            break
        time.sleep(0.3)
    if len(interfaces) != 1:
        raise bridge.BridgeError(f"Expected one SocketCAN channel for this serial, found {len(interfaces)}. No bus was changed.")
    interface = interfaces[0]["ifname"]
    bridge.configure_interface(interface, 1_000_000)
    bridge.check_interface(bridge.interface_info(interface))
    logfile = state / "backend.log"
    with logfile.open("a") as log:
        os.chown(logfile, user.pw_uid, user.pw_gid)
        # setsid + file-backed stdio deliberately outlive the GUI and wsl.exe.
        child = subprocess.Popen([str(python), "-u", str(Path(__file__).with_name("bridge.py")),
            "--interface", interface, "--lock-fd", str(lock.fileno())],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            pass_fds=(lock.fileno(),), user=user.pw_uid, group=user.pw_gid, extra_groups=[],
            cwd=str(home), env={**os.environ, "HOME": str(home), "PYTHONNOUSERSITE": "1"})
    record = {"pid": child.pid, "token": process_token(child.pid), "interface": interface,
              "serial": args.serial, "profile": args.profile, "version": version, "log": str(logfile)}
    temp = record_file.with_suffix(".tmp")
    temp.write_text(json.dumps(record))
    os.chown(temp, user.pw_uid, user.pw_gid)
    temp.replace(record_file)
    lock.close()
    for _ in range(50):
        if child.poll() is not None:
            raise bridge.BridgeError(f"Backend exited ({child.returncode}). See {logfile}.")
        try:
            bridge.diagnostic_request("getversion")
            print(json.dumps({"running": True, "record": record, "reused": False}))
            return
        except (OSError, ValueError):
            time.sleep(0.2)
    raise bridge.BridgeError(f"Backend is still starting; it was left running. See {logfile}.")


if __name__ == "__main__":
    try:
        main()
    except (bridge.BridgeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
