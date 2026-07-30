[CmdletBinding()]
param(
    [string]$SetupPath,
    [string]$InstallDir,
    [string]$RuntimeDir
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $SetupPath) {
    $setup = Get-ChildItem -LiteralPath (Join-Path $repoRoot "release") `
        -Filter "Folder2Feishu-Windows-x64-Setup-*.exe" `
        -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $setup) {
        throw "release 目录中没有 Folder2Feishu 安装程序。"
    }
    $SetupPath = $setup.FullName
}
else {
    $SetupPath = (Resolve-Path $SetupPath).Path
}
if (-not $InstallDir) {
    $InstallDir = Join-Path $env:TEMP ("folder2feishu-installed-" + [guid]::NewGuid().ToString("N"))
}
if (-not $RuntimeDir) {
    $RuntimeDir = Join-Path $env:TEMP ("folder2feishu-installer-smoke-" + [guid]::NewGuid().ToString("N"))
}

$installArgument = '/DIR="{0}"' -f $InstallDir
$installer = Start-Process `
    -FilePath $SetupPath `
    -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", $installArgument) `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
if ($installer.ExitCode -ne 0) {
    throw "Inno Setup 静默安装失败，退出码：$($installer.ExitCode)"
}

$installedExecutable = Join-Path $InstallDir "Folder2Feishu.exe"
if (-not (Test-Path -LiteralPath $installedExecutable)) {
    throw "安装完成后未找到 Folder2Feishu.exe。"
}
$uninstaller = Join-Path $InstallDir "unins000.exe"
if (-not (Test-Path -LiteralPath $uninstaller)) {
    throw "安装完成后未找到 Inno Setup 卸载程序。"
}

try {
    & (Join-Path $PSScriptRoot "Test-PackagedApp.ps1") `
        -Executable $installedExecutable `
        -RuntimeDir $RuntimeDir
}
finally {
    if (-not (Test-Path -LiteralPath $uninstaller)) {
        throw "启动检查后卸载程序丢失，无法验证卸载。"
    }
    $uninstall = Start-Process `
        -FilePath $uninstaller `
        -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($uninstall.ExitCode -ne 0) {
        throw "Inno Setup 静默卸载失败，退出码：$($uninstall.ExitCode)"
    }
}

if (Test-Path -LiteralPath $installedExecutable) {
    throw "卸载完成后 Folder2Feishu.exe 仍然存在。"
}
if (Test-Path -LiteralPath $uninstaller) {
    throw "卸载完成后 Inno Setup 卸载程序仍然存在。"
}

Write-Host "安装、启动与卸载检查通过。" -ForegroundColor Green
