#!/usr/bin/env python3
"""PTY wrapper — run a command in a pseudo-terminal to eliminate pipe buffering
and TTY detection issues.

Claude Code detects non-TTY (subprocess.PIPE) and behaves differently — wasting
turns on permission prompts or refusing to proceed.  A PTY makes it believe it's
in an interactive session, solving the recurring \"Claude call timed out\" issue
on root / Ascend / headless containers.

Usage:
    # CLI wrapper
    echo "prompt" | python claude_pty.py claude -p --model deepseek-v4-pro --verbose

    # Python API (recommended for subprocess replacement)
    from claude_pty import run_in_pty
    rc, stdout = run_in_pty(["claude", "-p", ...], prompt, timeout=600, cwd=".", env={})
"""

import os
import sys
import pty
import signal
import select
import errno
import fcntl
import struct
import termios
import subprocess
import time as _time_module
import logging

logger = logging.getLogger("claude_pty")


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Set terminal window size on a PTY fd (compat with Python < 3.11)."""
    try:
        # Python 3.11+
        termios.tcsetwinsize(fd, (rows, cols))
    except AttributeError:
        # Python ≤3.10: use ioctl directly
        winsize = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _spawn_with_eof(cmd: list[str]) -> int:
    """Fork + exec cmd in a PTY. Return raw wait status.

    Unlike pty.spawn(), this correctly signals EOF to the child when
    our stdin pipe closes: we write Ctrl-D (0x04) to the PTY master.
    """
    master_fd, slave_fd = pty.openpty()

    # Match terminal size to the real terminal (if available)
    try:
        cols, rows = os.get_terminal_size(0)
    except OSError:
        cols, rows = 80, 24
    try:
        _set_winsize(slave_fd, rows, cols)
    except OSError:
        pass  # non-critical

    # Disable ECHO on the PTY slave so input isn't echoed back.
    # Without this, every byte we forward from stdin appears twice in output
    # (once from terminal echo, once from the child's own output).
    attrs = termios.tcgetattr(slave_fd)
    attrs[3] = attrs[3] & ~termios.ECHO  # lflags: clear ECHO
    attrs[3] = attrs[3] & ~termios.ECHONL  # also clear ECHONL
    termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

    pid = os.fork()
    if pid == 0:
        # ── child ──
        os.close(master_fd)
        os.setsid()
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        try:
            os.execvp(cmd[0], cmd)
        except OSError:
            pass
        os._exit(127)

    # ── parent ──
    os.close(slave_fd)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    stdin_eof = False

    try:
        while True:
            rlist = [master_fd]
            if not stdin_eof:
                rlist.append(stdin_fd)

            try:
                r, _, _ = select.select(rlist, [], [])
            except (select.error, ValueError) as e:
                if isinstance(e, ValueError) or getattr(e, 'errno', None) == errno.EINTR:
                    continue
                raise

            # ── read from child (PTY master → stdout) ──
            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break  # child closed PTY
                    raise
                if not data:
                    break  # EOF from child
                # Write immediately — real-time output
                os.write(stdout_fd, data)

            # ── read from stdin (pipe → PTY master → child) ──
            if not stdin_eof and stdin_fd in r:
                try:
                    data = os.read(stdin_fd, 4096)
                except OSError:
                    data = b''
                if not data:
                    # EOF on stdin pipe — send Ctrl-D so child sees terminal EOF
                    stdin_eof = True
                    try:
                        os.write(master_fd, b'\x04')  # Ctrl-D = VEOF
                    except OSError:
                        pass
                else:
                    try:
                        os.write(master_fd, data)
                    except OSError as e:
                        if e.errno == errno.EIO:
                            stdin_eof = True

    except KeyboardInterrupt:
        # Ctrl-C: kernel delivers SIGINT to child via PTY automatically
        pass
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    # Reap the child
    while True:
        try:
            _, status = os.waitpid(pid, 0)
            break
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            raise

    return status


def run_in_pty(
    cmd: list[str],
    prompt: str,
    timeout: int = 600,
    cwd: str = ".",
    env: dict | None = None,
) -> tuple[int, str]:
    """Run *cmd* in a PTY, feed *prompt* via stdin, return (returncode, stdout).

    Prompt is passed as the final positional argument (``-p "prompt"`` style),
    stdin is /dev/null.  The PTY makes the child process believe it has a real
    terminal, avoiding TTY-detection behaviours that cause hangs in pipe mode.
    """
    full_cmd = list(cmd) + [prompt]
    if env is None:
        env = {}

    master_fd, slave_fd = pty.openpty()
    # Disable echo so prompt text doesn't appear in output
    try:
        attrs = termios.tcgetattr(slave_fd)
        attrs[3] = attrs[3] & ~termios.ECHO
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    except termios.error:
        pass

    try:
        proc = subprocess.Popen(
            full_cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=env,
            close_fds=True,
        )
        os.close(slave_fd)

        output_chunks: list[bytes] = []
        deadline = _time_module.time() + timeout
        while _time_module.time() < deadline:
            r, _, _ = select.select([master_fd], [], [], 1.0)
            if r:
                try:
                    chunk = os.read(master_fd, 65536)
                    if not chunk:
                        break
                    output_chunks.append(chunk)
                except OSError:
                    break
            if proc.poll() is not None:
                break

        # Drain any remaining output
        while True:
            r, _, _ = select.select([master_fd], [], [], 0.1)
            if not r:
                break
            try:
                chunk = os.read(master_fd, 65536)
                if not chunk:
                    break
                output_chunks.append(chunk)
            except OSError:
                break

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Process still flushing — return what we have
            pass
        stdout = b"".join(output_chunks).decode("utf-8", errors="replace")
        return proc.returncode or 0, stdout
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass


def main() -> None:
    # Forward signals so Ctrl-C reaches the child via the PTY
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGQUIT, signal.SIG_DFL)
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)

    cmd = sys.argv[1:]
    if not cmd:
        print(
            "Usage: echo 'prompt' | python claude_pty.py <command> [args...]",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        status = _spawn_with_eof(cmd)
    except OSError as e:
        if e.errno == errno.ENOENT:
            print(f"claude_pty: command not found: {cmd[0]}", file=sys.stderr)
            sys.exit(127)
        raise

    # Forward the child's exit code
    if os.WIFEXITED(status):
        sys.exit(os.WEXITSTATUS(status))
    elif os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        signal.raise_signal(sig)
        sys.exit(128 + sig)
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()
