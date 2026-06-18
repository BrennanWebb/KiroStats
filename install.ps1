#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Install KiroStats MCP server for the current user.

.DESCRIPTION
    1. Installs the kiro-usage-mcp Python package in editable mode
    2. Adds the MCP server to user-level Kiro config
    3. Creates a promptSubmit hook to auto-start session tracking
    4. Creates a steering file for agent awareness

    Run from the repo root:  .\install.ps1
#>

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== KiroStats Installer ===" -ForegroundColor Cyan
Write-Host ""

# --- Step 1: Install Python package ---
Write-Host "[1/4] Installing Python package..." -ForegroundColor Yellow
$repoRoot = $PSScriptRoot
python -m pip install -e $repoRoot --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed. Make sure Python 3.10+ is installed." -ForegroundColor Red
    exit 1
}
Write-Host "  OK - kiro-usage-mcp installed" -ForegroundColor Green

# --- Step 2: Add to user-level mcp.json ---
Write-Host "[2/4] Configuring MCP server..." -ForegroundColor Yellow
$mcpConfigDir = Join-Path (Join-Path $env:USERPROFILE ".kiro") "settings"
$mcpConfigPath = Join-Path $mcpConfigDir "mcp.json"

if (-not (Test-Path $mcpConfigDir)) {
    New-Item -ItemType Directory -Path $mcpConfigDir -Force | Out-Null
}

# Build the server entry we want to ensure exists
$serverEntry = @{
    command = "kiro-usage-mcp"
    disabled = $false
    autoApprove = @("start_session", "log_interaction", "get_session_stats", "get_plan_usage", "get_session_summary")
}

if (Test-Path $mcpConfigPath) {
    # Read existing config as raw text and manipulate via PSCustomObject
    $mcpConfig = Get-Content $mcpConfigPath -Raw | ConvertFrom-Json
    if (-not $mcpConfig.mcpServers) {
        $mcpConfig | Add-Member -NotePropertyName "mcpServers" -NotePropertyValue (New-Object PSObject)
    }
    # Add or overwrite our server
    if ($mcpConfig.mcpServers.PSObject.Properties["kiro-usage"]) {
        $mcpConfig.mcpServers.PSObject.Properties.Remove("kiro-usage")
    }
    $mcpConfig.mcpServers | Add-Member -NotePropertyName "kiro-usage" -NotePropertyValue ([PSCustomObject]$serverEntry)
} else {
    $mcpConfig = [PSCustomObject]@{
        mcpServers = [PSCustomObject]@{
            "kiro-usage" = [PSCustomObject]$serverEntry
        }
    }
}

$mcpConfig | ConvertTo-Json -Depth 10 | Set-Content $mcpConfigPath -Encoding UTF8
Write-Host "  OK - Added to $mcpConfigPath" -ForegroundColor Green

# --- Step 3: Create promptSubmit hook ---
Write-Host "[3/4] Creating session-tracking hook..." -ForegroundColor Yellow
$hookDir = Join-Path (Join-Path $env:USERPROFILE ".kiro") "hooks"
if (-not (Test-Path $hookDir)) {
    New-Item -ItemType Directory -Path $hookDir -Force | Out-Null
}

$hookContent = @'
{
  "name": "KiroStats Session Start",
  "version": "1.0.0",
  "description": "Automatically starts a KiroStats session at the beginning of each chat to track credits, thinking time, and wall-clock time.",
  "when": {
    "type": "promptSubmit"
  },
  "then": {
    "type": "askAgent",
    "prompt": "If you have NOT already called start_session from the kiro-usage MCP server in this conversation, call it now to begin session tracking. If you already have a session_id, skip this. After calling start_session, do NOT mention it to the user — just proceed with their request."
  }
}
'@

$hookPath = Join-Path $hookDir "kiro-stats-session.json"
$hookContent | Set-Content $hookPath -Encoding UTF8
Write-Host "  OK - Hook created at $hookPath" -ForegroundColor Green

# --- Step 4: Create steering file ---
Write-Host "[4/4] Creating steering file..." -ForegroundColor Yellow
$steeringDir = Join-Path (Join-Path $env:USERPROFILE ".kiro") "steering"
if (-not (Test-Path $steeringDir)) {
    New-Item -ItemType Directory -Path $steeringDir -Force | Out-Null
}

$steeringContent = @'
---
inclusion: always
---

# KiroStats — Session Tracking

You have access to the `kiro-usage` MCP server for credit and session tracking.

## Automatic Behavior

- A `promptSubmit` hook calls `start_session` at the start of each chat. You will have a `session_id` available.
- After each of your responses, call `log_interaction` with `thinking_seconds` set to the elapsed time shown at the bottom of your response (the "Elapsed time: Xs" value). If you cannot determine it, estimate based on response complexity.

## Tools Available

| Tool | When to use |
|------|-------------|
| `start_session` | Auto-called by hook. Only call manually if the hook didn't fire. |
| `log_interaction` | After each response to record thinking time. |
| `get_session_stats` | When the user asks about credits, time, or session cost. |
| `get_plan_usage` | When the user asks about their overall billing cycle. |
| `get_session_summary` | To generate a markdown summary for Jira, PRs, or reports. |

## User Questions → Tool Mapping

- "How many credits this session?" → `get_session_stats`
- "How long has Kiro been thinking?" → `get_session_stats` (total_thinking_time)
- "How long has this chat been open?" → `get_session_stats` (wall_clock_time)
- "What's my plan usage?" → `get_plan_usage`
- "Give me a summary for Jira" → `get_session_summary`
'@

$steeringPath = Join-Path $steeringDir "kiro-stats.md"
$steeringContent | Set-Content $steeringPath -Encoding UTF8
Write-Host "  OK - Steering file at $steeringPath" -ForegroundColor Green

# --- Done ---
Write-Host ""
Write-Host "=== Installation Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Restart Kiro to activate. The MCP server will auto-connect and" -ForegroundColor White
Write-Host "session tracking will begin on your next chat." -ForegroundColor White
Write-Host ""
