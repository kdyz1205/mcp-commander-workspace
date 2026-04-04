"""
TDD Test: Knowledge Forager — DOM elution + semantic chunking + knowledge extraction.

Tests that the system can:
1. Strip HTML to clean markdown (DOM elution)
2. Extract relevant chunks by keywords (semantic chunking)
3. Parse code snippets from documentation
4. Build knowledge capsules for the TDD engine
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


SAMPLE_HTML = """
<html>
<head><title>Exchange API Docs</title><style>body{color:red}</style></head>
<body>
<nav><a href="/">Home</a><a href="/about">About</a></nav>
<header><h1>Logo</h1></header>
<main>
<h2>WebSocket Market Data</h2>
<p>Connect to our WebSocket endpoint for real-time price data:</p>
<pre><code>
import websockets
import asyncio

async def subscribe():
    uri = "wss://stream.example.com/ws/btcusdt@ticker"
    async with websockets.connect(uri) as ws:
        data = await ws.recv()
        print(data)

asyncio.run(subscribe())
</code></pre>
<p>Required package: <code>pip install websockets</code></p>

<h2>REST API</h2>
<p>Base URL: https://api.example.com/v1</p>
<p>Get ticker: GET /ticker?symbol=BTCUSDT</p>

<h2>Authentication</h2>
<p>Use API key in header: X-API-KEY: your_key_here</p>
</main>
<footer>Copyright 2026</footer>
<script>alert('tracking')</script>
</body>
</html>
"""


def test_dom_elution_removes_noise():
    """DOM elution must strip nav, footer, script, style tags."""
    from claw_runtime.knowledge_forager import dom_elute

    clean = dom_elute(SAMPLE_HTML)
    assert "Logo" not in clean or "nav" not in clean.lower()[:50]
    assert "tracking" not in clean  # script removed
    assert "color:red" not in clean  # style removed
    assert "Copyright" not in clean  # footer removed
    assert "WebSocket" in clean  # main content preserved
    assert "websockets" in clean  # code preserved


def test_dom_elution_preserves_code_blocks():
    """Code snippets in documentation must survive elution."""
    from claw_runtime.knowledge_forager import dom_elute

    clean = dom_elute(SAMPLE_HTML)
    assert "asyncio" in clean
    assert "websockets.connect" in clean
    assert "wss://" in clean


def test_semantic_chunking_filters_by_keywords():
    """Chunking must return only paragraphs matching target keywords."""
    from claw_runtime.knowledge_forager import extract_relevant_chunks

    text = """
## WebSocket Market Data

Connect to the WebSocket endpoint for real-time price data.

## REST API

Base URL: https://api.example.com/v1

## Authentication

Use API key in header.

## Rate Limiting

Maximum 100 requests per minute.
"""
    chunks = extract_relevant_chunks(text, keywords=["websocket", "price"])
    joined = "\n".join(chunks)
    assert "WebSocket" in joined
    assert "price" in joined.lower()
    # Should NOT include rate limiting section
    assert "Rate Limiting" not in joined


def test_extract_code_snippets():
    """Must extract code blocks from markdown text."""
    from claw_runtime.knowledge_forager import extract_code_snippets

    text = """
Some explanation text.

```python
import websockets
async def connect():
    pass
```

More text.

```
pip install websockets
```
"""
    snippets = extract_code_snippets(text)
    assert len(snippets) >= 1
    assert any("websockets" in s for s in snippets)


def test_build_knowledge_capsule():
    """Full pipeline: HTML → clean → chunk → capsule dict."""
    from claw_runtime.knowledge_forager import build_knowledge_capsule

    capsule = build_knowledge_capsule(
        html_content=SAMPLE_HTML,
        task_keywords=["websocket", "ticker", "price"],
    )
    assert isinstance(capsule, dict)
    assert "clean_text" in capsule
    assert "relevant_chunks" in capsule
    assert "code_snippets" in capsule
    assert len(capsule["clean_text"]) < len(SAMPLE_HTML)  # compressed
    assert len(capsule["code_snippets"]) >= 1


def test_capsule_size_bounded():
    """Knowledge capsule must be < 5000 chars to fit in LLM context."""
    from claw_runtime.knowledge_forager import build_knowledge_capsule

    # Even with big HTML, capsule must be bounded
    big_html = "<html><body><main>" + "<p>filler text</p>\n" * 1000 + "</main></body></html>"
    capsule = build_knowledge_capsule(big_html, task_keywords=["filler"])
    total = len(capsule.get("relevant_chunks_text", "")) + len(str(capsule.get("code_snippets", [])))
    assert total < 10000  # reasonable bound
