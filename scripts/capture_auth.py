"""
Auth Capture — Launch headed browser for manual login.

Run this once to save session cookies for WebLLM proxy.
After login, the state is saved to .auth/state.json.
"""
import os
from pathlib import Path

def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright && python -m playwright install chromium")
        return

    auth_dir = Path(".auth")
    auth_dir.mkdir(exist_ok=True)
    state_path = auth_dir / "state.json"

    print("=" * 50)
    print("  DevClaw Auth Capture")
    print("  A browser will open. Log in to Claude/Groq.")
    print("  When done, close the browser window.")
    print("=" * 50)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        target = os.environ.get("AUTH_TARGET", "https://claude.ai")
        page.goto(target)

        print(f"\nBrowser opened at {target}")
        print("Log in manually, then close the browser window...")

        try:
            page.wait_for_event("close", timeout=300000)  # 5 min
        except Exception:
            pass

        # Save state
        state = context.storage_state()
        import json
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(f"\nAuth state saved to {state_path}")

        browser.close()

if __name__ == "__main__":
    main()
