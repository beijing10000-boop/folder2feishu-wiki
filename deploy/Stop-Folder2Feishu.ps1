[CmdletBinding()]
param(
    [switch]$Quiet,
    [string]$ProjectsRoot = "D:\Folder2FeishuDrive\Projects",
    [string]$RuntimeDir = ""
)

$ErrorActionPreference = "Stop"
$projectsDir = [System.IO.Path]::GetFullPath($ProjectsRoot)
$pidFiles = @((Join-Path (Join-Path $projectsDir ".service") "server.pid"))
if ($RuntimeDir) {
    $runtimePath = [System.IO.Path]::GetFullPath($RuntimeDir)
    $pidFiles += (Join-Path $runtimePath "server.pid")
    if ((Split-Path $runtimePath -Parent) -ieq $projectsDir) {
        $pidFiles += (Join-Path (Join-Path (Split-Path $runtimePath -Parent) ".service") "server.pid")
    }
}
$pidFiles = @($pidFiles | Select-Object -Unique)

if (-not ($pidFiles | Where-Object { Test-Path -LiteralPath $_ })) {
    if (-not $Quiet) {
        Write-Host "Folder2Feishu is not running."
    }
    exit 0
}

foreach ($pidFile in $pidFiles) {
    if (-not (Test-Path -LiteralPath $pidFile)) {
        continue
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
}
if (-not $Quiet) {
    Write-Host "Folder2Feishu stopped." -ForegroundColor Green
}
