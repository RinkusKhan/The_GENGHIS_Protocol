# GENGHIS POC — client/orchestrator launcher (Windows laptop).
# Splits ONE model across the RPC donors listed in $Donors and runs inference.
#
# Usage:
#   .\run-client.ps1 -Model "E:\models\qwen2.5-1.5b-instruct-q4_k_m.gguf" `
#                    -Donors "<donor-ip>:50052","<donor2-ip>:50052"
#
# The client also contributes its own CPU as a local backend unless -RpcOnly is set.

param(
  [Parameter(Mandatory=$true)][string]$Model,
  [Parameter(Mandatory=$true)][string[]]$Donors,
  [string]$Prompt = "Explain what the GENGHIS Protocol does in two sentences.",
  [int]$NPredict = 128,
  [int]$NGL = 99,          # layers to offload to the RPC devices; 99 = all
  [switch]$IncludeLaptop,  # also let the laptop CPU hold a shard (default: donors only)
  [switch]$Verbose         # -v: print per-layer device placement so you can VERIFY it's remote
)

$ErrorActionPreference = "Stop"
$cli = "E:\The_GENGHIS_Protocol\poc\llama.cpp\build-rpc\bin\Release\llama-cli.exe"
if (-not (Test-Path $cli)) { throw "llama-cli not found at $cli — build may still be running." }
if (-not (Test-Path $Model)) { throw "Model not found: $Model" }

$rpc = ($Donors -join ",")
# Each --rpc endpoint is exposed as a device named RPC0, RPC1, ... in listed order.
$rpcDevices = 0..($Donors.Count - 1) | ForEach-Object { "RPC$_" }
# GOTCHA: '--device none' does NOT mean "RPC only" — it excludes RPC too and falls back to
# the laptop CPU. To force compute onto donors you must NAME the RPC devices explicitly.
$deviceList = if ($IncludeLaptop) { ($rpcDevices + "CPU") -join "," } else { $rpcDevices -join "," }

Write-Host "==> Donors  : $rpc"
Write-Host "==> Devices : $deviceList"
Write-Host "==> Model   : $Model"

$cliArgs = @(
  "-m", $Model,
  "--rpc", $rpc,
  "--device", $deviceList,
  "-ngl", "$NGL",
  "-n", "$NPredict",
  "--single-turn",
  "-p", $Prompt
)
if ($Verbose) { $cliArgs += "-v" }

& $cli @cliArgs
