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
    $probe = & $c.File @($c.Pre + @("-c", "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2])")) 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $probe) { continue }

    $exe = ($probe | Select-Object -First 1).Trim()
    $ver = ($probe | Select-Object -Skip 1 -First 1).Trim()
    if (-not (Test-Path $exe)) { continue }

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
& $pythonExe -m pip install -e $repoRoot --quiet --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed." -ForegroundColor Red
    exit 1
}

# Confirm the server module actually imports. The config written below invokes
# it as `-m kiro_stats_mcp.server`, so failing here beats failing silently
# inside Kiro at startup.
& $pythonExe -c "import kiro_stats_mcp.server" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: kiro_stats_mcp.server does not import after install." -ForegroundColor Red
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
$json = $mcpConfig | ConvertTo-Json -Depth 10 -Compress |
    & $pythonExe -c "import sys, json; print(json.dumps(json.loads(sys.stdin.read()), indent=2))"
if ($LASTEXITCODE -ne 0 -or -not $json) {
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

# --- Done ---
Write-Host ""
Write-Host "=== Installation Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Restart Kiro to activate. Usage:" -ForegroundColor White
Write-Host "  /stats - check credits, agent time, session time" -ForegroundColor White
Write-Host ""

# pip writes upgrade notices to stderr, which leaves a nonzero code behind on
# some hosts. Everything above succeeded or exited, so report success.
exit 0
