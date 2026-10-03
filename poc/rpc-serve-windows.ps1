# GENGHIS -- Windows RPC DONOR launcher (lend this box's GPU/CPU to the pool). D27.
# The Windows analogue of donor-serve.sh: keeps `ggml-rpc-server` alive across CRASHES (restart loop) and
# REBOOTS (installed as a per-user Startup entry by install\install-windows.ps1 -Donor).
#
# How it starts: a GPU donor from a per-user Startup entry (logon), because the GPU (Vulkan/CUDA) needs a real
# user desktop session -- session 0 can't reach it; for a headless GPU box enable auto-login (Sysinternals
# Autologon). A CPU donor needs no session, so the installer (run as admin) gives it a scheduled task that starts
# at BOOT as SYSTEM: it lends again after a reboot without anyone logging in.
#
# Device: auto = the GPU of whichever build exists (build-cuda -> CUDA0, build-vulkan -> Vulkan0, else CPU).
# Override: -Device Vulkan0 / -Device CUDA0 / -Device CPU.  Port: -Port 50052 (what fleet.json must list).
#
# Start now without waiting for a logon:
#   Start-Process -WindowStyle Hidden powershell -ArgumentList '-ExecutionPolicy','Bypass','-File',"$PSScriptRoot\rpc-serve-windows.ps1"
param(
  [int]$Port = 50052,
  [string]$Device = "",
  [string]$BindHost = "0.0.0.0",
  [string]$Python = "",               # the installer's Python: register this port with the authority once the server is up
  [string]$Coord = "",                # the authority (host[:port]); a boot-time task runs as SYSTEM and cannot see the user's GENGHIS_COORD
  [double]$CacheGB = 0                # cap on the -c tensor cache (default: $env:GENGHIS_RPC_CACHE_GB, else 30)
)
$ErrorActionPreference = 'Continue'   # not SilentlyContinue: that would drop the server's stderr from the log (PS 5.1)
$here = $PSScriptRoot
$log  = Join-Path $here 'rpc-serve.log'
Set-Location $here

# ONE launcher per port (the Windows side of donor-serve.sh's flock). The installer starts one now and the Startup entry
# starts another at the next logon; without this, two loops fight over one port.
$created = $false
try   { $script:portLock = New-Object System.Threading.Mutex($true, "Global\GENGHIS-rpc-$Port", [ref]$created) }
catch { $script:portLock = New-Object System.Threading.Mutex($true, "Local\GENGHIS-rpc-$Port", [ref]$created) }
if (-not $created) {
  "[{0}] another GENGHIS-rpc launcher already serves :{1} -- this copy exits" -f (Get-Date -Format o), $Port | Out-File -Append $log -Encoding utf8
  exit 0
}

# Resolve the binary from THIS checkout's build dirs (same preference order as the coordinator: CUDA, Vulkan, CPU).
function Find-Rpc {
  foreach ($b in @('build-cuda','build-vulkan','build-rpc')) {
    $exe = Join-Path $here "llama.cpp\$b\bin\Release\ggml-rpc-server.exe"
    if (Test-Path $exe) { return @($exe, $b) }
  }
  return $null
}

# --- The -c tensor cache has no limit of its own --------------------------------------------------------------------
# ggml-rpc-server keeps every tensor it was ever sent, one file per hash, in <LLAMA_CACHE or %LOCALAPPDATA%\llama.cpp>\rpc.
# Nothing ever removed one: on the laptop it reached 52 GB in two weeks and filled C: (2026-09-30). Keep it under a cap,
# least recently USED first (NTFS last-access time), and prune harder while the disk is under 10 % free. A file the
# server has open just fails to delete and is skipped; a pruned tensor only costs one more LAN transfer next load.
if ($CacheGB -le 0) { $CacheGB = if ($env:GENGHIS_RPC_CACHE_GB) { [double]$env:GENGHIS_RPC_CACHE_GB } else { 30 } }
function Limit-RpcCache {
  try {
    $root = if ($env:LLAMA_CACHE) { $env:LLAMA_CACHE } else { Join-Path $env:LOCALAPPDATA 'llama.cpp' }
    $dir = Join-Path $root 'rpc'
    if (-not (Test-Path -LiteralPath $dir)) { return }
    $files = @(Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue | Sort-Object LastAccessTime)
    $total = [double](($files | Measure-Object Length -Sum).Sum)
    $disk = New-Object System.IO.DriveInfo ([System.IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $dir).Path))
    $free = [double]$disk.AvailableFreeSpace; $floor = 0.10 * $disk.TotalSize
    $cap = $CacheGB * 1GB; $n = 0; $freed = 0.0
    foreach ($f in $files) {
      if ($total -le $cap -and $free -ge $floor) { break }
      try { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop; $total -= $f.Length; $free += $f.Length; $freed += $f.Length; $n++ } catch {}
    }
    if ($n) {
      "[{0}] rpc cache: removed {1} least-used tensor files ({2:N1} GB); now {3:N1} GB (cap {4} GB, {5:N0} GB free on {6})" -f `
        (Get-Date -Format o), $n, ($freed / 1GB), ($total / 1GB), $CacheGB, ($free / 1GB), $disk.Name | Out-File -Append $log -Encoding utf8
    }
  } catch {
    "[{0}] rpc cache: could not prune ({1})" -f (Get-Date -Format o), $_.Exception.Message | Out-File -Append $log -Encoding utf8
  }
}

# --- D35: lending my GPU keeps the box awake (screen still sleeps) -------------------------------------------
# A named POWER AVAILABILITY REQUEST (PowerCreateRequest / PowerSetRequest, PowerRequestSystemRequired) -- the
# modern API renderers use; it shows in `powercfg /requests` under SYSTEM with our reason string, so it can be
# verified. (The first cut used SetThreadExecutionState; `powercfg /requests` showed SYSTEM: None -- it was a no-op.)
# Held while THIS donor is lending on AC power; released when lending is off (fleet lend off), on battery, or when
# the server dies. The power plan is never edited: the display still dims on its own schedule, and the normal sleep
# timer applies the moment we let go. A lid close still wins (explicit action).
Add-Type -Namespace Genghis -Name Power -MemberDefinition @"
[System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public struct REASON_CONTEXT {
    public uint Version;
    public uint Flags;
    [System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)] public string SimpleReasonString;
}
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
public static extern System.IntPtr PowerCreateRequest(ref REASON_CONTEXT Context);
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
public static extern bool PowerSetRequest(System.IntPtr PowerRequest, int RequestType);
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
public static extern bool PowerClearRequest(System.IntPtr PowerRequest, int RequestType);
"@
$POWER_REQUEST_CONTEXT_VERSION = 0; $POWER_REQUEST_CONTEXT_SIMPLE_STRING = 1; $PowerRequestSystemRequired = 1
$ctx = New-Object Genghis.Power+REASON_CONTEXT
$ctx.Version = $POWER_REQUEST_CONTEXT_VERSION; $ctx.Flags = $POWER_REQUEST_CONTEXT_SIMPLE_STRING
$ctx.SimpleReasonString = "GENGHIS: lending this GPU to the fleet (fleet lend off to release)"
$script:powerReq = [Genghis.Power]::PowerCreateRequest([ref]$ctx)
$script:awake = $false
function Set-Awake([bool]$on) {
  if ($on -eq $script:awake) { return }
  $ok = $false
  if ($script:powerReq -ne [System.IntPtr]::Zero) {
    if ($on) { $ok = [Genghis.Power]::PowerSetRequest($script:powerReq, $PowerRequestSystemRequired) }
    else     { $ok = [Genghis.Power]::PowerClearRequest($script:powerReq, $PowerRequestSystemRequired) }
  }
  $script:awake = $on
  "[{0}] keep-awake {1} (lending {2}) api-ok={3}" -f (Get-Date -Format o), ($(if ($on) { 'HELD' } else { 'released' })), ($(if ($on) { 'on AC' } else { 'off / on battery / server down' })), $ok | Out-File -Append $log -Encoding utf8
}
function Get-OnAC {
  try { $b = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue; if (-not $b) { return $true }   # no battery = desktop
        return ($b.BatteryStatus -ne 1) }                                                                 # 1 = discharging
  catch { return $true }
}
function Get-Lending {
  # Our own fleet entry: from the AUTHORITY (GENGHIS_COORD, set by the installer) -- a pure donor runs no serve of its own,
  # so asking 127.0.0.1:8899 always failed there and lending was always assumed. Unknown -> assume lending.
  try {
    $c = $Coord; if (-not $c) { $c = $env:GENGHIS_COORD }; if (-not $c) { $c = [Environment]::GetEnvironmentVariable("GENGHIS_COORD", "User") }
    $base = if ($c) { if ($c -match ':\d+$') { "http://$c" } else { "http://${c}:8899" } } else { "http://127.0.0.1:8899" }
    $f = Invoke-RestMethod -Uri "$base/fleet.json" -TimeoutSec 3
    $me = $env:COMPUTERNAME.ToLower()
    $d = $f.donors | Where-Object { ("$($_.host)").ToLower() -eq $me } | Select-Object -First 1
    if ($null -eq $d) { return $true }
    if ($null -ne $d.lend -and $d.lend -eq $false) { return $false }
    return $true
  } catch { return $true }
}

while ($true) {
  $found = Find-Rpc
  if (-not $found) {
    "[{0}] ggml-rpc-server.exe NOT FOUND under {1}\llama.cpp\build-*\bin\Release -- run install\install-windows.ps1 (it builds it). Re-checking in 60s." -f (Get-Date -Format o), $here | Out-File -Append $log -Encoding utf8
    Start-Sleep -Seconds 60
    continue
  }
  $exe, $build = $found
  Limit-RpcCache
  $dev = $Device
  if (-not $dev) { $dev = switch ($build) { 'build-cuda' { 'CUDA0' } 'build-vulkan' { 'Vulkan0' } default { 'CPU' } } }
  # -c = local tensor cache (same as donor-serve.sh): the FIRST load of a model streams its weights over the LAN,
  # every later load of the same model comes from this box's own disk. Without it a 20 GB model re-crosses Wi-Fi
  # on every single call (~13 min) -- the difference between a usable remote GPU and a demo.
  $args = @('-H', $BindHost, '-p', "$Port", '-c')
  if ($dev -ne 'CPU') { $args += @('--device', $dev) }   # ggml-rpc-server serves the CPU backend unless told otherwise
  "[{0}] starting: {1} {2}   (build={3}, device={4})" -f (Get-Date -Format o), $exe, ($args -join ' '), $build, $dev | Out-File -Append $log -Encoding utf8
  # Run the server as a child so this loop stays free to manage the keep-awake flag once a minute.
  $p = Start-Process -FilePath $exe -ArgumentList $args -NoNewWindow -PassThru -RedirectStandardOutput "$here\rpc-serve.out" -RedirectStandardError "$here\rpc-serve.err"
  # Once it listens, tell the authority this box lends on this port (idempotent; the authority dials it and says if it
  # cannot). A Windows donor used to stay registered with no port because nothing ever told it (a fresh-box test).
  if ($Python) {
    for ($i = 0; $i -lt 30 -and -not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
    $regArgs = @((Join-Path $here 'genghis_coordinator.py'), 'register', '--port', "$Port")
    if ($Coord) { $regArgs += @('--coord', $Coord) }
    $reg = & $Python @regArgs 2>&1 | ForEach-Object { "$_" }
    "[{0}] register --port {1}: {2}" -f (Get-Date -Format o), $Port, (($reg -join ' ').Trim()) | Out-File -Append $log -Encoding utf8
  }
  $tick = 0
  while (-not $p.HasExited) {
    Set-Awake ((Get-OnAC) -and (Get-Lending))
    if ((++$tick % 10) -eq 0) { Limit-RpcCache }       # every 10 minutes: a long-lived server keeps adding tensors
    Start-Sleep -Seconds 60
    $p.Refresh()
  }
  Set-Awake $false
  "[{0}] ggml-rpc-server exited (code {1}) -- restarting in 5s" -f (Get-Date -Format o), $p.ExitCode | Out-File -Append $log -Encoding utf8
  Start-Sleep -Seconds 5
}
