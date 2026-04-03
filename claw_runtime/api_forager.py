"""
Zero-shot API discovery & usage — DevClaw reads API documentation and
self-constructs API calls for unknown services.

State stored in <workspace>/.claw/api_knowledge/
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# API doc discovery
# ---------------------------------------------------------------------------

_DOC_URL_PATTERNS: list[str] = [
    "{base}/openapi.json",
    "{base}/swagger.json",
    "{base}/api/v1/docs",
    "{base}/api/v1/openapi.json",
    "{base}/api/v2/openapi.json",
    "{base}/api-docs",
    "{base}/api-docs/swagger.json",
    "{base}/docs/api.json",
    "{base}/v1/swagger.json",
    "{base}/v2/swagger.json",
    "{base}/.well-known/openapi.json",
]


def _fetch_url(url: str, *, timeout: float = 15.0) -> str | None:
    """Attempt to fetch a URL; return body text or None on failure."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DevClaw-APIForager/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        pass
    return None


def discover_api_docs(service_name: str) -> dict[str, Any]:
    """Probe common API documentation URL patterns for a service.

    Tries multiple well-known paths and attempts to parse any discovered
    OpenAPI / Swagger JSON spec.

    Args:
        service_name: Base URL or service hostname (e.g. ``https://api.example.com``).

    Returns:
        Dict with ``base_url`` and ``endpoints`` list.  If nothing is found
        the endpoints list will be empty.
    """
    base = service_name.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"

    result: dict[str, Any] = {"base_url": base, "endpoints": [], "spec_url": None}

    for pattern in _DOC_URL_PATTERNS:
        url = pattern.format(base=base)
        body = _fetch_url(url, timeout=10.0)
        if body is None:
            continue

        try:
            spec = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue

        # Quick sniff: does it look like an OpenAPI/Swagger doc?
        if isinstance(spec, dict) and ("paths" in spec or "swagger" in spec or "openapi" in spec):
            result["spec_url"] = url
            result["endpoints"] = parse_openapi_spec(body)
            break

    return result


# ---------------------------------------------------------------------------
# OpenAPI / Swagger parsing
# ---------------------------------------------------------------------------

def parse_openapi_spec(spec_text: str) -> list[dict[str, Any]]:
    """Parse an OpenAPI 3.0 or Swagger 2.0 JSON spec into endpoint descriptions.

    Args:
        spec_text: Raw JSON string of the spec.

    Returns:
        List of dicts, each with ``path``, ``method``, ``params``,
        ``description``, and ``auth``.
    """
    try:
        spec = json.loads(spec_text)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(spec, dict):
        return []

    endpoints: list[dict[str, Any]] = []

    # Determine auth requirements at spec level
    global_auth = _extract_auth_info(spec)

    paths = spec.get("paths", {})
    if not isinstance(paths, dict):
        return []

    for path_str, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue

            params = _extract_parameters(operation, path_item)
            description = (
                operation.get("summary", "")
                or operation.get("description", "")
                or ""
            )[:500]

            endpoints.append({
                "path": path_str,
                "method": method.upper(),
                "params": params,
                "description": description,
                "auth": global_auth,
            })

    return endpoints


def _extract_auth_info(spec: dict[str, Any]) -> str:
    """Extract a short auth description from the spec."""
    # OpenAPI 3.x
    components = spec.get("components", {})
    if isinstance(components, dict):
        schemes = components.get("securitySchemes", {})
        if isinstance(schemes, dict) and schemes:
            first = next(iter(schemes.values()))
            if isinstance(first, dict):
                scheme_type = first.get("type", "unknown")
                scheme_in = first.get("in", "")
                name = first.get("name", "")
                if scheme_type == "apiKey":
                    return f"apiKey in {scheme_in} ({name})"
                if scheme_type == "http":
                    return f"http/{first.get('scheme', 'bearer')}"
                return scheme_type

    # Swagger 2.x
    sec_defs = spec.get("securityDefinitions", {})
    if isinstance(sec_defs, dict) and sec_defs:
        first = next(iter(sec_defs.values()))
        if isinstance(first, dict):
            return first.get("type", "unknown")

    return "none"


def _extract_parameters(
    operation: dict[str, Any],
    path_item: dict[str, Any],
) -> list[dict[str, str]]:
    """Extract parameter info from an operation + path-level params."""
    params: list[dict[str, str]] = []

    # Merge path-level and operation-level parameters
    raw_params: list[Any] = []
    if isinstance(path_item.get("parameters"), list):
        raw_params.extend(path_item["parameters"])
    if isinstance(operation.get("parameters"), list):
        raw_params.extend(operation["parameters"])

    for p in raw_params:
        if not isinstance(p, dict):
            continue
        params.append({
            "name": str(p.get("name", "")),
            "in": str(p.get("in", "")),
            "required": str(p.get("required", False)),
            "type": str(p.get("schema", {}).get("type", p.get("type", "string"))),
        })

    # OpenAPI 3.x request body
    req_body = operation.get("requestBody")
    if isinstance(req_body, dict):
        content = req_body.get("content", {})
        if isinstance(content, dict):
            for media_type, media_obj in content.items():
                if isinstance(media_obj, dict):
                    params.append({
                        "name": "__request_body__",
                        "in": "body",
                        "required": str(req_body.get("required", False)),
                        "type": media_type,
                    })
                    break

    return params


# ---------------------------------------------------------------------------
# Request construction & execution
# ---------------------------------------------------------------------------

def construct_api_call(endpoint_spec: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Build a complete HTTP request dict from an endpoint spec and user params.

    Args:
        endpoint_spec: One endpoint dict as returned by ``parse_openapi_spec``.
        params: User-supplied parameter values keyed by param name.  Special
            keys: ``__auth_token__``, ``__api_key__``, ``__base_url__``.

    Returns:
        Dict with ``url``, ``method``, ``headers``, ``body``, ``auth_type``.
    """
    base_url = params.pop("__base_url__", "").rstrip("/")
    auth_token = params.pop("__auth_token__", "")
    api_key = params.pop("__api_key__", "")

    path: str = endpoint_spec.get("path", "/")
    method: str = endpoint_spec.get("method", "GET").upper()
    auth_type: str = endpoint_spec.get("auth", "none")

    headers: dict[str, str] = {"User-Agent": "DevClaw-APIForager/1.0"}
    query_params: dict[str, str] = {}
    body: Any = None

    # Classify each spec param and fill from user values
    for p_spec in endpoint_spec.get("params", []):
        p_name = p_spec.get("name", "")
        p_in = p_spec.get("in", "")
        value = params.get(p_name)
        if value is None:
            continue

        if p_in == "path":
            path = path.replace(f"{{{p_name}}}", str(value))
        elif p_in == "query":
            query_params[p_name] = str(value)
        elif p_in == "header":
            headers[p_name] = str(value)
        elif p_in == "body":
            body = value

    # Build final URL
    url = f"{base_url}{path}"
    if query_params:
        url = f"{url}?{urllib.parse.urlencode(query_params)}"

    # Auth handling
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    elif api_key:
        if "apiKey" in auth_type and "header" in auth_type:
            # Try to extract header name from auth spec
            headers["X-API-Key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    # JSON body
    if body is not None and isinstance(body, (dict, list)):
        headers["Content-Type"] = "application/json"
        body = json.dumps(body, ensure_ascii=False)

    return {
        "url": url,
        "method": method,
        "headers": headers,
        "body": body,
        "auth_type": auth_type,
    }


def execute_api_call(call_spec: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """Execute a constructed HTTP request.

    Args:
        call_spec: Dict from ``construct_api_call`` with ``url``, ``method``,
            ``headers``, ``body``.
        timeout: Request timeout in seconds.

    Returns:
        Dict with ``status``, ``body``, ``headers``, ``elapsed_ms``.
    """
    url: str = call_spec.get("url", "")
    method: str = call_spec.get("method", "GET")
    headers: dict[str, str] = call_spec.get("headers", {})
    body: str | bytes | None = call_spec.get("body")

    data: bytes | None = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else body

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
            resp_body = resp.read().decode("utf-8", errors="replace")
            resp_headers = dict(resp.headers.items())
            return {
                "status": resp.status,
                "body": resp_body[:50_000],  # cap to avoid memory issues
                "headers": resp_headers,
                "elapsed_ms": elapsed_ms,
            }
    except urllib.error.HTTPError as e:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:10_000]
        except Exception:
            err_body = ""
        return {
            "status": e.code,
            "body": err_body,
            "headers": dict(e.headers.items()) if e.headers else {},
            "elapsed_ms": elapsed_ms,
        }
    except (urllib.error.URLError, OSError, ValueError) as e:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        return {
            "status": 0,
            "body": f"Connection error: {e}",
            "headers": {},
            "elapsed_ms": elapsed_ms,
        }


# ---------------------------------------------------------------------------
# Knowledge persistence
# ---------------------------------------------------------------------------

def _knowledge_dir(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "api_knowledge"


def _service_slug(url: str) -> str:
    """Derive a filesystem-safe slug from a URL."""
    parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
    host = parsed.hostname or "unknown"
    return host.replace(".", "_").replace(":", "_")[:80]


def learn_api(workspace: Path, service_url: str, purpose: str = "") -> Path:
    """Discover, parse, and save API knowledge for a service.

    Args:
        workspace: Project workspace root.
        service_url: Base URL of the API service.
        purpose: Free-text note about why this API is being learned.

    Returns:
        Path to the saved knowledge JSON file.
    """
    workspace = Path(workspace).resolve()
    discovery = discover_api_docs(service_url)

    slug = _service_slug(service_url)
    out_dir = _knowledge_dir(workspace)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.json"

    knowledge: dict[str, Any] = {
        "service_url": service_url,
        "purpose": purpose,
        "discovered_at": time.time(),
        "base_url": discovery.get("base_url", ""),
        "spec_url": discovery.get("spec_url"),
        "endpoints": discovery.get("endpoints", []),
        "endpoint_count": len(discovery.get("endpoints", [])),
    }

    try:
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(knowledge, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(out_path)
    except OSError:
        pass

    return out_path


def _load_known_apis(workspace: Path) -> list[dict[str, Any]]:
    """Load all saved API knowledge files."""
    kdir = _knowledge_dir(workspace)
    if not kdir.is_dir():
        return []
    apis: list[dict[str, Any]] = []
    try:
        for f in sorted(kdir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    apis.append(data)
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass
    return apis


def forage_for_data(
    workspace: Path,
    query: str,
    known_apis: list[str] | None = None,
) -> dict[str, Any]:
    """Search known APIs for relevant endpoints, construct and execute calls.

    A best-effort keyword-matching approach: scans endpoint descriptions for
    overlap with the query string.

    Args:
        workspace: Project workspace root.
        query: Natural-language description of what data is needed.
        known_apis: Optional list of service URLs to restrict search to.

    Returns:
        Dict with ``results`` (list of API responses) and ``sources``
        (list of endpoint paths used).
    """
    workspace = Path(workspace).resolve()
    apis = _load_known_apis(workspace)

    # Filter to requested APIs if specified
    if known_apis:
        slug_set = {_service_slug(u) for u in known_apis}
        apis = [a for a in apis if _service_slug(a.get("service_url", "")) in slug_set]

    query_lower = query.lower()
    query_words = set(query_lower.split())

    results: list[dict[str, Any]] = []
    sources: list[str] = []

    for api_info in apis:
        base_url = api_info.get("base_url", "")
        endpoints = api_info.get("endpoints", [])

        for ep in endpoints:
            if not isinstance(ep, dict):
                continue

            desc = (ep.get("description", "") + " " + ep.get("path", "")).lower()
            desc_words = set(desc.split())

            # Simple keyword overlap scoring
            overlap = len(query_words & desc_words)
            if overlap == 0:
                continue

            # Only try GET endpoints for data foraging
            if ep.get("method", "GET") != "GET":
                continue

            call_spec = construct_api_call(ep, {"__base_url__": base_url})
            response = execute_api_call(call_spec, timeout=15.0)

            if response.get("status") in (200, 201):
                # Try to parse JSON body
                body = response.get("body", "")
                try:
                    parsed = json.loads(body)
                    results.append({"endpoint": ep["path"], "data": parsed})
                except (json.JSONDecodeError, ValueError):
                    results.append({"endpoint": ep["path"], "data": body[:5000]})
                sources.append(f"{base_url}{ep['path']}")

            if len(results) >= 5:  # cap results to avoid runaway requests
                break

        if len(results) >= 5:
            break

    return {"results": results, "sources": sources}
