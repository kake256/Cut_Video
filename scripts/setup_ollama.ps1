# Optional local Ollama bootstrap for Cut_Video on Windows.
#
# This script is deliberately separate from the normal application bootstrap:
# downloading a model is several GiB and is only needed for the experimental
# transcript analysis feature.  It never sends transcripts to a remote host.
[CmdletBinding()]
param(
    [string]$Model = "qwen3:8b",
    [switch]$SkipModelPull,
    [int]$ReadyTimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"

if ($Model -notmatch '^[A-Za-z0-9][A-Za-z0-9._:/-]*$') {
    throw "Invalid Ollama model name: $Model"
}
if ($ReadyTimeoutSeconds -lt 5 -or $ReadyTimeoutSeconds -gt 300) {
    throw "ReadyTimeoutSeconds must be between 5 and 300."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $repoRoot
$dependenciesRoot = Join-Path $workspaceRoot "dependencies"
$ollamaRoot = Join-Path $dependenciesRoot "ollama"
$managedInstallDir = Join-Path $ollamaRoot "app"
$managedModelsDir = Join-Path $ollamaRoot "models"
$officialInstallerUrl = "https://ollama.com/download/OllamaSetup.exe"
$apiUrl = "http://127.0.0.1:11434/api/tags"

function Get-OllamaExecutable {
    $candidates = @()

    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if (-not $command) {
        $command = Get-Command ollama -ErrorAction SilentlyContinue
    }
    if ($command -and $command.Source) {
        $candidates += $command.Source
    }

    $candidates += @(
        (Join-Path $managedInstallDir "ollama.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"),
        (Join-Path $env:ProgramFiles "Ollama\ollama.exe")
    )

    foreach ($candidate in $candidates | Select-Object -Unique) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Test-OllamaReady {
    try {
        $response = Invoke-WebRequest -Uri $apiUrl -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-OllamaReady {
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-OllamaReady) {
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "Ollama did not become ready at $apiUrl within $ReadyTimeoutSeconds seconds."
}

function Install-Ollama {
    New-Item -ItemType Directory -Force -Path $managedInstallDir, $managedModelsDir | Out-Null
    # Keep the large temporary installer on the same external dependency
    # drive instead of consuming the system drive. It is removed afterward.
    $installerPath = Join-Path $ollamaRoot "OllamaSetup.exe.download"
    try {
        Write-Host "[INFO] Downloading Ollama from its official distribution site..." -ForegroundColor Cyan
        Invoke-WebRequest -Uri $officialInstallerUrl -OutFile $installerPath -UseBasicParsing

        $signature = Get-AuthenticodeSignature -FilePath $installerPath
        if ($signature.Status -ne "Valid") {
            throw "The downloaded Ollama installer does not have a valid Authenticode signature ($($signature.Status))."
        }

        # Ollama documents /DIR for a custom Windows installation location.
        # Keep the binary outside this repository, alongside other dependencies.
        Write-Host "[INFO] Installing Ollama under $managedInstallDir ..." -ForegroundColor Cyan
        $installerArguments = @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/DIR=$managedInstallDir"
        )
        # Start-Process -Wait waits for the full descendant process tree on
        # Windows. Ollama's installer launches its tray app, so -Wait would
        # never return while that app remains active. Wait only for the
        # installer process itself, then manage the local API explicitly below.
        $installer = Start-Process -FilePath $installerPath -ArgumentList $installerArguments -PassThru
        $installer.WaitForExit()
        if ($installer.ExitCode -ne 0) {
            throw "Ollama installer exited with code $($installer.ExitCode)."
        }
    } finally {
        if (Test-Path -LiteralPath $installerPath) {
            Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
        }
    }

    $exe = Get-OllamaExecutable
    if (-not $exe) {
        throw "Ollama installed, but ollama.exe could not be found. Re-run this script or install it from https://ollama.com/download"
    }

    # Only a newly managed install gets a persistent F-drive-derived model path.
    # An existing installation is reused as-is so already-downloaded models are not hidden or duplicated.
    [Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $managedModelsDir, "User")
    $env:OLLAMA_MODELS = $managedModelsDir
    return $exe
}

Write-Host "=== Cut_Video optional local Ollama setup ===" -ForegroundColor Cyan
Write-Host "Repository: $repoRoot"
Write-Host "Experimental LLM analysis stays local at 127.0.0.1."

$ollamaExe = Get-OllamaExecutable
$installedNow = $false
if ($ollamaExe) {
    Write-Host "[INFO] Reusing existing Ollama: $ollamaExe" -ForegroundColor Green
} else {
    $ollamaExe = Install-Ollama
    $installedNow = $true
    Write-Host "[INFO] Ollama installed: $ollamaExe" -ForegroundColor Green
}

$resolvedManagedInstall = [IO.Path]::GetFullPath($managedInstallDir).TrimEnd('\') + '\'
$resolvedOllamaExe = [IO.Path]::GetFullPath($ollamaExe)
if ($resolvedOllamaExe.StartsWith($resolvedManagedInstall, [StringComparison]::OrdinalIgnoreCase)) {
    [Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $managedModelsDir, "User")
    $env:OLLAMA_MODELS = $managedModelsDir
}

# The Windows installer may launch Ollama before the new OLLAMA_MODELS user
# variable is visible. For a just-created managed install, restart only its
# fresh local process so the model pull uses the external dependency directory.
if ($installedNow) {
    Get-Process -Name "ollama", "ollama app" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($managedInstallDir, [StringComparison]::OrdinalIgnoreCase) } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
}

if (Test-OllamaReady) {
    Write-Host "[INFO] Ollama API is already ready at $apiUrl" -ForegroundColor Green
} else {
    Write-Host "[INFO] Starting local Ollama API..." -ForegroundColor Cyan
    Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WorkingDirectory $repoRoot -WindowStyle Hidden | Out-Null
    Wait-OllamaReady
    Write-Host "[INFO] Ollama API is ready at $apiUrl" -ForegroundColor Green
}

if ($SkipModelPull) {
    Write-Host "[INFO] Model pull skipped. Run this script without -SkipModelPull before enabling LLM analysis."
    exit 0
}

Write-Host "[INFO] Checking model '$Model'..." -ForegroundColor Cyan
& $ollamaExe show $Model *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[INFO] Model '$Model' is already available; nothing to download." -ForegroundColor Green
} else {
    Write-Host "[INFO] Pulling '$Model' (this can download several GiB)..." -ForegroundColor Cyan
    & $ollamaExe pull $Model
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to pull Ollama model '$Model' (exit code $LASTEXITCODE)."
    }
    Write-Host "[INFO] Model '$Model' is ready." -ForegroundColor Green
}

Write-Host "[DONE] You can now enable the experimental local LLM analysis in Cut_Video." -ForegroundColor Green
