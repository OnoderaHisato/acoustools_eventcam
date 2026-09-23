<#
.SYNOPSIS
Thermal hold test at reduced supply voltage (THERMAL_PLAN_20260922.md section 3a, session 1):
kcheck x / y / z every 5 minutes for 60 minutes with the particle held and no trajectory in between.

.DESCRIPTION
1. Uses the stiffness checks kcheck_x/y/z_S105_6jumps of ff_heart_20260918\export_ff_heart
   (1.05 mm, 6 jumps, 2.0 s holds, 14 s each). The export is regenerated when it no longer
   matches ff_heart_plan.json.
2. Runs the hardware-free dry run.
3. Opens PAT/OpenMPD once. The clock starts when PAT output starts (sound on). Confirm the
   particle in the stereo preview right after sound on and press Enter; group 0 (x, y, z) is
   recorded right away.
   Group g (g = 1, 2, ...) starts g x IntervalMin minutes after sound on. Between groups the
   particle is held at centre with PAT on (this counts as operating, not as rest).
   A failed capture asks "re-record? (Y/n)" (-Unattended re-records automatically instead).
   Particle loss is NOT detected automatically.
4. Every run directory ends in _V<voltage>_<time> and records supply_voltage_V.

The voltage is set on the power supply by the operator; this script only labels the runs.
Start from the cold state (rest or power off for 45 minutes or more).
After the session:  .\venv\Scripts\python.exe .\thermal_log_prefill.py <OutputDir>

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_thermal_hold_test.ps1 -SupplyVoltage 15 -DryRunOnly

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_thermal_hold_test.ps1 -SupplyVoltage 15

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_thermal_hold_test.ps1 -SupplyVoltage 12
#>
[CmdletBinding()]
param(
    # Supply voltage set on the power supply (required, so that runs are never mislabelled).
    [Parameter(Mandatory = $true)]
    [double]$SupplyVoltage,
    # Default: .\stereo_acoustools_3d_records_V<voltage> (one folder per voltage, short enough
    # that the _V<voltage> tag is never cut from the run directory names).
    [string]$OutputDir = "",
    # Test length and check interval in minutes: groups at 0, 5, ..., 60 min -> 13 groups.
    [double]$DurationMin = 60.0,
    [double]$IntervalMin = 5.0,
    # Axes checked in each group, in this order.
    [string]$Axes = "x,y,z",
    # Re-record a failed capture automatically instead of asking.
    [switch]$Unattended,
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    [double]$CaptureTailMarginSec = 5.0,
    [switch]$KeepFailedCaptures,
    # Log the PAT supply (Kikusui PWR801L) during the session: -PsuUsb finds it on USB (needs a
    # VISA library such as KI-VISA), -PsuResource names a VISA resource, -PsuHost uses the LAN.
    # The logger is read-only and is started before PAT opens. See PSU_LOGGING_JP.md.
    [switch]$PsuUsb,
    [string]$PsuResource = "",
    [string]$PsuHost = "",
    [double]$PsuIntervalSec = 1.0,
    [switch]$DryRunOnly
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "psu_logging.ps1")
Set-Location -LiteralPath $PSScriptRoot

$python = ".\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "venv Python not found: $python"
}
$campaign = ".\ff_heart_20260918"
$plan = Join-Path $campaign "ff_heart_plan.json"
$export = Join-Path $campaign "export_ff_heart"

if (($SupplyVoltage -le 0.0) -or ($SupplyVoltage -gt 30.0)) {
    throw "-SupplyVoltage must be in (0, 30]; got $SupplyVoltage"
}
if (($IntervalMin -le 0.0) -or ($DurationMin -lt 0.0)) {
    throw "-IntervalMin must be positive and -DurationMin must not be negative"
}
$checkLabels = @{
    "x" = "kcheck_x_S105_6jumps"
    "y" = "kcheck_y_S105_6jumps"
    "z" = "kcheck_z_S105_6jumps"
}
$selectedAxes = @(
    $Axes.Split(",") | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ -ne "" }
)
if ($selectedAxes.Count -eq 0) {
    throw "No axes selected. Use a comma-separated list of x, y, z."
}
foreach ($axis in $selectedAxes) {
    if (-not $checkLabels.ContainsKey($axis)) {
        throw "Unknown axis '$axis'. Use x, y, z."
    }
}

$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$groups = [int][Math]::Floor($DurationMin / $IntervalMin + 1e-9) + 1
$intervalSec = $IntervalMin * 60.0
$voltageText = $SupplyVoltage.ToString($invariant)
if ($OutputDir -eq "") {
    $OutputDir = ".\stereo_acoustools_3d_records_V" + $voltageText.Replace(".", "p")
}
$intervalText = $intervalSec.ToString($invariant)
$marginText = $CaptureTailMarginSec.ToString($invariant)

$manifest = Join-Path $export "export_manifest.json"
$exportIsStale = $true
if (Test-Path -LiteralPath $manifest) {
    $planHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $plan).Hash.ToLowerInvariant()
    try {
        $manifestHash = [string]((Get-Content -Raw -Encoding UTF8 -LiteralPath $manifest | ConvertFrom-Json).source_plan_sha256)
        $exportIsStale = ($manifestHash.ToLowerInvariant() -ne $planHash)
    }
    catch {
        $exportIsStale = $true
    }
}
if ($exportIsStale) {
    Write-Host "[THERMAL] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) {
        throw "Export generation failed (exit code $LASTEXITCODE). Run run_ff_heart_validation.ps1 -DryRunOnly to see which source command is missing."
    }
}
else {
    Write-Host "[THERMAL] Using existing $export"
}

$requests = @()
for ($group = 0; $group -lt $groups; $group++) {
    foreach ($axis in $selectedAxes) {
        $requests += "$($checkLabels[$axis])=1"
    }
}

# About 1.6 GB of left+right events per 14 s check; stop before a long test fills the disk.
$needGB = 1.6 * $requests.Count
$root = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot $OutputDir)))
$freeGB = (New-Object System.IO.DriveInfo($root)).AvailableFreeSpace / 1GB
Write-Host ("[THERMAL] Disk {0}: {1:N0} GB free, about {2:N0} GB needed for {3} runs." -f $root, $freeGB, $needGB, $requests.Count)
if ($freeGB -lt $needGB + 20.0) {
    throw ("Not enough free space on {0}: {1:N0} GB free, {2:N0} GB needed plus 20 GB margin." -f $root, $freeGB, $needGB)
}

$recordArgs = @(
    "--hf-export-dir", $export,
    "--output-dir", $OutputDir,
    "--capture-tail-margin-sec", $marginText,
    "--supply-voltage-v", $voltageText,
    "--unattended-after-first-checkpoint",
    "--schedule-interval-sec", $intervalText,
    "--schedule-group-size", "$($selectedAxes.Count)"
)
foreach ($request in $requests) {
    $recordArgs += @("--hf-run", $request)
}
if ($KeepFailedCaptures) {
    $recordArgs += "--keep-failed-captures"
}
if ($Unattended) {
    $recordArgs += @("--automatic-capture-retries", "$AutomaticCaptureRetries")
}
else {
    $recordArgs += "--prompt-on-capture-failure"
}

Write-Host ("[THERMAL] {0} V hold test: {1} groups ({2}) every {3} min, 0 to {4} min after sound on; {5} runs in one PAT session." -f $voltageText, $groups, ($selectedAxes -join ", "), $IntervalMin, (($groups - 1) * $IntervalMin), $requests.Count)

Write-Host "[THERMAL] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) {
    throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened."
}
if ($DryRunOnly) {
    Write-Host "[THERMAL] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[THERMAL] Check before starting:"
Write-Host "  - The power supply is set to $voltageText V and the boards are cold (45 min or more of rest or power off)."
Write-Host "  - Read the supply current right after the sound starts and at every group (thermal log)."
Write-Host "  - Temperature sensors stay outside the acoustic field."
Write-Host "[THERMAL] Opening PAT. The clock starts now (sound on): levitate the particle, confirm it in the stereo preview and press Enter."
$psuTarget = Get-PsuTargetArgs -Usb:$PsuUsb -Resource $PsuResource -HostName $PsuHost
$psuLogger = Start-PsuLogger -Python $python -OutputDir $OutputDir -TargetArgs $psuTarget -IntervalSec $PsuIntervalSec
if ($null -ne $psuLogger) { $recordArgs += @("--psu-log", $psuLogger.Log) }
try {
    & $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --acknowledge-step-response-risk
    $code = $LASTEXITCODE
}
finally {
    Stop-PsuLogger $psuLogger
    Write-PsuReport -Python $python -OutputDir $OutputDir -Logger $psuLogger
}
if ($code -eq 0) {
    Write-Host "[THERMAL] All groups were recorded. Output: $OutputDir"
}
else {
    Write-Host "[THERMAL] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir."
    Write-Host "[THERMAL] If the PAT stopped by itself (resettable fuse), note the time as last_trip_at."
}
Write-Host "[THERMAL] Thermal log: $python .\thermal_log_prefill.py $OutputDir"
exit $code
