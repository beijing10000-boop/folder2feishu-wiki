[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Folder2FeishuWikiNext"),
    [string]$PythonCommand = "",
    [switch]$NoShortcut,
    [switch]$SkipLaunch
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$sourceRoot = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot "folder2feishu\__main__.py")) {
    (Resolve-Path $PSScriptRoot).Path
}
else {
    (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$runtimeDir = Join-Path $env:LOCALAPPDATA "Folder2FeishuWikiNext"
$sameLocation = $sourceRoot.Equals($InstallDir, [System.StringComparison]::OrdinalIgnoreCase)

if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot "folder2feishu\__main__.py"))) {
    throw "Incomplete package: Python application source is missing."
}
if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot "frontend\dist\index.html"))) {
    throw "Incomplete package: built web assets are missing."
}

function Resolve-Python312 {
    if ($PythonCommand) {
        $candidate = $PythonCommand
        $arguments = @()
    }
    elseif (Get-Command "py.exe" -ErrorAction SilentlyContinue) {
        $candidate = "py.exe"
        $arguments = @("-3.12")
    }
    elseif (Get-Command "python.exe" -ErrorAction SilentlyContinue) {
        $candidate = "python.exe"
        $arguments = @()
    }
    else {
        throw "Python was not found. Install 64-bit Python 3.12 and enable Add Python to PATH."
    }

    $version = & $candidate @arguments -c "import sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor)+'|'+str(64 if sys.maxsize > 2**32 else 32))" 2>$null
    if ($LASTEXITCODE -ne 0 -or $version.Trim() -ne "3.12|64") {
        throw "64-bit Python 3.12 is required. Detected version: $version"
    }
    return [pscustomobject]@{ Command = $candidate; Arguments = $arguments }
}

function Stop-InstalledServer {
    $stopScript = Join-Path $InstallDir "Stop-Folder2Feishu.ps1"
    if (Test-Path -LiteralPath $stopScript) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stopScript -Quiet
        if ($LASTEXITCODE -ne 0) {
            throw "The previous version is still running. Stop Folder2Feishu and retry."
        }
    }
}

function Remove-LegacySchedules {
    $scheduleFile = Join-Path $runtimeDir "schedules.json"
    if (-not (Test-Path -LiteralPath $scheduleFile)) {
        return
    }
    try {
        $legacy = Get-Content -LiteralPath $scheduleFile -Raw | ConvertFrom-Json
        foreach ($property in $legacy.PSObject.Properties) {
            $projectId = [string]$property.Name
            if ($projectId -match '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$') {
                & schtasks.exe /Delete /F /TN "Folder2FeishuWiki-$projectId" 2>$null | Out-Null
            }
        }
        Remove-Item -LiteralPath $scheduleFile -Force
    }
    catch {
        Write-Warning "Legacy scheduled-task cleanup was not completed: $($_.Exception.Message)"
    }
}

$python = Resolve-Python312
Stop-InstalledServer
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

$entries = @(
    "folder2feishu",
    "frontend\dist",
    "deploy",
    "pyproject.toml",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "VERSION"
)
foreach ($entry in $entries) {
    $source = Join-Path $sourceRoot $entry
    if (-not (Test-Path -LiteralPath $source)) {
        continue
    }
    $destination = Join-Path $InstallDir $entry
    if ($sameLocation -and $source.Equals($destination, [System.StringComparison]::OrdinalIgnoreCase)) {
        continue
    }
    if ((Get-Item -LiteralPath $source).PSIsContainer) {
        if (Test-Path -LiteralPath $destination) {
            Remove-Item -LiteralPath $destination -Recurse -Force
        }
        New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
    }
    else {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
}

Copy-Item -LiteralPath (Join-Path $InstallDir "deploy\Start-Folder2Feishu.ps1") -Destination (Join-Path $InstallDir "Start-Folder2Feishu.ps1") -Force
Copy-Item -LiteralPath (Join-Path $InstallDir "deploy\Stop-Folder2Feishu.ps1") -Destination (Join-Path $InstallDir "Stop-Folder2Feishu.ps1") -Force
Copy-Item -LiteralPath (Join-Path $InstallDir "deploy\Update-Folder2Feishu.ps1") -Destination (Join-Path $InstallDir "Update-Folder2Feishu.ps1") -Force

$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python.Command @($python.Arguments) -m venv (Join-Path $InstallDir ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the Python virtual environment."
    }
}
& $venvPython -m pip install --disable-pip-version-check --upgrade -r (Join-Path $InstallDir "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install Python dependencies."
}

Remove-LegacySchedules

if (-not $NoShortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "Folder2Feishu Wiki Next.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = (Get-Command "powershell.exe").Source
    $shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $InstallDir "Start-Folder2Feishu.ps1")
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.Description = "Start the Folder2Feishu Wiki migration console"
    $shortcut.Save()
}

Write-Host "Folder2Feishu installed at: $InstallDir" -ForegroundColor Green
Write-Host "Migration ledger and credentials remain at: $runtimeDir" -ForegroundColor Green

if (-not $SkipLaunch) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir "Start-Folder2Feishu.ps1")
}
