[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$PackagePath,
    [ValidateRange(10, 180)]
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PackagePath = (Resolve-Path $PackagePath).Path
$testRoot = Join-Path $env:TEMP ("folder2feishu-python-test-" + [guid]::NewGuid().ToString("N"))
$expanded = Join-Path $testRoot "expanded"
$installed = Join-Path $testRoot "installed"
$originalLocalAppData = $env:LOCALAPPDATA
$env:LOCALAPPDATA = Join-Path $testRoot "localappdata"
$legacyRuntime = Join-Path $env:LOCALAPPDATA "Folder2FeishuDrive"
$projectsRoot = Join-Path $testRoot "simulated-d-drive\Folder2FeishuDrive\Projects"
$runtime = Join-Path $projectsRoot "Data"
New-Item -ItemType Directory -Path $expanded -Force | Out-Null
New-Item -ItemType Directory -Path $legacyRuntime -Force | Out-Null
Set-Content -LiteralPath (Join-Path $legacyRuntime "migration-sentinel.txt") -Value "move-me" -Encoding ascii

try {
    Expand-Archive -LiteralPath $PackagePath -DestinationPath $expanded
    $installer = Get-ChildItem -LiteralPath $expanded -Filter "Install-Folder2Feishu.ps1" -Recurse -File |
        Select-Object -First 1
    if (-not $installer) {
        throw "发布包中没有安装脚本。"
    }
    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $installer.FullName `
        -InstallDir $installed `
        -ProjectsRoot $projectsRoot `
        -RuntimeDir $runtime `
        -NoShortcut `
        -SkipLaunch
    if ($LASTEXITCODE -ne 0) {
        throw "Python 版安装测试失败，退出码：$LASTEXITCODE"
    }
    if (Test-Path -LiteralPath $legacyRuntime) {
        throw "旧版 C 盘运行数据在校验迁移后仍然存在。"
    }
    if ((Get-Content -LiteralPath (Join-Path $runtime "migration-sentinel.txt") -Raw).Trim() -ne "move-me") {
        throw "C 盘运行数据未完整迁移到新的 D 盘目录。"
    }
    if (Test-Path -LiteralPath (Join-Path $installed "Folder2Feishu.exe")) {
        throw "Python 发布包不应包含 Folder2Feishu.exe。"
    }
    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File (Join-Path $installed "Start-Folder2Feishu.ps1") `
        -ProjectsRoot $projectsRoot `
        -RuntimeDir $runtime `
        -NoBrowser `
        -TimeoutSeconds $TimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Python 版启动测试失败，退出码：$LASTEXITCODE"
    }
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v2/health" -TimeoutSec 5
    if ($health.status -ne "ok") {
        throw "Python 版健康检查未通过。"
    }
    $index = Invoke-WebRequest -Uri "http://127.0.0.1:8000/" -TimeoutSec 5
    if ($index.StatusCode -ne 200 -or $index.Content -notmatch "<title>飞书云盘迁移") {
        throw "Python 版未正确提供前端首页。"
    }
    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File (Join-Path $installed "Stop-Folder2Feishu.ps1") `
        -ProjectsRoot $projectsRoot `
        -RuntimeDir $runtime `
        -Quiet
    if ($LASTEXITCODE -ne 0) {
        throw "Python 版首次启动测试完成后无法停止服务。"
    }

    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File (Join-Path $installed "Update-Folder2Feishu.ps1") `
        -PackagePath $PackagePath `
        -ProjectsRoot $projectsRoot `
        -RuntimeDir $runtime `
        -NoBrowser
    if ($LASTEXITCODE -ne 0) {
        throw "Python 版离线更新测试失败，退出码：$LASTEXITCODE"
    }
    $updatedHealth = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v2/health" -TimeoutSec 5
    if ($updatedHealth.status -ne "ok") {
        throw "更新后的 Python 服务健康检查未通过。"
    }
    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File (Join-Path $installed "Stop-Folder2Feishu.ps1") `
        -ProjectsRoot $projectsRoot `
        -RuntimeDir $runtime `
        -Quiet
    if ($LASTEXITCODE -ne 0) {
        throw "更新测试完成后无法停止 Python 服务。"
    }
    Write-Host "Python 安装、启动、离线更新与网页健康检查通过。" -ForegroundColor Green
}
finally {
    $stopScript = Join-Path $installed "Stop-Folder2Feishu.ps1"
    if (Test-Path -LiteralPath $stopScript) {
        & powershell.exe `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $stopScript `
            -ProjectsRoot $projectsRoot `
            -RuntimeDir $runtime `
            -Quiet
    }
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
    $env:LOCALAPPDATA = $originalLocalAppData
}
