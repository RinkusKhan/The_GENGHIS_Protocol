# GENGHIS -- Windows `serve` launcher (the coordinator / inference endpoint on this box).
# The Windows analogue of serve.sh / donor-serve.sh: keeps `genghis_coordinator.py serve` alive across
# CRASHES (restart loop) and REBOOTS (registered as a Scheduled Task, ONLOGON).
#
# Why ONLOGON, not ONSTART/SYSTEM: the fast path uses this box's GPU (CUDA/5090), and GPU access needs a
# real user desktop session -- a session-0 SYSTEM task can't reach it. So it starts right after you log in.
# For a truly headless always-on box, enable auto-login. (Same reasoning as the NUC's Vulkan RPC task.)
#
# Install (once) -- NO-ADMIN default: a per-user Startup entry (runs at logon, no elevation, in your
# desktop session so the GPU is reachable). Drop a one-line .vbs in the Startup folder:
#   $s = [Environment]::GetFolderPath('Startup')
#   $cmd = 'CreateObject("WScript.Shell").Run "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File ""<repo>\poc\serve-laptop.ps1""", 0, False'
#   Set-Content (Join-Path $s 'GENGHIS-serve.vbs') $cmd -Encoding ASCII
# (Window style 0 = fully hidden, no console flash.) Remove it to disable.
#
# ADMIN alternative (a managed Scheduled Task instead of a Startup entry):
#   schtasks /Create /TN "GENGHIS serve" /SC ONLOGON /RL LIMITED /F ^
#     /TR "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File <repo>\poc\serve-laptop.ps1"
#
# Start now without waiting for a logon:
#   Start-Process -WindowStyle Hidden powershell -ArgumentList '-ExecutionPolicy','Bypass','-File','<repo>\poc\serve-laptop.ps1'

# 'Continue', NOT 'SilentlyContinue': in Windows PowerShell 5.1 a native program's stderr becomes error
# records once redirected, and SilentlyContinue DROPS them -- i.e. every Python traceback vanished and
# serve.log only ever showed "starting" / "exited". (Found on the NUC: an empty /v1 reply and a blank log.)
$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot                      # wherever THIS checkout's poc\ lives (laptop: E:\..., NUC: C:\genghis\...)
$log  = Join-Path $here 'serve.log'
Set-Location $here

while ($true) {
    # Resolve the interpreter by ABSOLUTE path first, never trusting PATH. At logon the hidden
    # wscript->powershell context can have a different PATH than an interactive shell (e.g. a
    # Microsoft-Store python shim shadowing the real one), which made serve silently fail to bind.
    # Pin the known-good interpreter; fall back to PATH only if that exact path is gone.
    # Order: a python.exe under the user's own Programs\Python (the python.org installer's location -- never the
    # Store shim), then `py`, then whatever `python` resolves to.
    $pinned = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe') -ErrorAction SilentlyContinue |
              Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
    if ($pinned -and (Test-Path $pinned)) { $py = $pinned }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { $py = (Get-Command py).Source }
    else { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $py) {
        "[{0}] python NOT FOUND (pinned path missing AND not on PATH) -- install Python 3 (winget install -e --id Python.Python.3.12). Re-checking in 30s." -f (Get-Date -Format o) | Out-File -Append $log -Encoding utf8
        Start-Sleep -Seconds 30
        continue
    }
    # ONE encoding for the whole file: PS 5.1's Out-File default is UTF-16, and a UTF-16 first line makes
    # Get-Content read every later UTF-8 line as CJK mojibake. utf8 everywhere.
    "[{0}] starting: {1} -u genghis_coordinator.py serve --goal fastest" -f (Get-Date -Format o), $py | Out-File -Append $log -Encoding utf8
    # -u = unbuffered stdout so the coordinator's own lines land in the log AS they happen (block-buffered
    # otherwise when redirected). 2>&1 + ForEach "$_" flattens stderr records into plain text lines.
    & $py -u genghis_coordinator.py serve --goal fastest 2>&1 | ForEach-Object { "$_" } | Out-File -Append $log -Encoding utf8
    "[{0}] serve exited (code {1}) -- restarting in 5s" -f (Get-Date -Format o), $LASTEXITCODE | Out-File -Append $log -Encoding utf8
    Start-Sleep -Seconds 5
}
