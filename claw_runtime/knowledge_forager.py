"""
Knowledge Forager — DOM elution + semantic chunking + knowledge extraction.

DevClaw's "visual and digestive system". Turns raw HTML documentation
into clean, dense knowledge capsules that fit in LLM context.

Pipeline:
1. dom_elute: Strip HTML noise (nav, footer, script, style) → clean markdown
2. extract_relevant_chunks: Filter by keywords → only relevant paragraphs
3. extract_code_snippets: Pull code blocks from markdown
4. build_knowledge_capsule: Full pipeline → structured dict

The LLM NEVER sees raw HTML. Only pre-digested knowledge.
"""
from __future__ import annotations

import re
from typing import Any


def dom_elute(raw_html: str) -> str:
    """
    Strip HTML to clean markdown. Remove all noise.

    Kills: script, style, nav, footer, aside, header, svg, form
    Keeps: main/article content, code blocks, headings, paragraphs
    """
    try:
        from bs4 import BeautifulSoup
        import html2text
    except ImportError:
        # Fallback: regex-based stripping
        return _regex_elute(raw_html)

    soup = BeautifulSoup(raw_html, "html.parser")

    # Kill noise tags
    for tag in soup(["script", "style", "nav", "footer", "aside",
                     "header", "svg", "form", "iframe", "noscript"]):
        tag.decompose()

    # Find main content area
    main = soup.find("main") or soup.find("article") or soup.find("body") or soup

    # Convert to markdown
    converter = html2text.HTML2Text()
    converter.ignore_links = False  # keep links for API URLs
    converter.ignore_images = True
    converter.ignore_emphasis = False
    converter.body_width = 0  # no line wrapping

    clean = converter.handle(str(main))

    # Post-process: remove excessive blank lines
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def _regex_elute(raw_html: str) -> str:
    """Fallback: regex-based HTML stripping when bs4 not available."""
    # Remove script/style blocks
    text = re.sub(r"<(script|style|nav|footer|aside|header)[^>]*>[\s\S]*?</\1>",
                  "", raw_html, flags=re.IGNORECASE)
    # Remove all tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_relevant_chunks(
    text: str,
    keywords: list[str],
    max_chunks: int = 10,
) -> list[str]:
    """
    Extract paragraphs/sections that contain target keywords.

    Splits by double-newline, keeps only chunks with keyword matches.
    Returns at most max_chunks results.
    """
    if not keywords:
        return [text[:3000]]

    # Split into paragraphs/sections
    sections = re.split(r"\n\n+", text)

    # Group consecutive lines into logical chunks
    chunks: list[str] = []
    current: list[str] = []
    for section in sections:
        if section.strip():
            current.append(section.strip())
            # If this section is a header, start new chunk
            if section.strip().startswith("#"):
                if len(current) > 1:
                    chunks.append("\n\n".join(current[:-1]))
                current = [section.strip()]
        else:
            if current:
                chunks.append("\n\n".join(current))
                current = []
    if current:
        chunks.append("\n\n".join(current))

    # Filter by keywords
    lower_keywords = [k.lower() for k in keywords]
    relevant = []
    for chunk in chunks:
        if any(kw in chunk.lower() for kw in lower_keywords):
            relevant.append(chunk)

    return relevant[:max_chunks]


def extract_code_snippets(text: str) -> list[str]:
    """
    Extract code blocks from markdown text.

    Finds ```...``` blocks and indented code blocks.
    """
    snippets: list[str] = []

    # Fenced code blocks (```...```)
    fenced = re.findall(r"```[\w]*\n([\s\S]*?)```", text)
    snippets.extend(s.strip() for s in fenced if s.strip())

    # If no fenced blocks, try indented blocks (4+ spaces)
    if not snippets:
        lines = text.split("\n")
        current_block: list[str] = []
        for line in lines:
            if line.startswith("    ") or line.startswith("\t"):
                current_block.append(line.strip())
            else:
                if len(current_block) >= 2:
                    snippets.append("\n".join(current_block))
                current_block = []
        if len(current_block) >= 2:
            snippets.append("\n".join(current_block))

    return snippets


def build_knowledge_capsule(
    html_content: str,
    task_keywords: list[str],
    max_total_chars: int = 5000,
) -> dict[str, Any]:
    """
    Full pipeline: HTML → clean → chunk → capsule.

    Returns a structured dict ready for LLM injection.
    """
    # Step 1: DOM elution
    clean = dom_elute(html_content)

    # Step 2: Semantic chunking
    chunks = extract_relevant_chunks(clean, task_keywords)
    chunks_text = "\n---\n".join(chunks)

    # Step 3: Code extraction
    snippets = extract_code_snippets(clean)

    # Step 4: Truncate to fit context
    if len(chunks_text) > max_total_chars:
        chunks_text = chunks_text[:max_total_chars]

    return {
        "clean_text": clean[:max_total_chars],
        "relevant_chunks": chunks,
        "relevant_chunks_text": chunks_text,
        "code_snippets": snippets,
        "compression_ratio": f"{len(clean)}/{len(html_content)} ({len(clean)/max(len(html_content),1)*100:.0f}%)",
    }


def fetch_and_digest(
    url: str,
    task_keywords: list[str],
    timeout: int = 30,
) -> dict[str, Any]:
    """
    Fetch a URL and build a knowledge capsule.

    This is the full "eat documentation" pipeline:
    HTTP GET → DOM elute → chunk → capsule
    """
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DevClaw-KnowledgeForager/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(500_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return {"error": str(e), "clean_text": "", "code_snippets": []}

    return build_knowledge_capsule(html, task_keywords)
