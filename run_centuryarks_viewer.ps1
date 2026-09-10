$CenturyArks = "$env:ProgramFiles\CenturyArks"
$env:PATH = "$CenturyArks\bin;$CenturyArks\plugins;$env:PATH"
$env:MV_HAL_PLUGIN_PATH = "$CenturyArks\plugins"
$env:HDF5_PLUGIN_PATH = "$CenturyArks\bin"

& "$CenturyArks\bin\metavision_viewer.exe"
