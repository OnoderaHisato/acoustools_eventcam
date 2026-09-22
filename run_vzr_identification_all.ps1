<#
.SYNOPSIS
Record the vzr identification series (simultaneous x/y + z sine excitation) in one PAT session.

.DESCRIPTION
1. Generates the export from vzr_identification_20260917\vzr_identification_plan.json
   (skipped when export_manifest.json already exists; use -Regenerate).
2. Runs the hardware-free dry run for the selected runs.
3. Opens PAT/OpenMPD once and records the selected runs in export order:
   L1x -> L1z -> L1 -> L2 -> L3 -> L4 -> Y1y -> Y1 -> Y2
   (the ALT fallback is added only with -IncludeAlt, or by naming it in -Labels).
   Every run has a stereo preview and an Enter checkpoint. Ctrl+C at a checkpoint
   stops the session and turns PAT output off. Particle loss is NOT detected automatically.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_vzr_identification_all.ps1 -DryRunOnly

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_vzr_identification_all.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_vzr_identification_all.ps1 -Labels "vzr_Y1y_y0.5_55Hz_only,vzr_Y1_y0.5_55Hz_z0.20_187Hz"
#>
[CmdletBinding()]
param(
    [string]$OutputDir = ".\stereo_acoustools_3d_records_vzr",
    # Comma-separated subset of exported run names, in any order; recording always follows export order.
    [string]$Labels = "",
    [switch]$IncludeAlt,
    # Scale applied to every selected command after loading (0 < scale <= 1); 1.0 records the plan values.
    [double]$CommandScale = 1.0,
    [switch]$Regenerate,
    [switch]$DryRunOnly
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = ".\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "venv Python not found: $python"
}

$plan = ".\vzr_identification_20260917\vzr_identification_plan.json"
$export = ".\vzr_identification_20260917\export_vzr_identification"
if (-not (Test-Path -LiteralPath $plan)) {
    throw "Plan not found: $plan"
}

# Export order of the plan. Only the ALT fallback is optional.
$mainLabels = @(
    "vzr_L1x_x0.4_57Hz_only",
    "vzr_L1z_z0.15_187Hz_only",
    "vzr_L1_x0.4_57Hz_z0.15_187Hz",
    "vzr_L2_x0.5_57Hz_z0.20_187Hz",
    "vzr_L3_x0.6_57Hz_z0.20_187Hz",
    "vzr_L4_x0.6_57Hz_z0.25_187Hz",
    "vzr_Y1y_y0.5_55Hz_only",
    "vzr_Y1_y0.5_55Hz_z0.20_187Hz",
    "vzr_Y2_y0.6_55Hz_z0.20_187Hz"
)
$altLabel = "vzr_ALT_x0.7_50Hz_z0.20_187Hz"
$knownLabels = $mainLabels + @($altLabel)

if ($Labels.Trim() -ne "") {
    $selectedLabels = @(
        $Labels.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
    )
    foreach ($label in $selectedLabels) {
        if ($knownLabels -notcontains $label) {
            throw "Unknown run '$label'. Use: $($knownLabels -join ', ')"
        }
    }
    if ($IncludeAlt -and ($selectedLabels -notcontains $altLabel)) {
        $selectedLabels += $altLabel
    }
}
else {
    $selectedLabels = @($mainLabels)
    if ($IncludeAlt) {
        $selectedLabels += $altLabel
    }
}
if ($selectedLabels.Count -eq 0) {
    throw "No runs selected."
}
# Keep the safe escalation order regardless of how the subset was written.
$selectedLabels = @($knownLabels | Where-Object { $selectedLabels -contains $_ })

if (($CommandScale -le 0.0) -or ($CommandScale -gt 1.0)) {
    throw "-CommandScale must be in (0, 1]; got $CommandScale"
}
$scaleText = $CommandScale.ToString([System.Globalization.CultureInfo]::InvariantCulture)

$manifest = Join-Path $export "export_manifest.json"
if ($Regenerate -or -not (Test-Path -LiteralPath $manifest)) {
    Write-Host "[VZR] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) {
        throw "Export generation failed (exit code $LASTEXITCODE)"
    }
}
else {
    Write-Host "[VZR] Using existing $export"
}

$recordArgs = @("--hf-export-dir", $export, "--output-dir", $OutputDir, "--hf-command-scale", $scaleText)
foreach ($label in $selectedLabels) {
    $recordArgs += @("--label", $label)
}

Write-Host "[VZR] Selected $($selectedLabels.Count) run(s) in export order:"
foreach ($label in $selectedLabels) {
    Write-Host "[VZR]   $label"
}

Write-Host "[VZR] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) {
    throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened."
}
if ($DryRunOnly) {
    Write-Host "[VZR] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[VZR] Opening PAT and cameras. Every run waits for the stereo preview and Enter; press Ctrl+C there to stop."
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --acknowledge-vzr-protocol
$code = $LASTEXITCODE
if ($code -eq 0) {
    Write-Host "[VZR] All selected runs were recorded. Output: $OutputDir"
}
else {
    Write-Host "[VZR] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir."
}
exit $code
