#!/usr/bin/env python3
"""
Claude CLI 诊断 — 昇腾/NPU 环境超时排查

用法:
    python3 test_claude_diag.py
    DIAG_TIMEOUT=30 python3 test_claude_diag.py   # 快速
"""

import os, sys, json, re, time, subprocess, socket
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

GREEN  = "\033[0;32m"
RED    = "\033[0;31m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
NC     = "\033[0m"
BOLD   = "\033[1m"

_ok = 0
_bad = 0
_wrn = 0

def ok(msg=""):
    global _ok; _ok += 1
    print(f"  {GREEN}✓ OK{NC}  {msg}" if msg else f"  {GREEN}✓ OK{NC}")

def bad(msg=""):
    global _bad; _bad += 1
    print(f"  {RED}✗ FAIL{NC}  {msg}" if msg else f"  {RED}✗ FAIL{NC}")

def wrn(msg=""):
    global _wrn; _wrn += 1
    print(f"  {YELLOW}! WARN{NC}  {msg}" if msg else f"  {YELLOW}! WARN{NC}")

def info(msg=""):
    print(f"  {CYAN}→{NC} {msg}")

# ── Config ──
claude_cmd = os.environ.get("CLAUDE_CMD", "claude")
claude_model = os.environ.get("CLAUDE_MODEL", "deepseek-v4-pro")
api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY") or ""
base_url = os.environ.get("ANTHROPIC_BASE_URL") or ""

# Try settings.json
if not base_url or not api_key:
    sf = PROJECT_ROOT / ".claude" / "settings.json"
    if sf.exists():
        try:
            d = json.loads(sf.read_text())
            env_d = d.get("env", {}) or {}
            api_key = api_key or env_d.get("ANTHROPIC_API_KEY", "")
            base_url = base_url or env_d.get("ANTHROPIC_BASE_URL", "")
        except Exception:
            pass
base_url = base_url or "https://api.anthropic.com"

TIMEOUT = int(os.environ.get("DIAG_TIMEOUT", "60"))
TEST_PROMPT = "say exactly 'OK' and nothing else"

print("=" * 60)
print(f" Claude CLI 诊断 — {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)

# ═══ 0. Environment ═══
print(f"\n{BOLD}[0] 环境信息{NC}")
print(f"    CLAUDE_CMD:          {claude_cmd}")
print(f"    CLAUDE_MODEL:        {claude_model}")
print(f"    ANTHROPIC_BASE_URL:  {base_url}")
print(f"    ANTHROPIC_API_KEY:   {'已设置 (' + str(len(api_key)) + ' chars)' if api_key else '<未设置>'}")
for k in ("http_proxy", "https_proxy", "no_proxy"):
    v = os.environ.get(k, "")
    if v:
        print(f"    {k}: {v}")

# ═══ 1. Binary ═══
print(f"\n{BOLD}[1] 二进制检测{NC}")
bin_path = None
for path in os.environ.get("PATH", "").split(os.pathsep):
    for name in (claude_cmd, f"{claude_cmd}.exe"):
        cand = Path(path) / name
        if cand.is_file():
            bin_path = str(cand)
            break
    if bin_path:
        break

if bin_path:
    ok(f"found: {bin_path}")
    r = subprocess.run([claude_cmd, "--version"], capture_output=True, text=True, timeout=10)
    info(f"version: {(r.stdout or r.stderr).strip()[:200]}")
else:
    bad(f"'{claude_cmd}' not found in PATH")
    sys.exit(1)

# ═══ 2. Network ═══
print(f"\n{BOLD}[2] API 网络连通性{NC}")
from urllib.parse import urlparse
host = urlparse(base_url).hostname
port = urlparse(base_url).port or 443
info(f"TCP {host}:{port}")

try:
    sock = socket.create_connection((host, port), timeout=5)
    sock.close()
    ok(f"TCP {host}:{port} reachable")
except Exception as e:
    bad(f"TCP {host}:{port} — {e}")

try:
    import urllib.request
    models_url = f"{base_url.rstrip('/')}/v1/models"
    req = urllib.request.Request(models_url)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    resp = urllib.request.urlopen(req, timeout=10)
    ok(f"HTTP {models_url} → {resp.status}")
except urllib.request.HTTPError as e:
    if e.code in (401, 403):
        ok(f"HTTP {models_url} → {e.code} (认证拒绝，网络通)")
    else:
        bad(f"HTTP {models_url} → {e.code}")
except Exception as e:
    bad(f"HTTP → {e}")

# Build common env
env = os.environ.copy()
if api_key:
    env["ANTHROPIC_API_KEY"] = api_key
if base_url:
    env["ANTHROPIC_BASE_URL"] = base_url
env["IS_SANDBOX"] = "1"

# ═══ 3. stdin-PIPE (当前统一方式) ═══
print(f"\n{BOLD}[3] stdin-PIPE (当前方式: cmd + input=prompt){NC}")
cmd = [claude_cmd, "-p", "--output-format", "text", "--model", claude_model, "--max-turns", "1"]
info(f"echo '$TEST_PROMPT' | {' '.join(cmd)}")

t0 = time.time()
try:
    r = subprocess.run(cmd, input=TEST_PROMPT, capture_output=True, text=True,
                      timeout=TIMEOUT, env=env)
    elapsed = time.time() - t0
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out:
        ok(f"stdin-PIPE ({elapsed:.0f}s, {len(out)} chars)")
        info(f"Output: {out[:300]}")
    elif out:
        wrn(f"stdin-PIPE rc={r.returncode} ({elapsed:.0f}s)")
        info(f"Output: {out[:300]}")
    else:
        bad(f"stdin-PIPE rc={r.returncode} no output ({elapsed:.0f}s)")
        if r.stderr:
            info(f"Stderr: {r.stderr[:300]}")
except subprocess.TimeoutExpired:
    bad(f"stdin-PIPE TIMEOUT after {TIMEOUT}s")
except Exception as e:
    bad(f"stdin-PIPE {e}")

# ═══ 4. CLI-arg (旧方式: cmd + [prompt]) ═══
print(f"\n{BOLD}[4] CLI-arg (旧方式: prompt 作为命令行参数){NC}")
cmd = [claude_cmd, "-p", "--output-format", "text", "--model", claude_model,
       "--max-turns", "1", TEST_PROMPT]
info(f"{' '.join(cmd[:5])} ... '{TEST_PROMPT}'")

t0 = time.time()
try:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, env=env)
    elapsed = time.time() - t0
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out:
        ok(f"CLI-arg ({elapsed:.0f}s, {len(out)} chars)")
        info(f"Output: {out[:300]}")
    elif out:
        wrn(f"CLI-arg rc={r.returncode} ({elapsed:.0f}s)")
        info(f"Output: {out[:300]}")
    else:
        bad(f"CLI-arg rc={r.returncode} no output ({elapsed:.0f}s)")
        if r.stderr:
            info(f"Stderr: {r.stderr[:300]}")
except subprocess.TimeoutExpired:
    bad(f"CLI-arg TIMEOUT after {TIMEOUT}s")
except Exception as e:
    bad(f"CLI-arg {e}")

# ═══ 5. -p 位置: prompt紧随-p ═══
print(f"\n{BOLD}[5] -p 参数位置: prompt 紧接 -p{NC}")
cmd = [claude_cmd, "-p", TEST_PROMPT, "--output-format", "text",
       "--model", claude_model, "--max-turns", "1"]
info(f"{' '.join(cmd[:3])} ...")

t0 = time.time()
try:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, env=env)
    elapsed = time.time() - t0
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out:
        ok(f"-p紧接 ({elapsed:.0f}s, {len(out)} chars)")
        info(f"Output: {out[:300]}")
    elif out:
        wrn(f"-p紧接 rc={r.returncode} ({elapsed:.0f}s)")
        info(f"Output: {out[:300]}")
    else:
        bad(f"-p紧接 rc={r.returncode} no output ({elapsed:.0f}s)")
except subprocess.TimeoutExpired:
    bad(f"-p紧接 TIMEOUT after {TIMEOUT}s")
except Exception as e:
    bad(f"-p紧接 {e}")

# ═══ 6. PTY mode ═══
print(f"\n{BOLD}[6] PTY 模式 (claude_pty.run_in_pty){NC}")
try:
    from claude_pty import run_in_pty
    info(f"PTY: {' '.join(cmd[:4])} ...")
    t0 = time.time()
    rc, stdout = run_in_pty(
        [claude_cmd, "-p", "--output-format", "text", "--model", claude_model, "--max-turns", "1"],
        TEST_PROMPT, timeout=TIMEOUT, cwd=str(PROJECT_ROOT), env=env,
    )
    elapsed = time.time() - t0
    stdout = (stdout or "").strip()
    if rc == 0 and stdout:
        ok(f"PTY ({elapsed:.0f}s, {len(stdout)} chars)")
        info(f"Output: {stdout[:300]}")
    elif stdout:
        wrn(f"PTY rc={rc} ({elapsed:.0f}s)")
        info(f"Output: {stdout[:300]}")
    else:
        bad(f"PTY rc={rc} no output ({elapsed:.0f}s)")
except ImportError:
    wrn("claude_pty module 不可用 — 跳过")
except Exception as e:
    bad(f"PTY {e}")

# ═══ Summary ═══
print(f"\n{'=' * 60}")
print(f" {_ok} PASS  {_bad} FAIL  {_wrn} WARN")
print(f"{'=' * 60}")

if _bad == 0 and _wrn == 0:
    print(f"\n{GREEN}全部通过。若仍超时检查 --max-turns 过大或 prompt 过长。{NC}")
else:
    print(f"\n诊断建议:")
    if _bad > 0:
        print(f"  [3] stdin成功+[4] CLI-arg失败 → 确认参数位置是根因")
        print(f"  [3] 超时+[6] PTY成功 → 用 PTY 兜底（当前代码已启用）")
        print(f"  全部超时 → API 不通，检查 base_url/代理")
        print(f"  [5] 成功+[4] 失败 → -p 位置差异，统一用 stdin 即可")
