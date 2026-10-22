param([ValidatePattern('^[\w.\-]+$')][string]$Distro = 'Ubuntu')
$ErrorActionPreference = 'Stop'
try {
    if (-not (Test-Path "$env:ProgramFiles\usbipd-win\usbipd.exe")) {
        & winget.exe install --exact --id dorssel.usbipd-win --source winget --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) { throw 'usbipd-win installation did not complete.' }
    }
    $distros = (& wsl.exe --list --quiet) -replace "`0", ''
    if ($LASTEXITCODE -ne 0 -or $Distro -notin ($distros | ForEach-Object { $_.Trim() })) {
        & wsl.exe --install --distribution $Distro --no-launch
        Write-Host 'If Windows requests a restart, save your work and restart manually.'
        Write-Host "Open $Distro once and create a normal Linux username, then rerun First-time setup."
    } else {
        & wsl.exe --distribution $Distro --user root --exec apt-get update
        if ($LASTEXITCODE -ne 0) { throw 'Linux package index could not be updated.' }
        & wsl.exe --distribution $Distro --user root --exec apt-get install --yes python3 python3-venv iproute2 kmod
        if ($LASTEXITCODE -ne 0) { throw 'Linux prerequisites could not be installed.' }
        Write-Host 'Setup complete. Return to the launcher and click Retry connection.'
    }
} catch { Write-Host $_ -ForegroundColor Red }
Read-Host 'Press Enter to close'
