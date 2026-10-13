<#
.SYNOPSIS
Extra measurement 3 on 2026-10-12 (Experiment/20261012/MESSAGE_TO_ACQUISITION_PC_20261012.md, ff_extra3/).

.DESCRIPTION
Fans ON, the same particle throughout, 15 V, current guard 3 %. Only the first run waits for -WarmupMin (default
40) minutes after sound-on; everything after it runs at once (29 runs, PAT motion 4.3 min):
  1  kcheck x/y/z, hold_center_10s
  2  circles: radius 20 mm at 7, 8, 9, 10, 11, 12, 13 Hz, then 10 Hz at radius 14, 17, 23 mm
  3  stiffness grid in the README order: kgrid x3, x4, z1, z2, x2, x5, z3, z4, x1, x6 (22.9 s each)
  4  diagonal steps kdiag_p45 / m45 (priority B): the staircase version (axis xy, one-sample steps) by default;
     -DiagImported takes the imported 5.65 ms steps instead; -SkipDiag leaves them out
  5  kcheck x/y/z
If the particle falls, stop with Ctrl+C, replace it, and resume with -WarmupMin 0 -StartAt <run> (once; if it
falls again, resume after it). Output: stereo_acoustools_3d_records_V15c.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .un_extra3_20261012.ps1 -DryRunOnly
powershell -ExecutionPolicy Bypass -File .un_extra3_20261012.ps1 -PsuUsb
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
    [switch]$DiagImported,
    [switch]$SkipDiag,
    [int]$StartAt = 1,
    [switch]$NoThermalPrompts,
    [double]$ThermalPromptTimeoutSec = 180.0,
    [string]$PatBoardGapMm = "236.5-237",
    [switch]$Regenerate,
    [switch]$PsuUsb,
    [string]$PsuResource = "",
    [string]$PsuHost = "",
    [double]$PsuIntervalSec = 1.0,
    [string]$EnvDevice = "F9EE9FFC",
    [switch]$NoEnvSensor,
    [switch]$DryRunOnly
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "psu_logging.ps1")
. (Join-Path $PSScriptRoot "env_logging.ps1")
Set-Location -LiteralPath $PSScriptRoot

$python = ".\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "venv Python not found: $python" }
$campaign = ".\extra3_20261012"
$plan = Join-Path $campaign "extra3_plan.json"
$export = Join-Path $campaign "export_extra3"
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
    Write-Host "[EXTRA3] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) { throw "Export generation failed (exit code $LASTEXITCODE)" }
}
else {
    Write-Host "[EXTRA3] Using existing $export"
}

$runs = New-Object System.Collections.ArrayList
$minutes = New-Object System.Collections.ArrayList
$checkpoints = @()
function Add-Run([string]$label, $minute = $null) { [void]$runs.Add($label); [void]$minutes.Add($minute) }
$kx = "kcheck_x_S105_6jumps"; $ky = "kcheck_y_S105_6jumps"; $kz = "kcheck_z_S105_6jumps"; $hold = "hold_center_10s"
if ($DiagImported -and $SkipDiag) { throw "-DiagImported and -SkipDiag cannot be used together" }

# 1
if ($WarmupMin -gt 0.0) { $useClock = $true; Add-Run $kx $WarmupMin } else { $useClock = $false; Add-Run $kx }
Add-Run $ky; Add-Run $kz; Add-Run $hold
# 2
foreach ($f in @(7, 8, 9, 10, 11, 12, 13)) { Add-Run "circle_s20_f${f}_OFF" }
foreach ($r in @(14, 17, 23)) { Add-Run "circle_s${r}_f10_OFF" }
# 3
foreach ($p in @("x3", "x4", "z1", "z2", "x2", "x5", "z3", "z4", "x1", "x6")) { Add-Run "kgrid_${p}_OFF" }
# 4
if (-not $SkipDiag) {
    if ($DiagImported) { Add-Run "kdiag_p45_OFF"; Add-Run "kdiag_m45_OFF" }
    else { Add-Run "kdiag_p45_S105_6jumps"; Add-Run "kdiag_m45_S105_6jumps" }
}
# 5
Add-Run $kx; Add-Run $ky; Add-Run $kz
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
    Write-Host "[EXTRA3] -StartAt ${StartAt}: resuming from run $StartAt of the full list; runs are renumbered from 1."
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
    # Temperatures every 10 minutes as in session 2.
    $recordArgs += @("--thermal-prompt-every", "0:10", "--thermal-prompt-timeout-sec", $ThermalPromptTimeoutSec.ToString($invariant))
}
if ($PatBoardGapMm -ne "") { $recordArgs += @("--pat-board-gap-mm", $PatBoardGapMm) }
if ($KeepFailedCaptures) { $recordArgs += "--keep-failed-captures" }
if ($Unattended) { $recordArgs += @("--automatic-capture-retries", "$AutomaticCaptureRetries") }
else { $recordArgs += "--prompt-on-capture-failure" }

Write-Host ("[EXTRA3] {0} V, {1} runs, output {2}." -f $SupplyVoltage, $runs.Count, $OutputDir)
$index = 0
foreach ($label in $runs) {
    $index += 1
    $at = if ($null -eq $minutes[$index - 1]) { "     " } else { "{0,3} m" -f $minutes[$index - 1] }
    $mark = if ($checkpoints -contains $index) { "  <- confirm" } else { "" }
    Write-Host ("[EXTRA3]  {0,3}. {1}  {2}{3}" -f $index, $at, $label, $mark)
}

Write-Host "[EXTRA3] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) { throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened." }
if ($DryRunOnly) {
    Write-Host "[EXTRA3] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[EXTRA3] Check before starting:"
Write-Host "  - Supply at $SupplyVoltage V, cooling fans ON. Keep the same particle all through (a change is recorded only if you note it)."
Write-Host "  - PAT-start LED bright (kcheck LED peak count 1000 or more), and no glint of the lighting LED inside the left LED ROI."
Write-Host "  - Room temperature, humidity and air pressure are logged from the Tempo Disc (close the bmLogger app; -NoEnvSensor turns it off)."
Write-Host "  - Temperatures (top/bottom PAT, room, humidity) are asked on screen every 10 minutes; -NoThermalPrompts turns that off."
Write-Host "  - If the particle falls, stop with Ctrl+C, replace it, and resume with -WarmupMin 0 -StartAt <run>."
$psuTarget = Get-PsuTargetArgs -Usb:$PsuUsb -Resource $PsuResource -HostName $PsuHost
$psuLogger = Start-PsuLogger -Python $python -OutputDir $OutputDir -TargetArgs $psuTarget -IntervalSec $PsuIntervalSec
$envLogger = $null
if (-not $NoEnvSensor) { $envLogger = Start-EnvLogger -Python $python -OutputDir $OutputDir -Device $EnvDevice }
if ($null -ne $envLogger) { $recordArgs += @("--env-log", $envLogger.Log) }
if ($null -ne $psuLogger) { $recordArgs += @("--psu-log", $psuLogger.Log) }
$recordArgs += Get-CurrentGuardArgs -Logger $psuLogger -Percent $CurrentGuardPercent -BaselineMin $guardBaseline -Disabled:$NoCurrentGuard
try {
    & $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs `
        --acknowledge-ff-validation-protocol --acknowledge-step-response-risk
    $code = $LASTEXITCODE
}
finally {
    Stop-EnvLogger $envLogger
    Stop-PsuLogger $psuLogger
    Write-PsuReport -Python $python -OutputDir $OutputDir -Logger $psuLogger
}
if ($code -eq 0) { Write-Host "[EXTRA3] Finished. Output: $OutputDir" }
elseif ($code -eq 3) { Write-Host "[EXTRA3] The current guard stopped the session before the next run (see current_guard in the session JSON)." }
else { Write-Host "[EXTRA3] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir." }
exit $code
