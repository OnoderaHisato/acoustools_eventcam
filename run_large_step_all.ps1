<#
.SYNOPSIS
Record the large single-axis steps that give the horizontal force ceiling, in one PAT session.

.DESCRIPTION
1. Generates the export from large_step_20260920\large_step_plan.json
   (skipped while export_manifest.json matches the plan; use -Regenerate).
   The commands are generated natively (kind=staircase) and are numerically identical
   to the analysis-side commands\vzrstep_*L*.npz.
2. Runs the hardware-free dry run for the selected sequence.
3. Opens PAT/OpenMPD once and records the sequence. Default: XL12 -> XL15.
   By default only the FIRST run has a stereo preview and an Enter checkpoint; the rest
   start automatically. A failed capture still asks "re-record? (Y/n)".
   -ConfirmEachRun restores the preview and Enter before every run.
   -Unattended re-records a failed capture automatically instead of asking.
   Particle loss is NOT detected automatically.

Escalate from the smallest step and stop where the particle returns sluggishly: 1.8 and
2.0 mm start from beyond the estimated force maximum (the estimated escape boundary is
2.144 mm; a single jump is 84% and 93% of it).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_large_step_all.ps1 -DryRunOnly

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_large_step_all.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_large_step_all.ps1 -Runs "XL18"

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_large_step_all.ps1 -Runs "YL12,YL15" -ConfirmEachRun
#>
[CmdletBinding()]
param(
    [string]$OutputDir = ".\stereo_acoustools_3d_records_large_step",
    # Ordered run list; repeats allowed. XL12/XL15/XL18/XL20 = x steps of 1.2/1.5/1.8/2.0 mm,
    # YL12...YL20 = the same on y.
    [string]$Runs = "XL12,XL15",
    # Scale applied to every selected command (0 < scale <= 1).
    [double]$CommandScale = 1.0,
    # Preview and Enter before every run (the behaviour before 2026-09-20).
    [switch]$ConfirmEachRun,
    # Re-record a failed capture automatically instead of asking.
    [switch]$Unattended,
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    # 54,000-frame commands start about 1 s after the camera; keep the final hold recorded.
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

$campaign = ".\large_step_20260920"
$plan = Join-Path $campaign "large_step_plan.json"
$export = Join-Path $campaign "export_large_step"
if (-not (Test-Path -LiteralPath $plan)) {
    throw "Plan not found: $plan"
}

$runLabels = @{
    "XL12" = "largestep_XL12"
    "XL15" = "largestep_XL15"
    "XL18" = "largestep_XL18"
    "XL20" = "largestep_XL20"
    "YL12" = "largestep_YL12"
    "YL15" = "largestep_YL15"
    "YL18" = "largestep_YL18"
    "YL20" = "largestep_YL20"
}

$selectedRuns = @(
    $Runs.Split(",") | ForEach-Object { $_.Trim().ToUpperInvariant() } | Where-Object { $_ -ne "" }
)
if ($selectedRuns.Count -eq 0) {
    throw "No runs selected. Use a comma-separated list of XL12, XL15, XL18, XL20, YL12, YL15, YL18, YL20."
}
foreach ($run in $selectedRuns) {
    if (-not $runLabels.ContainsKey($run)) {
        throw "Unknown run '$run'. Use: XL12, XL15, XL18, XL20, YL12, YL15, YL18, YL20"
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
        Write-Host "[LARGESTEP] The export does not match the current plan; it will be regenerated."
    }
}
if ($Regenerate -or $exportIsStale -or -not (Test-Path -LiteralPath $manifest)) {
    Write-Host "[LARGESTEP] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) {
        throw "Export generation failed (exit code $LASTEXITCODE)"
    }
}
else {
    Write-Host "[LARGESTEP] Using existing $export"
}

$requests = @()
foreach ($run in $selectedRuns) {
    $requests += "$($runLabels[$run])=$scaleText"
}
$motionSec = 5.4 * $requests.Count

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

Write-Host "[LARGESTEP] Sequence of $($requests.Count) run(s), PAT motion $motionSec s in total:"
$index = 0
foreach ($request in $requests) {
    $index += 1
    Write-Host ("[LARGESTEP]   {0,2}. {1}" -f $index, $request)
}

Write-Host "[LARGESTEP] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) {
    throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened."
}
if ($DryRunOnly) {
    Write-Host "[LARGESTEP] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[LARGESTEP] Escalate from the smallest step; stop when the particle returns sluggishly."
if ($ConfirmEachRun) {
    Write-Host "[LARGESTEP] Opening PAT and cameras. Every run waits for the stereo preview and Enter; press Ctrl+C there to stop."
}
else {
    Write-Host "[LARGESTEP] Opening PAT and cameras. Confirm the FIRST run in the stereo preview and press Enter; the rest start automatically."
}
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --acknowledge-step-response-risk
$code = $LASTEXITCODE
if ($code -eq 0) {
    Write-Host "[LARGESTEP] All selected runs were recorded. Output: $OutputDir"
}
else {
    Write-Host "[LARGESTEP] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir."
}
exit $code
