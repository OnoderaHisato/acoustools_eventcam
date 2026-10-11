<#
.SYNOPSIS
Measurement session 2 on 2026-10-11 (Experiment/20261011/SESSION2_PLAN_20261011.md): diverse training commands.

.DESCRIPTION
Fans ON, the same particle throughout (a change is recorded), 15 V, current guard 3 %. By the clock only the
first run waits for -WarmupMin (default 40) minutes after sound-on; everything after it runs at once:
  1  kcheck x/y/z, hold_center_10s
  2  wscan_XZ_R28 a/b, wscan_YZ_R28_a
  3  ff_diverse priority A (29), in the README's group order: random -> eight/infinity -> rounded rectangle and
     triangle -> other frequency / size -> helix -> the August 3-D shapes (confirm before the rms 10 mm random)
  4  kcheck x/z
  5  ff_diverse priority B (8)                                    (-SkipPriorityB leaves it out)
  6  heart_s7_f10, cardioid_a5p4_f10, circle_s20_f10, lissajous_s15_f10 OFF (bridge to V15c)
                                                                   (-SkipBridgeOff leaves it out)
  7  kcheck x/y/z, hold; then the compensation test ff_nn_v15c alternating with the same shape's OFF
     (heart_s7_f10 OFF, C_delay, C_nn_M1z x2; heart_s7_f7, cardioid_a5p4_f10, cardioid_a10_f10,
     lissajous_s15_f10, circle_s20_f10 each OFF then C_nn_M1z)  (-SkipNnTest leaves the whole of 7 out)
  8  kcheck x/y/z, hold_center_10s
The random designs (random_*, aug_random_*) can drop the particle: if it falls, replace it (the script asks
nothing; stop with Ctrl+C), then resume with -StartAt at the same run once; if it falls again, resume after it.
Output: stereo_acoustools_3d_records_V15c (same state as the 10/10 records; run folders carry the date).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_session2_20261011.ps1 -DryRunOnly
powershell -ExecutionPolicy Bypass -File .\run_session2_20261011.ps1 -PsuUsb
powershell -ExecutionPolicy Bypass -File .\run_session2_20261011.ps1 -WarmupMin 0 -StartAt 12 -PsuUsb
#>
[CmdletBinding()]
param(
    [double]$SupplyVoltage = 15.0,
    [string]$OutputDir = "",
    [double]$WarmupMin = 40.0,
    [double]$CurrentGuardPercent = 3.0,
    [switch]$NoCurrentGuard,
    [switch]$Unattended,
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    [double]$CaptureTailMarginSec = 5.0,
    [switch]$KeepFailedCaptures,
    [switch]$SkipPriorityB,
    [switch]$SkipBridgeOff,
    [switch]$SkipNnTest,
    [int]$StartAt = 1,
    [switch]$NoThermalPrompts,
    [double]$ThermalPromptTimeoutSec = 180.0,
    [string]$PatBoardGapMm = "236.5-237",
    [switch]$Regenerate,
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
if (-not (Test-Path -LiteralPath $python)) { throw "venv Python not found: $python" }
$campaign = ".\session2_20261011"
$plan = Join-Path $campaign "session2_plan.json"
$export = Join-Path $campaign "export_session2"
if (-not (Test-Path -LiteralPath $plan)) { throw "Plan not found: $plan" }
if (($SupplyVoltage -le 0.0) -or ($SupplyVoltage -gt 30.0)) { throw "-SupplyVoltage must be in (0, 30]; got $SupplyVoltage" }
if ($WarmupMin -lt 0.0) { throw "-WarmupMin must not be negative" }
if ($OutputDir -eq "") {
    # Keep the folder name this short: a longer one shortens the run names.
    $voltageTag = if ([math]::Abs($SupplyVoltage - [math]::Round($SupplyVoltage)) -lt 1e-9) { "{0:0}" -f $SupplyVoltage } else { ("{0}" -f $SupplyVoltage).Replace(".", "p") }
    $OutputDir = ".\stereo_acoustools_3d_records_V${voltageTag}c"
}

$manifest = Join-Path $export "export_manifest.json"
$exportIsStale = $true
if (Test-Path -LiteralPath $manifest) {
    $planHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $plan).Hash.ToLowerInvariant()
    try {
        $manifestHash = [string]((Get-Content -Raw -Encoding UTF8 -LiteralPath $manifest | ConvertFrom-Json).source_plan_sha256)
        $exportIsStale = ($manifestHash.ToLowerInvariant() -ne $planHash)
    }
    catch { $exportIsStale = $true }
}
if ($Regenerate -or $exportIsStale) {
    Write-Host "[SESSION2] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) { throw "Export generation failed (exit code $LASTEXITCODE)" }
}
else {
    Write-Host "[SESSION2] Using existing $export"
}

$runs = New-Object System.Collections.ArrayList
$minutes = New-Object System.Collections.ArrayList
$checkpoints = @()
function Add-Run([string]$label, $minute = $null) { [void]$runs.Add($label); [void]$minutes.Add($minute) }
$kx = "kcheck_x_S105_6jumps"; $ky = "kcheck_y_S105_6jumps"; $kz = "kcheck_z_S105_6jumps"; $hold = "hold_center_10s"

$priorityA = @(
    "random_rms3_5-40Hz_s0_OFF", "random_rms3_5-40Hz_s1_OFF", "random_rms6_5-30Hz_s0_OFF", "random_rms6_5-30Hz_s1_OFF",
    "random_rms10_5-20Hz_s0_OFF", "random_rms10_5-20Hz_s1_OFF", "random_rms6_slow_s0_OFF",
    "eight_v_s7_f7_OFF", "infinity_s7_f7_OFF", "eight_v_s7_f10_OFF", "infinity_s7_f10_OFF", "eight_v_s7_tilt30_f7_OFF",
    "rect14x10_r2_f7_OFF", "rect14x10_r2_f10_OFF", "tri14_r2_f7_OFF", "tri14_r2_f10_OFF",
    "heart_s7_f5_OFF", "heart_s7_f13_OFF", "cardioid_a5p4_f13_OFF", "heart_s5_f10_OFF", "heart_s10_f10_OFF",
    "helix_r7_h5_f7_OFF",
    "aug_trefoil_f7_OFF", "aug_torohelix_f7_OFF", "aug_figure8_3d_f7_OFF", "aug_deltoid_xz_y_f7_OFF",
    "aug_hypo5_xz_y_f7_OFF", "aug_random_balanced_OFF", "aug_random_depth_rich_OFF"
)
$priorityB = @(
    "random_rms6_slow_s1_OFF", "aug_nephroid_xy_z_f7_OFF", "aug_astroid_yz_x_f7_OFF", "aug_nephroid_r30_f8_OFF",
    "aug_hypo6_xy_z_f5_OFF", "aug_lissajous3d_f7_OFF", "aug_random_vertical_OFF", "aug_random_highfreq_OFF"
)

# 1
if ($WarmupMin -gt 0.0) { $useClock = $true; Add-Run $kx $WarmupMin } else { $useClock = $false; Add-Run $kx }
Add-Run $ky; Add-Run $kz; Add-Run $hold
# 2
Add-Run "wscan_XZ_R28_a"; Add-Run "wscan_XZ_R28_b"; Add-Run "wscan_YZ_R28_a"
# 3
foreach ($d in $priorityA) {
    if ($d -eq "random_rms10_5-20Hz_s0_OFF") { $checkpoints += $runs.Count + 1 }
    Add-Run $d
}
# 4
Add-Run $kx; Add-Run $kz
# 5
if (-not $SkipPriorityB) { foreach ($d in $priorityB) { Add-Run $d } }
# 6
if (-not $SkipBridgeOff) {
    foreach ($s in @("heart_s7_f10", "cardioid_a5p4_f10", "circle_s20_f10", "lissajous_s15_f10")) { Add-Run "${s}_OFF" }
}
# 7
if (-not $SkipNnTest) {
    Add-Run $kx; Add-Run $ky; Add-Run $kz; Add-Run $hold
    Add-Run "heart_s7_f10_OFF"; Add-Run "heart_s7_f10_C_delay"; Add-Run "heart_s7_f10_C_nn_M1z"; Add-Run "heart_s7_f10_C_nn_M1z"
    foreach ($s in @("heart_s7_f7", "cardioid_a5p4_f10", "cardioid_a10_f10", "lissajous_s15_f10", "circle_s20_f10")) {
        Add-Run "${s}_OFF"; Add-Run "${s}_C_nn_M1z"
    }
}
# 8
Add-Run $kx; Add-Run $ky; Add-Run $kz; Add-Run $hold
$guardBaseline = if ($WarmupMin -gt 0.0) { $WarmupMin } else { 5.0 }

if ($StartAt -lt 1 -or $StartAt -gt $runs.Count) { throw "-StartAt must be between 1 and $($runs.Count)" }
if ($StartAt -gt 1) {
    # Resume from run $StartAt of the same list at once (no clock); the runs are renumbered from 1.
    $useClock = $false
    $shift = $StartAt - 1
    $runs = [System.Collections.ArrayList]@($runs[$shift..($runs.Count - 1)])
    $minutes = [System.Collections.ArrayList]@($minutes[$shift..($minutes.Count - 1)])
    $checkpoints = @($checkpoints | Where-Object { $_ -gt $StartAt } | ForEach-Object { $_ - $shift })
    $guardBaseline = 5.0
    Write-Host "[SESSION2] -StartAt ${StartAt}: resuming from run $StartAt of the full list; runs are renumbered from 1."
}

$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$recordArgs = @(
    "--hf-export-dir", $export,
    "--output-dir", $OutputDir,
    "--capture-tail-margin-sec", $CaptureTailMarginSec.ToString($invariant),
    "--supply-voltage-v", $SupplyVoltage.ToString($invariant),
    "--unattended-after-first-checkpoint"
)
foreach ($label in $runs) { $recordArgs += @("--hf-run", "$label=1") }
if ($useClock) {
    $offsets = @()
    foreach ($minute in $minutes) {
        if ($null -eq $minute) { $offsets += "" } else { $offsets += (60.0 * [double]$minute).ToString($invariant) }
    }
    $recordArgs += @("--schedule-offsets-sec", ($offsets -join ","))
}
if ($checkpoints.Count -gt 0) { $recordArgs += @("--checkpoint-run-numbers", ($checkpoints -join ",")) }
if (-not $NoThermalPrompts) {
    # Plan: temperatures every 10-15 minutes.
    $recordArgs += @("--thermal-prompt-every", "0:10", "--thermal-prompt-timeout-sec", $ThermalPromptTimeoutSec.ToString($invariant))
}
if ($PatBoardGapMm -ne "") { $recordArgs += @("--pat-board-gap-mm", $PatBoardGapMm) }
if ($KeepFailedCaptures) { $recordArgs += "--keep-failed-captures" }
if ($Unattended) { $recordArgs += @("--automatic-capture-retries", "$AutomaticCaptureRetries") }
else { $recordArgs += "--prompt-on-capture-failure" }

Write-Host ("[SESSION2] {0} V, {1} runs, output {2}." -f $SupplyVoltage, $runs.Count, $OutputDir)
$index = 0
foreach ($label in $runs) {
    $index += 1
    $at = if ($null -eq $minutes[$index - 1]) { "     " } else { "{0,3} m" -f $minutes[$index - 1] }
    $mark = if ($checkpoints -contains $index) { "  <- confirm" } else { "" }
    Write-Host ("[SESSION2]  {0,3}. {1}  {2}{3}" -f $index, $at, $label, $mark)
}

Write-Host "[SESSION2] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) { throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened." }
if ($DryRunOnly) {
    Write-Host "[SESSION2] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[SESSION2] Check before starting:"
Write-Host "  - Supply at $SupplyVoltage V, cooling fans ON. Keep the same particle all through (a change is recorded only if you note it)."
Write-Host "  - PAT-start LED bright (kcheck LED peak count 1000 or more), and no glint of the lighting LED inside the left LED ROI."
Write-Host "  - Temperatures (top/bottom PAT, room, humidity) are asked on screen every 10 minutes; -NoThermalPrompts turns that off."
Write-Host "  - If the particle falls (most likely in random_*), stop with Ctrl+C, replace it, and resume with -WarmupMin 0 -StartAt <run>."
$psuTarget = Get-PsuTargetArgs -Usb:$PsuUsb -Resource $PsuResource -HostName $PsuHost
$psuLogger = Start-PsuLogger -Python $python -OutputDir $OutputDir -TargetArgs $psuTarget -IntervalSec $PsuIntervalSec
if ($null -ne $psuLogger) { $recordArgs += @("--psu-log", $psuLogger.Log) }
$recordArgs += Get-CurrentGuardArgs -Logger $psuLogger -Percent $CurrentGuardPercent -BaselineMin $guardBaseline -Disabled:$NoCurrentGuard
try {
    & $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs `
        --acknowledge-ff-validation-protocol --acknowledge-step-response-risk
    $code = $LASTEXITCODE
}
finally {
    Stop-PsuLogger $psuLogger
    Write-PsuReport -Python $python -OutputDir $OutputDir -Logger $psuLogger
}
if ($code -eq 0) { Write-Host "[SESSION2] Finished. Output: $OutputDir" }
elseif ($code -eq 3) { Write-Host "[SESSION2] The current guard stopped the session before the next run (see current_guard in the session JSON)." }
else { Write-Host "[SESSION2] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir." }
exit $code
