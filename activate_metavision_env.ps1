$Workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$Workspace\venv\Scripts\Activate.ps1"
$CenturyArks = "$env:ProgramFiles\CenturyArks"
$env:PATH = "$Workspace\openeb_install\bin;$Workspace\vcpkg\vcpkg_installed\x64-windows\bin;$Workspace\openeb_install\lib\metavision\hal\plugins;$CenturyArks\bin;$CenturyArks\plugins;$env:PATH"
$env:MV_HAL_PLUGIN_PATH = "$Workspace\openeb_install\lib\metavision\hal\plugins;$CenturyArks\plugins"
$env:HDF5_PLUGIN_PATH = "$Workspace\openeb_install\lib\hdf5\plugin"
$env:PYTHONNOUSERSITE = "true"
