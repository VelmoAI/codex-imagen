# install.ps1 — bootstrap uv (if needed) and run codex-imagen setup
#
# Usage:
#   iwr https://raw.githubusercontent.com/VelmoAI/codex-imagen/main/install.ps1 | iex
#
# Requires Windows PowerShell 5.1+ or PowerShell 7+.
$ErrorActionPreference = "Stop"

function Write-Status {
    param([string]$Message)
    Write-Host $Message
}

function Refresh-Path {
    # Pull the current User PATH from the registry and merge it into this
    # session's PATH so that tools installed moments ago become visible
    # without opening a new shell.
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath    = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($null -eq $machinePath) { $machinePath = "" }
    if ($null -eq $userPath)    { $userPath    = "" }
    $env:Path = "$machinePath;$userPath"
}

# ---------------------------------------------------------------------------
# 1. Ensure uv / uvx is available
# ---------------------------------------------------------------------------

$uvxCmd = Get-Command uvx -ErrorAction SilentlyContinue

if ($null -ne $uvxCmd) {
    Write-Status "uv is already installed."
} else {
    Write-Status "Installing uv..."
    try {
        irm https://astral.sh/uv/install.ps1 | iex
    } catch {
        Write-Error "uv installation failed: $_"
        exit 1
    }

    # Refresh PATH so the newly-installed uvx is visible in this session.
    Refresh-Path

    # Re-check for uvx on the refreshed PATH.
    $uvxCmd = Get-Command uvx -ErrorAction SilentlyContinue

    if ($null -eq $uvxCmd) {
        # Try the default install location before giving up.
        $fallback = Join-Path $env:USERPROFILE ".local\bin\uvx.exe"
        if (Test-Path $fallback) {
            $uvxCmd = $fallback
            Write-Status "Found uvx at: $fallback"
        } else {
            Write-Error "uvx was installed but could not be found on PATH. Please open a new shell and run: uvx codex-imagen setup"
            exit 1
        }
    }
}

# Resolve to a string path usable with & operator.
if ($uvxCmd -is [System.Management.Automation.CommandInfo]) {
    $uvxPath = $uvxCmd.Source
} else {
    $uvxPath = [string]$uvxCmd
}

# ---------------------------------------------------------------------------
# 2. Run codex-imagen setup
# ---------------------------------------------------------------------------

Write-Status "Running codex-imagen setup..."
try {
    & $uvxPath codex-imagen setup
    if ($LASTEXITCODE -ne 0) {
        Write-Error "codex-imagen setup exited with code $LASTEXITCODE"
        exit $LASTEXITCODE
    }
} catch {
    Write-Error "codex-imagen setup failed: $_"
    exit 1
}

Write-Status "Done — your AI clients now know about imagen."
