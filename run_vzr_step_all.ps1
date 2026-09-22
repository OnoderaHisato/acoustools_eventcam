<#
.SYNOPSIS
Record the step-type vzr identification commands (x or y jumps mixed with z jumps) in one PAT session.

.DESCRIPTION
1. Generates the export from vzr_step_20260919\vzr_step_plan.json
   (skipped while export_manifest.json matches the plan; use -Regenerate).
   The commands are generated natively (kind=staircase with level_sequence_mm) and are
   numerically identical to the analysis-side commands\vzrstep_*.npz.
2. Runs the hardware-free dry run for the selected sequence.
3. Opens PAT/OpenMPD once and records the sequence. Default (VZR_STEP_PLAN.md):
     XS1 -> XS2 -> XS2 -> XS2
   By default only the FIRST run has a stereo preview and an Enter checkpoint; the rest
   start automatically. A failed capture still asks "re-record? (Y/n)".
   -ConfirmEachRun restores the preview and Enter before every run.
   -Unattended re-records a failed capture automatically instead of asking.
   Particle loss is NOT detected automatically.

Rest the trap for at least 10 minutes before starting (plan requirement; not enforced here).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_vzr_step_all.ps1 -DryRunOnly

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_vzr_step_all.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_vzr_step_all.ps1 -Runs "XS1"

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_vzr_step_all.ps1 -Runs "XS3,YS2"
#>
[CmdletBinding()]
param(
    [string]$OutputDir = ".\stereo_acoustools_3d_records_vzr_step",
    # Ordered run list; repeats allowed. XS1/XS2/XS3 = x +/-0.6/0.8/1.0 mm with z +/-0.4/0.5/0.6 mm,
    # YS1/YS2/YS3 = the same with y as the horizontal axis.
    [string]$Runs = "XS1,XS2,XS2,XS2",
    # Scale applied to every selected command (0 < scale <= 1).
    [double]$CommandScale = 1.0,
    # Preview and Enter before every run (the behaviour before 2026-09-20).
    [switch]$ConfirmEachRun,
    # Re-record a failed capture automatically instead of asking.
    [switch]$Unattended,
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    # 97,000-frame commands start 1-2 s after the camera; keep the final hold recorded.
    [double]$CaptureTailMarginSec = 5.0,
    # Keep the files of a failed capture (LED count plot, recorder metadata) for diagnosis.
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

$campaign = ".\vzr_step_20260919"
$plan = Join-Path $campaign "vzr_step_plan.json"
$export = Join-Path $campaign "export_vzr_step"
if (-not (Test-Path -LiteralPath $plan)) {
    throw "Plan not found: $plan"
}

$runLabels = @{
    "XS1" = "vzrstep_XS1"
    "XS2" = "vzrstep_XS2"
    "XS3" = "vzrstep_XS3"
    "YS1" = "vzrstep_YS1"
    "YS2" = "vzrstep_YS2"
    "YS3" = "vzrstep_YS3"
}

$selectedRuns = @(
    $Runs.Split(",") | ForEach-Object { $_.Trim().ToUpperInvariant() } | Where-Object { $_ -ne "" }
)
if ($selectedRuns.Count -eq 0) {
    throw "No runs selected. Use a comma-separated list of XS1, XS2, XS3, YS1, YS2, YS3."
}
foreach ($run in $selectedRuns) {
    if (-not $runLabels.ContainsKey($run)) {
        throw "Unknown run '$run'. Use: XS1, XS2, XS3, YS1, YS2, YS3"
    }
}

if (($CommandScale -le 0.0) -or ($CommandScale -gt 1.0)) {
    throw "-CommandScale must be in (0, 1]; got $CommandScale"
}
if ($CaptureTailMarginSec -lt 0.0) {
    throw "-CaptureTailMarginSec must not be negative"
}
if ($ConfirmEachRun -and $Unattended) {
    throw "-ConfirmEachRun and -Unattended cannot be combined"
}
$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$scaleText = $CommandScale.ToString($invariant)
$marginText = $CaptureTailMarginSec.ToString($invariant)

$manifest = Join-Path $export "export_manifest.json"
# Regenerate automatically when the plan changed after the export was written.
# The export is never rewritten while it matches the plan.
$exportIsStale = $false
if (Test-Path -LiteralPath $manifest) {
    $planHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $plan).Hash.ToLowerInvariant()
    $manifestHash = ""
    try {
        $manifestHash = [string]((Get-Content -Raw -Encoding UTF8 -LiteralPath $manifest | ConvertFrom-Json).source_plan_sha256)
    }
    catch {
        $manifestHash = ""
    }
    if ($manifestHash.ToLowerInvariant() -ne $planHash) {
        $exportIsStale = $true
        Write-Host "[VZRSTEP] The export does not match the current plan; it will be regenerated."
    }
}
if ($Regenerate -or $exportIsStale -or -not (Test-Path -LiteralPath $manifest)) {
    Write-Host "[VZRSTEP] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) {
        throw "Export generation failed (exit code $LASTEXITCODE)"
    }
}
else {
    Write-Host "[VZRSTEP] Using existing $export"
}

$requests = @()
foreach ($run in $selectedRuns) {
    $requests += "$($runLabels[$run])=$scaleText"
}
$motionSec = 9.7 * $requests.Count

$recordArgs = @(
    "--hf-export-dir", $export,
    "--output-dir", $OutputDir,
    "--capture-tail-margin-sec", $marginText
)
foreach ($request in $requests) {
    $recordArgs += @("--hf-run", $request)
}
if ($KeepFailedCaptures) {
    $recordArgs += "--keep-failed-captures"
}
if (-not $ConfirmEachRun) {
    $recordArgs += "--unattended-after-first-checkpoint"
    if ($Unattended) {
        $recordArgs += @("--automatic-capture-retries", "$AutomaticCaptureRetries")
    }
    else {
        $recordArgs += "--prompt-on-capture-failure"
    }
}

Write-Host "[VZRSTEP] Sequence of $($requests.Count) run(s), PAT motion $motionSec s in total:"
$index = 0
foreach ($request in $requests) {
    $index += 1
    Write-Host ("[VZRSTEP]   {0,2}. {1}" -f $index, $request)
}
if ($requests.Count -gt 8) {
    Write-Host "[VZRSTEP] WARNING: $($requests.Count) runs in one PAT session; consider splitting -Runs so that each session has 8 runs or fewer."
}

Write-Host "[VZRSTEP] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) {
    throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened."
}
if ($DryRunOnly) {
    Write-Host "[VZRSTEP] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[VZRSTEP] The plan asks for at least 10 minutes of rest before the first run."
if ($ConfirmEachRun) {
    Write-Host "[VZRSTEP] Opening PAT and cameras. Every run waits for the stereo preview and Enter; press Ctrl+C there to stop."
}
else {
    Write-Host "[VZRSTEP] Opening PAT and cameras. Confirm the FIRST run in the stereo preview and press Enter; the rest start automatically."
}
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --acknowledge-step-response-risk
$code = $LASTEXITCODE
if ($code -eq 0) {
    Write-Host "[VZRSTEP] All selected runs were recorded. Output: $OutputDir"
}
else {
    Write-Host "[VZRSTEP] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir."
}
exit $code
