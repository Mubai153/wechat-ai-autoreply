$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$spec = Join-Path $root "微信自动回复.spec"
$distRoot = Join-Path $root "dist"
$appDir = Join-Path $distRoot "微信自动回复"
$exe = Join-Path $appDir "微信自动回复.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment not found: $python"
}

& $python -m PyInstaller --noconfirm --clean $spec
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $exe)) {
    throw "PyInstaller failed to create the application."
}

# The user's settings and local conversation database stay outside the bundle.
Copy-Item -LiteralPath (Join-Path $root ".env") -Destination (Join-Path $appDir ".env") -Force
if (Test-Path -LiteralPath (Join-Path $root "data")) {
    Copy-Item -LiteralPath (Join-Path $root "data") -Destination $appDir -Recurse -Force
}

# Create a clickable shortcut in the project folder.
$shell = New-Object -ComObject WScript.Shell
$shortcutPath = Join-Path $root "启动微信自动回复.lnk"
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $appDir
$shortcut.Description = "Start WeChat AI auto-reply"
$shortcut.Save()

Write-Host "Application ready: $exe"
Write-Host "Shortcut ready: $shortcutPath"
