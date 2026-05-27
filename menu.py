#!/usr/bin/env python3
"""Interactive arrow-key menu. Reads items from stdin, returns selected index."""

import sys
import tty
import termios
import select


def read_key() -> str:
    """Read a single keypress, handling arrow escape sequences."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            # Escape sequence — check if there's more
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            if r:
                ch2 = sys.stdin.read(2)
                if ch2 == '[A': return 'UP'
                if ch2 == '[B': return 'DOWN'
                return f'ESC{ch2}'
            return 'ESC'
        if ch == '\r' or ch == '\n':
            return 'ENTER'
        if ch == '\x03':  # Ctrl-C
            raise KeyboardInterrupt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def menu(items: list[str], title: str = "请选择", default_new: bool = True) -> int | None:
    """
    Display an arrow-key navigable menu.
    Returns: index (0-based) of selected item, or None for new/quit.
    """
    idx = 0
    n = len(items)

    # Extra options
    options = list(items)
    extra_start = len(options)
    options.append("★ 开始全新研究")
    options.append("✕ 退出")

    def draw():
        sys.stderr.write('\x1b[H\x1b[J')  # clear screen
        sys.stderr.write(f'\x1b[1;36m{title}\x1b[0m\n\n')
        for i, opt in enumerate(options):
            if i < extra_start:
                prefix = "  "
            else:
                prefix = ""
            if i == idx:
                sys.stderr.write(f'\x1b[7m  ► {opt}  \x1b[0m\n')  # reverse video
            else:
                sys.stderr.write(f'    {opt}\n')
        sys.stderr.write('\n\x1b[90m↑↓ 移动  ↵ 确认  Ctrl-C 退出\x1b[0m\n')
        sys.stderr.flush()

    draw()
    while True:
        key = read_key()
        if key == 'UP':
            idx = (idx - 1) % len(options)
        elif key == 'DOWN':
            idx = (idx + 1) % len(options)
        elif key == 'ENTER':
            if idx < extra_start:
                return idx
            elif idx == extra_start:
                return None  # "new research"
            else:
                return -1   # "quit"
        else:
            continue
        draw()


if __name__ == '__main__':
    # 从命令行参数或 stdin 读取菜单项（命令行优先，保留 stdin 为 TTY）
    if len(sys.argv) > 1:
        items = sys.argv[1:]
    else:
        items = [line.rstrip('\n') for line in sys.stdin if line.strip()]
    if not items:
        print("No items", file=sys.stderr)
        sys.exit(1)
    result = menu(items)
    if result is None:
        print("__NEW__")
    elif result == -1:
        print("__QUIT__")
    else:
        print(result)
