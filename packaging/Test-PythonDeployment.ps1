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
$runtime = Join-Path $testRoot "runtime"
$originalLocalAppData = $env:LOCALAPPDATA
$env:LOCALAPPDATA = Join-Path $testRoot "localappdata"
New-Item -ItemType Directory -Path $expanded -Force | Out-Null

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
        -NoShortcut `
        -SkipLaunch
    if ($LASTEXITCODE -ne 0) {
        throw "Python 版安装测试失败，退出码：$LASTEXITCODE"
    }
    if (Test-Path -LiteralPath (Join-Path $installed "Folder2Feishu.exe")) {
        throw "Python 发布包不应包含 Folder2Feishu.exe。"
    }
    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File (Join-Path $installed "Start-Folder2Feishu.ps1") `
        -NoBrowser `
        -RuntimeDir $runtime `
        -TimeoutSeconds $TimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Python 版启动测试失败，退出码：$LASTEXITCODE"
    }
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v2/health" -TimeoutSec 5
    if ($health.status -ne "ok") {
        throw "Python 版健康检查未通过。"
    }
    $index = Invoke-WebRequest -Uri "http://127.0.0.1:8000/" -TimeoutSec 5
    if ($index.StatusCode -ne 200 -or $index.Content -notmatch "<title>Folder2Feishu") {
        throw "Python 版未正确提供前端首页。"
    }
    $pidFile = Join-Path $runtime "server.pid"
    $serverId = [int](Get-Content -LiteralPath $pidFile -Raw)
    Stop-Process -Id $serverId -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue

    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File (Join-Path $installed "Update-Folder2Feishu.ps1") `
        -PackagePath $PackagePath
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
        -Quiet
    if ($LASTEXITCODE -ne 0) {
        throw "更新测试完成后无法停止 Python 服务。"
    }
    Write-Host "Python 安装、启动、离线更新与网页健康检查通过。" -ForegroundColor Green
}
finally {
    $pidFile = Join-Path $runtime "server.pid"
    if (Test-Path -LiteralPath $pidFile) {
        $serverId = [int](Get-Content -LiteralPath $pidFile -Raw)
        Stop-Process -Id $serverId -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
    $env:LOCALAPPDATA = $originalLocalAppData
}
