[CmdletBinding()]
param([switch]$Quiet)

$ErrorActionPreference = "Stop"
$stateDir = Join-Path $env:LOCALAPPDATA "Folder2FeishuDrive"
$pidFile = Join-Path $stateDir "server.pid"

if (-not (Test-Path -LiteralPath $pidFile)) {
    if (-not $Quiet) {
        Write-Host "Folder2Feishu is not running."
    }
    exit 0
}

$processId = (Get-Content -LiteralPath $pidFile -Raw).Trim()
if ($processId -match '^\d+$') {
    $process = Get-Process -Id ([int]$processId) -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit()
    }
}
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
if (-not $Quiet) {
    Write-Host "Folder2Feishu stopped." -ForegroundColor Green
}
