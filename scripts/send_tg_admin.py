"""Send a UTF-8 safe admin notification to configured Telegram chats."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _load_dotenv(env_path: Path) -> None:
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _admin_ids() -> list[str]:
    raw = os.environ.get("TG_ADMIN_CHAT_IDS", "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def send_message(text: str) -> int:
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chats = _admin_ids()
    if not token or not chats:
        print("Missing TG_BOT_TOKEN or TG_ADMIN_CHAT_IDS", file=sys.stderr)
        return 1

    payload_template = {
        "text": text,
        "disable_web_page_preview": True,
    }
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chats:
        payload = dict(payload_template)
        payload["chat_id"] = chat_id
        req = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    print(f"Telegram send failed for {chat_id}: HTTP {resp.status}", file=sys.stderr)
                    return 2
        except (HTTPError, URLError) as exc:
            print(f"Telegram send failed for {chat_id}: {exc}", file=sys.stderr)
            return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    argv = argv or sys.argv[1:]
    if not argv:
        print("Usage: py scripts/send_tg_admin.py <message>", file=sys.stderr)
        return 1
    repo_root = Path(__file__).resolve().parents[1]
    _load_dotenv(repo_root / ".env")
    message = " ".join(argv).strip()
    return send_message(message)


if __name__ == "__main__":
    raise SystemExit(main())
