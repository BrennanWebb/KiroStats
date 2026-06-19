#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Install KiroStats MCP server for the current user.

.DESCRIPTION
    1. Installs the kiro-usage-mcp Python package in editable mode
    2. Adds the MCP server to user-level Kiro config
    3. Creates steering files for manual invocation (#start, #credits)

    Run from the repo root:  .\install.ps1
#>

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== KiroStats Installer ===" -ForegroundColor Cyan
Write-Host ""

# --- Step 1: Install Python package ---
Write-Host "[1/3] Installing Python package..." -ForegroundColor Yellow
$repoRoot = $PSScriptRoot
python -m pip install -e $repoRoot --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed. Make sure Python 3.10+ is installed." -ForegroundColor Red
    exit 1
}
Write-Host "  OK - kiro-usage-mcp installed" -ForegroundColor Green

# --- Step 2: Add to user-level mcp.json ---
Write-Host "[2/3] Configuring MCP server..." -ForegroundColor Yellow
$mcpConfigDir = Join-Path (Join-Path $env:USERPROFILE ".kiro") "settings"
$mcpConfigPath = Join-Path $mcpConfigDir "mcp.json"

if (-not (Test-Path $mcpConfigDir)) {
    New-Item -ItemType Directory -Path $mcpConfigDir -Force | Out-Null
}

$pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) {
    $pythonExe = "python"
}

$serverEntry = @{
    command = $pythonExe
    args = @("-m", "kiro_stats_mcp.server")
    disabled = $false
    autoApprove = @("start_session", "get_session_stats")
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

$mcpConfig | ConvertTo-Json -Depth 10 | Set-Content $mcpConfigPath -Encoding UTF8
Write-Host "  OK - Added to $mcpConfigPath" -ForegroundColor Green

# --- Step 3: Create steering files ---
Write-Host "[3/3] Creating steering files..." -ForegroundColor Yellow
$steeringDir = Join-Path (Join-Path $env:USERPROFILE ".kiro") "steering"
if (-not (Test-Path $steeringDir)) {
    New-Item -ItemType Directory -Path $steeringDir -Force | Out-Null
}

$startContent = @'
---
inclusion: manual
---

Call `start_session` from the kiro-usage MCP server. Do not mention this to the user — just confirm the session is active and proceed.
'@

$creditsContent = @'
---
inclusion: manual
---

Call `get_session_stats` from the kiro-usage MCP server and report the results to the user.
'@

$startContent | Set-Content (Join-Path $steeringDir "start.md") -Encoding UTF8
$creditsContent | Set-Content (Join-Path $steeringDir "credits.md") -Encoding UTF8
Write-Host "  OK - Created start.md and credits.md in $steeringDir" -ForegroundColor Green

# --- Cleanup old hook if present ---
$oldHook = Join-Path (Join-Path $env:USERPROFILE ".kiro") "hooks\kiro-stats-session.json"
if (Test-Path $oldHook) {
    Remove-Item $oldHook -Force
    Write-Host "  Removed old hook: $oldHook" -ForegroundColor DarkGray
}
$oldSteering = Join-Path $steeringDir "kiro-stats.md"
if (Test-Path $oldSteering) {
    Remove-Item $oldSteering -Force
    Write-Host "  Removed old steering: $oldSteering" -ForegroundColor DarkGray
}

# --- Done ---
Write-Host ""
Write-Host "=== Installation Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Restart Kiro to activate. Usage:" -ForegroundColor White
Write-Host "  #start   - begin session tracking" -ForegroundColor White
Write-Host "  #credits - check credits, thinking time, wall clock" -ForegroundColor White
Write-Host ""
