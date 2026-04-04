"""evolved_hello.py — 打印当前时间和 DevClaw 版本信息"""

from datetime import datetime

DEVCLAW_VERSION = "0.1.0"

def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[DevClaw v{DEVCLAW_VERSION}]")
    print(f"当前时间: {now}")

if __name__ == "__main__":
    main()
