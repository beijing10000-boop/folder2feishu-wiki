[CmdletBinding()]
param(
    [string]$InstallDir = "D:\Folder2FeishuDrive\App",
    [string]$ProjectsRoot = "D:\Folder2FeishuDrive\Projects",
    [string]$RuntimeDir = "",
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
$ProjectsRoot = [System.IO.Path]::GetFullPath($ProjectsRoot)
if ($RuntimeDir) {
    $RuntimeDir = [System.IO.Path]::GetFullPath($RuntimeDir)
    if ((Split-Path $RuntimeDir -Parent) -ieq $ProjectsRoot) {
        $ProjectsRoot = Split-Path $RuntimeDir -Parent
    }
}
$legacyInstallDir = [System.IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA "Programs\Folder2FeishuDrive")
)
$legacyRuntimeDir = [System.IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA "Folder2FeishuDrive")
)
$sameLocation = $sourceRoot.Equals($InstallDir, [System.StringComparison]::OrdinalIgnoreCase)

function Assert-TargetDriveExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Purpose
    )

    $root = [System.IO.Path]::GetPathRoot($Path)
    if (-not $root -or -not (Test-Path -LiteralPath $root)) {
        throw "$Purpose target drive is unavailable: $root. Connect or create the drive, then retry."
    }
}

Assert-TargetDriveExists -Path $InstallDir -Purpose "Program installation"
Assert-TargetDriveExists -Path $ProjectsRoot -Purpose "Projects data"
if ($RuntimeDir) {
    Assert-TargetDriveExists -Path $RuntimeDir -Purpose "Runtime data"
}

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

function Stop-InstalledServers {
    $installations = @($InstallDir, $legacyInstallDir) | Select-Object -Unique
    foreach ($installation in $installations) {
        $stopScript = Join-Path $installation "Stop-Folder2Feishu.ps1"
        if (Test-Path -LiteralPath $stopScript) {
            if ($installation.Equals($InstallDir, [System.StringComparison]::OrdinalIgnoreCase) -and
                -not $installation.Equals($legacyInstallDir, [System.StringComparison]::OrdinalIgnoreCase)) {
                # Older releases do not know -ProjectsRoot. Inspect the
                # installed script first so an in-place upgrade can still
                # stop v3/v4.0 without failing parameter binding.
                $stopCommand = Get-Command $stopScript
                if ($stopCommand.Parameters.ContainsKey("ProjectsRoot")) {
                    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stopScript -ProjectsRoot $ProjectsRoot -RuntimeDir $RuntimeDir -Quiet
                }
                elseif ($RuntimeDir -and $stopCommand.Parameters.ContainsKey("RuntimeDir")) {
                    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stopScript -RuntimeDir $RuntimeDir -Quiet
                }
                else {
                    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stopScript -Quiet
                }
            }
            else {
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stopScript -Quiet
            }
            if ($LASTEXITCODE -ne 0) {
                throw "The previous version is still running. Stop Folder2Feishu and retry."
            }
        }
    }
}

function Move-LegacyRuntime {
    if (-not $RuntimeDir -and (Test-Path -LiteralPath $legacyRuntimeDir)) {
        $script:RuntimeDir = Join-Path $ProjectsRoot "Imported"
    }
    if (-not $RuntimeDir) {
        New-Item -ItemType Directory -Path $ProjectsRoot -Force | Out-Null
        return
    }
    if ($legacyRuntimeDir.Equals($RuntimeDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        return
    }
    if (-not (Test-Path -LiteralPath $legacyRuntimeDir)) {
        New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
        return
    }
    if (Test-Path -LiteralPath $RuntimeDir) {
        $existing = Get-ChildItem -LiteralPath $RuntimeDir -Force -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($existing) {
            throw "Both the old C: data directory and the new D: data directory contain files. Automatic migration was stopped to prevent overwriting either copy. Back up and reconcile the two directories, then retry."
        }
    }

    $runtimeParent = Split-Path $RuntimeDir -Parent
    New-Item -ItemType Directory -Path $runtimeParent -Force | Out-Null
    $staging = "$RuntimeDir.migrating-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    try {
        Get-ChildItem -LiteralPath $legacyRuntimeDir -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $staging -Recurse -Force
        }

        $sourceFiles = @(Get-ChildItem -LiteralPath $legacyRuntimeDir -File -Recurse -Force)
        $targetFiles = @(Get-ChildItem -LiteralPath $staging -File -Recurse -Force)
        if ($sourceFiles.Count -ne $targetFiles.Count) {
            throw "Runtime migration verification failed: file count differs."
        }
        foreach ($sourceFile in $sourceFiles) {
            $relative = $sourceFile.FullName.Substring($legacyRuntimeDir.Length).TrimStart('\')
            $targetFile = Join-Path $staging $relative
            if (-not (Test-Path -LiteralPath $targetFile)) {
                throw "Runtime migration verification failed: missing $relative"
            }
            $targetLength = (Get-Item -LiteralPath $targetFile).Length
            if ($sourceFile.Length -ne $targetLength) {
                throw "Runtime migration verification failed: size differs for $relative"
            }
            $sourceHash = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash
            $targetHash = (Get-FileHash -LiteralPath $targetFile -Algorithm SHA256).Hash
            if ($sourceHash -ne $targetHash) {
                throw "Runtime migration verification failed: checksum differs for $relative"
            }
        }

        if (Test-Path -LiteralPath $RuntimeDir) {
            Remove-Item -LiteralPath $RuntimeDir -Force
        }
        Move-Item -LiteralPath $staging -Destination $RuntimeDir
        Remove-Item -LiteralPath $legacyRuntimeDir -Recurse -Force
        Write-Host "Migration data moved from C: to $RuntimeDir" -ForegroundColor Green
    }
    catch {
        if (Test-Path -LiteralPath $staging) {
            Remove-Item -LiteralPath $staging -Recurse -Force
        }
        throw
    }
}

function Remove-LegacyInstallation {
    if ($legacyInstallDir.Equals($InstallDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        return
    }
    if (-not (Test-Path -LiteralPath $legacyInstallDir)) {
        return
    }
    if ($sourceRoot.Equals($legacyInstallDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Warning "The installer is running from the old C: directory, so that program directory was kept. Run the online installer to remove it automatically."
        return
    }
    Remove-Item -LiteralPath $legacyInstallDir -Recurse -Force
    Write-Host "Old C: program directory removed: $legacyInstallDir" -ForegroundColor Green
}

function Remove-LegacySchedules {
    if (-not $RuntimeDir) {
        return
    }
    $scheduleFile = Join-Path $runtimeDir "schedules.json"
    if (-not (Test-Path -LiteralPath $scheduleFile)) {
        return
    }
    try {
        $legacy = Get-Content -LiteralPath $scheduleFile -Raw | ConvertFrom-Json
        foreach ($property in $legacy.PSObject.Properties) {
            $projectId = [string]$property.Name
            if ($projectId -match '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$') {
                & schtasks.exe /Delete /F /TN "Folder2FeishuDrive-$projectId" 2>$null | Out-Null
            }
        }
        Remove-Item -LiteralPath $scheduleFile -Force
    }
    catch {
        Write-Warning "Legacy scheduled-task cleanup was not completed: $($_.Exception.Message)"
    }
}

$python = Resolve-Python312
Stop-InstalledServers
Move-LegacyRuntime
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
New-Item -ItemType Directory -Path $ProjectsRoot -Force | Out-Null
if ($RuntimeDir) {
    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    $env:FOLDER2FEISHU_HOME = $RuntimeDir
}
$env:FOLDER2FEISHU_PROJECTS_ROOT = $ProjectsRoot

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
    $shortcutPath = Join-Path $desktop "Folder2Feishu Drive.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = (Get-Command "powershell.exe").Source
    $shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -ProjectsRoot "{1}"' -f (Join-Path $InstallDir "Start-Folder2Feishu.ps1"), $ProjectsRoot
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.Description = "启动本地目录到飞书云盘迁移控制台"
    $shortcut.Save()
}

Remove-LegacyInstallation

Write-Host "Folder2Feishu installed at: $InstallDir" -ForegroundColor Green
Write-Host "Projects data root: $ProjectsRoot" -ForegroundColor Green
if ($RuntimeDir) {
    Write-Host "Initial project data: $RuntimeDir" -ForegroundColor Green
}

if (-not $SkipLaunch) {
    $startArguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $InstallDir "Start-Folder2Feishu.ps1"),
        "-ProjectsRoot", $ProjectsRoot
    )
    if ($RuntimeDir) {
        $startArguments += @("-RuntimeDir", $RuntimeDir)
    }
    & powershell.exe @startArguments
}
