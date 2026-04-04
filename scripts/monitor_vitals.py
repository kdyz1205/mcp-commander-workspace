"""
DevClaw Vitals Monitor — Real-time evolution dashboard.

Refreshes every 30s. Shows TTL, current task, evolution progress.
"""
import json
import os
import sys
import time
from pathlib import Path

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WS)

# ANSI
C = "\033[96m"; G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"
M = "\033[95m"; W = "\033[97m"; D = "\033[2m"; B = "\033[1m"; X = "\033[0m"


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default or {}


def get_ttl():
    d = read_json(".auth/balance.json", {"balance": 0, "bmr": 1})
    bal = float(d.get("balance", 0))
    bmr = max(float(d.get("bmr", 1)), 0.1)
    return bal / bmr, bal, bmr


def get_profile():
    return read_json(".claw/intelligence_profile.json", {})


def get_backlog_stats():
    try:
        content = Path("EVOLUTION_BACKLOG.md").read_text(encoding="utf-8")
        done = content.count("[DONE]") + content.count("[x]")
        pending = content.count("- [RESEARCH]") + content.count("- [PROFIT]") + content.count("- [ ]")
        return done, pending
    except Exception:
        return 0, 0


def get_git_log(n=3):
    import subprocess
    try:
        r = subprocess.run(["git", "log", f"--oneline", f"-{n}"],
                           capture_output=True, text=True, timeout=5, encoding="utf-8")
        return r.stdout.strip()
    except Exception:
        return "git unavailable"


def get_consciousness():
    try:
        lines = Path(".claw/consciousness_log.jsonl").read_text(encoding="utf-8").splitlines()
        if lines:
            last = json.loads(lines[-1])
            return last.get("type", "?"), last.get("content", "")[:100]
    except Exception:
        pass
    return "idle", ""


def get_cursorrules_count():
    try:
        return sum(1 for l in Path(".cursorrules").read_text(encoding="utf-8").splitlines()
                   if l.strip().startswith("- RULE") or "Auto-evolved" in l)
    except Exception:
        return 0


def render():
    os.system("")  # Enable ANSI on Windows
    os.system("cls" if os.name == "nt" else "clear")

    ttl, bal, bmr = get_ttl()
    profile = get_profile()
    done, pending = get_backlog_stats()
    git = get_git_log()
    cons_type, cons_content = get_consciousness()
    rule_count = get_cursorrules_count()

    # TTL color
    tc = G if ttl > 30 else Y if ttl > 7 else R
    status = "HEALTHY" if ttl > 30 else "HUNGRY" if ttl > 7 else "CRITICAL"

    iq = profile.get("overall_iq", "?")
    gen = profile.get("generation", "?")
    actions = profile.get("total_actions", "?")
    rules = len(profile.get("learned_rules", []))

    print(f"""
{C}╔══════════════════════════════════════════════════════════╗{X}
{C}║{X} {B}{W}        DEVCLAW VITALS MONITOR{X}              {C}║{X}
{C}╠══════════════════════════════════════════════════════════╣{X}
{C}║{X}                                                          {C}║{X}
{C}║{X}  {B}TTL:{X}  {tc}{ttl:>6.1f} days{X}  |  {B}STATUS:{X} {tc}{status}{X}              {C}║{X}
{C}║{X}  {B}BAL:{X}  ${bal:>7.2f}   |  {B}BURN:{X}  ${bmr:.2f}/day              {C}║{X}
{C}║{X}                                                          {C}║{X}
{C}╠══════════════════════════════════════════════════════════╣{X}
{C}║{X}  {M}{B}INTELLIGENCE{X}                                         {C}║{X}
{C}║{X}  IQ: {G}{iq}{X}  |  Gen: {Y}{gen}{X}  |  Actions: {W}{actions}{X}          {C}║{X}
{C}║{X}  Learned Rules: {G}{rules}{X}  |  .cursorrules: {Y}{rule_count}{X} entries    {C}║{X}
{C}║{X}                                                          {C}║{X}
{C}╠══════════════════════════════════════════════════════════╣{X}
{C}║{X}  {M}{B}BACKLOG{X}                                              {C}║{X}
{C}║{X}  Done: {G}{done}{X}  |  Pending: {Y}{pending}{X}                          {C}║{X}
{C}║{X}                                                          {C}║{X}
{C}╠══════════════════════════════════════════════════════════╣{X}
{C}║{X}  {M}{B}CONSCIOUSNESS{X}  [{cons_type[:20]}]                      {C}║{X}
{C}║{X}  {D}{cons_content[:55]}{X}  {C}║{X}
{C}║{X}                                                          {C}║{X}
{C}╠══════════════════════════════════════════════════════════╣{X}
{C}║{X}  {M}{B}RECENT COMMITS{X}                                       {C}║{X}""")

    for line in git.split("\n")[:3]:
        print(f"{C}║{X}  {D}{line[:56]}{X}  {C}║{X}")

    print(f"""{C}║{X}                                                          {C}║{X}
{C}╠══════════════════════════════════════════════════════════╣{X}
{C}║{X}  {D}Updated: {time.strftime('%H:%M:%S')}  |  Refresh: 30s  |  Ctrl+C to exit{X}  {C}║{X}
{C}╚══════════════════════════════════════════════════════════╝{X}
""")


def main():
    try:
        sys.stdin = open(os.devnull, "r")
    except Exception:
        pass

    while True:
        try:
            render()
        except Exception as e:
            print(f"Render error: {e}")
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
