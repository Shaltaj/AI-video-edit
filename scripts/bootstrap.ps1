$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
$env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot '.tools\python'
$env:UV_CACHE_DIR = Join-Path $projectRoot '.tools\uv-cache'
$env:UV_PYTHON_BIN_DIR = Join-Path $projectRoot '.tools\python-bin'
$env:TEMP = Join-Path $projectRoot '.tools\tmp'
$env:TMP = $env:TEMP
[void][IO.Directory]::CreateDirectory($env:TEMP)
[void][IO.Directory]::CreateDirectory((Join-Path $projectRoot '.tools\downloads'))
$uvPath = Join-Path $projectRoot '.tools\uv\uv.exe'
if (-not (Test-Path -LiteralPath $uvPath)) {
    & curl.exe --fail --location --retry 2 --output .tools\downloads\uv.zip 'https://github.com/astral-sh/uv/releases/download/0.13.1/uv-x86_64-pc-windows-msvc.zip'
    if ($LASTEXITCODE -ne 0) { throw 'Could not download uv. Internet access is required for the first launch.' }
    Expand-Archive -LiteralPath '.tools\downloads\uv.zip' -DestinationPath '.tools\uv' -Force
}
$pythonVersion = (Get-Content -LiteralPath '.python-version' -Raw).Trim()
& $uvPath python install $pythonVersion --no-bin --no-registry
if ($LASTEXITCODE -ne 0) { throw 'Could not prepare portable Python for the UI.' }
# uv writes UTF-8, while Windows PowerShell decodes captured native output using
# the console code page. Read the interpreter path with an explicit encoding.
# --system also excludes the project's .venv and any activated environment.
$findInfo = New-Object System.Diagnostics.ProcessStartInfo
$findInfo.FileName = $uvPath
$findInfo.Arguments = "python find --managed-python --system --no-project --offline $pythonVersion"
$findInfo.UseShellExecute = $false
$findInfo.CreateNoWindow = $true
$findInfo.RedirectStandardOutput = $true
$findInfo.RedirectStandardError = $true
$findInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$findInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
$findProcess = New-Object System.Diagnostics.Process
$findProcess.StartInfo = $findInfo
try {
    [void]$findProcess.Start()
    $pythonOutput = $findProcess.StandardOutput.ReadToEndAsync()
    $pythonError = $findProcess.StandardError.ReadToEndAsync()
    $findProcess.WaitForExit()
    if ($findProcess.ExitCode -ne 0) { throw ('Portable Python was not found: ' + $pythonError.Result) }
    $uiPython = $pythonOutput.Result.Trim()
} finally {
    $findProcess.Dispose()
}
$pythonRoot = [IO.Path]::GetFullPath($env:UV_PYTHON_INSTALL_DIR).TrimEnd('\') + '\'
if (-not $uiPython.StartsWith($pythonRoot, [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $uiPython -PathType Leaf)) {
    throw ('Portable Python path is invalid: ' + $uiPython)
}
Set-Content -LiteralPath '.tools\bootstrap-python.txt' -Value $uiPython -Encoding UTF8
