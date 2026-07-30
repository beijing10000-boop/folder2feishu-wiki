[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipInstall,
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$frontendDir = Join-Path $repoRoot "frontend"
$buildDir = Join-Path $repoRoot "build\python-release"
$bundleDir = Join-Path $buildDir "Folder2Feishu-Python"
$releaseDir = Join-Path $repoRoot "release"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$pythonExe = if (Test-Path -LiteralPath $venvPython) {
    $venvPython
}
else {
    (Get-Command "python.exe" -ErrorAction Stop).Source
}

function Assert-RepositoryChild {
    param([Parameter(Mandatory)][string]$Path)
    $absolute = [System.IO.Path]::GetFullPath($Path)
    $prefix = $repoRoot.TrimEnd("\") + "\"
    if (-not $absolute.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作仓库外路径：$absolute"
    }
}

function Remove-SafeDirectory {
    param([Parameter(Mandatory)][string]$Path)
    Assert-RepositoryChild -Path $Path
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

$pyproject = Get-Content -LiteralPath (Join-Path $repoRoot "pyproject.toml") -Raw
$versionMatch = [regex]::Match($pyproject, '(?m)^version\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) {
    throw "无法从 pyproject.toml 读取版本号。"
}
$packageVersion = $versionMatch.Groups[1].Value
$releaseVersion = $packageVersion.Replace("rc", "-rc.")
$moduleVersion = [regex]::Match(
    (Get-Content -LiteralPath (Join-Path $repoRoot "folder2feishu\__init__.py") -Raw),
    '__version__\s*=\s*"([^"]+)"'
).Groups[1].Value
$frontendVersion = (Get-Content -LiteralPath (Join-Path $frontendDir "package.json") -Raw | ConvertFrom-Json).version
if ($moduleVersion -ne $packageVersion -or $frontendVersion -ne $releaseVersion) {
    throw "后端、模块和前端版本号不一致。"
}

if (-not $SkipInstall) {
    Invoke-Checked -FilePath $pythonExe -ArgumentList @("-m", "pip", "install", "-e", "${repoRoot}[dev]")
}
if (-not $SkipTests) {
    Invoke-Checked -FilePath $pythonExe -ArgumentList @("-m", "ruff", "check", "folder2feishu", "tests")
    Invoke-Checked -FilePath $pythonExe -ArgumentList @("-m", "mypy", "folder2feishu")
    Invoke-Checked -FilePath $pythonExe -ArgumentList @("-m", "pytest")
}

Push-Location $frontendDir
try {
    Invoke-Checked -FilePath "npm.cmd" -ArgumentList @("ci", "--no-audit", "--no-fund")
    if (-not $SkipTests) {
        Invoke-Checked -FilePath "npm.cmd" -ArgumentList @("run", "lint")
        Invoke-Checked -FilePath "npm.cmd" -ArgumentList @("test")
    }
    Invoke-Checked -FilePath "npm.cmd" -ArgumentList @("run", "build")
}
finally {
    Pop-Location
}

Remove-SafeDirectory -Path $buildDir
Remove-SafeDirectory -Path $releaseDir
New-Item -ItemType Directory -Path $bundleDir -Force | Out-Null
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $repoRoot "folder2feishu") -Destination (Join-Path $bundleDir "folder2feishu") -Recurse
New-Item -ItemType Directory -Path (Join-Path $bundleDir "frontend") -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $frontendDir "dist") -Destination (Join-Path $bundleDir "frontend\dist") -Recurse
Copy-Item -LiteralPath (Join-Path $repoRoot "deploy") -Destination (Join-Path $bundleDir "deploy") -Recurse
foreach ($file in @("pyproject.toml", "requirements.txt", "README.md", "LICENSE", "SECURITY.md")) {
    Copy-Item -LiteralPath (Join-Path $repoRoot $file) -Destination (Join-Path $bundleDir $file)
}
Set-Content -LiteralPath (Join-Path $bundleDir "VERSION") -Value $releaseVersion -Encoding ascii
Copy-Item -LiteralPath (Join-Path $bundleDir "deploy\Install-Folder2Feishu.ps1") -Destination (Join-Path $bundleDir "Install-Folder2Feishu.ps1")
Copy-Item -LiteralPath (Join-Path $bundleDir "deploy\Install.cmd") -Destination (Join-Path $bundleDir "Install.cmd")

Get-ChildItem -LiteralPath $bundleDir -Directory -Filter "__pycache__" -Recurse |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
Get-ChildItem -LiteralPath $bundleDir -File -Include "*.pyc", "*.pyo" -Recurse |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

$zipPath = Join-Path $releaseDir "Folder2Feishu-Python-$releaseVersion.zip"
Compress-Archive -LiteralPath $bundleDir -DestinationPath $zipPath -CompressionLevel Optimal

if (-not $SkipSmoke) {
    & (Join-Path $PSScriptRoot "Test-PythonDeployment.ps1") -PackagePath $zipPath
}

$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content `
    -LiteralPath (Join-Path $releaseDir "SHA256SUMS.txt") `
    -Value "$hash  $([System.IO.Path]::GetFileName($zipPath))" `
    -Encoding utf8NoBOM

Write-Host "Python 发布包构建完成：" -ForegroundColor Green
Get-ChildItem -LiteralPath $releaseDir -File | Select-Object Name, Length, LastWriteTime
