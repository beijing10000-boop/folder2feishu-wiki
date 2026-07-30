[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipInstall,
    [switch]$SkipInstaller,
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$frontendDir = Join-Path $repoRoot "frontend"
$distDir = Join-Path $repoRoot "dist"
$buildDir = Join-Path $repoRoot "build"
$releaseDir = Join-Path $repoRoot "release"
$specFile = Join-Path $PSScriptRoot "Folder2Feishu.spec"
$installerScript = Join-Path $PSScriptRoot "Folder2Feishu.iss"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$pythonExe = if (Test-Path -LiteralPath $venvPython) {
    $venvPython
}
else {
    (Get-Command "python" -ErrorAction Stop).Source
}

function Assert-PathInsideRepository {
    param([Parameter(Mandatory)][string]$Path)
    $absolute = [System.IO.Path]::GetFullPath($Path)
    $rootWithSeparator = $repoRoot.TrimEnd("\") + "\"
    if (-not $absolute.StartsWith($rootWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作仓库外路径：$absolute"
    }
    if ($absolute -eq $repoRoot) {
        throw "拒绝操作仓库根目录本身。"
    }
}

function Remove-BuildDirectory {
    param([Parameter(Mandatory)][string]$Path)
    Assert-PathInsideRepository -Path $Path
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @()
    )
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败（$LASTEXITCODE）：$FilePath $($ArgumentList -join ' ')"
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $frontendDir "package-lock.json"))) {
    throw "缺少 frontend/package-lock.json，无法执行可复现构建。"
}

$pyprojectText = Get-Content -LiteralPath (Join-Path $repoRoot "pyproject.toml") -Raw
$versionMatch = [regex]::Match($pyprojectText, '(?m)^version\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) {
    throw "无法从 pyproject.toml 读取版本号。"
}
$appVersion = $versionMatch.Groups[1].Value.Replace("rc", "-rc.")
$packageVersion = $versionMatch.Groups[1].Value

$moduleVersionText = Get-Content -LiteralPath (Join-Path $repoRoot "folder2feishu\__init__.py") -Raw
$moduleVersionMatch = [regex]::Match($moduleVersionText, '__version__\s*=\s*"([^"]+)"')
if (-not $moduleVersionMatch.Success -or $moduleVersionMatch.Groups[1].Value -ne $packageVersion) {
    throw "folder2feishu.__version__ 与 pyproject.toml 版本不一致。"
}
$frontendPackage = Get-Content -LiteralPath (Join-Path $frontendDir "package.json") -Raw |
    ConvertFrom-Json
if ($frontendPackage.version -ne $appVersion) {
    throw "frontend/package.json 与 pyproject.toml 版本不一致。"
}
$versionInfoText = Get-Content -LiteralPath (Join-Path $PSScriptRoot "version_info.txt") -Raw
if (
    $versionInfoText -notmatch [regex]::Escape("FileVersion', u'$appVersion'") -or
    $versionInfoText -notmatch [regex]::Escape("ProductVersion', u'$appVersion'")
) {
    throw "packaging/version_info.txt 与 pyproject.toml 版本不一致。"
}

Remove-BuildDirectory -Path $distDir
Remove-BuildDirectory -Path $buildDir
Remove-BuildDirectory -Path $releaseDir
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

Push-Location $frontendDir
try {
    Invoke-Checked -FilePath "npm.cmd" -ArgumentList @("ci", "--no-audit", "--no-fund")
    if (-not $SkipTests) {
        Invoke-Checked -FilePath "npm.cmd" -ArgumentList @("test")
    }
    Invoke-Checked -FilePath "npm.cmd" -ArgumentList @("run", "build")
}
finally {
    Pop-Location
}

if (-not $SkipInstall) {
    Invoke-Checked -FilePath $pythonExe -ArgumentList @("-m", "pip", "install", "-e", "${repoRoot}[dev]")
}
if (-not $SkipTests) {
    Invoke-Checked -FilePath $pythonExe -ArgumentList @("-m", "pytest")
}

Push-Location $repoRoot
try {
    Invoke-Checked -FilePath $pythonExe -ArgumentList @(
        "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "--distpath", $distDir,
        "--workpath", (Join-Path $buildDir "pyinstaller"),
        $specFile
    )
}
finally {
    Pop-Location
}

if (-not $SkipSmoke) {
    & (Join-Path $PSScriptRoot "Test-PackagedApp.ps1")
}

$portableZip = Join-Path $releaseDir "Folder2Feishu-Windows-x64-Portable-$appVersion.zip"
Compress-Archive -LiteralPath (Join-Path $distDir "Folder2Feishu") -DestinationPath $portableZip -CompressionLevel Optimal

if (-not $SkipSmoke) {
    $portableSmokeDir = Join-Path $buildDir "portable-smoke"
    Remove-BuildDirectory -Path $portableSmokeDir
    Expand-Archive -LiteralPath $portableZip -DestinationPath $portableSmokeDir
    & (Join-Path $PSScriptRoot "Test-PackagedApp.ps1") `
        -Executable (Join-Path $portableSmokeDir "Folder2Feishu\Folder2Feishu.exe") `
        -RuntimeDir (Join-Path $portableSmokeDir "runtime")
}

if (-not $SkipInstaller) {
    $isccCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    $iscc = $isccCandidates | Select-Object -First 1
    if (-not $iscc) {
        throw "未找到 Inno Setup 6。可安装后重试，或使用 -SkipInstaller 只生成便携版。"
    }
    Invoke-Checked -FilePath $iscc -ArgumentList @("/DMyAppVersion=$appVersion", $installerScript)
}

$checksumFile = Join-Path $releaseDir "SHA256SUMS.txt"
$hashLines = Get-ChildItem -LiteralPath $releaseDir -File |
    Where-Object { $_.Name -ne "SHA256SUMS.txt" } |
    Sort-Object Name |
    ForEach-Object {
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $($_.Name)"
    }
Set-Content -LiteralPath $checksumFile -Value $hashLines -Encoding utf8NoBOM

Write-Host ""
Write-Host "Windows 交付构建完成：" -ForegroundColor Green
Get-ChildItem -LiteralPath $releaseDir -File | Select-Object Name, Length, LastWriteTime
