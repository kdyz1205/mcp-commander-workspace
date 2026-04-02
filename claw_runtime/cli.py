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


def cmd_evolve_draft(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).resolve()
    from claw_runtime.nightly_evolution import materialize_draft_skill

    out = materialize_draft_skill(ws)
    if out:
        print(out)
        return 0
    print("No failure log entries.", file=sys.stderr)
    return 1


def cmd_ultimate_colab_bundle(args: argparse.Namespace) -> int:
    from claw_runtime.ultimate.colab_bundle import build_colab_job_bundle

    ws = Path(args.workspace).resolve()
    paths = [x.strip() for x in (args.paths or "").split(",") if x.strip()]
    z = build_colab_job_bundle(ws, relative_paths=paths, instruction=(args.instruction or "").strip())
    print(z)
    return 0


def cmd_ultimate_cold_zip(args: argparse.Namespace) -> int:
    from claw_runtime.ultimate.storage_cold import compress_paths

    ws = Path(args.workspace).resolve()
    paths = [x.strip() for x in (args.paths or "").split(",") if x.strip()]
    z = compress_paths(ws, paths)
    print(z)
    return 0


def cmd_ultimate_tg_upload(args: argparse.Namespace) -> int:
    from claw_runtime.ultimate.storage_cold import compress_paths, telegram_send_document_if_configured

    ws = Path(args.workspace).resolve()
    paths = [x.strip() for x in (args.paths or "").split(",") if x.strip()]
    z = compress_paths(ws, paths)
    ok, msg = telegram_send_document_if_configured(z)
    print(msg)
    return 0 if ok else 1


def cmd_ultimate_proxy_rotate(args: argparse.Namespace) -> int:
    import json as _json

    from claw_runtime.ultimate.proxy_env import rotate_proxy_index

    ws = Path(args.workspace).resolve()
    out = rotate_proxy_index(ws)
    print(_json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


def cmd_ultimate_mesh_demo(args: argparse.Namespace) -> int:
    import json as _json

    from claw_runtime.ultimate.subagent_mesh import run_subagent_mesh

    text = " ".join(args.text).strip() or "demo task"
    r = run_subagent_mesh(text)
    print(_json.dumps(r, indent=2, ensure_ascii=False))
    return 0


def cmd_ultimate_synthesize(args: argparse.Namespace) -> int:
    from claw_runtime.ultimate.skill_synthesis import synthesize_two_skills

    ws = Path(args.workspace).resolve()
    path, msg = synthesize_two_skills(ws, args.skill_a, args.skill_b, args.out_name)
    print(msg, path)
    return 0


def cmd_ultimate_self_heal_emit(args: argparse.Namespace) -> int:
    from claw_runtime.ultimate.self_heal import emit_rebuild_venv_scripts

    ws = Path(args.workspace).resolve()
    ps1, sh = emit_rebuild_venv_scripts(ws)
    print(ps1)
    print(sh)
    return 0


def cmd_ultimate_nomad_snapshot(args: argparse.Namespace) -> int:
    from claw_runtime.ultimate.nomad import write_nomad_snapshot

    ws = Path(args.workspace).resolve()
    p = write_nomad_snapshot(ws)
    print(p)
    return 0


def cmd_ultimate_nomad_register(args: argparse.Namespace) -> int:
    from claw_runtime.ultimate.nomad import register_nomad_handlers

    ws = Path(args.workspace).resolve()
    register_nomad_handlers(ws)
    print("Nomad handlers registered (signals + optional atexit).")
    return 0


def cmd_ultimate_treasury_probe(args: argparse.Namespace) -> int:
    from claw_runtime.ultimate.treasury import probe_eth_balance

    rpc = os.environ.get("ETH_RPC_URL", "").strip()
    addr = os.environ.get("ETH_TREASURY_ADDRESS", "").strip()
    if not rpc or not addr:
        print("Set ETH_RPC_URL and ETH_TREASURY_ADDRESS", file=sys.stderr)
        return 1
    ok, msg, wei = probe_eth_balance(rpc, addr)
    if wei is not None:
        print(f"{ok} {msg} wei={wei}")
    else:
        print(f"{ok} {msg}", file=sys.stderr)
    return 0 if ok else 2


def cmd_ultimate_status(args: argparse.Namespace) -> int:
    keys = [
        "NOMAD_REGISTER_HANDLERS",
        "NOMAD_GIT_PUSH",
        "NOMAD_ATEXIT_SNAPSHOT",
        "PROXY_LIST_FILE",
        "VPN_SWITCH_CMD",
        "TG_COLD_STORAGE_CHAT_ID",
        "ETH_RPC_URL",
        "ETH_TREASURY_ADDRESS",
    ]
    for k in keys:
        v = os.environ.get(k, "")
        print(f"{k}={'set' if v else '(unset)'}")
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

    ev = sub.add_parser("evolve-draft", help="Materialize draft SKILL from evolution failure log")
    ev.set_defaults(func=cmd_evolve_draft)

    mu = sub.add_parser("multi-agent", help="Run phased planner/builder/auditor pipeline")
    mu.add_argument("instruction", nargs="+")
    mu.set_defaults(func=cmd_multi)

    uc = sub.add_parser("ultimate-colab-bundle", help="Zip job + files for manual Colab upload")
    uc.add_argument("--paths", default="", help="Comma-separated workspace-relative files")
    uc.add_argument("--instruction", default="", help="Job instruction text")
    uc.set_defaults(func=cmd_ultimate_colab_bundle)

    uz = sub.add_parser("ultimate-cold-zip", help="Zip paths into .claw/cold_storage/")
    uz.add_argument("--paths", required=True, help="Comma-separated workspace-relative files")
    uz.set_defaults(func=cmd_ultimate_cold_zip)

    ut = sub.add_parser("ultimate-tg-upload", help="Cold-zip paths and sendDocument to TG chat")
    ut.add_argument("--paths", required=True, help="Comma-separated workspace-relative files")
    ut.set_defaults(func=cmd_ultimate_tg_upload)

    up = sub.add_parser("ultimate-proxy-rotate", help="Rotate to next proxy from PROXY_LIST_FILE")
    up.set_defaults(func=cmd_ultimate_proxy_rotate)

    um = sub.add_parser("ultimate-mesh-demo", help="Run local subagent mesh demo")
    um.add_argument("text", nargs="*", help="Task text")
    um.set_defaults(func=cmd_ultimate_mesh_demo)

    us = sub.add_parser("ultimate-synthesize", help="Merge two skills into skills/synth_*/")
    us.add_argument("--a", dest="skill_a", required=True)
    us.add_argument("--b", dest="skill_b", required=True)
    us.add_argument("--out-name", dest="out_name", default=None)
    us.set_defaults(func=cmd_ultimate_synthesize)

    uh = sub.add_parser("ultimate-self-heal-emit", help="Write venv rebuild scripts under scripts/")
    uh.set_defaults(func=cmd_ultimate_self_heal_emit)

    un = sub.add_parser("ultimate-nomad-snapshot", help="Write .claw/nomad_snapshot.json once")
    un.set_defaults(func=cmd_ultimate_nomad_snapshot)

    ur = sub.add_parser("ultimate-nomad-register", help="Register shutdown handlers for nomad")
    ur.set_defaults(func=cmd_ultimate_nomad_register)

    utp = sub.add_parser("ultimate-treasury-probe", help="Read-only ETH balance via JSON-RPC")
    utp.set_defaults(func=cmd_ultimate_treasury_probe)

    ust = sub.add_parser("ultimate-status", help="Show ultimate-related env toggles")
    ust.set_defaults(func=cmd_ultimate_status)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
