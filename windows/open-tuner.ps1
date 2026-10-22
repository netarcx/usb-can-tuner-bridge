param([ValidateSet('stable','alpha')][string]$Profile = 'stable')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System; using System.Runtime.InteropServices;
public static class TunerWindow {
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr handle);
}
'@
$existing = @(Get-Process | Where-Object { $_.MainWindowTitle -like 'Phoenix Tuner X*' })
foreach ($process in $existing) {
    try {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
        $names = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition) |
            ForEach-Object { $_.Current.Name }
        if ($names -match '^Connected to server: (127\.0\.0\.1|localhost):1250$') {
            [TunerWindow]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
            Write-Output 'Opened the existing Tuner window connected to localhost:1250.'
            exit 0
        }
    } catch { } # A window can disappear while being inspected.
}
if ($existing.Count -gt 0) {
    [TunerWindow]::SetForegroundWindow($existing[0].MainWindowHandle) | Out-Null
    Write-Output 'Tuner is already open. After any operation finishes, select localhost, port 1250, with CANivore USB OFF. Its active target was left unchanged.'
    exit 0
}
$app = Get-StartApps | Where-Object { $_.Name -match 'Phoenix.*Tuner' } |
    Sort-Object @{Expression={ if (($_.Name -match 'Preview') -eq ($Profile -eq 'alpha')) { 0 } else { 1 } }} |
    Select-Object -First 1
if (-not $app) { throw 'Install Phoenix Tuner X from CTRE or Microsoft Store first.' }
Start-Process ('shell:AppsFolder\' + $app.AppID)
Write-Output 'Opened Phoenix Tuner. On first use, select localhost, port 1250, with CANivore USB OFF.'
