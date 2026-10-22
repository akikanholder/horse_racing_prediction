# Create or reuse backend/.venv from the pinned lock, then run an offline self-check.
# Idempotent. Pass --force to delete backend/.venv and create it again.
# Requires Python 3.11 or newer. Node.js is optional and is not required.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Script = Join-Path $PSScriptRoot "setup_support.py"

function Find-Python {
    $names = @()
    if ($env:PYTHON) {
        $names += $env:PYTHON
    }
    $names += @("python", "python3", "py")
    foreach ($name in $names) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            return $cmd.Source
        }
    }
    Write-Host "[setup] FAIL Python was not found on PATH. Install Python 3.11 or newer and rerun."
    exit 1
}

$Py = Find-Python
& $Py $Script @args
exit $LASTEXITCODE
