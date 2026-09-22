<#
.SYNOPSIS
Record the whole single-amplitude step-response series (2.0 s holds) in one PAT session.

.DESCRIPTION
1. Generates the six exports (skipped when export_manifest.json already exists; use -Regenerate).
2. Runs the hardware-free dry run for all selected runs.
3. Opens PAT/OpenMPD once and records every run in order
   (0.25 -> 0.40 -> 0.70 -> 1.05 -> 1.40 -> 1.70 mm, X -> Y -> Z within each amplitude).
   Only the first run has the stereo preview and Enter checkpoint. A failed capture is
   re-recorded automatically up to -AutomaticCaptureRetries times; if it still fails, the
   session stops and PAT output is turned off. Particle loss is NOT detected automatically.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_step_response_single_amplitude_all.ps1 -DryRunOnly

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_step_response_single_amplitude_all.ps1
#>
[CmdletBinding()]
param(
    [string]$OutputDir = ".\stereo_acoustools_3d_records_step_response_2s",
    # Comma-separated subset, e.g. -Amplitudes "0p25,0p40" (works with -File).
    [string]$Amplitudes = "0p25,0p40,0p70,1p05,1p40,1p70",
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    [switch]$Regenerate,
    [switch]$DryRunOnly
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = ".\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "venv Python not found: $python"
}

$knownAmplitudes = @("0p25", "0p40", "0p70", "1p05", "1p40", "1p70")
$selectedAmplitudes = @(
    $Amplitudes.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
)
if ($selectedAmplitudes.Count -eq 0) {
    throw "No amplitudes selected."
}
foreach ($amplitude in $selectedAmplitudes) {
    if ($knownAmplitudes -notcontains $amplitude) {
        throw "Unknown amplitude '$amplitude'. Use: $($knownAmplitudes -join ', ')"
    }
}
# Keep the safe small-to-large order regardless of how the subset was written.
$selectedAmplitudes = @($knownAmplitudes | Where-Object { $selectedAmplitudes -contains $_ })

$exportArgs = @()
foreach ($amplitude in $selectedAmplitudes) {
    $plan = ".\step_response_single_amplitude_${amplitude}_plan.json"
    $export = ".\step_response_single_amplitude_export_$amplitude"
    if (-not (Test-Path -LiteralPath $plan)) {
        throw "Plan not found: $plan"
    }
    $manifest = Join-Path $export "export_manifest.json"
    if ($Regenerate -or -not (Test-Path -LiteralPath $manifest)) {
        Write-Host "[SERIES] Generating $export from $plan"
        & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
        if ($LASTEXITCODE -ne 0) {
            throw "Export generation failed for $amplitude (exit code $LASTEXITCODE)"
        }
    }
    else {
        Write-Host "[SERIES] Using existing $export"
    }
    $exportArgs += @("--hf-export-dir", $export)
}

$recordArgs = $exportArgs + @(
    "--output-dir", $OutputDir,
    "--unattended-after-first-checkpoint",
    "--automatic-capture-retries", "$AutomaticCaptureRetries"
)

Write-Host "[SERIES] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) {
    throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened."
}
if ($DryRunOnly) {
    Write-Host "[SERIES] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[SERIES] Opening PAT and cameras. Confirm the first run in the stereo preview and press Enter."
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --acknowledge-step-response-risk
$code = $LASTEXITCODE
if ($code -eq 0) {
    Write-Host "[SERIES] All selected runs were recorded. Output: $OutputDir"
}
else {
    Write-Host "[SERIES] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir."
}
exit $code
