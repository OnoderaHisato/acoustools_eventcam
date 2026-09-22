# Run from PowerShell: . .\activate_metavision_env.ps1
# Configure this toolkit only; no parent-project path discovery.
$EventcamToolkit = $PSScriptRoot
$EventcamActivate = Join-Path $EventcamToolkit 'venv\Scripts\Activate.ps1'
if (-not (Test-Path -LiteralPath $EventcamActivate)) {
    throw 'Create this directory''s venv first; see SETUP_OPENEB_JP.md.'
}
. $EventcamActivate
$EventcamOpenEB = if ($env:EVENTCAM_OPENEB_ROOT) { $env:EVENTCAM_OPENEB_ROOT } else { Join-Path $EventcamToolkit 'openeb_install' }
$EventcamVcpkgBin = if ($env:EVENTCAM_VCPKG_BIN) { $env:EVENTCAM_VCPKG_BIN } else { Join-Path $EventcamToolkit 'vcpkg\vcpkg_installed\x64-windows\bin' }
if (-not $env:EVENTCAM_VCPKG_BIN -and -not (Test-Path -LiteralPath $EventcamVcpkgBin)) {
    $EventcamVcpkgBin = Join-Path $EventcamToolkit 'vcpkg\installed\x64-windows\bin'
}
$EventcamVendorBin = if ($env:EVENTCAM_CAMERA_BIN_DIR) { $env:EVENTCAM_CAMERA_BIN_DIR } else { Join-Path $env:ProgramFiles 'CenturyArks\bin' }
$EventcamVendorPlugin = if ($env:EVENTCAM_CAMERA_PLUGIN_DIR) { $env:EVENTCAM_CAMERA_PLUGIN_DIR } else { Join-Path $env:ProgramFiles 'CenturyArks\plugins' }
$EventcamHalPlugin = Join-Path $EventcamOpenEB 'lib\metavision\hal\plugins'
$EventcamExpected = [ordered]@{
    'OpenEB bin'             = (Join-Path $EventcamOpenEB 'bin')
    'vcpkg bin'              = $EventcamVcpkgBin
    'Metavision HAL plugins' = $EventcamHalPlugin
    'camera vendor bin'      = $EventcamVendorBin
    'camera vendor plugins'  = $EventcamVendorPlugin
}
$EventcamMissing = @($EventcamExpected.GetEnumerator() | Where-Object { -not (Test-Path -LiteralPath $_.Value) })
$EventcamDllDirs = @($EventcamExpected.Values | Where-Object { Test-Path -LiteralPath $_ })
if ($EventcamDllDirs.Count -gt 0) {
    $env:PATH = (@($EventcamDllDirs) + @($env:PATH -split ';') | Where-Object { $_ } | Select-Object -Unique) -join ';'
}
# Only set the plugin variables when a real directory was found. Assigning an empty
# value is not the same as leaving them unset and can hide a working system install.
$EventcamPluginDirs = @($EventcamHalPlugin, $EventcamVendorPlugin) | Where-Object { Test-Path -LiteralPath $_ }
if ($EventcamPluginDirs.Count -gt 0) {
    $env:MV_HAL_PLUGIN_PATH = (@($EventcamPluginDirs) + @($env:MV_HAL_PLUGIN_PATH -split ';') | Where-Object { $_ } | Select-Object -Unique) -join ';'
}
$EventcamHdf5 = Join-Path $EventcamOpenEB 'lib\hdf5\plugin'
if (Test-Path -LiteralPath $EventcamHdf5) {
    $env:HDF5_PLUGIN_PATH = (@($EventcamHdf5) + @($env:HDF5_PLUGIN_PATH -split ';') | Where-Object { $_ } | Select-Object -Unique) -join ';'
}
$env:PYTHONNOUSERSITE = 'true'
if ($EventcamMissing.Count -gt 0) {
    foreach ($entry in $EventcamMissing) {
        Write-Warning ('Not found, so it was not added to the search path: {0} -> {1}' -f $entry.Key, $entry.Value)
    }
    Write-Warning 'Set EVENTCAM_OPENEB_ROOT / EVENTCAM_VCPKG_BIN / EVENTCAM_CAMERA_BIN_DIR / EVENTCAM_CAMERA_PLUGIN_DIR to the real locations, or see SETUP_OPENEB_JP.md. Opening a camera will fail until every entry above resolves; offline NPZ processing is unaffected.'
    Write-Output 'Standalone event-camera venv activated with an INCOMPLETE camera SDK path. No cameras opened.'
} else {
    Write-Output 'Standalone event-camera environment activated. No cameras opened.'
}
Write-Output ('Verify with: .\venv\Scripts\python.exe .\check_environment.py --sdk')
