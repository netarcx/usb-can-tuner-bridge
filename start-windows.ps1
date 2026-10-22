param(
    [string]$BusId,
    [string]$Interface = "can0",
    [string]$Distro = "Ubuntu",
    [ValidateSet("stable", "alpha")]
    [string]$PhoenixProfile = "stable"
)
$ErrorActionPreference = "Stop"

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed (exit $LASTEXITCODE)." }
}

try {
    $usbipd = (Get-Command usbipd.exe -ErrorAction Stop).Source
    if (-not $BusId) {
        & $usbipd list
        Write-Host "Run again with -BusId <BUSID> for the USB-CAN adapter shown above."
        exit 1
    }
    if ($BusId -notmatch '^\d+-\d+$') { throw "Invalid USB bus ID." }
    $linuxDir = (& wsl.exe --distribution $Distro --exec wslpath -u $PSScriptRoot).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $linuxDir) { throw "Cannot locate this folder in WSL distribution $Distro." }

    # Loading drivers before USB attach lets Linux bind the adapter immediately.
    # gs_usb covers candleLight/CANable. Other adapters need their own Linux driver.
    Invoke-Checked "wsl.exe" @("--distribution", $Distro, "--exec", "sudo", "modprobe", "gs_usb")
    $stateJson = & $usbipd state
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect USB sharing state." }
    $device = ($stateJson | ConvertFrom-Json).Devices | Where-Object { $_.BusId -eq $BusId }
    if (-not $device) { throw "USB $BusId is not connected. Run usbipd list again." }
    if (-not $device.PersistedGuid) {
        Write-Host "Sharing USB $BusId with WSL. First-time bind requires Administrator PowerShell."
        Invoke-Checked $usbipd @("bind", "--busid", $BusId)
    }
    if (-not $device.ClientIPAddress) {
        Invoke-Checked $usbipd @("attach", "--wsl", "--busid", $BusId)
    }
    Write-Host "Starting Phoenix diagnostics. Tuner target: localhost, CANivore USB OFF."
    Invoke-Checked "wsl.exe" @("--distribution", $Distro, "--exec", "env", "PHOENIX_PROFILE=$PhoenixProfile", "bash", "$linuxDir/start-linux.sh", "--interface", $Interface, "--configure")
} catch {
    Write-Error $_
    exit 1
}
