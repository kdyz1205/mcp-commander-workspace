"""Deterministic public research scout for paper-learning and factor mining tasks."""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET

import requests

from claw_runtime.ultimate.proxy_env import current_proxy_env

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass
class PaperHit:
    title: str
    published: str
    summary: str
    url: str
    query_label: str


@dataclass
class FactorHypothesis:
    name: str
    signal_family: str
    rationale: str
    implementation_hint: str
    evidence_titles: list[str]


@dataclass
class ResearchScoutResult:
    markdown_path: Path
    json_path: Path
    papers: list[PaperHit]
    factors: list[FactorHypothesis]
    note: str


def _emit(emit: Callable[[str], None] | None, text: str) -> None:
    if emit:
        emit(text)


def _request_text(url: str, workspace: Path, *, timeout: float = 25.0) -> str:
    proxy_map = current_proxy_env(workspace)
    mapped: dict[str, str] = {}
    if proxy_map.get("HTTP_PROXY"):
        mapped["http"] = proxy_map["HTTP_PROXY"]
    if proxy_map.get("HTTPS_PROXY"):
        mapped["https"] = proxy_map["HTTPS_PROXY"]

    def _fetch_with(mapped_proxy: dict[str, str] | None) -> str:
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            url,
            headers={"User-Agent": "DevClaw-ResearchLab/1.0"},
            timeout=timeout,
            proxies=mapped_proxy or {},
        )
        response.raise_for_status()
        return response.text

    if mapped:
        try:
            return _fetch_with(mapped)
        except requests.RequestException:
            return _fetch_with({})
    return _fetch_with({})


def _build_queries(instruction: str) -> list[tuple[str, str]]:
    lowered = instruction.lower()
    year = "2026" if "2026" in lowered else ""
    year_hint = f" ({year})" if year else ""
    queries = [
        ("deep learning OR transformer OR diffusion OR representation learning", f"latest_deep_learning{year_hint}"),
    ]
    if any(
        token in lowered
        for token in (
            "\u4ea4\u6613",
            "\u56e0\u5b50",
            "\u91cf\u5316",
            "\u91d1\u878d",
            "factor",
            "finance",
            "quant",
            "trading",
        )
    ):
        queries.append(
            (
                "trading OR finance OR factor investing OR market microstructure OR order flow",
                f"trading_factors{year_hint}",
            )
        )
    return queries


def query_arxiv(workspace: Path, query: str, label: str, *, max_results: int = 5) -> list[PaperHit]:
    params = {
        "search_query": f"all:({query})",
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    xml_text = _request_text(url, workspace)
    root = ET.fromstring(xml_text)
    papers: list[PaperHit] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        title = " ".join((entry.findtext("atom:title", default="", namespaces=_ATOM_NS) or "").split())
        summary = " ".join((entry.findtext("atom:summary", default="", namespaces=_ATOM_NS) or "").split())
        published = (entry.findtext("atom:published", default="", namespaces=_ATOM_NS) or "")[:10]
        link = entry.findtext("atom:id", default="", namespaces=_ATOM_NS) or ""
        if not title:
            continue
        papers.append(
            PaperHit(
                title=title,
                published=published,
                summary=summary[:1200],
                url=link,
                query_label=label,
            )
        )
    return papers


_FACTOR_RULES: list[tuple[str, str, tuple[str, ...], str]] = [
    ("news_sentiment_alpha", "text_sentiment", ("llm", "language model", "text", "news", "sentiment", "multimodal"), "Use model-scored news, filing, or social text embeddings as a cross-sectional alpha input."),
    ("microstructure_liquidity", "market_microstructure", ("order book", "liquidity", "spread", "depth", "microstructure"), "Track spread, depth imbalance, and queue pressure to estimate short-horizon edge and slippage."),
    ("order_flow_pressure", "flow", ("order flow", "volume", "flow", "imbalance"), "Build intraday pressure factors from buy/sell imbalance, aggressive flow, and volume surprise."),
    ("momentum_regime", "trend", ("momentum", "trend", "continuation"), "Separate slow momentum from crowded short-term continuation and regime-switch the lookback."),
    ("mean_reversion", "reversal", ("reversal", "mean reversion", "reversion"), "Use residual or overreaction signals to capture post-shock snapback behavior."),
    ("volatility_state", "risk_state", ("volatility", "variance", "uncertainty"), "Model realized/implied volatility regime as a gating factor for position sizing and signal activation."),
    ("correlation_dispersion", "cross_asset", ("correlation", "dispersion", "cross-asset"), "Use changing cross-asset or sector correlation structure as a factor and as a portfolio concentration guard."),
    ("funding_basis_spread", "carry", ("funding", "perpetual", "basis"), "Monitor perp funding and spot-futures basis as a carry/mean-reversion factor in simulation only."),
    ("onchain_activity", "alt_data", ("on-chain", "wallet", "address", "blockchain"), "Use wallet flow, active addresses, and realized on-chain behavior as a supplemental crypto factor."),
    ("representation_regime", "latent_representation", ("representation", "embedding", "latent", "regime"), "Cluster learned latent states and use them to gate which factors should be active."),
]


def extract_factor_hypotheses(papers: list[PaperHit]) -> list[FactorHypothesis]:
    lowered_corpus = [
        (paper, f"{paper.title}\n{paper.summary}".lower())
        for paper in papers
    ]
    factors: list[FactorHypothesis] = []
    for name, family, keywords, hint in _FACTOR_RULES:
        evidence = [paper.title for paper, blob in lowered_corpus if any(keyword in blob for keyword in keywords)]
        if not evidence:
            continue
        rationale = (
            f"Matched {family} keywords in {len(evidence)} recent papers, so this factor family is worth a read-only research pass."
        )
        factors.append(
            FactorHypothesis(
                name=name,
                signal_family=family,
                rationale=rationale,
                implementation_hint=hint,
                evidence_titles=evidence[:4],
            )
        )
    if factors:
        return factors
    generic_titles = [paper.title for paper in papers[:3]]
    return [
        FactorHypothesis(
            name="representation_regime",
            signal_family="latent_representation",
            rationale="Recent deep-learning papers imply regime-aware learned features are still a strong default direction.",
            implementation_hint="Start with learned embeddings plus a simple regime classifier before wiring more fragile alpha logic.",
            evidence_titles=generic_titles,
        ),
        FactorHypothesis(
            name="cross_sectional_residual",
            signal_family="residual_alpha",
            rationale="When paper coverage is broad but not finance-specific, a residualized cross-sectional factor is a robust first candidate.",
            implementation_hint="Build sector- or beta-neutral residual returns, then let learned features rank the residual basket.",
            evidence_titles=generic_titles,
        ),
    ]


def run_public_research_scout(
    workspace: Path | str,
    instruction: str,
    *,
    emit: Callable[[str], None] | None = None,
) -> ResearchScoutResult:
    workspace = Path(workspace).resolve()
    _emit(emit, "[离线研究] 识别到论文/因子任务，开始走公开来源研究流程。")
    papers: list[PaperHit] = []
    notes: list[str] = []
    for query, label in _build_queries(instruction):
        try:
            found = query_arxiv(workspace, query, label, max_results=5)
            papers.extend(found)
            _emit(emit, f"[离线研究] 已抓取 {len(found)} 篇 arXiv 条目: {label}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{label}: {exc!s}")
            _emit(emit, f"[离线研究] arXiv 检索失败: {label} -> {exc!s}")
    deduped: list[PaperHit] = []
    seen: set[str] = set()
    for paper in papers:
        if paper.url in seen:
            continue
        seen.add(paper.url)
        deduped.append(paper)
    factors = extract_factor_hypotheses(deduped)

    out_dir = workspace / ".claw" / "research"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "latest_research_factor_report"
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"

    markdown_lines = [
        "# Research Scout Report",
        "",
        f"- ts_utc: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"- instruction: {instruction}",
        f"- paper_count: {len(deduped)}",
        f"- factor_count: {len(factors)}",
    ]
    if notes:
        markdown_lines.extend(["", "## Notes", ""] + [f"- {note}" for note in notes])
    markdown_lines.extend(["", "## Recent Papers", ""])
    if deduped:
        for paper in deduped:
            markdown_lines.extend(
                [
                    f"### {paper.title}",
                    f"- published: {paper.published or 'unknown'}",
                    f"- query: {paper.query_label}",
                    f"- url: {paper.url}",
                    f"- summary: {paper.summary}",
                    "",
                ]
            )
    else:
        markdown_lines.append("- No papers fetched from public sources in this run.")

    markdown_lines.extend(["", "## Factor Hypotheses", ""])
    for factor in factors:
        markdown_lines.extend(
            [
                f"### {factor.name}",
                f"- family: {factor.signal_family}",
                f"- rationale: {factor.rationale}",
                f"- implementation_hint: {factor.implementation_hint}",
                f"- evidence: {', '.join(factor.evidence_titles) if factor.evidence_titles else 'n/a'}",
                "",
            ]
        )
    markdown_lines.extend(
        [
            "## Next Steps",
            "",
            "- Keep this report read-only and simulation-first while cloud quota or local model availability is degraded.",
            "- Promote only the factor families that have both paper support and a clean backtest design.",
            "- If you want a deeper literature review, rerun once the cloud/local model path is healthy and ask for a structured synthesis.",
            "",
        ]
    )
    md_path.write_text("\n".join(markdown_lines), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "instruction": instruction,
                "papers": [asdict(paper) for paper in deduped],
                "factors": [asdict(factor) for factor in factors],
                "notes": notes,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _emit(emit, f"[离线研究] 已写入研究摘要: {md_path.name}")
    return ResearchScoutResult(
        markdown_path=md_path,
        json_path=json_path,
        papers=deduped,
        factors=factors,
        note="; ".join(notes) if notes else "ok",
    )
