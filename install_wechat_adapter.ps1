$ErrorActionPreference = 'Stop'

$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$vendorPath = Join-Path $projectPath '.vendor\wechatauto-replica'
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
$zipUrl = 'https://codeload.github.com/fanyuantaier/wechatauto-replica/zip/refs/heads/main'

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Git was not found. Install Git for Windows first.'
}

if (-not (Test-Path -LiteralPath $vendorPath)) {
    Write-Host 'Downloading the WeChat 4.x adapter...'
    & git -c http.version=HTTP/1.1 clone --depth 1 https://github.com/fanyuantaier/wechatauto-replica.git $vendorPath
    if ($LASTEXITCODE -ne 0) {
        if (Test-Path -LiteralPath $vendorPath) {
            Remove-Item -LiteralPath $vendorPath -Recurse -Force
        }
        Write-Host 'Git download failed. Trying the official GitHub ZIP download...'
        $zipPath = Join-Path $env:TEMP 'wechatauto-replica-main.zip'
        $extractPath = Join-Path $env:TEMP ('wechatauto-replica-' + [guid]::NewGuid().ToString('N'))
        try {
            Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing -TimeoutSec 120
            Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force
            $unzippedPath = Join-Path $extractPath 'wechatauto-replica-main'
            if (-not (Test-Path -LiteralPath (Join-Path $unzippedPath 'pyproject.toml'))) {
                throw 'The downloaded ZIP does not contain a valid Python project.'
            }
            Move-Item -LiteralPath $unzippedPath -Destination $vendorPath
        } catch {
            throw "Git and ZIP downloads both failed. Check network access to GitHub. Details: $($_.Exception.Message)"
        } finally {
            if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
            if (Test-Path -LiteralPath $extractPath) { Remove-Item -LiteralPath $extractPath -Recurse -Force }
        }
    }
} else {
    if (-not (Test-Path -LiteralPath (Join-Path $vendorPath 'pyproject.toml'))) {
        throw "The adapter directory is incomplete. Remove it manually and retry: $vendorPath"
    }
    Write-Host "Adapter already exists: $vendorPath"
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = 'python'
}

& $pythonPath -m pip install --disable-pip-version-check --upgrade 'setuptools>=61.0'
if ($LASTEXITCODE -ne 0) {
    throw 'Installing the Python build tools failed.'
}

& $pythonPath -m pip install --disable-pip-version-check --no-build-isolation -e $vendorPath
if ($LASTEXITCODE -ne 0) {
    throw 'Adapter pip installation failed. Review the pip error above.'
}
Write-Host 'WeChat 4.x adapter installation completed.'
