# Create or reuse backend/.venv from the pinned lock, then run an offline self-check.
# Idempotent. Pass --force to delete backend/.venv and create it again.
# Requires Python 3.11 or newer. Node.js is optional and is not required.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Script = Join-Path $PSScriptRoot "setup_support.py"

# Prefer the py launcher (-3) before python. On Windows, python is sometimes
# the Microsoft Store alias.
$candidates = @(
    @{ Exe = "py"; Prefix = @("-3") },
    @{ Exe = "python3"; Prefix = @() },
    @{ Exe = "python"; Prefix = @() }
)

function Test-Candidate([string]$Exe, [string[]]$Prefix) {
    $cmd = Get-Command $Exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        return $false
    }
    & $cmd.Source @Prefix -c "import sys"
    return $LASTEXITCODE -eq 0
}

$chosen = $null
if ($env:PYTHON) {
    $chosen = @{ Exe = $env:PYTHON; Prefix = @() }
} else {
    foreach ($candidate in $candidates) {
        if (Test-Candidate $candidate.Exe $candidate.Prefix) {
            $cmd = Get-Command $candidate.Exe -ErrorAction SilentlyContinue
            $chosen = @{ Exe = $cmd.Source; Prefix = $candidate.Prefix }
            break
        }
    }
}

if (-not $chosen) {
    Write-Host "[setup] FAIL Python was not found on PATH. Install Python 3.11 or newer and rerun."
    exit 1
}

& $chosen.Exe @($chosen.Prefix) $Script @args
exit $LASTEXITCODE
