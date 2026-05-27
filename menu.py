#!/usr/bin/env python3
"""Interactive arrow-key menu. Items as args. Returns selection index to stdout."""

import sys


def menu_curses(items: list[str]) -> int | None:
    """Use curses for reliable arrow-key navigation."""
    import curses

    options = list(items) + ["★ 开始全新研究", "✕ 退出"]
    extra_start = len(items)
    idx = 0

    def draw(stdscr):
        nonlocal idx
        curses.curs_set(0)
        stdscr.clear()
        h, w = stdscr.getmaxyx()

        while True:
            stdscr.erase()
            stdscr.addstr(0, 2, "ChenResearch — 选择项目", curses.A_BOLD | curses.color_pair(1))

            visible_start = max(0, idx - h + 8)
            for i in range(visible_start, min(len(options), visible_start + h - 5)):
                y = i - visible_start + 2
                prefix = "  " if i < extra_start else ""
                line = f"{prefix}{options[i]}"
                if len(line) > w - 4:
                    line = line[:w-7] + "..."
                if i == idx:
                    stdscr.addstr(y, 2, f"► {line}", curses.A_REVERSE)
                else:
                    stdscr.addstr(y, 4, line)

            stdscr.addstr(h - 2, 2, "↑↓ 移动  ↵ 确认  q 退出", curses.A_DIM)
            stdscr.refresh()

            key = stdscr.getch()
            if key == curses.KEY_UP:
                idx = (idx - 1) % len(options)
            elif key == curses.KEY_DOWN:
                idx = (idx + 1) % len(options)
            elif key == ord('k'):
                idx = (idx - 1) % len(options)
            elif key == ord('j'):
                idx = (idx + 1) % len(options)
            elif key in (10, 13, curses.KEY_ENTER):  # Enter
                if idx < extra_start:
                    return idx
                elif idx == extra_start:
                    return None
                else:
                    return -1
            elif key in (ord('q'), ord('Q'), 27):  # q or ESC
                return -1

    return curses.wrapper(draw)


if __name__ == '__main__':
    items = sys.argv[1:] if len(sys.argv) > 1 else [
        line.rstrip('\n') for line in sys.stdin if line.strip()
    ]
    if not items:
        sys.exit(1)

    result = menu_curses(items)

    # 结果写入临时文件（不能 print 到 stdout，因为 $() 会破坏 curses）
    outfile = "/tmp/cr_menu_result.txt"
    if result is None:
        with open(outfile, 'w') as f: f.write("__NEW__")
    elif result == -1:
        with open(outfile, 'w') as f: f.write("__QUIT__")
    else:
        with open(outfile, 'w') as f: f.write(str(result))
