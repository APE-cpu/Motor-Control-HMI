$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $projectRoot 'native_replay_runtime'
$sourcePath = Join-Path $projectRoot 'native_replay'
python -m pip install --no-deps --upgrade --target $runtimePath $sourcePath
if ($LASTEXITCODE -ne 0) { throw 'Offline replay native build failed' }
