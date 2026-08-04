[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [string]$RuntimeDir = "D:\Folder2FeishuDrive\Data",
    [ValidateRange(5, 120)]
    [int]$TimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$installDir = $PSScriptRoot
$python = Join-Path $installDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Folder2Feishu is not installed. Run Install-Folder2Feishu.ps1 first."
}
if (-not (Test-Path -LiteralPath (Join-Path $installDir "frontend\dist\index.html"))) {
    throw "Web assets are missing. Reinstall or update Folder2Feishu."
}

$stateDir = [System.IO.Path]::GetFullPath($RuntimeDir)
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$pidFile = Join-Path $stateDir "server.pid"

if (Test-Path -LiteralPath $pidFile) {
    $existingId = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($existingId -match '^\d+$' -and (Get-Process -Id ([int]$existingId) -ErrorAction SilentlyContinue)) {
        if (-not $NoBrowser) {
            Start-Process "http://127.0.0.1:8000"
        }
        Write-Host "Folder2Feishu is already running (PID $existingId)." -ForegroundColor Green
        exit 0
    }
    Remove-Item -LiteralPath $pidFile -Force
}

$arguments = @("-m", "folder2feishu", "--no-browser", "--runtime-dir", $stateDir)
$stdout = Join-Path $stateDir "launcher-output.log"
$stderr = Join-Path $stateDir "launcher-error.log"
$process = Start-Process `
    -FilePath $python `
    -ArgumentList $arguments `
    -WorkingDirectory $installDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$health = $null
do {
    Start-Sleep -Milliseconds 400
    if ($process.HasExited) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        $detail = if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Tail 30 | Out-String } else { "" }
        throw "Folder2Feishu failed to start (exit code $($process.ExitCode)).`n$detail"
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v2/health" -TimeoutSec 2
    }
    catch {
        $health = $null
    }
} while (-not $health -and (Get-Date) -lt $deadline)

if (-not $health -or $health.status -ne "ok") {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    throw "Folder2Feishu did not start within $TimeoutSeconds seconds. See $stderr."
}

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:8000"
}
Write-Host "Folder2Feishu started: http://127.0.0.1:8000" -ForegroundColor Green
