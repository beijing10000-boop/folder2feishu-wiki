[CmdletBinding()]
param(
    [string]$PackagePath = "",
    [string]$Repository = "beijing10000-boop/folder2feishu-wiki",
    [string]$GitHubToken = "",
    [string]$RuntimeDir = "D:\Folder2FeishuDrive\Data",
    [switch]$IncludePrerelease,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$legacyInstallDir = [System.IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA "Programs\Folder2FeishuDrive")
)
$installDir = if ($PSScriptRoot.Equals(
        $legacyInstallDir,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
    "D:\Folder2FeishuDrive\App"
}
else {
    $PSScriptRoot
}
$temporaryRoot = Join-Path $env:TEMP ("folder2feishu-update-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null

try {
    if (-not $PackagePath) {
        $headers = @{
            "Accept" = "application/vnd.github+json"
            "X-GitHub-Api-Version" = "2022-11-28"
            "User-Agent" = "Folder2Feishu-Updater"
        }
        if ($GitHubToken) {
            $headers["Authorization"] = "Bearer $GitHubToken"
        }
        $releases = Invoke-RestMethod `
            -Uri "https://api.github.com/repos/$Repository/releases?per_page=20" `
            -Headers $headers
        $release = $releases |
            Where-Object { -not $_.draft -and ($IncludePrerelease -or -not $_.prerelease) } |
            Select-Object -First 1
        if (-not $release) {
            throw "No release was found. Pass -GitHubToken for a private repo and -IncludePrerelease for an RC."
        }
        $asset = $release.assets |
            Where-Object { $_.name -like "Folder2Feishu-Python-*.zip" } |
            Select-Object -First 1
        if (-not $asset) {
            throw "Release $($release.tag_name) does not contain a Python bundle."
        }
        $PackagePath = Join-Path $temporaryRoot $asset.name
        $downloadHeaders = $headers.Clone()
        $downloadHeaders["Accept"] = "application/octet-stream"
        Invoke-WebRequest -Uri $asset.url -Headers $downloadHeaders -OutFile $PackagePath
    }
    else {
        $PackagePath = (Resolve-Path $PackagePath).Path
    }

    $expanded = Join-Path $temporaryRoot "expanded"
    Expand-Archive -LiteralPath $PackagePath -DestinationPath $expanded
    $installer = Get-ChildItem -LiteralPath $expanded -Filter "Install-Folder2Feishu.ps1" -Recurse -File |
        Select-Object -First 1
    if (-not $installer) {
        throw "The update bundle is incomplete: Install-Folder2Feishu.ps1 is missing."
    }
    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $installer.FullName `
        -InstallDir $installDir `
        -RuntimeDir $RuntimeDir `
        -NoShortcut `
        -SkipLaunch
    if ($LASTEXITCODE -ne 0) {
        throw "Update installation failed with exit code $LASTEXITCODE."
    }
    $startArguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $installDir "Start-Folder2Feishu.ps1"),
        "-RuntimeDir", $RuntimeDir
    )
    if ($NoBrowser) {
        $startArguments += "-NoBrowser"
    }
    & powershell.exe @startArguments
    Write-Host "Folder2Feishu update completed." -ForegroundColor Green
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
