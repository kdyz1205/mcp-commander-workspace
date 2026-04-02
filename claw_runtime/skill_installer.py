"""Install ClawHub-compatible skill folders from URL (zip) or local path."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from claw_runtime.config_load import load_claw_config
from claw_runtime.safety_scan import scan_path, should_block_install

_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _find_skill_root(extracted: Path) -> Path | None:
    for skill_file in ("SKILL.md", "skill.md"):
        for p in extracted.rglob(skill_file):
            if p.is_file():
                return p.parent
    return None


def _write_origin(workspace: Path, name: str, source: str) -> None:
    hub = workspace / ".clawhub"
    hub.mkdir(parents=True, exist_ok=True)
    origin = {"name": name, "source": source, "installed_at": datetime.now(timezone.utc).isoformat()}
    (hub / "origin.json").write_text(json.dumps(origin, indent=2, ensure_ascii=False), encoding="utf-8")
    lock_p = hub / "lock.json"
    lock: dict = {"installed": []}
    if lock_p.is_file():
        try:
            lock = json.loads(lock_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    if not isinstance(lock.get("installed"), list):
        lock["installed"] = []
    lock["installed"] = [x for x in lock["installed"] if isinstance(x, dict) and x.get("name") != name]
    lock["installed"].append(origin)
    lock_p.write_text(json.dumps(lock, indent=2, ensure_ascii=False), encoding="utf-8")


def install_skill(
    workspace: Path,
    source: str,
    *,
    target_name: str | None = None,
    skip_safety: bool = False,
) -> str:
    cfg = load_claw_config(workspace)
    safety_cfg = cfg.get("safety") or {}
    block_critical = safety_cfg.get("blockCritical", True)
    block_high = safety_cfg.get("blockHigh", False)
    scan_on_install = safety_cfg.get("scanSkillsOnInstall", True)

    ws = workspace.resolve()
    dest_skills = ws / "skills"
    dest_skills.mkdir(parents=True, exist_ok=True)

    tmp_dir: Path | None = None
    try:
        if source.startswith("http://") or source.startswith("https://"):
            req = Request(source, headers={"User-Agent": "DevClaw-skill-install/1.0"}, method="GET")
            with urlopen(req, timeout=120) as resp:
                data = resp.read()
            zhash = hashlib.sha256(data).hexdigest()[:16]
            tmp_dir = Path(tempfile.mkdtemp(prefix="claw-skill-"))
            zip_path = tmp_dir / "bundle.zip"
            zip_path.write_bytes(data)
            extracted = tmp_dir / "extracted"
            extracted.mkdir()
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extracted)
            skill_root = _find_skill_root(extracted)
            if skill_root is None:
                return "安装失败：压缩包内未找到 SKILL.md"
        else:
            zhash = "local"
            local = Path(source).expanduser().resolve()
            if not local.exists():
                return f"路径不存在: {local}"
            if local.is_file():
                local = local.parent
            skill_root = _find_skill_root(local)
            if skill_root is None and (local / "SKILL.md").is_file():
                skill_root = local
            if skill_root is None and (local / "skill.md").is_file():
                skill_root = local
            if skill_root is None:
                return "本地目录中未找到 SKILL.md"

        skill_md = skill_root / "SKILL.md"
        if not skill_md.is_file():
            skill_md = skill_root / "skill.md"
        if not skill_md.is_file():
            return "缺少 SKILL.md"

        if scan_on_install and not skip_safety:
            findings = scan_path(skill_md)
            blocked, reason = should_block_install(findings, block_critical=block_critical, block_high=block_high)
            if blocked:
                return f"安全扫描拒绝安装: {reason}"

        name = target_name
        if not name:
            body = skill_md.read_text(encoding="utf-8", errors="replace")
            if body.startswith("---"):
                end = body.find("\n---", 3)
                if end != -1:
                    for line in body[3:end].splitlines():
                        if line.lower().startswith("name:"):
                            name = line.split(":", 1)[1].strip().strip("\"'")
                            break
        if not name:
            name = skill_root.name
        name = name.strip().lower().replace(" ", "-")
        if not name or not _SKILL_NAME.match(name):
            return f"无效技能名（需小写+连字符）: {name!r}"

        dest = dest_skills / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_root, dest, dirs_exist_ok=True)

        src_label = source if len(source) < 200 else source[:200] + "…"
        _write_origin(ws, name, f"{src_label}#{zhash}")

        return f"已安装技能 `{name}` → skills/{name}/ （下一轮 DevClaw 热重载可见）"
    except Exception as e:  # noqa: BLE001
        return f"安装异常: {e!s}"
    finally:
        if tmp_dir and tmp_dir.is_dir():
            shutil.rmtree(tmp_dir, ignore_errors=True)
