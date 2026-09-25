# GENGHIS -- Windows installer (D18: preflight + guided remediation, not error-dump-and-exit).
# Sets up a Windows box as a CLIENT (launch models) and optionally a /v1 SERVE host. Idempotent -- re-run
# any time; it detects what's already done and only fills gaps.
#
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1                 # client (default)
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Serve          # + reboot-proof /v1 serve
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Coord <authority-ip>   # pin the coordinator
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Donor          # + reboot-proof RPC donor (lend this GPU/CPU)
#   ... -LlamaDir C:\path\to\llama.cpp   # reuse an existing pinned-commit clone/build instead of cloning under poc\
#   ... -Yes            # non-interactive: auto-accept winget installs
#
# Accelerator is auto-detected: NVIDIA + CUDA toolkit -> build-cuda; any other GPU (Intel Arc / AMD) + the
# Vulkan SDK -> build-vulkan (D27: e.g. the NUC's Arc becomes this box's local anchor AND, with -Donor, a
# donor for everyone else); otherwise the CPU + RPC build.
#
# It will NEVER silently change your system: every install is announced and (unless -Yes) confirmed. The two
# things Windows can't reliably automate -- the VS C++ build tools workload and the NVIDIA/CUDA driver -- are
# DETECTED and you're told exactly what to click.

[CmdletBinding()]
param(
  [switch]$Serve,                 # also install the reboot-proof /v1 serve (Startup launcher)
  [switch]$Donor,                 # also install the reboot-proof RPC donor (ggml-rpc-server on :50052, Startup launcher)
  [string]$LlamaDir = "",         # existing llama.cpp clone to reuse (junctioned to poc\llama.cpp); default: clone under poc\
  [int]$RpcPort = 50052,          # donor port (with -Donor)
  [string]$Coord = "",            # coordinator host[:port] to pin (else localhost + mDNS)
  [string]$ModelsDir = "",        # where GGUFs live (default: <repo>\poc\models)
  [string]$Name = "",             # this box's name in the fleet (default: its hostname)
  [switch]$Yes,                   # non-interactive: accept winget installs without prompting
  [switch]$PreflightOnly          # just detect + report prereqs; change nothing (a safe "doctor")
)
# NOT "Stop": under Stop, Windows PowerShell 5.1 turns ANY stderr line from a native command (git, py, cmake,
# nvcc ... even a friendly "py.exe was updated" notice) into a terminating NativeCommandError. Every step
# below checks its own result explicitly (Ok / Miss / Die), which is the D18 contract anyway.
$ErrorActionPreference = "Continue"
# Python writes UTF-8; Windows PowerShell 5.1 decodes a native program's output with the OEM code page, so dashes and
# dots arrived as "ΓÇö" in any logged install (a fresh-box test, 2026-09-24).
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:PYTHONIOENCODING = "utf-8"
$RepoRoot = Split-Path -Parent $PSScriptRoot          # install\ is under the repo root
$Poc      = Join-Path $RepoRoot "poc"
$PinCommit = "eab8ee41f889ef7823af517e8098fb8a9b3cf601"

function Say  ($m){ Write-Host $m }
function Ok   ($m){ Write-Host "  [ ok ] $m" -ForegroundColor Green }
function Miss ($m){ Write-Host "  [ -- ] $m" -ForegroundColor Yellow }
function Info ($m){ Write-Host "  $m" -ForegroundColor DarkGray }
function Die  ($m){ Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }
function Have ($c){ [bool](Get-Command $c -ErrorAction SilentlyContinue) }
# Run a native command and return its combined stdout+stderr as one trimmed string, never throwing.
function Native { param([string]$exe, [string[]]$argv)
  try { $o = (& $exe @argv 2>&1 | ForEach-Object { "$_" }) -join "`n"; return $o.Trim() } catch { return "" }
}
# Nobody at the keyboard (over SSH, or run by an agent): Read-Host returns "" at once and every question silently
# became "no" (a fresh-box test, 2026-09-23). Say so, and how to consent, instead.
$Interactive = -not [Console]::IsInputRedirected
function Ask  ($m){
  if($Yes){ return $true }
  if(-not $Interactive){ Write-Host "  [ ?? ] $m -- nobody at the keyboard to answer, so: NO (re-run with -Yes to accept)" -ForegroundColor Yellow; return $false }
  $r = Read-Host "  $m [y/N]"; return ($r -match '^(y|yes)$')
}

function Winget-Install($id,$label){
  # -PreflightOnly promises "change nothing": say what WOULD be installed, never install (not even with -Yes).
  if($PreflightOnly){ Info "Fix: winget install -e --id $id   ($label)"; return $false }
  if(-not (Have winget)){ Miss "$label missing and winget isn't available -- install $label manually."; return $false }
  if(Ask "install $label (winget $id)?"){
    Say "  installing $label ..."
    winget install --id $id -s winget --accept-source-agreements --accept-package-agreements --silent | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
    return $true
  }
  return $false
}

Say "== GENGHIS Windows installer ($((@('client') + @(if($Serve){'serve'}) + @(if($Donor){'donor'})) -join ' + ')) =="
Say "== 1. Preflight -- what's here, what's missing =="

# --- Python ---
function Find-Python {
  # Candidates in order: a real python.exe from the python.org installer (never the Store shim), then py, then python.
  $c = @()
  $c += Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe') -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
  $c += Get-ChildItem 'C:\Program Files\Python3*\python.exe' -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
  if(Have py){ $c += "py" }
  if(Have python){ $c += (Get-Command python).Source }
  foreach($cand in $c){
    if($cand -match 'WindowsApps'){ continue }                       # the Store alias shim, not an interpreter
    $v = Native $cand @("-c","import sys;print('%d.%d.%d'%sys.version_info[:3])")
    if($v -match '^3\.(\d+)\.(\d+)$'){ return @($cand, $v) }
  }
  return $null
}
$found = Find-Python
if($found){ $py = $found[0]; Ok "Python $($found[1]) ($py)" } else {
  $py = $null
  Miss "Python 3 -- no working interpreter found (a bare 'py' launcher / install manager without a runtime does not count)"
  if(Winget-Install "Python.Python.3.12" "Python 3.12"){ $found = Find-Python; if($found){ $py = $found[0]; Ok "Python $($found[1]) ($py)" } }
  # Preflight reports everything missing, not just the first thing (it used to stop here, and Git, CMake and the
  # C++ tools were never listed).
  if(-not $py -and -not $PreflightOnly){ Die "Python 3 is required. Install it (winget install -e --id Python.Python.3.12), open a NEW window, and re-run." }
}

# --- pypdf: reads PDFs attached to a chat and PDFs in a role's knowledge folder (D50). Offered on EVERY Windows install:
#     a Windows PC is the box a person sits at, and the one most likely to start answering chats later (-Serve). Installed
#     into $py -- the same Python the serve launcher picks -- because `py` can be a DIFFERENT Python: the reference
#     laptop's `py -m pip install pypdf` went into 3.13 while its serve runs 3.11. Optional: without it every PDF is
#     named as unreadable, never skipped quietly. ---
if($py){
  if((Native $py @("-c","import pypdf;print('ok')")) -eq "ok"){ Ok "pypdf (PDFs attached to a chat, or in a role's knowledge folder, can be read)" }
  else {
    Miss "pypdf -- optional: without it, a PDF attached to a chat or in a role's knowledge folder is not read (and says so)"
    if($PreflightOnly){ Info "Fix: $py -m pip install pypdf" }
    elseif(Ask "pip install pypdf (so PDFs in chats and role knowledge can be read)?"){
      Native $py @("-m","pip","install","--user","pypdf") | Out-Null
      if((Native $py @("-c","import pypdf;print('ok')")) -eq "ok"){ Ok "installed pypdf" }
      else { Miss "pip install pypdf did not work -- run it yourself: $py -m pip install pypdf" }
    }
  }
}

# --- Git ---
function Show-Git  { Ok "Git ($((Native git @('--version')) -replace 'git version ',''))" }
function Show-CMake{ Ok "CMake ($(((Native cmake @('--version')) -split "`n" | Select-Object -First 1) -replace 'cmake version ',''))" }
if(Have git){ Show-Git }
else { Miss "Git -- needed to fetch llama.cpp at the pinned commit"; if((Winget-Install "Git.Git" "Git") -and (Have git)){ Show-Git } }

# --- CMake ---
if(Have cmake){ Show-CMake }
else { Miss "CMake -- needed to build llama.cpp"; if((Winget-Install "Kitware.CMake" "CMake") -and (Have cmake)){ Show-CMake } }

# --- MSVC C++ toolset (can't be auto-installed reliably -> guide) ---
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
function Find-MSVC {
  if(-not (Test-Path $vswhere)){ return $null }
  $vc = Native $vswhere @('-latest','-products','*','-requires','Microsoft.VisualStudio.Component.VC.Tools.x86.x64','-property','installationPath')
  if($vc){ return $vc } else { return $null }
}
$vc = Find-MSVC; $hasMSVC = [bool]$vc
if($hasMSVC){ Ok "MSVC C++ build tools ($vc)" }
else {
  # Unattended, the workload included: no screen needed (the old advice, "run its installer and tick the workload",
  # stopped an agent cold, and with -Yes the install carried on without a compiler and built nothing).
  $vsId = "Microsoft.VisualStudio.2022.BuildTools"
  $vsOverride = "--quiet --wait --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
  Miss "MSVC C++ build tools -- NOT found (needed to build llama.cpp)"
  if($PreflightOnly){ Info "Fix: winget install -e --id $vsId -s winget --override `"$vsOverride`"   (unattended, ~10 min)" }
  elseif(-not (Have winget)){ Info "Install 'Visual Studio Build Tools 2022' with the 'Desktop development with C++' workload, then re-run." }
  elseif(Ask "install the Visual Studio C++ build tools now (unattended, about 10 minutes and a few GB)?"){
    Say "  installing the C++ build tools (unattended; this takes a while) ..."
    winget install -e --id $vsId -s winget --accept-source-agreements --accept-package-agreements --override $vsOverride | Out-Null
    $vc = Find-MSVC; $hasMSVC = [bool]$vc
    if($hasMSVC){ Ok "MSVC C++ build tools ($vc)" } else { Miss "the build tools install did not finish -- see above" }
  }
  if(-not $hasMSVC -and -not $PreflightOnly){
    Die "the C++ build tools are required to build llama.cpp. Install them (winget install -e --id $vsId -s winget --override `"$vsOverride`"), then re-run."
  }
}

# --- Accelerator: CUDA? Vulkan? else CPU ---
$hasCuda = (Have nvcc)
$hasNvidiaGpu = (Have nvidia-smi)
$gpus = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
$otherGpu = @($gpus | Where-Object { $_ -and ($_ -notmatch 'NVIDIA') -and ($_ -notmatch 'Basic Display|Remote Display|Virtual') }) | Select-Object -First 1
# Vulkan SDK = what a GGML_VULKAN build needs (headers + glslc). Detect via VULKAN_SDK env or glslc on PATH.
$vkSdk = $env:VULKAN_SDK
if(-not $vkSdk -and (Have glslc)){ $vkSdk = Split-Path -Parent (Split-Path -Parent (Get-Command glslc).Source) }
$hasVulkan = [bool]$vkSdk
$accel = "cpu"
if($hasCuda){
  $accel = "cuda"; Ok "CUDA toolkit ($(((Native nvcc @('--version')) -split "`n" | Select-String 'release') -replace '.*release ',''))"
}
elseif($hasNvidiaGpu){
  Miss "NVIDIA GPU present but no CUDA toolkit -- for the fastest local anchor, install it"
  Info "  winget install --id Nvidia.CUDA -s winget   (or from nvidia.com); then re-run to build with GPU"
}
if($accel -ne "cuda" -and $otherGpu){
  if($hasVulkan){ $accel = "vulkan"; Ok "Vulkan GPU path: '$otherGpu' + Vulkan SDK ($vkSdk)" }
  else {
    Miss "GPU '$otherGpu' found but no Vulkan SDK -- without it this box builds CPU-only and the GPU idles"
    if(Winget-Install "KhronosGroup.VulkanSDK" "Vulkan SDK"){
      $vkSdk = [Environment]::GetEnvironmentVariable("VULKAN_SDK","Machine"); if(-not $vkSdk){ $vkSdk = [Environment]::GetEnvironmentVariable("VULKAN_SDK","User") }
      if($vkSdk){ $env:VULKAN_SDK = $vkSdk; $accel = "vulkan"; Ok "Vulkan SDK installed ($vkSdk)" }
      else { Info "  SDK installed but VULKAN_SDK isn't visible in this shell yet -- re-run the installer from a NEW window to build with Vulkan." }
    } else { Info "  later: winget install --id KhronosGroup.VulkanSDK -s winget ; then re-run (new window) to build with the GPU" }
  }
}
if($accel -eq "cpu"){ Info "No usable GPU toolchain detected -- will build the CPU + RPC path (still a full client / donor)." }

if($PreflightOnly){
  $role = (@('client') + @(if($Serve){'serve'}) + @(if($Donor){'donor'})) -join ' + '
  # Everything the real run will change on this box, so a person saying "yes" (-Yes) has seen it first.
  if($Coord){
    $ch = ($Coord -split ':')[0]
    $up = try { $c = New-Object Net.Sockets.TcpClient; $r = $c.ConnectAsync($ch, 8899).Wait(2000); $c.Close(); $r } catch { $false }
    if($up){ Ok "the authority answers at ${ch}:8899" } else { Miss "the authority at ${ch}:8899 does not answer -- check the address and that its serve runs" }
  }
  if($Donor){
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $prof = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | ForEach-Object { "$($_.InterfaceAlias): $($_.NetworkCategory)" }) -join ', '
    Say ""
    Say "  The real run will also, for the donor:"
    Info ("  - start ggml-rpc-server on :$RpcPort now, and " + $(if($accel -eq 'cpu' -and $isAdmin){ "at every boot (a scheduled task as SYSTEM; a CPU donor needs no login)" } else { "at every logon (a Startup entry)" }))
    Info "  - open TCP $RpcPort in Windows Firewall for this local network only, every profile (this network: $prof):"
    Info "      New-NetFirewallRule -DisplayName 'GENGHIS rpc-server $RpcPort' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $RpcPort -Profile Any -RemoteAddress LocalSubnet"
    Info "  - register this box's port with the authority, which then dials it"
    if(-not $isAdmin){ Miss "not running as administrator: the C++ tools install and the firewall rule need it" }
  }
  Say ""
  Say "== Preflight only -- no changes made. =="
  $ready = $py -and $hasMSVC -and (Test-Path (Join-Path $Poc 'genghis_coordinator.py'))
  if($ready){ Ok "ready to build + set up: $role (accelerator: $accel)" } else { Miss "the missing items above will be installed by the real run (they need your yes, or -Yes), or install them yourself" }
  exit 0
}

# --- llama.cpp source at the pinned commit ---
Say "== 2. llama.cpp source (pinned commit) =="
$Llama = Join-Path $Poc "llama.cpp"
if($LlamaDir -and -not (Test-Path (Join-Path $Llama ".git"))){
  # reuse an existing clone (e.g. the NUC's donor-session build): junction poc\llama.cpp -> it, so the
  # coordinator's build lookup (poc\llama.cpp\build-*\bin\Release) finds it without copying gigabytes
  if(-not (Test-Path (Join-Path $LlamaDir ".git"))){ Die "-LlamaDir '$LlamaDir' is not a git checkout of llama.cpp" }
  if(Test-Path $Llama){ Miss "poc\llama.cpp exists but is not a checkout -- remove it or drop -LlamaDir" }
  else { New-Item -ItemType Junction -Path $Llama -Target $LlamaDir | Out-Null; Ok "junction poc\llama.cpp -> $LlamaDir" }
}
if(Test-Path (Join-Path $Llama ".git")){
  Ok "llama.cpp present ($Llama)"
  $at = Native git @('-C',$Llama,'rev-parse','HEAD')
  if($at -and $at -ne $PinCommit){ Miss "checkout is at $($at.Substring(0,9)), NOT the pinned $($PinCommit.Substring(0,9)) -- RPC has zero cross-version tolerance; run: git -C `"$Llama`" checkout $PinCommit" }
} else {
  if(Ask "fetch llama.cpp into $Llama at the pinned commit?"){
    # Only the pinned commit, not the whole history (a full clone first cost minutes and hundreds of MB for nothing).
    New-Item -ItemType Directory -Force $Llama | Out-Null
    git -C $Llama init -q
    git -C $Llama remote add origin https://github.com/ggml-org/llama.cpp 2>$null
    git -C $Llama fetch --depth 1 origin $PinCommit
    git -C $Llama checkout -q FETCH_HEAD
    if((Native git @('-C',$Llama,'rev-parse','HEAD')) -eq $PinCommit){ Ok "fetched + checked out $PinCommit" }
    else { Die "could not fetch llama.cpp at $PinCommit -- check the internet connection and re-run." }
  } else { Info "skipped -- the build step needs it." }
}

# --- Build ---
Say "== 3. Build llama.cpp (RPC has ZERO cross-version tolerance -> the pin above is load-bearing) =="
if($hasMSVC -and (Test-Path $Llama)){
  if($hasCuda){
    $build = Join-Path $Llama "build-cuda"
    if(Test-Path (Join-Path $build "bin\Release\llama-cli.exe")){ Ok "build-cuda already built" }
    else {
      Say "  configuring build-cuda (CUDA + RPC, arch=native) ..."
      cmake -S $Llama -B $build -A x64 -DGGML_CUDA=ON -DGGML_RPC=ON -DCMAKE_CUDA_ARCHITECTURES=native -DLLAMA_CURL=OFF
      cmake --build $build --config Release --target llama-cli llama-server ggml-rpc-server -j
      if(Test-Path (Join-Path $build "bin\Release\llama-cli.exe")){ Ok "built build-cuda (llama-cli, llama-server, ggml-rpc-server)" }
      else { Miss "build-cuda did not produce bin\Release\llama-cli.exe -- read the cmake output above (usually a missing toolchain piece); re-run after fixing." }
    }
  } elseif($accel -eq "vulkan"){
    $build = Join-Path $Llama "build-vulkan"
    if(Test-Path (Join-Path $build "bin\Release\llama-cli.exe")){ Ok "build-vulkan already built" }
    else {
      Say "  configuring build-vulkan (Vulkan + RPC) ..."
      cmake -S $Llama -B $build -A x64 -DGGML_VULKAN=ON -DGGML_RPC=ON -DLLAMA_CURL=OFF
      cmake --build $build --config Release --target llama-cli llama-server ggml-rpc-server -j
      if(Test-Path (Join-Path $build "bin\Release\llama-cli.exe")){ Ok "built build-vulkan (llama-cli, llama-server, ggml-rpc-server)" }
      else { Miss "build-vulkan did not produce bin\Release\llama-cli.exe -- read the cmake output above (usually a missing toolchain piece); re-run after fixing." }
    }
  } else {
    $build = Join-Path $Llama "build-rpc"
    if(Test-Path (Join-Path $build "bin\Release\llama-cli.exe")){ Ok "build-rpc already built" }
    else {
      Say "  configuring build-rpc (CPU + RPC) ..."
      cmake -S $Llama -B $build -DGGML_RPC=ON -DLLAMA_CURL=OFF
      cmake --build $build --config Release --target llama-cli llama-server ggml-rpc-server -j
      if(Test-Path (Join-Path $build "bin\Release\llama-cli.exe")){ Ok "built build-rpc (llama-cli, llama-server, ggml-rpc-server)" }
      else { Miss "build-rpc did not produce bin\Release\llama-cli.exe -- read the cmake output above (usually a missing toolchain piece); re-run after fixing." }
    }
  }
} else { Miss "skipping build -- need the C++ workload + llama.cpp source above first." }
# A donor lends through ggml-rpc-server; without it the install is not a donor, whatever else went right. It used to carry
# on, register with no port, and end in a passing verify (a fresh-box test, 2026-09-23).
$RpcExe = Get-ChildItem (Join-Path $Llama "build-*\bin\Release\ggml-rpc-server.exe") -ErrorAction SilentlyContinue | Select-Object -First 1
if($Donor -and -not $RpcExe){ Die "a donor needs ggml-rpc-server.exe, and the build above did not produce it -- read the build output, fix it, and re-run." }

# --- First-run fleet (genghis init) ---
Say "== 4. Generate THIS machine's fleet (genghis init -- ships nothing of anyone else's) =="
if($py -and (Test-Path (Join-Path $Poc "genghis_coordinator.py"))){
  $initArgs = @("genghis_coordinator.py","init","--yes")
  if($Coord){ $initArgs += @("--coord",$Coord) }
  if($ModelsDir){ $initArgs += @("--models-dir",$ModelsDir) }
  if($Name){ $initArgs += @("--node-name",$Name) }
  Push-Location $Poc; & $py @initArgs; $rc = $LASTEXITCODE; Pop-Location
  if($rc -eq 0){ Ok "wrote poc\fleet.json + poc\config.json for $(if($Name){ $Name } else { hostname })" } else { Miss "genghis init exited with code $rc -- see its output above" }
  if($Coord){ [Environment]::SetEnvironmentVariable("GENGHIS_COORD",$Coord,"User"); $env:GENGHIS_COORD = $Coord; Ok "pinned GENGHIS_COORD=$Coord (user env; also set for this session)" }
} else { Miss "genghis_coordinator.py not found under $Poc -- copy the repo's poc\ folder here." }

# --- Optional: reboot-proof /v1 serve ---
if($Serve){
  Say "== 5. Reboot-proof /v1 serve (no-admin Startup launcher) =="
  $launcher = Join-Path $Poc "serve-laptop.ps1"
  if(Test-Path $launcher){
    $s = [Environment]::GetFolderPath('Startup')
    $vbs = Join-Path $s 'GENGHIS-serve.vbs'
    $cmd = 'CreateObject("WScript.Shell").Run "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File ""'+$launcher+'""", 0, False'
    Set-Content $vbs $cmd -Encoding ASCII
    Ok "installed Startup launcher -> serve auto-starts at logon ($vbs)"
    Info "start it now: wscript `"$vbs`"   -   /v1 + Control Room will be at http://localhost:8899/"
    # Reachability from OTHER boxes: a per-app python.exe rule is Private-profile only and stops matching the
    # moment Windows reclassifies the NIC as Public (seen after an RDP reconnect on the NUC). A port rule on
    # Profile Any is what actually keeps :8899 reachable. Needs admin -> offer, else print the one-liner.
    if(-not (Get-NetFirewallRule -DisplayName "GENGHIS serve 8899" -ErrorAction SilentlyContinue)){
      $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
      if($isAdmin -and (Ask "open TCP 8899 in Windows Firewall so other devices can reach this Control Room / API?")){
        New-NetFirewallRule -DisplayName "GENGHIS serve 8899" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8899 -Profile Any | Out-Null
        Ok "firewall: TCP 8899 allowed (all profiles)"
      } else {
        Info "firewall: to reach this box from the LAN / Tailscale, run once in an ADMIN PowerShell:"
        Info "  New-NetFirewallRule -DisplayName 'GENGHIS serve 8899' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8899 -Profile Any"
      }
    } else { Ok "firewall: TCP 8899 already allowed" }
  } else { Miss "serve-laptop.ps1 not found -- copy it into poc\ (or run serve manually)." }
}

# --- Optional: reboot-proof RPC donor (lend this box's GPU/CPU to the pool) ---
if($Donor){
  Say "== 6. The RPC donor (ggml-rpc-server on 0.0.0.0:$RpcPort), started now and after every reboot =="
  $launcher = Join-Path $Poc "rpc-serve-windows.ps1"
  if(Test-Path $launcher){
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $launchArgs = "-Port $RpcPort" + $(if($py){ " -Python `"$py`"" } else { "" }) + $(if($Coord){ " -Coord `"$Coord`"" } else { "" })
    $taskName = "GENGHIS rpc-server $RpcPort"
    $vbs = Join-Path ([Environment]::GetFolderPath('Startup')) 'GENGHIS-rpc.vbs'
    function PortListening($p){ [bool](Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue) }
    if($accel -eq "cpu" -and $isAdmin){
      # A CPU donor needs no desktop session: a scheduled task at BOOT, as SYSTEM, lends again after a reboot with nobody
      # logged in. (Only a Startup entry used to exist, so a rebooted headless CPU donor sat idle until someone logged on.)
      $act  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" $launchArgs"
      $trig = New-ScheduledTaskTrigger -AtStartup
      $prin = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
      $set  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
      Register-ScheduledTask -TaskName $taskName -Action $act -Trigger $trig -Principal $prin -Settings $set -Force | Out-Null
      if(Test-Path $vbs){ Remove-Item $vbs -Force }          # one way to start it, not two
      Ok "scheduled task '$taskName': the donor starts at boot, no login needed"
      if(-not (PortListening $RpcPort)){ Start-ScheduledTask -TaskName $taskName }
    } else {
      # A GPU needs a user session (session 0 can't reach it): a Startup entry, which runs at logon.
      $cmd = 'CreateObject("WScript.Shell").Run "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File ""'+$launcher+'"" '+($launchArgs -replace '"','""')+'", 0, False'
      Set-Content $vbs $cmd -Encoding ASCII
      Ok "installed Startup launcher -> the donor starts at logon ($vbs)"
      if($accel -eq "cpu"){ Info "run this installer as administrator to make it start at boot instead (a CPU donor needs no login)" }
      else { Info "NOTE: a GPU needs a logged-in session -> for a headless box enable auto-login (Sysinternals Autologon)." }
      # Start it NOW, through WMI, so it outlives this window -- and an SSH session, whose processes Windows ends when it
      # closes (a `wscript` started over SSH died with it, 2026-09-23). The launcher holds a per-port lock, so the copy the
      # Startup entry starts at next logon cannot run a second server beside this one.
      if(-not (PortListening $RpcPort)){ Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "wscript.exe `"$vbs`"" } | Out-Null }
    }
    for($i = 0; $i -lt 60 -and -not (PortListening $RpcPort); $i++){ Start-Sleep 1 }
    if(PortListening $RpcPort){ Ok "ggml-rpc-server listening on :$RpcPort" }
    else { Miss "ggml-rpc-server did not start listening on :$RpcPort within a minute -- see $Poc\rpc-serve.log" }

    # The fleet has to be able to reach it. A fresh Windows network is usually classed PUBLIC, so a Private-only rule
    # (the old advice) does not apply; this one covers every profile but only this LAN.
    $fw = "GENGHIS rpc-server $RpcPort"
    $fwCmd = "New-NetFirewallRule -DisplayName '$fw' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $RpcPort -Profile Any -RemoteAddress LocalSubnet"
    $old = Get-NetFirewallRule -DisplayName "GENGHIS rpc-server" -ErrorAction SilentlyContinue   # an earlier install's rule
    if(Get-NetFirewallRule -DisplayName $fw -ErrorAction SilentlyContinue){ Ok "firewall: TCP $RpcPort already allowed from this network" }
    elseif($old -and ($old.Profile -eq 'Any')){ Ok "firewall: TCP $RpcPort already allowed ('GENGHIS rpc-server')" }
    else {
      if($isAdmin -and (Ask "open TCP $RpcPort in Windows Firewall, for this local network only, so the fleet can use this box?")){
        Invoke-Expression "$fwCmd | Out-Null"
        Ok "firewall: TCP $RpcPort allowed from this local network (every network profile)"
      } else {
        Miss "firewall: the fleet cannot reach this donor until TCP $RpcPort is allowed. Run once in an ADMIN PowerShell:"
        Info "  $fwCmd"
      }
    }

    # Tell the authority the port NOW that the server is up (init, above, ran before it was). The authority dials it
    # and says at once if it cannot reach it.
    if($py){
      $regArgs = @("genghis_coordinator.py","register","--port","$RpcPort")
      if($Coord){ $regArgs += @("--coord",$Coord) }
      Push-Location $Poc; & $py @regArgs; Pop-Location
    }
  } else { Miss "rpc-serve-windows.ps1 not found -- copy it into poc\ (or run ggml-rpc-server manually)." }
}

# --- Starter model: end the install READY to chat (D28) ---
if($py -and $Coord -and ($Serve -or -not $Donor) -and (Test-Path (Join-Path $Poc "genghis_coordinator.py"))){
  # (Not for a pure donor: it runs no chats of its own, and the 1.1 GB download was pure waste.)
  Say "== 7. Starter model =="
  $starter = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
  $modelsDir = if($ModelsDir){ $ModelsDir } else { Join-Path $Poc "models" }
  if(Test-Path (Join-Path $modelsDir $starter)){ Ok "$starter already local" }
  elseif(Ask "pull the starter model now from the coordinator ($starter, ~1.1 GB) so the first chat is instant?"){
    Push-Location $Poc; & $py genghis_coordinator.py models pull $starter; $rc = $LASTEXITCODE; Pop-Location
    if($rc -eq 0){ Ok "starter model cached" } else { Miss "pull failed (code $rc) -- serve will fetch it in the background on first use instead" }
  } else { Info "skipped -- serve pre-fetches missing models in the background at startup; the first chat says so if it isn't here yet." }
}

Say ""
Say "== Installed. Full guide: INSTALL.md =="
Info "Next: put GGUFs in your models folder, then:  cd $Poc ;  $py genghis_coordinator.py decide --goal fastest"

# The finish line: an install is done when `verify` passes, not when this script reaches its last line. verify is
# read-only; it prints what it checked (with the fix for anything that failed) and the addresses worth bookmarking
# (real LAN / Tailscale addresses that answered -- never "localhost"), saved to %USERPROFILE%\genghis-addresses.txt.
# Its exit code becomes ours, so a script or an agent can gate on it.
function PortUp($h, $p){ try { $c = New-Object Net.Sockets.TcpClient; $ok = $c.ConnectAsync($h, $p).Wait(1000); $c.Close(); $ok } catch { $false } }
$rc = 0
if($py -and (Test-Path (Join-Path $Poc "genghis_coordinator.py"))){
  $waitFor = if($Coord){ ($Coord -split ':')[0] } elseif($Serve){ "127.0.0.1" } else { $null }
  if($waitFor){ for($i = 0; $i -lt 20; $i++){ if(PortUp $waitFor 8899){ break }; Start-Sleep 1 } }   # a just-started serve needs a moment
  Say ""
  Say "== Verify: is this box really in the fleet? (read-only; re-run any time: cd $Poc ; $py genghis_coordinator.py verify) =="
  $vArgs = @("genghis_coordinator.py","verify"); if($Donor){ $vArgs += "--expect-donor" }   # a donor with no port FAILs
  Push-Location $Poc; & $py @vArgs; $rc = $LASTEXITCODE; Pop-Location
} else { Info "verify skipped (Python or poc\genghis_coordinator.py missing)" }
exit $rc
