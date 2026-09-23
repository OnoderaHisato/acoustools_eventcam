<#
.SYNOPSIS
14 mm validation session at the 15 V operating point (README_15V_SESSION.md, 2026-09-24):
stiffness checks, two w-field scans, and the heart / cardioid designs identified at 15 V.

.DESCRIPTION
One PAT session of about 95 minutes. The clock starts when PAT output starts (sound on);
each group begins at the minute the plan asks for, and the particle is held at centre in
between. Start from the cold state (rest or power off for 45 minutes or more) with the
supply set to 15 V.

Timetable (minutes after sound on), from the analysis side:
   0     particle levitated (start of this script)
   5..35 kcheck x / z every 5 min (warm-up watch; the stiffness levelled off near 40 min)
  40     kcheck x / y / z
  43     wscan_XZ_a, wscan_XZ_b            (first scan)
  46     heart 10 Hz: OFF, OFF_Ws4, C_delay, C_delay_Ws4, C_delay_Ws, C_delay, OFF
  56     kcheck x / z
  58     cardioid 10 Hz: OFF, OFF_Ws4, C_delay, C_Ws4, C_Ws, C_delay, OFF
  68     kcheck x / z
  70     heart 7 Hz: OFF, C_delay, C_delay_Ws4, C_delay_Ws, C_delay
  77     cardioid 7 Hz: C_delay, C_Ws4, C_Ws
  82     kcheck x / y / z
  85     wscan_XZ_a, wscan_XZ_b            (second scan, 40+ min after the first)
  95     kcheck x / z

Only the FIRST run has a stereo preview and an Enter checkpoint; the rest start on the
clock. A failed capture asks "re-record? (Y/n)" (-Unattended re-records automatically).
Particle loss is NOT detected automatically.

Run directories end in _V15_<time>; the analysis side matches on cardioid_a5p4, _Ws4_ and _Ws_.
Record one thermal_log row per run (see THERMAL_15V_MEASUREMENT_JP.md) and note the room
temperature and the supply voltage/current at the start and at the end.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_15v_session.ps1 -DryRunOnly

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_15v_session.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_15v_session.ps1 -SkipWarmupChecks
#>
[CmdletBinding()]
param(
    [double]$SupplyVoltage = 15.0,
    [string]$OutputDir = ".\stereo_acoustools_3d_records_V15",
    # Drop the 5..35 min kcheck pairs and just wait for the 40 min point.
    [switch]$SkipWarmupChecks,
    # Drop the optional 7 Hz cardioid block at 77 min.
    [switch]$SkipCardioidF7,
    # Re-record a failed capture automatically instead of asking.
    [switch]$Unattended,
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    [double]$CaptureTailMarginSec = 5.0,
    [switch]$KeepFailedCaptures,
    [switch]$Regenerate,
    [switch]$DryRunOnly
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = ".\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "venv Python not found: $python"
}
$campaign = ".\ff_heart_15v_20260923"
$plan = Join-Path $campaign "ff_heart_15v_plan.json"
$export = Join-Path $campaign "export_ff_heart_15v"
if (-not (Test-Path -LiteralPath $plan)) {
    throw "Plan not found: $plan"
}
if (($SupplyVoltage -le 0.0) -or ($SupplyVoltage -gt 30.0)) {
    throw "-SupplyVoltage must be in (0, 30]; got $SupplyVoltage"
}

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
if ($Regenerate -or $exportIsStale) {
    Write-Host "[FF15V] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) {
        throw "Export generation failed (exit code $LASTEXITCODE)"
    }
}
else {
    Write-Host "[FF15V] Using existing $export"
}

# Ordered (run label, start minute after sound on). An empty minute starts right after the previous run.
$sequence = New-Object System.Collections.ArrayList
function Add-Run([string]$label, $minute) { [void]$sequence.Add([pscustomobject]@{ Label = $label; Minute = $minute }) }

if (-not $SkipWarmupChecks) {
    foreach ($minute in 5, 10, 15, 20, 25, 30, 35) {
        Add-Run "kcheck_x_S105_6jumps" $minute
        Add-Run "kcheck_z_S105_6jumps" $null
    }
}
Add-Run "kcheck_x_S105_6jumps" 40
Add-Run "kcheck_y_S105_6jumps" $null
Add-Run "kcheck_z_S105_6jumps" $null
Add-Run "wscan_XZ_a" 43
Add-Run "wscan_XZ_b" $null
foreach ($label in "heart_f10_OFF", "heart_f10_OFF_Ws4", "heart_f10_C_delay", "heart_f10_C_delay_Ws4",
                   "heart_f10_C_delay_Ws", "heart_f10_C_delay", "heart_f10_OFF") {
    if ($label -eq "heart_f10_OFF" -and $sequence[$sequence.Count - 1].Label -eq "wscan_XZ_b") {
        Add-Run $label 46
    }
    else {
        Add-Run $label $null
    }
}
Add-Run "kcheck_x_S105_6jumps" 56
Add-Run "kcheck_z_S105_6jumps" $null
Add-Run "cardioid_a5p4_f10_OFF" 58
foreach ($label in "cardioid_a5p4_f10_OFF_Ws4", "cardioid_a5p4_f10_C_delay", "cardioid_a5p4_f10_C_Ws4",
                   "cardioid_a5p4_f10_C_Ws", "cardioid_a5p4_f10_C_delay", "cardioid_a5p4_f10_OFF") {
    Add-Run $label $null
}
Add-Run "kcheck_x_S105_6jumps" 68
Add-Run "kcheck_z_S105_6jumps" $null
Add-Run "heart_f7_OFF" 70
foreach ($label in "heart_f7_C_delay", "heart_f7_C_delay_Ws4", "heart_f7_C_delay_Ws", "heart_f7_C_delay") {
    Add-Run $label $null
}
if (-not $SkipCardioidF7) {
    Add-Run "cardioid_a5p4_f7_C_delay" 77
    Add-Run "cardioid_a5p4_f7_C_Ws4" $null
    Add-Run "cardioid_a5p4_f7_C_Ws" $null
}
Add-Run "kcheck_x_S105_6jumps" 82
Add-Run "kcheck_y_S105_6jumps" $null
Add-Run "kcheck_z_S105_6jumps" $null
Add-Run "wscan_XZ_a" 85
Add-Run "wscan_XZ_b" $null
Add-Run "kcheck_x_S105_6jumps" 95
Add-Run "kcheck_z_S105_6jumps" $null

$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$offsets = @()
$recordArgs = @(
    "--hf-export-dir", $export,
    "--output-dir", $OutputDir,
    "--capture-tail-margin-sec", $CaptureTailMarginSec.ToString($invariant),
    "--supply-voltage-v", $SupplyVoltage.ToString($invariant),
    "--unattended-after-first-checkpoint"
)
foreach ($item in $sequence) {
    $recordArgs += @("--hf-run", "$($item.Label)=1")
    if ($null -eq $item.Minute) { $offsets += "" } else { $offsets += (60.0 * [double]$item.Minute).ToString($invariant) }
}
$recordArgs += @("--schedule-offsets-sec", ($offsets -join ","))
if ($KeepFailedCaptures) { $recordArgs += "--keep-failed-captures" }
if ($Unattended) { $recordArgs += @("--automatic-capture-retries", "$AutomaticCaptureRetries") }
else { $recordArgs += "--prompt-on-capture-failure" }

$lastMinute = ($sequence | Where-Object { $null -ne $_.Minute } | Select-Object -Last 1).Minute
Write-Host ("[FF15V] {0} V session: {1} runs, last group at {2} min after sound on." -f $SupplyVoltage, $sequence.Count, $lastMinute)
$index = 0
foreach ($item in $sequence) {
    $index += 1
    $at = if ($null -eq $item.Minute) { "     " } else { "{0,3} m" -f $item.Minute }
    Write-Host ("[FF15V]  {0,3}. {1}  {2}" -f $index, $at, $item.Label)
}

Write-Host "[FF15V] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) {
    throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened."
}
if ($DryRunOnly) {
    Write-Host "[FF15V] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[FF15V] Check before starting:"
Write-Host "  - Supply set to $SupplyVoltage V, boards cold (45 min or more of rest or power off)."
Write-Host "  - Note the room temperature and the supply voltage/current now, and again at the end."
Write-Host "  - Fill one thermal_log row per run; the timing is anchored to PAT output start."
Write-Host "[FF15V] Opening PAT and cameras. Confirm the FIRST run in the stereo preview and press Enter."
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs `
    --acknowledge-ff-validation-protocol --acknowledge-step-response-risk
$code = $LASTEXITCODE
if ($code -eq 0) {
    Write-Host "[FF15V] The session finished. Output: $OutputDir"
    Write-Host "[FF15V] Thermal log: $python .\thermal_log_prefill.py $OutputDir"
}
else {
    Write-Host "[FF15V] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir."
}
exit $code
