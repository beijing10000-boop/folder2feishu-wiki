[CmdletBinding()]
param(
    [string]$Version = "v3.0.0-rc.6",
    [string]$Repository = "beijing10000-boop/folder2feishu-wiki",
    [string]$InstallDir = "D:\Folder2FeishuDrive\App",
    [string]$RuntimeDir = "D:\Folder2FeishuDrive\Data"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($Version -notmatch '^v\d+\.\d+\.\d+(-rc\.\d+)?$') {
    throw "无效版本号：$Version"
}
if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    throw "无效 GitHub 仓库：$Repository"
}

$releaseVersion = $Version.Substring(1)
$assetName = "Folder2Feishu-Python-$releaseVersion.zip"
$releaseBase = "https://github.com/$Repository/releases/download/$Version"
$temporaryRoot = Join-Path $env:TEMP ("folder2feishu-online-" + [guid]::NewGuid().ToString("N"))
$packagePath = Join-Path $temporaryRoot $assetName
$checksumPath = Join-Path $temporaryRoot "SHA256SUMS.txt"
$expandedPath = Join-Path $temporaryRoot "expanded"

New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null
try {
    Write-Host "正在下载 Folder2Feishu Drive $Version..." -ForegroundColor Cyan
    Invoke-WebRequest -UseBasicParsing -Uri "$releaseBase/$assetName" -OutFile $packagePath
    Invoke-WebRequest -UseBasicParsing -Uri "$releaseBase/SHA256SUMS.txt" -OutFile $checksumPath

    $checksumLine = Get-Content -LiteralPath $checksumPath |
        Where-Object { $_ -match ("\s+" + [regex]::Escape($assetName) + "$") } |
        Select-Object -First 1
    if (-not $checksumLine -or $checksumLine -notmatch '^([a-fA-F0-9]{64})\s+') {
        throw "发布页中的 SHA-256 校验文件无效。"
    }
    $expectedHash = $Matches[1].ToLowerInvariant()
    $actualHash = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "安装包 SHA-256 校验失败，已停止安装。"
    }

    Expand-Archive -LiteralPath $packagePath -DestinationPath $expandedPath
    $installer = Get-ChildItem -LiteralPath $expandedPath -Filter "Install-Folder2Feishu.ps1" -Recurse -File |
        Select-Object -First 1
    if (-not $installer) {
        throw "发布包不完整：找不到安装程序。"
    }

    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $installer.FullName `
        -InstallDir $InstallDir `
        -RuntimeDir $RuntimeDir
    if ($LASTEXITCODE -ne 0) {
        throw "安装失败，退出码：$LASTEXITCODE"
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
