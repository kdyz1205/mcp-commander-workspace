import os
import sys
import subprocess

try:
    import pyautogui
except ImportError:
    print("Missing pyautogui. Run: py -m pip install pyautogui", file=sys.stderr)
    sys.exit(1)


def _workspace_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def handle_action(action: str, target: str | None = None) -> None:
    if action == "open_app":
        if not target:
            print("open_app requires target (path, .lnk, or app name)", file=sys.stderr)
            sys.exit(2)
        if os.path.isfile(target) or os.path.isdir(target):
            os.startfile(target)  # noqa: S606
        else:
            subprocess.run(["cmd", "/c", "start", "", target], shell=False, check=False)
    elif action == "screenshot":
        path = os.path.join(_workspace_dir(), "screen_context.png")
        pyautogui.screenshot(path)
        print(f"截图已保存：{path}")
    elif action == "type":
        if not target:
            print("type requires target text", file=sys.stderr)
            sys.exit(2)
        pyautogui.write(target, interval=0.05)
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if len(sys.argv) < 2:
        print("Usage: py commander.py <action> [target]", file=sys.stderr)
        sys.exit(1)
    handle_action(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
