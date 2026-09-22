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
  [switch]$Yes,                   # non-interactive: accept winget installs without prompting
  [switch]$PreflightOnly          # just detect + report prereqs; change nothing (a safe "doctor")
)
# NOT "Stop": under Stop, Windows PowerShell 5.1 turns ANY stderr line from a native command (git, py, cmake,
# nvcc ... even a friendly "py.exe was updated" notice) into a terminating NativeCommandError. Every step
# below checks its own result explicitly (Ok / Miss / Die), which is the D18 contract anyway.
$ErrorActionPreference = "Continue"
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
function Ask  ($m){ if($Yes){return $true}; $r = Read-Host "  $m [y/N]"; return ($r -match '^(y|yes)$') }

function Winget-Install($id,$label){
  if(-not (Have winget)){ Miss "$label missing and winget isn't available -- install $label manually."; return $false }
  if(Ask "install $label (winget $id)?"){
    Say "  installing $label ..."
    winget install --id $id -s winget --accept-source-agreements --accept-package-agreements --silent | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
    return $true
  }
  return $false
}

Say "== GENGHIS Windows installer (client$(if($Serve){' + serve'})) =="
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
  if(-not $py){ Die "Python 3 is required. Install it (winget install -e --id Python.Python.3.12), open a NEW window, and re-run." }
}

# --- Git ---
if(Have git){ Ok "Git ($((Native git @('--version')) -replace 'git version ',''))" }
else { Miss "Git -- needed to fetch llama.cpp at the pinned commit"; Winget-Install "Git.Git" "Git" | Out-Null }

# --- CMake ---
if(Have cmake){ Ok "CMake ($(((Native cmake @('--version')) -split "`n" | Select-Object -First 1) -replace 'cmake version ',''))" }
else { Miss "CMake -- needed to build llama.cpp"; Winget-Install "Kitware.CMake" "CMake" | Out-Null }

# --- MSVC C++ toolset (can't be auto-installed reliably -> guide) ---
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$hasMSVC = $false
if(Test-Path $vswhere){
  $vc = Native $vswhere @('-latest','-products','*','-requires','Microsoft.VisualStudio.Component.VC.Tools.x86.x64','-property','installationPath')
  if($vc){ $hasMSVC = $true; Ok "MSVC C++ build tools ($vc)" }
}
if(-not $hasMSVC){
  Miss "MSVC C++ build tools -- NOT found (the #1 Windows build snag)"
  Info "Install 'Visual Studio Build Tools' (or Community) and CHECK the workload:"
  Info "  ->  'Desktop development with C++'   (that box is what installs cl.exe + the x64 toolset)"
  Info "  winget install --id Microsoft.VisualStudio.2022.BuildTools -s winget   (then run its installer and tick that workload)"
  if(-not (Ask "continue anyway (the build step will fail until this is installed)?")){ Die "Install the C++ workload, then re-run." }
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
  Say ""
  Say "== Preflight only -- no changes made. =="
  $ready = $py -and $hasMSVC -and (Test-Path (Join-Path $Poc 'genghis_coordinator.py'))
  if($ready){ Ok "ready to build + run the client role (accelerator: $accel)" } else { Miss "install the missing items above, then re-run without -PreflightOnly" }
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
  if(Ask "clone llama.cpp into $Llama at the pinned commit?"){
    git clone https://github.com/ggerganov/llama.cpp $Llama
    Push-Location $Llama; git fetch --depth 1 origin $PinCommit; git checkout $PinCommit; Pop-Location
    Ok "cloned + checked out $PinCommit"
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

# --- First-run fleet (genghis init) ---
Say "== 4. Generate THIS machine's fleet (genghis init -- ships nothing of anyone else's) =="
if($py -and (Test-Path (Join-Path $Poc "genghis_coordinator.py"))){
  $initArgs = @("genghis_coordinator.py","init","--yes")
  if($Coord){ $initArgs += @("--coord",$Coord) }
  if($ModelsDir){ $initArgs += @("--models-dir",$ModelsDir) }
  Push-Location $Poc; & $py @initArgs; $rc = $LASTEXITCODE; Pop-Location
  if($rc -eq 0){ Ok "wrote poc\fleet.json + poc\config.json for $(hostname)" } else { Miss "genghis init exited with code $rc -- see its output above" }
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
  Say "== 6. Reboot-proof RPC donor (ggml-rpc-server on 0.0.0.0:$RpcPort, no-admin Startup launcher) =="
  $launcher = Join-Path $Poc "rpc-serve-windows.ps1"
  if(Test-Path $launcher){
    $s = [Environment]::GetFolderPath('Startup')
    $vbs = Join-Path $s 'GENGHIS-rpc.vbs'
    $cmd = 'CreateObject("WScript.Shell").Run "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File ""'+$launcher+'"" -Port '+$RpcPort+'", 0, False'
    Set-Content $vbs $cmd -Encoding ASCII
    Ok "installed Startup launcher -> ggml-rpc-server auto-starts at logon ($vbs)"
    Info "start it now: wscript `"$vbs`"   -   it registers itself with the coordinator (D33) once the rpc-server is up: py genghis_coordinator.py register"
    Info "NOTE: the GPU needs a user session -> for a headless box enable auto-login (Sysinternals Autologon)."
    if(-not (Get-NetFirewallRule -DisplayName "GENGHIS rpc-server" -ErrorAction SilentlyContinue)){
      Info "firewall: allow inbound TCP $RpcPort from the LAN (admin PowerShell):"
      Info "  New-NetFirewallRule -DisplayName 'GENGHIS rpc-server' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $RpcPort -Profile Private"
    }
  } else { Miss "rpc-serve-windows.ps1 not found -- copy it into poc\ (or run ggml-rpc-server manually)." }
}

# --- Starter model: end the install READY to chat (D28) ---
if($py -and $Coord -and (Test-Path (Join-Path $Poc "genghis_coordinator.py"))){
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
Say "== Done. =="
Info "Next: put GGUFs in your models folder, then:  cd $Poc ;  $py genghis_coordinator.py decide --goal fastest"
Info "Control Room (if serving): http://localhost:8899/   -   full guide: INSTALL.md"
