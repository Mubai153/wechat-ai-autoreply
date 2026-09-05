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

$runtimeBackup = Join-Path ([System.IO.Path]::GetTempPath()) ('wechat-autoreply-build-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runtimeBackup | Out-Null

# A user may have changed settings from the packaged GUI. Keep that copy while
# rebuilding; otherwise the next build would silently restore the old prompt
# from the source checkout.
$packagedEnvPath = Join-Path $appDir ".env"
$packagedEnvContent = $null
if (Test-Path -LiteralPath $packagedEnvPath) {
    $packagedEnvContent = [System.IO.File]::ReadAllText($packagedEnvPath, [System.Text.Encoding]::UTF8)
}

# PyInstaller replaces the whole application directory. Preserve all mutable
# runtime state first so rebuilding cannot erase conversation state or logs.
foreach ($runtimeName in @('data', 'logs')) {
    $runtimePath = Join-Path $appDir $runtimeName
    if (Test-Path -LiteralPath $runtimePath) {
        Copy-Item -LiteralPath $runtimePath -Destination $runtimeBackup -Recurse -Force
    }
}

function Restore-RuntimeState {
    if (-not (Test-Path -LiteralPath $appDir)) {
        New-Item -ItemType Directory -Path $appDir | Out-Null
    }
    if ($null -ne $packagedEnvContent) {
        [System.IO.File]::WriteAllText($packagedEnvPath, $packagedEnvContent, [System.Text.UTF8Encoding]::new($false))
    } elseif (Test-Path -LiteralPath (Join-Path $root '.env')) {
        Copy-Item -LiteralPath (Join-Path $root '.env') -Destination $packagedEnvPath -Force
    }

    foreach ($runtimeName in @('data', 'logs')) {
        $backupPath = Join-Path $runtimeBackup $runtimeName
        $destinationPath = Join-Path $appDir $runtimeName
        if (Test-Path -LiteralPath $backupPath) {
            if (Test-Path -LiteralPath $destinationPath) {
                Remove-Item -LiteralPath $destinationPath -Recurse -Force
            }
            Copy-Item -LiteralPath $backupPath -Destination $appDir -Recurse -Force
        } elseif ($runtimeName -eq 'data' -and (Test-Path -LiteralPath (Join-Path $root 'data'))) {
            Copy-Item -LiteralPath (Join-Path $root 'data') -Destination $appDir -Recurse -Force
        }
    }
}

function Ensure-BundledData {
    # Preserve mutable packaged state, but add newly generated read-only assets
    # (persona and local memory) when an older package did not have them yet.
    foreach ($relativePath in @('persona_prompt.md', 'raw\my_wechat_messages.jsonl')) {
        $sourcePath = Join-Path (Join-Path $root 'data') $relativePath
        $destinationPath = Join-Path (Join-Path $appDir 'data') $relativePath
        if ((Test-Path -LiteralPath $sourcePath) -and -not (Test-Path -LiteralPath $destinationPath)) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
            Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Force
        }
    }
}

try {
    & $python -m PyInstaller --noconfirm --clean $spec
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $exe)) {
        throw "PyInstaller failed to create the application."
    }

    Restore-RuntimeState
    Ensure-BundledData

    # Create a clickable shortcut in the project folder.
    $shell = New-Object -ComObject WScript.Shell
    $shortcutPath = Join-Path $root "启动微信自动回复.lnk"
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = $appDir
    $shortcut.IconLocation = "$(Join-Path $root 'assets\wechat_autoreply_pixel.ico'),0"
    $shortcut.Description = "Start WeChat AI auto-reply"
    $shortcut.Save()

    Write-Host "Application ready: $exe"
    Write-Host "Shortcut ready: $shortcutPath"
} catch {
    # Even a failed clean build must not discard the last packaged runtime data.
    Restore-RuntimeState
    Ensure-BundledData
    throw
} finally {
    if (Test-Path -LiteralPath $runtimeBackup) {
        Remove-Item -LiteralPath $runtimeBackup -Recurse -Force
    }
}
