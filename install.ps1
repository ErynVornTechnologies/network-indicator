# Installs the Network Indicator for the current Windows user and starts it.
#
# - creates a Startup shortcut pointing at pythonw.exe (no console window at boot)
# - launches the widget immediately
# - idempotent: re-running replaces the shortcut
#
# Author: Kamil Nagorski, ErynVorn Technologies
$ErrorActionPreference = "Stop"

$appDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$script  = Join-Path $appDir "network_indicator.py"
$startup = [Environment]::GetFolderPath("Startup")
$link    = Join-Path $startup "Network Indicator.lnk"

if (-not (Test-Path $script)) { throw "network_indicator.py not found next to this script" }

# pythonw.exe of the Python the user runs (3.12 here); fall back to the py launcher default.
$pythonw = $null
$candidates = @()
try {
    $py = & py -3.12 -c "import sys, os; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))" 2>$null
    if ($py) { $candidates += $py.Trim() }
} catch { }
$candidates += @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe"
)
foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { $pythonw = $c; break } }
if (-not $pythonw) { throw "pythonw.exe not found. Install Python 3.12 (python.org build with tkinter)." }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath       = $pythonw
$sc.Arguments        = '"' + $script + '"'
$sc.WorkingDirectory = $appDir
$sc.WindowStyle      = 7           # minimised: pythonw has no console anyway
$sc.Description      = "Live network throughput indicator on the taskbar"
$sc.Save()

Start-Process -FilePath $pythonw -ArgumentList ('"' + $script + '"') -WorkingDirectory $appDir

"Installed: $link"
"Target   : $pythonw"
"Started  : $script"
