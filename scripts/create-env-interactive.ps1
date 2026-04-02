# 在仓库根目录运行: .\scripts\create-env-interactive.ps1
# 在终端里粘贴密钥（不会回显到聊天），生成 .env（已被 .gitignore 忽略）

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$out = Join-Path $root ".env"

Write-Host "=== 创建 .env（不会提交到 Git）===" -ForegroundColor Cyan
Write-Host "从 @BotFather 复制 Bot Token；OpenAI 从 platform.openai.com/api-keys 复制。"

$tg = Read-Host "TG_BOT_TOKEN"
$admin = Read-Host "TG_ADMIN_CHAT_IDS (数字，可多个用英文逗号)"
$oai = Read-Host "OPENAI_API_KEY"

$lines = @(
    "TG_BOT_TOKEN=$tg",
    "TG_ADMIN_CHAT_IDS=$admin",
    "OPENAI_API_KEY=$oai",
    "",
    "# 可选：",
    "# DEVCLAW_WORKSPACE=$root",
    "# OPENAI_MODEL=gpt-4o"
)
Set-Content -Path $out -Value $lines -Encoding UTF8
Write-Host "已写入: $out" -ForegroundColor Green
Write-Host "启动: .\scripts\start-tg-bot.ps1"
