param(
    [Parameter(Mandatory = $true)]
    [string]$Pi,

    [double]$Seconds = 5.0,
    [string]$Name = "genx320",
    [string]$RemoteDir = "~/genx320_captures",
    [string]$LocalRawDir = ".\rec_eventcam_genx320",
    [string]$PostprocessDir = ".\proc_eventcam_genx320",
    [switch]$SkipDeploy,
    [switch]$InstallPiPythonDeps,
    [switch]$SkipSetupV4l,
    [switch]$SkipPostprocess,
    [switch]$ExportFilteredEventsNpz,
    [double]$Fps = 10000.0,
    [double]$VideoFps = 120.0,
    [int]$AccumulationUs = 100,
    [string]$SyncLedRoi = "",
    [switch]$MaskLedRoi
)

$ErrorActionPreference = "Stop"
$Workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
$PiScript = Join-Path $Workspace "pi_genx320_camera_server.py"
$LocalRawDir = [System.IO.Path]::GetFullPath((Join-Path $Workspace $LocalRawDir))
$PostprocessDir = [System.IO.Path]::GetFullPath((Join-Path $Workspace $PostprocessDir))

if (-not (Test-Path $PiScript)) {
    throw "Pi helper script not found: $PiScript"
}

New-Item -ItemType Directory -Force -Path $LocalRawDir | Out-Null

if (-not $SkipDeploy) {
    Write-Host "[GENX320] Deploying Pi helper to $Pi..."
    scp $PiScript "${Pi}:~/pi_genx320_camera_server.py"
}

if ($InstallPiPythonDeps) {
    Write-Host "[GENX320] Installing/checking Pi Python dependencies..."
    $depCommand = @'
python3 - <<'PY'
missing = []
for name in ['h5py', 'numpy']:
    try:
        __import__(name)
    except Exception:
        missing.append(name)
print('missing=' + ','.join(missing))
PY
if ! python3 -c 'import h5py, numpy' >/dev/null 2>&1; then sudo apt update && sudo apt install -y python3-h5py python3-numpy; fi
python3 - <<'PY'
import h5py
import numpy
print('h5py=' + h5py.__version__)
print('numpy=' + numpy.__version__)
PY
'@
    ssh $Pi $depCommand
}

$setupArg = if ($SkipSetupV4l) { "" } else { " --setup-v4l" }
$remoteCommand = "python3 ~/pi_genx320_camera_server.py capture --seconds $Seconds --basename '$Name' --output-dir '$RemoteDir' --include-status$setupArg"
Write-Host "[GENX320] Starting remote capture..."
$jsonText = ssh $Pi $remoteCommand
try {
    $summary = $jsonText | ConvertFrom-Json
} catch {
    Write-Host "[GENX320][ERROR] Remote command did not return JSON. Raw output:"
    Write-Host $jsonText
    throw
}

if (-not $summary.raw_path) {
    Write-Host $jsonText
    throw "Remote capture did not report raw_path."
}

$remoteRaw = $summary.raw_path
$remoteJson = $summary.summary_path
Write-Host "[GENX320] Remote RAW: $remoteRaw"
Write-Host "[GENX320] Events: $($summary.total_events)"

Write-Host "[GENX320] Copying RAW/JSON to Windows..."
scp "${Pi}:$remoteRaw" $LocalRawDir
scp "${Pi}:$remoteJson" $LocalRawDir

$localRaw = Join-Path $LocalRawDir ([System.IO.Path]::GetFileName($remoteRaw))
$localJson = Join-Path $LocalRawDir ([System.IO.Path]::GetFileName($remoteJson))

Write-Host "[GENX320] Local RAW: $localRaw"
Write-Host "[GENX320] Local JSON: $localJson"

if (-not $SkipPostprocess) {
    Write-Host "[GENX320] Running Windows postprocess..."
    & (Join-Path $Workspace "activate_metavision_env.ps1")

    $postArgs = @(
        (Join-Path $Workspace "eventcam_raw_postprocess.py"),
        $localRaw,
        "--output-dir", $PostprocessDir,
        "--fps", "$Fps",
        "--video-fps", "$VideoFps",
        "--accumulation-us", "$AccumulationUs"
    )
    if ($ExportFilteredEventsNpz) {
        $postArgs += "--export-filtered-events-npz"
    }
    if ($SyncLedRoi) {
        $postArgs += @("--sync-led-roi", $SyncLedRoi)
    }
    if ($MaskLedRoi) {
        $postArgs += "--mask-led-roi"
    }

    python @postArgs
}

[PSCustomObject]@{
    Pi = $Pi
    RemoteRaw = $remoteRaw
    RemoteJson = $remoteJson
    LocalRaw = $localRaw
    LocalJson = $localJson
    TotalEvents = $summary.total_events
    FirstEventTsUs = $summary.first_event_ts_us
    LastEventTsUs = $summary.last_event_ts_us
} | Format-List
