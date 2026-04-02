# Run from repo root: .\scripts\start-tg-bot.ps1
# Copy env.example -> .env and fill TG_BOT_TOKEN, TG_ADMIN_CHAT_IDS, OPENAI_API_KEY (never commit .env)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    Get-Content $envFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -match '^\s*#' -or $line -eq "") { return }
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $key = $matches[1]
            $val = $matches[2].Trim().Trim('"').Trim("'")
            Set-Item -Path "Env:$key" -Value $val
        }
    }
    Write-Host "Loaded .env"
} else {
    Write-Host "No .env found. Copy env.example to .env or set TG_BOT_TOKEN, TG_ADMIN_CHAT_IDS, OPENAI_API_KEY in this shell."
}

if (-not $env:TG_BOT_TOKEN -or -not $env:TG_ADMIN_CHAT_IDS -or -not $env:OPENAI_API_KEY) {
    Write-Error "Missing TG_BOT_TOKEN, TG_ADMIN_CHAT_IDS, or OPENAI_API_KEY"
    exit 1
}

py -m pip install -q -r requirements-telegram.txt
py tg_dev_claw.py
