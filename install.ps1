#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Install KiroStats MCP server for the current user.

.DESCRIPTION
    1. Locates a usable Python 3.10+ interpreter
    2. Installs the kiro-stats-mcp package in editable mode
    3. Registers the MCP server in ~/.kiro/settings/mcp.json
    4. Creates the /stats steering file for manual invocation

    Run from the repo root:  .\install.ps1

    Works on Windows PowerShell 5.1 and PowerShell 7+.
#>

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== KiroStats Installer ===" -ForegroundColor Cyan
Write-Host ""

# Write UTF-8 without a BOM. Set-Content -Encoding UTF8 emits a BOM on Windows
# PowerShell 5.1, and -Encoding utf8NoBOM does not exist there, so neither is
# usable. A BOM ahead of the `---` in a steering file breaks front-matter
# parsing, and ahead of `{` it breaks strict JSON readers.
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Write-TextFile($Path, $Content) {
    [System.IO.File]::WriteAllText($Path, $Content, $Utf8NoBom)
}

# Run a native command and report its exit code without letting stderr abort us.
# pip and git routinely write notices to stderr while exiting 0 (for example
# "[notice] A new release of pip is available"). Under
# ErrorActionPreference=Stop, Windows PowerShell promotes native stderr output
# to a terminating error, which would kill this script mid-install even though
# the command succeeded. The exit code is the only reliable signal.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string] $Exe,
        [string[]] $Arguments = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Exe @Arguments 2>&1
        return [pscustomobject]@{
            ExitCode = $LASTEXITCODE
            Output   = $output
        }
    } finally {
        $ErrorActionPreference = $previous
    }
}

# --- Step 1: Locate a Python interpreter ---
Write-Host "[1/4] Locating Python..." -ForegroundColor Yellow

# Candidates cover the common Windows setups: python.org installers (python,
# and the `py` launcher), Store builds, and Linux/macOS style python3.
$candidates = @(
    @{ File = "python";  Pre = @() },
    @{ File = "py";      Pre = @("-3") },
    @{ File = "python3"; Pre = @() }
)

$pythonExe = $null
foreach ($c in $candidates) {
    if (-not (Get-Command $c.File -ErrorAction SilentlyContinue)) { continue }

    # Ask the interpreter for its own path rather than trusting Get-Command.
    # On Windows 10/11 `python` often resolves to the Microsoft Store app
    # execution alias under WindowsApps, which is a stub that can launch the
    # Store instead of Python. sys.executable always gives the real binary,
    # and guarantees we record the same interpreter that runs the install.
    $probe = Invoke-Native $c.File ($c.Pre + @("-c", "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2])"))
    if ($probe.ExitCode -ne 0 -or -not $probe.Output) { continue }

    $lines = @($probe.Output | Where-Object { $_ -is [string] -and $_.Trim() })
    if ($lines.Count -lt 2) { continue }
    $exe = $lines[0].Trim()
    $ver = $lines[1].Trim()
    if (-not (Test-Path $exe)) { continue }
    if ($ver -notmatch '^\d+\.\d+$') { continue }

    $parts = $ver.Split('.')
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 10)) {
        Write-Host "  Skipping $exe (Python $ver, need 3.10+)" -ForegroundColor DarkGray
        continue
    }

    $pythonExe = $exe
    Write-Host "  OK - Python $ver at $exe" -ForegroundColor Green
    break
}

if (-not $pythonExe) {
    Write-Host "ERROR: No Python 3.10+ interpreter found." -ForegroundColor Red
    Write-Host "       Install from https://www.python.org/downloads/ and re-run." -ForegroundColor Red
    Write-Host "       If 'python' opens the Microsoft Store, disable the app" -ForegroundColor Red
    Write-Host "       execution alias under Settings > Apps > App execution aliases." -ForegroundColor Red
    exit 1
}

# --- Step 2: Install the package ---
Write-Host "[2/4] Installing Python package..." -ForegroundColor Yellow
$repoRoot = $PSScriptRoot
$pip = Invoke-Native $pythonExe @("-m", "pip", "install", "-e", $repoRoot, "--quiet", "--no-warn-script-location")
if ($pip.ExitCode -ne 0) {
    Write-Host "ERROR: pip install failed." -ForegroundColor Red
    $pip.Output | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    exit 1
}

# Confirm the server module actually imports. The config written below invokes
# it as `-m kiro_stats_mcp.server`, so failing here beats failing silently
# inside Kiro at startup.
$check = Invoke-Native $pythonExe @("-c", "import kiro_stats_mcp.server")
if ($check.ExitCode -ne 0) {
    Write-Host "ERROR: kiro_stats_mcp.server does not import after install." -ForegroundColor Red
    $check.Output | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    exit 1
}
Write-Host "  OK - kiro-stats-mcp installed and importable" -ForegroundColor Green

# --- Step 3: Register the MCP server ---
Write-Host "[3/4] Configuring MCP server..." -ForegroundColor Yellow
$kiroDir = Join-Path $env:USERPROFILE ".kiro"
$mcpConfigDir = Join-Path $kiroDir "settings"
$mcpConfigPath = Join-Path $mcpConfigDir "mcp.json"

if (-not (Test-Path $mcpConfigDir)) {
    New-Item -ItemType Directory -Path $mcpConfigDir -Force | Out-Null
}

# `-m` with an absolute interpreter path, rather than the kiro-stats-mcp
# console script: pip drops that script in a Scripts directory that is
# frequently not on PATH, and Kiro launches MCP servers without a shell.
$serverEntry = @{
    command     = $pythonExe
    args        = @("-m", "kiro_stats_mcp.server")
    disabled    = $false
    autoApprove = @("get_session_stats")
}

if (Test-Path $mcpConfigPath) {
    $mcpConfig = Get-Content $mcpConfigPath -Raw | ConvertFrom-Json
    if (-not $mcpConfig.mcpServers) {
        $mcpConfig | Add-Member -NotePropertyName "mcpServers" -NotePropertyValue (New-Object PSObject)
    }
    if ($mcpConfig.mcpServers.PSObject.Properties["kiro-stats"]) {
        $mcpConfig.mcpServers.PSObject.Properties.Remove("kiro-stats")
    }
    $mcpConfig.mcpServers | Add-Member -NotePropertyName "kiro-stats" -NotePropertyValue ([PSCustomObject]$serverEntry)
} else {
    $mcpConfig = [PSCustomObject]@{
        mcpServers = [PSCustomObject]@{
            "kiro-stats" = [PSCustomObject]$serverEntry
        }
    }
}

# Round-trip through Python for stable 2-space indentation; ConvertTo-Json
# formatting differs between PowerShell 5.1 and 7.
$compact = $mcpConfig | ConvertTo-Json -Depth 10 -Compress
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $json = $compact | & $pythonExe -c "import sys, json; print(json.dumps(json.loads(sys.stdin.read()), indent=2))" 2>&1
    $serializeCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEap
}
if ($serializeCode -ne 0 -or -not $json) {
    Write-Host "ERROR: failed to serialize $mcpConfigPath" -ForegroundColor Red
    exit 1
}
Write-TextFile $mcpConfigPath (($json -join "`n") + "`n")
Write-Host "  OK - Added to $mcpConfigPath" -ForegroundColor Green

# --- Step 4: Create the steering file ---
Write-Host "[4/4] Creating steering file..." -ForegroundColor Yellow
$steeringDir = Join-Path $kiroDir "steering"
if (-not (Test-Path $steeringDir)) {
    New-Item -ItemType Directory -Path $steeringDir -Force | Out-Null
}

$statsContent = @'
---
inclusion: manual
---
Call `get_session_stats` from the kiro-stats MCP server. Pass one of this session's workspace root paths as `workspace_path` so the right session is picked when several Kiro windows are open.
'@

Write-TextFile (Join-Path $steeringDir "stats.md") ($statsContent + "`n")
Write-Host "  OK - Created stats.md in $steeringDir" -ForegroundColor Green

# --- Cleanup old files if present ---
$oldFiles = @(
    (Join-Path $steeringDir "start.md"),
    (Join-Path $steeringDir "credits.md"),
    (Join-Path $steeringDir "kiro-stats.md"),
    (Join-Path $kiroDir "hooks\kiro-stats-session.json")
)
foreach ($f in $oldFiles) {
    if (Test-Path $f) {
        Remove-Item $f -Force
        Write-Host "  Removed old file: $f" -ForegroundColor DarkGray
    }
}

# --- Verify ---
# Assert the artifacts actually landed. A silent abort mid-script previously
# left the package installed but no config and no steering file, which looked
# like success until you tried to use it.
$problems = @()
if (-not (Test-Path $mcpConfigPath)) {
    $problems += "missing $mcpConfigPath"
} else {
    $verify = Invoke-Native $pythonExe @("-c", @"
import json, sys
p = sys.argv[1]
cfg = json.loads(open(p, 'rb').read().decode('utf-8-sig'))
e = cfg.get('mcpServers', {}).get('kiro-stats')
assert e, 'kiro-stats entry missing'
assert e['args'] == ['-m', 'kiro_stats_mcp.server'], 'unexpected args'
"@, $mcpConfigPath)
    if ($verify.ExitCode -ne 0) { $problems += "config check failed: $($verify.Output -join ' ')" }
}
$statsPath = Join-Path $steeringDir "stats.md"
if (-not (Test-Path $statsPath)) { $problems += "missing $statsPath" }

if ($problems.Count -gt 0) {
    Write-Host ""
    Write-Host "ERROR: install did not complete cleanly:" -ForegroundColor Red
    $problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}

# --- Done ---
Write-Host ""
Write-Host "=== Installation Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Type /stats in any Kiro chat for credits, agent time and session time." -ForegroundColor White
Write-Host "Kiro normally picks up the server on its own; restart it if /stats does not respond." -ForegroundColor White
Write-Host ""

exit 0
