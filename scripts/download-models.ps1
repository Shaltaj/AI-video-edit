$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
$env:PYTHONUTF8 = '1'
$env:HF_HOME = Join-Path $projectRoot '.tools\huggingface'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
$env:HF_HUB_DISABLE_XET = '1'
$env:DO_NOT_TRACK = '1'
$env:HF_HUB_DOWNLOAD_TIMEOUT = '120'
Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue
Remove-Item Env:TRANSFORMERS_OFFLINE -ErrorAction SilentlyContinue
& (Join-Path $projectRoot '.venv\Scripts\python.exe') -m aivideo.models @args
exit $LASTEXITCODE
