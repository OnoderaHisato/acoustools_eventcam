<#
.SYNOPSIS
Generalization session at the 15 V operating point (Experiment/20260927/GENERALIZATION_SESSION_PLAN.md):
model values fixed from steps, kcheck and scans only; unseen shapes, positions, 3-D paths, time and particle.

.DESCRIPTION
Each block starts cold: warm-up -WarmupMin (default 40) with kcheck x/z every 5 minutes (-SkipWarmupChecks
just waits), then the block's runs. Leave 45 minutes or more without sound between the blocks (the
particle falls when PAT stops). The current guard (3 %) stops before the next run when the current has
risen 3 % above its value at the end of the warm-up; each block is meant to end within 65 minutes.

Block 1 (31 runs, about 25 minutes after the warm-up):
  kcheck x/y/z, vzrstep_XL30, (confirm) wscan_XZ_R28 a/b, hold_center_10s,
  shapes heart_s7 / cardioid_a10 / lissajous_s15 / circle_s20 x {OFF, C_delay, C_Ws4},
  positions heart_s7_{xp15,xm15,zp15,zm15}_C_Ws4 and heart_s7_{yp10,ym10}_C_W3,
  3-D circle_tilt30_s13 / lissajous3d_s13 x {OFF, C_W3}, kcheck x/z.
Block 2 (15 runs, about 12 minutes after the warm-up):
  kcheck x/y/z, hold_center_10s, the 4 shapes C_Ws4,
  (replace the particle; the time is recorded) hold_center_10s, the 4 shapes C_Ws4, kcheck x/z.
-IncludeW3f adds the optional _C_W3f runs (3-D shapes and the y-shifted hearts, 4 runs) before the
closing kcheck.

Run names use short design tags: C_delay = C_delay_inverse, C_Ws4 = C_delay_inverse_Ws4,
C_W3 / C_W3f = C_delay_inverse_W3 / _W3f (full names in feedforward_design of each manifest).
Make the PAT-start LED bright enough: the kcheck LED peak count should be 1000 or more.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_generalization_15v.ps1 -Block 1 -DryRunOnly

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_generalization_15v.ps1 -Block 1 -PsuUsb
powershell -ExecutionPolicy Bypass -File .\run_generalization_15v.ps1 -Block 2 -PsuUsb
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("1", "2")]
    [string]$Block,
    [double]$SupplyVoltage = 15.0,
    # Separate from the 9/23-9/25 sessions in stereo_acoustools_3d_records_V15. Keep the name this short:
    # a longer folder name shortens the run names.
    [string]$OutputDir = ".\stereo_acoustools_3d_records_V15g",
    [double]$WarmupMin = 40.0,
    [switch]$SkipWarmupChecks,
    [switch]$IncludeW3f,
    [double]$CurrentGuardPercent = 3.0,
    [switch]$NoCurrentGuard,
    [switch]$Unattended,
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    [double]$CaptureTailMarginSec = 5.0,
    [switch]$KeepFailedCaptures,
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
$campaign = ".\generalization_20260927"
$plan = Join-Path $campaign "generalization_plan.json"
$export = Join-Path $campaign "export_generalization"
if (-not (Test-Path -LiteralPath $plan)) { throw "Plan not found: $plan" }
if (($SupplyVoltage -le 0.0) -or ($SupplyVoltage -gt 30.0)) { throw "-SupplyVoltage must be in (0, 30]; got $SupplyVoltage" }
if ($WarmupMin -le 0.0) { throw "-WarmupMin must be positive (each block starts cold with its warm-up)" }

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
    Write-Host "[GENERALIZATION] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) { throw "Export generation failed (exit code $LASTEXITCODE)" }
}
else {
    Write-Host "[GENERALIZATION] Using existing $export"
}

$shapes = @("heart_s7", "cardioid_a10", "lissajous_s15", "circle_s20")
$runs = New-Object System.Collections.ArrayList
$minutes = New-Object System.Collections.ArrayList
$checkpoints = @()
$particleChange = @()
function Add-Run([string]$label, $minute = $null) { [void]$runs.Add($label); [void]$minutes.Add($minute) }

if (-not $SkipWarmupChecks) {
    for ($minute = 5.0; $minute -lt $WarmupMin - 1e-9; $minute += 5.0) {
        Add-Run "kcheck_x_S105_6jumps" $minute
        Add-Run "kcheck_z_S105_6jumps"
    }
}
Add-Run "kcheck_x_S105_6jumps" $WarmupMin
Add-Run "kcheck_y_S105_6jumps"; Add-Run "kcheck_z_S105_6jumps"
if ($Block -eq "1") {
    Add-Run "vzrstep_XL30"
    $checkpoints += $runs.Count + 1          # confirm the particle survived the 3.0 mm steps
    Add-Run "wscan_XZ_R28_a"; Add-Run "wscan_XZ_R28_b"
    Add-Run "hold_center_10s"
    foreach ($shape in $shapes) {
        foreach ($design in @("OFF", "C_delay", "C_Ws4")) { Add-Run "${shape}_f10_${design}" }
    }
    foreach ($offset in @("xp15", "xm15", "zp15", "zm15")) { Add-Run "heart_s7_${offset}_f10_C_Ws4" }
    foreach ($offset in @("yp10", "ym10")) { Add-Run "heart_s7_${offset}_f10_C_W3" }
    foreach ($shape in @("circle_tilt30_s13", "lissajous3d_s13")) {
        Add-Run "${shape}_f10_OFF"; Add-Run "${shape}_f10_C_W3"
    }
}
else {
    Add-Run "hold_center_10s"
    foreach ($shape in $shapes) { Add-Run "${shape}_f10_C_Ws4" }
    $particleChange += $runs.Count + 1       # replace the particle before the second hold
    Add-Run "hold_center_10s"
    foreach ($shape in $shapes) { Add-Run "${shape}_f10_C_Ws4" }
}
if ($IncludeW3f) {
    foreach ($label in @("circle_tilt30_s13_f10_C_W3f", "lissajous3d_s13_f10_C_W3f", "heart_s7_yp10_f10_C_W3f", "heart_s7_ym10_f10_C_W3f")) {
        Add-Run $label
    }
}
Add-Run "kcheck_x_S105_6jumps"; Add-Run "kcheck_z_S105_6jumps"

$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$recordArgs = @(
    "--hf-export-dir", $export,
    "--output-dir", $OutputDir,
    "--capture-tail-margin-sec", $CaptureTailMarginSec.ToString($invariant),
    "--supply-voltage-v", $SupplyVoltage.ToString($invariant),
    "--unattended-after-first-checkpoint"
)
foreach ($label in $runs) { $recordArgs += @("--hf-run", "$label=1") }
$offsets = @()
foreach ($minute in $minutes) {
    if ($null -eq $minute) { $offsets += "" } else { $offsets += (60.0 * [double]$minute).ToString($invariant) }
}
$recordArgs += @("--schedule-offsets-sec", ($offsets -join ","))
if ($checkpoints.Count -gt 0) { $recordArgs += @("--checkpoint-run-numbers", ($checkpoints -join ",")) }
if ($particleChange.Count -gt 0) { $recordArgs += @("--particle-change-run-numbers", ($particleChange -join ",")) }
if ($KeepFailedCaptures) { $recordArgs += "--keep-failed-captures" }
if ($Unattended) { $recordArgs += @("--automatic-capture-retries", "$AutomaticCaptureRetries") }
else { $recordArgs += "--prompt-on-capture-failure" }

$afterWarmup = $runs.Count - $(if ($SkipWarmupChecks) { 0 } else { 2 * [math]::Ceiling(($WarmupMin - 5.0) / 5.0 - 1e-9) })
Write-Host ("[GENERALIZATION] Block {0}, {1} V: {2} runs ({3} from {4} min after sound on)." -f `
    $Block, $SupplyVoltage, $runs.Count, $afterWarmup, $WarmupMin)
$index = 0
foreach ($label in $runs) {
    $index += 1
    $at = if ($null -eq $minutes[$index - 1]) { "     " } else { "{0,3} m" -f $minutes[$index - 1] }
    $mark = if ($particleChange -contains $index) { "  <- replace the particle (time recorded)" } elseif ($checkpoints -contains $index) { "  <- confirm" } else { "" }
    Write-Host ("[GENERALIZATION]  {0,3}. {1}  {2}{3}" -f $index, $at, $label, $mark)
}

Write-Host "[GENERALIZATION] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) { throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened." }
if ($DryRunOnly) {
    Write-Host "[GENERALIZATION] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[GENERALIZATION] Check before starting:"
Write-Host "  - Supply at $SupplyVoltage V and the boards cold (45 min or more without sound)."
Write-Host "  - PAT-start LED bright: kcheck LED peak count 1000 or more."
Write-Host "  - Confirm the particle at the preview right after PAT opens; after that the runs follow the clock."
Write-Host "  - Stop with Ctrl+C if the particle is gone; the session then stops and PAT is turned off."
$psuTarget = Get-PsuTargetArgs -Usb:$PsuUsb -Resource $PsuResource -HostName $PsuHost
$psuLogger = Start-PsuLogger -Python $python -OutputDir $OutputDir -TargetArgs $psuTarget -IntervalSec $PsuIntervalSec
if ($null -ne $psuLogger) { $recordArgs += @("--psu-log", $psuLogger.Log) }
$recordArgs += Get-CurrentGuardArgs -Logger $psuLogger -Percent $CurrentGuardPercent -BaselineMin $WarmupMin -Disabled:$NoCurrentGuard
try {
    & $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs `
        --acknowledge-ff-validation-protocol --acknowledge-step-response-risk
    $code = $LASTEXITCODE
}
finally {
    Stop-PsuLogger $psuLogger
    Write-PsuReport -Python $python -OutputDir $OutputDir -Logger $psuLogger
}
if ($code -eq 0) { Write-Host "[GENERALIZATION] Block $Block finished. Output: $OutputDir" }
elseif ($code -eq 3) { Write-Host "[GENERALIZATION] The current guard stopped the block before the next run (see current_guard in the session JSON). Let the boards cool before continuing." }
else { Write-Host "[GENERALIZATION] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir." }
exit $code
