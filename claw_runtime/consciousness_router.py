"""
Intent router — maps TG / CLI text to a lane (code vs trading vs general).

Inspired by claude-tg-bot dispatch patterns; no 404 consciousness.py in upstream:
this is a lightweight, deterministic first version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RouteDecision:
    lane: str  # code | trading | general
    system_injection: str
    preferred_skills: tuple[str, ...]


_CODE_PAT = re.compile(
    r"bug|fix|refactor|pytest|git|commit|build|deploy|docker|api|error|traceback|"
    r"代码|修复|重构|提交|报错|异常|测试|部署|接口",
    re.I,
)
_TRADE_PAT = re.compile(
    r"\b(btc|eth|sol|dex|perp|funding|mcap|volume|liquidity|chain|on-?chain|"
    r"whale|token|pair|swap|defi)\b|"
    r"行情|K线|交易|资金费|链上|聪明钱|市值|流动性",
    re.I,
)


def route_message(text: str) -> RouteDecision:
    t = (text or "").strip()
    if not t:
        return RouteDecision("general", "", ())

    code_hits = len(_CODE_PAT.findall(t))
    trade_hits = len(_TRADE_PAT.findall(t))

    if code_hits == 0 and trade_hits == 0:
        return RouteDecision("general", "", ())

    if code_hits >= trade_hits:
        return RouteDecision(
            "code",
            (
                "【意识路由 — 工程/自愈】优先小步验证：读文件、跑测试、看日志。"
                "复杂流程 load_skill：git-commit、code-review、skill-creator。"
            ),
            ("git-commit", "code-review"),
        )

    return RouteDecision(
        "trading",
        (
            "【意识路由 — 交易/数据猎食】优先 load_skill：trading（生存/策略总览）、trading_dex_pulse、"
            "trading_funding_public、trading_correlation_note；需要公开 HTTP 时用 web_fetch；"
            "需要自然语言行情摘要可 execute_terminal 调用 `py tools\\\\web_agent.py \"...\"`。"
            "不做非法爬取、不提供内幕或保证收益；资金与计费须人工托管。"
        ),
        ("trading", "trading_dex_pulse", "trading_funding_public"),
    )
