[CmdletBinding()]
param(
    [string]$Executable,
    [string]$RuntimeDir,
    [ValidateRange(5, 120)]
    [int]$TimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Executable) {
    $Executable = Join-Path $repoRoot "dist\Folder2Feishu\Folder2Feishu.exe"
}
if (-not $RuntimeDir) {
    $RuntimeDir = Join-Path $env:TEMP ("folder2feishu-packaged-smoke-" + [guid]::NewGuid().ToString("N"))
}

$Executable = (Resolve-Path $Executable).Path
New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

$existingListener = Get-NetTCPConnection `
    -LocalAddress 127.0.0.1 `
    -LocalPort 8000 `
    -State Listen `
    -ErrorAction SilentlyContinue
if ($existingListener) {
    throw "端口 8000 已被 PID $($existingListener.OwningProcess) 占用，无法验证打包程序。"
}

$quotedRuntimeDir = '"{0}"' -f $RuntimeDir
$process = Start-Process `
    -FilePath $Executable `
    -ArgumentList @("--no-browser", "--runtime-dir", $quotedRuntimeDir) `
    -WindowStyle Hidden `
    -PassThru

try {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $health = $null
    do {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) {
            throw "打包程序提前退出，退出码：$($process.ExitCode)"
        }
        try {
            $health = Invoke-RestMethod `
                -Uri "http://127.0.0.1:8000/api/v2/health" `
                -TimeoutSec 2
        }
        catch {
            $health = $null
        }
    } while (-not $health -and (Get-Date) -lt $deadline)

    if (-not $health -or $health.status -ne "ok") {
        throw "打包程序未在 $TimeoutSeconds 秒内通过健康检查。"
    }
    $index = Invoke-WebRequest -Uri "http://127.0.0.1:8000/" -TimeoutSec 5
    if ($index.StatusCode -ne 200 -or $index.Content -notmatch "<title>Folder2Feishu") {
        throw "打包程序可启动，但没有正确提供前端首页。"
    }
    $assetMatch = [regex]::Match($index.Content, '(?:src|href)="(/assets/[^"]+)"')
    if (-not $assetMatch.Success) {
        throw "前端首页没有引用打包后的静态资源。"
    }
    $asset = Invoke-WebRequest `
        -Uri ("http://127.0.0.1:8000" + $assetMatch.Groups[1].Value) `
        -TimeoutSec 5
    if ($asset.StatusCode -ne 200 -or $asset.RawContentLength -le 0) {
        throw "前端静态资源未正确打包。"
    }
    Write-Host "打包程序健康检查通过：PID $($process.Id)" -ForegroundColor Green
}
catch {
    $logFile = Join-Path $RuntimeDir "logs\folder2feishu.log"
    if (Test-Path -LiteralPath $logFile) {
        Write-Host "----- 打包程序日志 -----"
        Get-Content -LiteralPath $logFile -Tail 100
    }
    throw
}
finally {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
    }
}
