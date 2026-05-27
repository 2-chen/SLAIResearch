#!/usr/bin/env python3
"""Interactive arrow-key menu. Items as args. Selection to /tmp/cr_menu_result.txt."""

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
            r, _, _ = select.select([sys.stdin], [], [], 0.15)
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
    No screen clearing — just inline selection.
    """
    options = list(items) + ["★ 开始全新研究", "✕ 退出"]
    extra_start = len(items)
    idx = 0
    n = len(options)
    last_displayed = -1

    def draw():
        nonlocal last_displayed
        if last_displayed >= 0:
            # Move cursor back up to overwrite previous display
            lines = last_displayed + 2
            sys.stdout.write(f'\x1b[{lines}A')
        sys.stdout.write('\x1b[J')  # clear to end of screen
        for i, opt in enumerate(options):
            prefix = "  " if i < extra_start else ""
            line = f"{prefix}{opt}"
            if len(line) > 78:
                line = line[:75] + "..."
            if i == idx:
                sys.stdout.write(f'\x1b[7m  ► {line}\x1b[0m\n')
            else:
                sys.stdout.write(f'    {line}\n')
        sys.stdout.write('\n\x1b[90m↑↓/jk 移动  ↵/Enter 确认  q 退出\x1b[0m')
        sys.stdout.flush()
        last_displayed = n

    # Print initial header
    sys.stdout.write('\n\x1b[1;36m已有项目:\x1b[0m\n\n')
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

    result = menu(items)

    outfile = "/tmp/cr_menu_result.txt"
    if result is None:
        with open(outfile, 'w') as f: f.write("__NEW__")
    elif result == -1:
        with open(outfile, 'w') as f: f.write("__QUIT__")
    else:
        with open(outfile, 'w') as f: f.write(str(result))
