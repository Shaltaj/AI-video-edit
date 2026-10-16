[CmdletBinding()]
param(
    [ValidateSet('all','runtime','ffmpeg')][string]$Component = 'all',
    [switch]$RepairTorch,
    [switch]$RepairFFmpeg
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
$env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot '.tools\python'
$env:UV_CACHE_DIR = Join-Path $projectRoot '.tools\uv-cache'
$env:UV_PYTHON_BIN_DIR = Join-Path $projectRoot '.tools\python-bin'
$env:HF_HOME = Join-Path $projectRoot '.tools\huggingface'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:DO_NOT_TRACK = '1'
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path $projectRoot '.tools\tmp'
$env:TMP = $env:TEMP
[void][System.IO.Directory]::CreateDirectory($env:TEMP)
[void][System.IO.Directory]::CreateDirectory((Join-Path $projectRoot '.tools\downloads'))

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw ('Command failed: ' + $Executable + ' ' + ($Arguments -join ' ')) }
}

& (Join-Path $PSScriptRoot 'bootstrap.ps1')
$uvPath = Join-Path $projectRoot '.tools\uv\uv.exe'
$pythonVersion = (Get-Content -LiteralPath '.python-version' -Raw).Trim()
if ($Component -in @('all','runtime')) {
    $syncArgs = @('sync','--python',$pythonVersion,'--locked')
    if ($RepairTorch) { $syncArgs += @('--reinstall-package','torch') }
    Invoke-Checked $uvPath $syncArgs
}

$ffmpegPath = Join-Path $projectRoot '.tools\ffmpeg\bin\ffmpeg.exe'
if ($Component -in @('all','ffmpeg') -and ($RepairFFmpeg -or -not (Test-Path -LiteralPath $ffmpegPath) -or -not (Test-Path -LiteralPath '.tools\ffmpeg\bin\ffprobe.exe'))) {
    $archivePath = Join-Path $projectRoot '.tools\downloads\ffmpeg.zip'
    $checksumPath = Join-Path $projectRoot '.tools\downloads\ffmpeg.sha256'
    $assetName = 'ffmpeg-n8.1-latest-win64-gpl-8.1.zip'
    Invoke-Checked 'curl.exe' @('--fail','--location','--retry','2','--output',$archivePath,('https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/' + $assetName))
    Invoke-Checked 'curl.exe' @('--fail','--location','--retry','2','--output',$checksumPath,'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256')
    $checksumLine = @(Get-Content -LiteralPath $checksumPath | Where-Object { $_ -match ('\s+\*?' + [regex]::Escape($assetName) + '$') })
    if ($checksumLine.Count -ne 1) { throw 'FFmpeg checksum entry not found.' }
    $expectedHash = ($checksumLine[0].Trim() -split '\s+')[0]
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
    if ($actualHash -ine $expectedHash) { throw 'FFmpeg archive checksum mismatch.' }
    Expand-Archive -LiteralPath $archivePath -DestinationPath '.tools\ffmpeg-unpacked' -Force
    [void][System.IO.Directory]::CreateDirectory((Join-Path $projectRoot '.tools\ffmpeg\bin'))
    foreach ($binaryName in @('ffmpeg.exe','ffprobe.exe')) {
        $binary = @(Get-ChildItem -LiteralPath '.tools\ffmpeg-unpacked' -Filter $binaryName -File -Recurse)
        if ($binary.Count -ne 1) { throw ('Expected exactly one ' + $binaryName) }
        Copy-Item -LiteralPath $binary[0].FullName -Destination (Join-Path '.tools\ffmpeg\bin' $binaryName)
    }
    Copy-Item -LiteralPath $checksumPath -Destination '.tools\ffmpeg\archive.sha256'
}
Invoke-Checked $uvPath @('--version')
if (Test-Path -LiteralPath '.\.venv\Scripts\python.exe') { Invoke-Checked '.\.venv\Scripts\python.exe' @('--version') }
if (Test-Path -LiteralPath $ffmpegPath) { Invoke-Checked $ffmpegPath @('-version') }
Write-Output 'Environment ready. Download model weights with scripts/download-models.ps1, then run scripts/run.ps1 doctor.'
