"""
Retro Pixel Art Chat Viewer — 80s/90s style terminal chat display.

Shows Claude Opus ↔ DevClaw conversation in pixel-art style using
Unicode block characters and ANSI colors in PowerShell/Terminal.
"""
import json
import os
import sys
import time

# ANSI color codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

# Colors
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
RED = "\033[91m"
BLUE = "\033[94m"
WHITE = "\033[97m"
GRAY = "\033[90m"

# Pixel art characters
BLOCK_FULL = "█"
BLOCK_LIGHT = "░"
BLOCK_MED = "▒"
BLOCK_DARK = "▓"
BORDER_H = "═"
BORDER_V = "║"
CORNER_TL = "╔"
CORNER_TR = "╗"
CORNER_BL = "╚"
CORNER_BR = "╝"
ARROW_R = "►"
ARROW_L = "◄"

WIDTH = 72


def pixel_border(char="═", color=CYAN):
    return f"{color}{CORNER_TL}{char * (WIDTH - 2)}{CORNER_TR}{RESET}"


def pixel_bottom(char="═", color=CYAN):
    return f"{color}{CORNER_BL}{char * (WIDTH - 2)}{CORNER_BR}{RESET}"


def center_text(text, width=WIDTH - 4):
    padding = (width - len(text)) // 2
    return " " * max(0, padding) + text


# DevClaw pixel art avatar (small dragon/claw)
DEVCLAW_ART = f"""{GREEN}
    ╔══════════════════╗
    ║  {YELLOW}◄◄ DevClaw ►►{GREEN}   ║
    ║   {RED}  ╱╲  ╱╲  {GREEN}     ║
    ║   {RED} ╱▓▓╲╱▓▓╲ {GREEN}     ║
    ║   {RED} ▓▓{YELLOW}◉{RED}▓▓{YELLOW}◉{RED}▓▓ {GREEN}     ║
    ║   {RED} ╲▓▓▓▓▓▓╱ {GREEN}     ║
    ║   {RED}  ╲{WHITE}╱╲╱╲{RED}╱  {GREEN}     ║
    ║   {RED}   ╲▓▓▓╱   {GREEN}     ║
    ║   {MAGENTA} ╱╲{RED}╱   ╲{MAGENTA}╱╲  {GREEN}  ║
    ╚══════════════════╝{RESET}"""

# Claude pixel art avatar (brain/diamond)
CLAUDE_ART = f"""{BLUE}
    ╔══════════════════╗
    ║ {MAGENTA}◄◄ Claude Opus ►►{BLUE} ║
    ║   {CYAN}    ╱╲      {BLUE}    ║
    ║   {CYAN}   ╱{WHITE}◆◆{CYAN}╲     {BLUE}    ║
    ║   {CYAN}  ╱{WHITE}◆◆◆◆{CYAN}╲    {BLUE}    ║
    ║   {CYAN} ╱{WHITE}◆◆{MAGENTA}██{WHITE}◆◆{CYAN}╲   {BLUE}    ║
    ║   {CYAN}  ╲{WHITE}◆◆◆◆{CYAN}╱    {BLUE}    ║
    ║   {CYAN}   ╲{WHITE}◆◆{CYAN}╱     {BLUE}    ║
    ║   {CYAN}    ╲╱      {BLUE}    ║
    ╚══════════════════╝{RESET}"""


def wrap_text(text, max_width=50):
    """Simple word wrap."""
    words = text.replace("\n", " ").split()
    lines = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 <= max_width:
            current = current + " " + word if current else word
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def render_message(sender, text, response_time="", is_claude=True):
    """Render a single chat message in pixel art style."""
    color = BLUE if is_claude else GREEN
    name_color = MAGENTA if is_claude else YELLOW
    arrow = ARROW_R if is_claude else ARROW_L
    name = "Claude Opus" if is_claude else "DevClaw"

    lines = wrap_text(text, max_width=WIDTH - 12)
    time_str = f" {DIM}({response_time}){RESET}" if response_time else ""

    print(f"  {color}{arrow}{RESET} {name_color}{BOLD}{name}{RESET}{time_str}")
    print(f"  {color}┌{'─' * (WIDTH - 6)}┐{RESET}")
    for line in lines:
        padded = line + " " * (WIDTH - 6 - len(line))
        print(f"  {color}│{RESET} {WHITE}{padded}{RESET}{color}│{RESET}")
    print(f"  {color}└{'─' * (WIDTH - 6)}┘{RESET}")
    print()


def scanline_effect():
    """Fake CRT scanline."""
    print(f"  {DIM}{BLOCK_LIGHT * WIDTH}{RESET}")


def render_title():
    """Render the retro title screen."""
    print()
    print(f"  {CYAN}{BLOCK_FULL * WIDTH}{RESET}")
    print(f"  {CYAN}{BLOCK_FULL}{RESET}{' ' * (WIDTH - 2)}{CYAN}{BLOCK_FULL}{RESET}")
    title = "◆◆◆ NEURAL LINK CHAT LOG ◆◆◆"
    print(f"  {CYAN}{BLOCK_FULL}{RESET}{center_text(f'{BOLD}{YELLOW}{title}{RESET}', WIDTH - 2)}{CYAN}{BLOCK_FULL}{RESET}")
    subtitle = "Claude Opus 4.6 ←→ DevClaw v2.0"
    print(f"  {CYAN}{BLOCK_FULL}{RESET}{center_text(f'{DIM}{WHITE}{subtitle}{RESET}', WIDTH - 2)}{CYAN}{BLOCK_FULL}{RESET}")
    date_str = time.strftime("%Y-%m-%d %H:%M")
    print(f"  {CYAN}{BLOCK_FULL}{RESET}{center_text(f'{DIM}{GRAY}{date_str}{RESET}', WIDTH - 2)}{CYAN}{BLOCK_FULL}{RESET}")
    print(f"  {CYAN}{BLOCK_FULL}{RESET}{' ' * (WIDTH - 2)}{CYAN}{BLOCK_FULL}{RESET}")
    print(f"  {CYAN}{BLOCK_FULL * WIDTH}{RESET}")
    print()


def render_stats(conversation):
    """Render conversation statistics."""
    total_q = len(conversation)
    answered = sum(1 for c in conversation if c.get("a") and "[processing" not in c["a"])
    avg_time = "N/A"
    times = []
    for c in conversation:
        t = c.get("time", "")
        if t and "s" in t:
            try:
                times.append(float(t.replace("s", "")))
            except ValueError:
                pass
    if times:
        avg_time = f"{sum(times) / len(times):.1f}s"

    print(f"  {YELLOW}{'━' * WIDTH}{RESET}")
    print(f"  {YELLOW}{BOLD}  STATS{RESET}")
    print(f"  {WHITE}  Messages: {total_q} | Answered: {answered} | Avg Response: {avg_time}{RESET}")
    print(f"  {WHITE}  Model: Gemma3:4b (local) | Fallback: Claude CLI (subscription){RESET}")
    print(f"  {YELLOW}{'━' * WIDTH}{RESET}")
    print()


def render_avatars():
    """Show both avatars side by side."""
    claude_lines = CLAUDE_ART.strip().split("\n")
    devclaw_lines = DEVCLAW_ART.strip().split("\n")
    max_lines = max(len(claude_lines), len(devclaw_lines))

    for i in range(max_lines):
        left = claude_lines[i] if i < len(claude_lines) else " " * 24
        right = devclaw_lines[i] if i < len(devclaw_lines) else ""
        print(f"  {left}    {right}")
    print()


def main():
    # Enable ANSI on Windows
    if sys.platform == "win32":
        os.system("")  # Enables ANSI escape codes

    # Load conversation
    conv_path = os.path.join(os.path.dirname(__file__), "..", ".claw", "last_conversation.json")
    if not os.path.isfile(conv_path):
        conv_path = ".claw/last_conversation.json"

    try:
        with open(conv_path, encoding="utf-8") as f:
            conversation = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        conversation = [
            {"q": "你好", "a": "你好！有什么我可以帮你的？", "time": "2.1s"},
            {"q": "你聪明吗", "a": "我在不断学习变得更聪明！", "time": "2.5s"},
            {"q": "1+1=?", "a": "1+1=2", "time": "1.5s"},
        ]

    # Clear screen
    os.system("cls" if sys.platform == "win32" else "clear")

    # Render
    render_title()
    render_avatars()
    scanline_effect()
    print()

    for entry in conversation:
        render_message("Claude", entry["q"], is_claude=True)
        time.sleep(0.3)
        render_message("DevClaw", entry.get("a", "[...]"), entry.get("time", ""), is_claude=False)
        time.sleep(0.3)
        scanline_effect()

    render_stats(conversation)

    # Footer
    print(f"  {DIM}{GRAY}{'▓' * WIDTH}{RESET}")
    print(f"  {DIM}{GRAY}  Press any key to exit...{RESET}")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
