#!/usr/bin/env python3
"""Interactive arrow-key menu. Items as args. Selection to /tmp/slai_menu_result.txt."""

import sys
import tty
import termios
import select


def read_key() -> str:
    """Read a single keypress with escape sequence handling."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            # Arrow keys: ESC [ A/B/C/D
            r, _, _ = select.select([sys.stdin], [], [], 0.3)
            if r:
                seq = sys.stdin.read(2)
                if seq == '[A': return 'UP'
                if seq == '[B': return 'DOWN'
            return 'ESC'
        if ch in ('\r', '\n'):
            return 'ENTER'
        if ch == '\x03':
            raise KeyboardInterrupt
        if ch.lower() == 'q':
            return 'QUIT'
        if ch == 'j':
            return 'DOWN'
        if ch == 'k':
            return 'UP'
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def menu(items: list[str]) -> int | None:
    """
    Display a selectable menu. Returns index, None for new, -1 for quit.
    Uses in-place cursor movement — no full-screen clear to avoid flicker.
    """
    options = list(items) + ["★ 开始全新研究", "✕ 退出"]
    extra_start = len(items)
    idx = 0
    n = len(options)

    # Count total lines we'll render
    total_lines = n + 3  # items + header(1) + blank(1) + help(1)

    def draw():
        # Move cursor to top-left WITHOUT clearing — overwrite in-place
        sys.stdout.write(f'\x1b[{total_lines}A')  # up to where we started
        sys.stdout.write('\x1b[1;36m📂 已有项目:\x1b[0m')
        sys.stdout.write('\x1b[K\n')  # clear to end of line
        sys.stdout.write('\x1b[K\n')
        for i, opt in enumerate(options):
            prefix = "  " if i < extra_start else ""
            line = f"{prefix}{opt}"
            if len(line) > 78:
                line = line[:75] + "..."
            if i == idx:
                sys.stdout.write(f'\x1b[7m  ► {line}\x1b[0m\x1b[K\n')
            else:
                sys.stdout.write(f'    {line}\x1b[K\n')
        sys.stdout.write('\x1b[K\n')
        sys.stdout.write('\x1b[90m↑↓/jk 移动  ↵/Enter 确认  q 退出\x1b[0m\x1b[K')
        sys.stdout.flush()

    # Initial render: print header then draw
    sys.stdout.write('\n\x1b[1;36m📂 已有项目:\x1b[0m\n\n')
    for _ in range(n):
        sys.stdout.write('\n')  # reserve space
    sys.stdout.write('\n')
    draw()

    while True:
        try:
            key = read_key()
        except Exception:
            break

        if key == 'UP':
            idx = (idx - 1) % n
        elif key == 'DOWN':
            idx = (idx + 1) % n
        elif key == 'ENTER':
            if idx < extra_start:
                return idx
            elif idx == extra_start:
                return None
            else:
                return -1
        elif key == 'QUIT':
            return -1
        else:
            continue
        draw()


if __name__ == '__main__':
    items = sys.argv[1:] if len(sys.argv) > 1 else [
        line.rstrip('\n') for line in sys.stdin if line.strip()
    ]
    if not items:
        sys.exit(1)

    try:
        result = menu(items)
    finally:
        # 确保终端不残留异常状态
        sys.stdout.write('\n\x1b[?25h')  # 换行 + 显示光标
        sys.stdout.flush()
        import subprocess
        subprocess.run(['stty', 'sane'], capture_output=True)  # 终极恢复

    outfile = "/tmp/slai_menu_result.txt"
    if result is None:
        with open(outfile, 'w') as f: f.write("__NEW__")
    elif result == -1:
        with open(outfile, 'w') as f: f.write("__QUIT__")
    else:
        with open(outfile, 'w') as f: f.write(str(result))
