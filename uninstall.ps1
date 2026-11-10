# Removes the Network Indicator startup shortcut and stops a running instance.
# Author: Kamil Nagorski, ErynVorn Technologies
$ErrorActionPreference = "Continue"

$startup = [Environment]::GetFolderPath("Startup")
$link    = Join-Path $startup "Network Indicator.lnk"

if (Test-Path $link) { Remove-Item $link -Force; "Removed: $link" } else { "No shortcut to remove" }

$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $appDir "network_indicator.py"

# Stop only instances of this app, never other pythonw processes.
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$script*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "Stopped pid $($_.ProcessId)" }
