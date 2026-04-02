# Run from repo root: .\scripts\create-env-interactive.ps1
# Writes .env (gitignored). ASCII-only to avoid Windows PowerShell encoding issues.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$out = Join-Path $root ".env"

Write-Host "=== Create .env (not committed to Git) ===" -ForegroundColor Cyan
Write-Host "Paste: Bot token from @BotFather; chat id from /whoami; OpenAI key from platform.openai.com/api-keys"

$tg = Read-Host "TG_BOT_TOKEN"
$admin = Read-Host "TG_ADMIN_CHAT_IDS (numbers only, comma-separated for multiple)"
$oai = Read-Host "OPENAI_API_KEY"

$lines = @(
    "TG_BOT_TOKEN=$tg",
    "TG_ADMIN_CHAT_IDS=$admin",
    "OPENAI_API_KEY=$oai",
    "",
    "# Optional:",
    "# DEVCLAW_WORKSPACE=$root",
    "# OPENAI_MODEL=gpt-4o"
)
Set-Content -Path $out -Value $lines -Encoding utf8
Write-Host "Wrote: $out" -ForegroundColor Green
Write-Host "Start bot: .\scripts\start-tg-bot.ps1"
