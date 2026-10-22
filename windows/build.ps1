param([string]$Dotnet = 'dotnet.exe', [string]$Output)
$ErrorActionPreference = 'Stop'
if (-not $Output) { $Output = Join-Path $PSScriptRoot '..\dist' }
& $Dotnet publish (Join-Path $PSScriptRoot 'UsbCanTuner.csproj') --configuration Release --runtime win-x64 --self-contained true --output $Output
if ($LASTEXITCODE -ne 0) { throw 'Windows publish failed.' }
Get-FileHash (Join-Path $Output 'USB-CAN Tuner Bridge.exe') -Algorithm SHA256
