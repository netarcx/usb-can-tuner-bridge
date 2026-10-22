# USB-CAN Tuner Bridge

A Windows desktop launcher for Phoenix Tuner X with a CANable/candleLight USB
adapter. The connection uses WSL2 and CTRE's **unmodified Linux Phoenix diagnostic
server**. No SystemCore, CANivore, WPILib, robot program, or firmware emulation is
needed. The server truthfully identifies itself as Linux x86-64.

## Run on Windows

Download the Windows executable from the [v0.1.0 prototype release](https://github.com/netarcx/usb-can-tuner-bridge/releases/tag/v0.1.0).
Setup instructions and checksums are included with the download.

Build output: `dist/USB-CAN Tuner Bridge.exe` at the repository root.
This self-contained Windows x64 executable includes .NET and the backend scripts;
the source checkout is not needed on an end user's PC. The app downloads pinned,
SHA-256-verified CTRE wheels from PyPI rather than redistributing vendor binaries.

1. Launch **USB-CAN Tuner Bridge.exe**.
2. On a new PC, use **First-time setup** for WSL2/Ubuntu, usbipd-win, and Linux
   prerequisites. Finish the initial Linux username setup if requested. Enabling
   WSL may require restarting Windows manually; the app never reboots Windows.
3. Plug in the USB-CAN adapter. First-time sharing requests Administrator approval
   for that adapter. Subsequent connections attach automatically.
4. Wait for **Connected to Phoenix** and your devices. Click **Open Phoenix Tuner**,
   or open Tuner yourself.
5. Set Tuner to **localhost**, port **1250**, and **CANivore USB OFF** once. Tuner
   remembers these settings. Select the shown CAN bus if asked.

The Open Tuner button prefers an existing window connected to localhost:1250.
It never switches an already-open Tuner away from another target automatically.
After initial setup, the normal flow is launch → plug in → open Tuner.
Choose and apply firmware in Tuner yourself; this app never starts an update.

Automatic USB detection currently supports **`1d50:606f` gs_usb adapters**, including
our tested CANable/candleLight device. Multiple adapters require choosing one.
The app maps USB serial to Linux interface, rather than assuming `can0`.
Multi-channel adapters are rejected pending channel-selection support. Other
SocketCAN hardware can use the CLI below. Serial/SLCAN needs separate setup;
proprietary Windows-only adapters are not universally supported.

## Firmware profiles

| Profile | Pinned backend | Device generation |
| --- | --- | --- |
| 2026 stable, default | Phoenix `26.3.0` | Stable 2026; tested with 26.3.0.0 |
| 2027 alpha | Phoenix `26.70.0a2` | 26.70.x; Tuner Preview required |

Choose a profile **before connecting**. The app follows an existing backend and
never switches it automatically. Mixed firmware generations produce a warning.
Discovery can work across generations even when self-test does not: a 26.70
backend discovered our 26.3 Kraken but returned self-test error -10011. Matching
the backend to 26.3 fixed it. Stable and alpha also use different native CAN-status
ABIs. Unknown versions are rejected before loading libraries.

## Connection lifetime and recovery

- Closing the window minimizes it to the tray. The tray menu exits the launcher
  while **leaving the detached backend running**. A launcher crash also leaves it
  running. A separate attached WSL session holds the distribution active while
  the managed backend runs, without changing global WSL idle-timeout settings.
- A per-user runtime lock covers interface setup and server lifetime. Another
  bridge cannot take over an active session. Dependencies use separate versioned
  environments and installation locks; a running environment is not upgraded.
- A healthy, already-up classic 1 Mbps bus is preserved. Different active settings,
  bus-off, ambiguous channels, missing serials, and occupied port 1250 fail without
  resetting an active bus. Failed bitrate setup leaves a previously down bus down.
- While Tuner is open, the launcher pauses its own device discovery; only local
  HTTP version checks continue. Tuner manages the device traffic.
- The app never detaches USB, force-binds drivers, resets adapters, enables CAN FD,
  feeds robot enable, writes device configuration, or starts firmware programming.
- Once connected, loss of connectivity is reported with **no automatic restart**.
  Do not unplug USB, shut down WSL/Windows, or remove device power during updates.

**Completed firmware programming and recovery have not been validated in this
project.** Validation covers device discovery and self-test. This prototype cannot
guarantee against device damage. CTRE's server and Tuner perform programming,
not custom bootloader code.

For a deliberate restart or profile change, first finish updates and close Tuner.
Inspect `~/.local/state/usb-can-tuner/process.json` in WSL, verify its PID,
`/proc/PID/cmdline`, and start-time token before sending SIGTERM. Then exit the
launcher from its tray menu and reopen it with the required profile. There is no
one-click stop/reset that could interrupt programming. After physically unplugging
an active adapter, the old server can retain its old interface binding and need
this deliberate restart. USB/IP detach/reattach is a manual recovery step only.

Windows logs and helper scripts: `%LOCALAPPDATA%\UsbCanTuner`.
Linux log: `~/.local/state/usb-can-tuner/backend.log`.
Environments: `~/.local/share/usb-can-tuner/venvs/<Phoenix version>`.

The native service listens on Linux TCP 1250. Use Windows localhost forwarding;
do not expose the service through a public port forward. Existing Windows/WSL
firewall policy still applies; the app does not modify firewall rules. USBPcap
conflicts and missing gs_usb kernel support require manual resolution; the app
does not replace drivers automatically.

## Verified on 2026-10-21

- Windows x64 self-contained single-executable build, .NET **10.0.13**, SDK **10.0.402**.
- WSL2 Ubuntu, kernel `6.18.54.1-microsoft-standard-WSL2`, adapter `1d50:606f`.
- The actual packaged GUI prepares the backend, maps the USB serial to `can0`,
  starts the detached service, and lists **Kraken /
  Talon FX vers. F, ID 34**, firmware **26.3.0.0**, CAN ERROR-ACTIVE at 1 Mbps.
- Tuner X Preview **2026.11.0.0** connects through Windows localhost. Its self-test
  page visibly renders temperatures and configuration; decorated self-test returns
  **167 populated values**. Robot Enable and Device Enable are Disabled.
- Earlier alpha testing discovered the same device on firmware 26.70.0.0. The
  later change to 26.3.0.0 exposed the self-test mismatch described above.
- Unit tests cover both ABIs, unsupported versions, runtime locks, port occupancy,
  TIME_WAIT, bus preservation, bitrate failure, and disabled enable transmission.
  A duplicate live CLI start is rejected without changing the working bus.
- Full Windows USB/IP attachment and startup from a detached adapter pass.
  Closing to tray, terminating/reopening the GUI, and duplicate GUI startup preserve
  the same backend; self-test remains populated afterward. The executable smoke
  test and a clean SHA-256-verified requirements installation also pass.
- No firmware writes, configuration changes, or motor-control tests are performed
  during project validation. Clean-PC prerequisite installation, other adapters, and
  completed firmware programming remain unvalidated.

## Build

From Windows PowerShell with the .NET 10 SDK:

```powershell
.\windows\build.ps1
```

`-Dotnet` selects a specific SDK executable; `-Output` selects the output directory.
The local executable is unsigned; public releases should be code-signed.
`--smoke-test` validates payload extraction and writes
`%LOCALAPPDATA%\UsbCanTuner\smoke-test.json` without touching USB or devices.

## Command-line backend

Linux prerequisites: Python 3.11+, glibc 2.35+, `python3-venv`, `iproute2`, `kmod`,
and a SocketCAN driver. Default: classic CAN **1 Mbps**, FD off, no `restart-ms`.

```bash
bash start-linux.sh --interface can0 --configure
# With matching alpha firmware and no other bridge running:
PHOENIX_PROFILE=alpha bash start-linux.sh --interface can0 --configure
```

Only link setup uses `sudo -n`; configure sudo access or preconfigure the interface
and omit `--configure`. Ctrl+C deliberately stops a CLI backend. Avoid stopping
it during any Tuner operation. All nodes need matching bitrate, correct wiring,
termination, and power. The original Windows CLI is also available:

```powershell
.\start-windows.ps1 -BusId 1-3 -Distro Ubuntu -PhoenixProfile stable
```

Virtual CAN tests startup and connectivity only:

```bash
sudo modprobe vcan
sudo ip link add dev vcan_tuner type vcan
sudo ip link set dev vcan_tuner up
bash start-linux.sh --interface vcan_tuner --allow-virtual
python3 -m unittest discover -s . -v
```

Primary references: CTRE's [SocketCAN requirements](https://v6.docs.ctr-electronics.com/en/stable/docs/installation/requirements.html#can-bus-requirements),
[diagnostic server](https://v6.docs.ctr-electronics.com/en/stable/docs/troubleshooting/running-diagnostics.html),
and [Tuner connection settings](https://v6.docs.ctr-electronics.com/en/latest/docs/tuner/connecting.html);
Microsoft's [USB in WSL2](https://learn.microsoft.com/en-us/windows/wsl/connect-usb).

## About

Developed from the standalone USB-CAN prototype in
[systemcore-rpi5-guide](https://github.com/netarcx/systemcore-rpi5-guide).
This repository contains only the desktop application and Linux backend.
It is an independent project, not affiliated with or endorsed by CTRE. Phoenix,
Phoenix Tuner, CANivore, and device names belong to their respective owners.
CTRE libraries are installed separately from pinned PyPI packages under their
own license terms; they are not included in this repository or release executable.
