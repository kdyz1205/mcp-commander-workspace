"""ClawHub-style CLI (Python): skills install, safety scan, plugins, multi-agent."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def cmd_skills_install(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).resolve()
    os.environ.setdefault("DEVCLAW_WORKSPACE", str(ws))
    from claw_runtime.skill_installer import install_skill

    msg = install_skill(
        ws,
        args.source,
        target_name=args.name,
        skip_safety=args.force,
    )
    print(msg)
    return 0 if msg.startswith("已安装") else 1


def cmd_safety_scan(args: argparse.Namespace) -> int:
    from claw_runtime.safety_scan import Severity, scan_path

    p = Path(args.target)
    if not p.is_file():
        print("Target must be a file.", file=sys.stderr)
        return 2
    findings = scan_path(p)
    if not findings:
        print("No findings.")
        return 0
    for f in findings:
        print(f"[{f.severity.value}] {f.rule_id}: {f.message}")
        if f.line_hint:
            print(f"  …{f.line_hint}…")
    return 1 if any(f.severity == Severity.CRITICAL for f in findings) else 0


def cmd_plugins_list(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).resolve()
    from claw_runtime.plugin_loader import plugin_skill_dirs

    for p in plugin_skill_dirs(ws):
        print(p)
    return 0


def cmd_multi(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).resolve()
    os.environ["DEVCLAW_WORKSPACE"] = str(ws)
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr)
        return 1
    from claw_runtime.multi_agent import run_phased_pipeline

    run_phased_pipeline(ws, " ".join(args.instruction))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="claw",
        description="DevClaw / OpenClaw-style Python CLI",
        epilog="例: py -m claw_runtime.cli --workspace . plugins-list",
    )
    p.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root（须写在子命令之前，默认当前目录）",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    si = sub.add_parser("skills-install", help="Install skill from URL (zip) or local folder")
    si.add_argument("source", help="https://…/archive.zip or local path")
    si.add_argument("--name", default=None, help="Override skill folder name")
    si.add_argument("--force", action="store_true", help="Skip safety scan (dangerous)")
    si.set_defaults(func=cmd_skills_install)

    ss = sub.add_parser("safety-scan", help="Scan a file for dangerous patterns")
    ss.add_argument("target", help="File path")
    ss.set_defaults(func=cmd_safety_scan)

    pl = sub.add_parser("plugins-list", help="List plugin skill directories")
    pl.set_defaults(func=cmd_plugins_list)

    mu = sub.add_parser("multi-agent", help="Run phased planner/builder/auditor pipeline")
    mu.add_argument("instruction", nargs="+")
    mu.set_defaults(func=cmd_multi)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
