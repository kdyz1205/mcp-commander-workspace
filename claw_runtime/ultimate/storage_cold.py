"""
Dimension 1 — Cold storage: local zip + optional Telegram document to *your* chat (Saved Messages).

No automated mass registration of third-party accounts.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def compress_paths(workspace: Path, relative_paths: Iterable[str], out_name: str = "cold_archive.zip") -> Path:
    workspace = Path(workspace).resolve()
    out = workspace / ".claw" / "cold_storage"
    out.mkdir(parents=True, exist_ok=True)
    zpath = out / out_name
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in relative_paths:
            p = (workspace / rel).resolve()
            if p.is_file() and str(p).startswith(str(workspace)):
                zf.write(p, arcname=rel.replace("\\", "/"))
    meta = out / "last_cold_manifest.json"
    meta.write_text(
        json.dumps({"zip": str(zpath), "members": list(relative_paths)}, indent=2),
        encoding="utf-8",
    )
    return zpath


def telegram_send_document_if_configured(zip_path: Path) -> tuple[bool, str]:
    """
    sendDocument to chat_id using TG_BOT_TOKEN from env (same bot as tg_dev_claw).
    For Saved Messages use your numeric user id as chat_id.
    """
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("TG_COLD_STORAGE_CHAT_ID", "").strip()
    if not token or not chat:
        return False, "Set TG_BOT_TOKEN and TG_COLD_STORAGE_CHAT_ID (e.g. your user id for Saved Messages)."
    if not zip_path.is_file():
        return False, f"Missing file: {zip_path}"

    file_bytes = zip_path.read_bytes()
    boundary = b"----------clawFormBoundary"
    crlf = b"\r\n"
    parts: list[bytes] = [
        b"--" + boundary + crlf,
        b'Content-Disposition: form-data; name="chat_id"' + crlf + crlf,
        chat.encode() + crlf,
        b"--" + boundary + crlf,
        (
            f'Content-Disposition: form-data; name="document"; filename="{zip_path.name}"'.encode()
            + crlf
            + b"Content-Type: application/zip"
            + crlf
            + crlf
        ),
        file_bytes + crlf,
        b"--" + boundary + b"--" + crlf,
    ]
    body = b"".join(parts)
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    req = Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        j = json.loads(raw)
        if j.get("ok"):
            return True, "Uploaded to Telegram."
        return False, raw[:500]
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        return False, str(e)
