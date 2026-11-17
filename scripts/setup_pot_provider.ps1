# Builds the token script of bgutil-ytdlp-pot-provider (PO tokens for YouTube, "script" mode).
#
# The yt-dlp plugin itself comes from requirements.txt (bgutil-ytdlp-pot-provider>=2.0.2;
# versions below 2.0.0 have a known vulnerability).  The plugin runs
# %USERPROFILE%\bgutil-ytdlp-pot-provider\server\build\generate_once.js with Node.js
# whenever yt-dlp needs a PO token, so this script clones the same version there
# and builds it.  No server is left running and no cookies are used.
# A different place can be used with CUT_VIDEO_BGUTIL_SERVER_HOME (the "server" folder).
#
# Usage: powershell -ExecutionPolicy Bypass -File scripts\setup_pot_provider.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv is missing. Run setup.ps1 first." }
foreach ($tool in "git", "node", "npm") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is not on PATH." }
}

$version = (& $python -c "from importlib.metadata import version; print(version('bgutil-ytdlp-pot-provider'))").Trim()
if ($LASTEXITCODE -ne 0 -or -not $version) { throw "bgutil-ytdlp-pot-provider is not installed in the venv (pip install -r requirements.txt)." }
if ([version]$version -lt [version]"2.0.2") { throw "bgutil-ytdlp-pot-provider $version is too old; 2.0.2 or later is required." }

$home_dir = Join-Path $env:USERPROFILE "bgutil-ytdlp-pot-provider"
if ($env:CUT_VIDEO_BGUTIL_SERVER_HOME) { $home_dir = Split-Path -Parent $env:CUT_VIDEO_BGUTIL_SERVER_HOME }
$server = Join-Path $home_dir "server"

if (Test-Path (Join-Path $home_dir ".git")) {
    $current = ""
    try { $current = (git -C $home_dir describe --tags --exact-match 2>$null) } catch { }  # no tag here: stderr
    if ($current -ne $version) {
        Write-Host "Switching bgutil provider $current -> $version"
        git -C $home_dir fetch --depth 1 origin "refs/tags/${version}:refs/tags/${version}"
        git -C $home_dir -c advice.detachedHead=false checkout --force $version
        if ($LASTEXITCODE -ne 0) { throw "git checkout $version failed." }
    }
} else {
    Write-Host "Cloning bgutil-ytdlp-pot-provider $version into $home_dir"
    git -c advice.detachedHead=false clone --quiet --depth 1 --single-branch --branch $version `
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git $home_dir
    if ($LASTEXITCODE -ne 0) { throw "git clone failed." }
}

Push-Location $server
try {
    npm ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed." }
    npx tsc
    if ($LASTEXITCODE -ne 0) { throw "npx tsc failed." }
} finally {
    Pop-Location
}
if (-not (Test-Path (Join-Path $server "build\generate_once.js"))) { throw "build\generate_once.js was not produced." }
Write-Host "bgutil PO token script $version is ready: $server"
& $python (Join-Path $root "scripts\check_ytdlp_setup.py")
