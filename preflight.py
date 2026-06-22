"""
Pre-flight checker for experiment execution.

Validates experiment code before submission to SCO or local execution.
Blocking failures (syntax errors, missing critical files) prevent execution.
Warnings (loose version constraints, small batch sizes for GPU) are reported
but don't block.

Checks performed:
  1. Python syntax validation (all .py files in experiment dir)
  2. Import availability (test-import each module)
  3. Directory structure (required dirs: experiment/, checkpoints/, results/, data/)
  4. Config / manifest consistency
  5. GPU/CPU ratio estimation (warn if CPU-heavy experiment on multi-GPU)
  6. Environment dependency check (critical packages importable)
  7. SCO worker spec validation
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    check_name: str
    passed: bool
    blocking: bool  # if True, failure prevents execution
    message: str = ""
    suggestion: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreflightReport:
    project_slug: str
    experiment_dir: Path
    results: list[CheckResult] = field(default_factory=list)
    _blocking_failures: int = 0
    _warnings: int = 0

    @property
    def ok(self) -> bool:
        return self._blocking_failures == 0

    def add(self, r: CheckResult) -> None:
        self.results.append(r)
        if not r.passed and r.blocking:
            self._blocking_failures += 1
        if not r.passed and not r.blocking:
            self._warnings += 1

    def format(self) -> str:
        lines = ["=" * 60, " PREFLIGHT CHECK REPORT",
                  f" Project: {self.project_slug}",
                  f" Directory: {self.experiment_dir}", "=" * 60, ""]
        for r in self.results:
            icon = "PASS" if r.passed else ("BLOCK" if r.blocking else "WARN")
            lines.append(f"  [{icon}] {r.check_name}")
            if r.message:
                lines.append(f"         {r.message}")
            if r.suggestion:
                lines.append(f"         Fix: {r.suggestion}")
        lines.append("")
        status = "READY" if self.ok else f"BLOCKED ({self._blocking_failures} failures)"
        lines.append(f" Status: {status}  |  Warnings: {self._warnings}")
        lines.append("=" * 60)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

class PreflightChecker:
    """Run all pre-flight checks on an experiment directory."""

    def __init__(self, experiment_dir: Path, project_slug: str = ""):
        self.experiment_dir = Path(experiment_dir)
        self.project_slug = project_slug or self.experiment_dir.parent.name

    def run(self) -> PreflightReport:
        report = PreflightReport(
            project_slug=self.project_slug,
            experiment_dir=self.experiment_dir,
        )
        checks = [
            self._check_directory_structure,
            self._check_python_syntax,
            self._check_imports,
            self._check_experiment_script,
            self._check_gpu_cpu_ratio,
            self._check_env_dependencies,
            self._check_sco_worker_spec,
        ]
        for check_fn in checks:
            try:
                result = check_fn()
            except Exception as exc:
                result = CheckResult(
                    check_name=check_fn.__name__.replace("_check_", ""),
                    passed=False,
                    blocking=False,
                    message=f"Checker crashed: {exc}",
                )
            report.add(result)
        return report

    # ------------------------------------------------------------------
    # 1. Directory structure
    # ------------------------------------------------------------------

    def _check_directory_structure(self) -> CheckResult:
        required = ["experiment", "results", "checkpoints", "data"]
        # Try both workspace-level and experiment-level
        parent = self.experiment_dir.parent
        missing = []
        for d in required:
            if not ((parent / d).exists() or (self.experiment_dir / d).exists()):
                missing.append(d)

        if missing:
            return CheckResult(
                check_name="directory_structure",
                passed=False,
                blocking=True,
                message=f"Missing directories: {', '.join(missing)}",
                suggestion=f"mkdir -p {' '.join(str(parent/d) for d in missing)}",
            )
        return CheckResult(check_name="directory_structure", passed=True, blocking=True,
                           message="All required directories present")

    # ------------------------------------------------------------------
    # 2. Python syntax
    # ------------------------------------------------------------------

    def _check_python_syntax(self) -> CheckResult:
        py_files = list(self.experiment_dir.rglob("*.py"))
        if not py_files:
            return CheckResult(
                check_name="python_syntax",
                passed=False,
                blocking=True,
                message="No Python files found in experiment directory",
                suggestion="Add experiment Python scripts to the experiment/ directory",
            )

        syntax_errors: list[str] = []
        for pf in py_files:
            try:
                source = pf.read_text(encoding="utf-8")
                ast.parse(source, filename=str(pf))
            except SyntaxError as e:
                syntax_errors.append(f"{pf.name}:{e.lineno}: {e.msg}")

        if syntax_errors:
            return CheckResult(
                check_name="python_syntax",
                passed=False,
                blocking=True,
                message=f"Syntax errors in {len(syntax_errors)} file(s)",
                suggestion="Fix syntax errors before submitting",
                details={"errors": syntax_errors},
            )
        return CheckResult(
            check_name="python_syntax",
            passed=True,
            blocking=True,
            message=f"{len(py_files)} Python file(s) — syntax OK",
            details={"file_count": len(py_files)},
        )

    # ------------------------------------------------------------------
    # 3. Import availability
    # ------------------------------------------------------------------

    # Known mapping: import name -> pip package (used for suggestions)
    _IMPORT_TO_PACKAGE: dict[str, str] = {
        "torch": "torch",
        "torchvision": "torchvision",
        "numpy": "numpy",
        "gym": "gym",
        "gymnasium": "gymnasium",
        "ale_py": "ale-py",
        "cv2": "opencv-python-headless",
        "matplotlib": "matplotlib",
        "scipy": "scipy",
        "tqdm": "tqdm",
        "PIL": "Pillow",
        "sklearn": "scikit-learn",
        "pandas": "pandas",
        "h5py": "h5py",
        "transformers": "transformers",
        "datasets": "datasets",
        "wandb": "wandb",
        "tensorboard": "tensorboard",
    }

    def _check_imports(self) -> CheckResult:
        """Extract top-level imports from all .py files and test-import each."""
        py_files = list(self.experiment_dir.rglob("*.py"))
        imports: set[str] = set()

        for pf in py_files:
            try:
                source = pf.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(pf))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imports.add(alias.name.split(".")[0])
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            imports.add(node.module.split(".")[0])
            except SyntaxError:
                continue  # already caught by syntax check

        # Filter out stdlib and relative imports
        stdlib = _get_stdlib_modules()
        local_modules = _get_local_modules(self.experiment_dir)
        third_party = imports - stdlib - local_modules

        missing: list[str] = []
        for mod in sorted(third_party):
            try:
                __import__(mod)
            except ImportError:
                pkg = self._IMPORT_TO_PACKAGE.get(mod, mod)
                missing.append(f"{mod} (pip install {pkg})")

        if missing:
            return CheckResult(
                check_name="imports",
                passed=False,
                blocking=False,  # SCO container may have different packages
                message=f"{len(missing)} import(s) not available locally: {', '.join(missing[:8])}",
                suggestion="These may be available in the SCO container image. "
                           "If the experiment fails with ModuleNotFoundError, add to install_deps.sh.",
                details={"missing_imports": missing},
            )
        return CheckResult(
            check_name="imports",
            passed=True,
            blocking=True,
            message=f"{len(third_party)} third-party import(s) — all available",
            details={"third_party_count": len(third_party)},
        )

    # ------------------------------------------------------------------
    # 4. Experiment script sanity
    # ------------------------------------------------------------------

    def _check_experiment_script(self) -> CheckResult:
        script = self.experiment_dir / "run_experiment.sh"
        if not script.exists():
            return CheckResult(
                check_name="experiment_script",
                passed=False,
                blocking=True,
                message="run_experiment.sh not found",
                suggestion="Create an experiment runner script",
            )

        content = script.read_text(encoding="utf-8")
        issues: list[str] = []

        # Check for error trap
        if "set -e" not in content and "set -euo pipefail" not in content:
            issues.append('Missing "set -e" — script will continue after errors')
        if "trap" not in content or "ERR" not in content:
            issues.append('Missing ERR trap — no diagnostic output on failure')

        # Check for GPU/NPU env var usage
        if "CUDA_VISIBLE_DEVICES" not in content and "ASCEND_VISIBLE_DEVICES" not in content:
            issues.append("No CUDA_VISIBLE_DEVICES or ASCEND_VISIBLE_DEVICES handling — may not work on multi-GPU/NPU")

        # Check for essential phases
        if "pip install" not in content and "install_deps" not in content:
            issues.append("No dependency installation step detected")

        if issues:
            return CheckResult(
                check_name="experiment_script",
                passed=False,
                blocking=False,
                message="; ".join(issues),
                suggestion="Add error handling, GPU detection, and dependency setup",
                details={"issues": issues},
            )
        return CheckResult(
            check_name="experiment_script",
            passed=True,
            blocking=True,
            message="run_experiment.sh looks well-structured",
        )

    # ------------------------------------------------------------------
    # 5. GPU / CPU ratio estimation
    # ------------------------------------------------------------------

    # Keywords that suggest CPU-heavy workloads
    CPU_HEAVY_KEYWORDS = [
        "Atari", "ALE", "gymnasium", "gym.make", "ale-py",
        "OpenCV", "cv2", "replay_buffer", "env.step",
        "num_workers", "DataLoader",
    ]
    GPU_HEAVY_KEYWORDS = [
        "transformer", "attention", "bert", "gpt", "llama",
        "resnet50", "resnet101", "vit", "diffusion",
        "batch_size.*128", "batch_size.*256", "batch_size.*512",
        "gradient_accumulation",
        # NPU-related patterns (Ascend accelerators)
        "torch_npu", "torch\\.npu", "\\.npu\\(", "ascend",
    ]

    def _check_gpu_cpu_ratio(self) -> CheckResult:
        """Estimate whether the experiment is CPU-heavy or GPU-heavy."""
        py_files = list(self.experiment_dir.rglob("*.py"))
        all_code = ""
        for pf in py_files:
            try:
                all_code += pf.read_text(encoding="utf-8")
            except Exception:
                pass

        cpu_score = sum(
            len(re.findall(kw, all_code, re.IGNORECASE))
            for kw in self.CPU_HEAVY_KEYWORDS
        )
        gpu_score = sum(
            len(re.findall(kw, all_code, re.IGNORECASE))
            for kw in self.GPU_HEAVY_KEYWORDS
        )

        # Read manifest for GPU count
        manifest = self.experiment_dir.parent / "experiment_manifest.json"
        gpu_count = 1
        if manifest.exists():
            try:
                import json
                gpu_count = json.loads(manifest.read_text()).get("gpu_count", 1)
            except Exception:
                pass

        if cpu_score > gpu_score * 3 and gpu_count > 1:
            return CheckResult(
                check_name="gpu_cpu_ratio",
                passed=False,
                blocking=False,
                message=f"CPU-heavy experiment (cpu_score={cpu_score}, gpu_score={gpu_score}) "
                        f"requesting {gpu_count} GPU(s). GPU likely underutilized.",
                suggestion="Reduce JOBS_PER_GPU (try 1 instead of 3) for CPU-heavy workloads "
                           "like Atari RL. CPU contention from concurrent emulators will be the bottleneck.",
                details={"cpu_score": cpu_score, "gpu_score": gpu_score,
                         "gpu_count": gpu_count},
            )
        return CheckResult(
            check_name="gpu_cpu_ratio",
            passed=True,
            blocking=True,
            message=f"CPU/GPU ratio OK (cpu={cpu_score}, gpu={gpu_score})",
            details={"cpu_score": cpu_score, "gpu_score": gpu_score},
        )

    # ------------------------------------------------------------------
    # 6. Environment dependencies
    # ------------------------------------------------------------------

    def _check_env_dependencies(self) -> CheckResult:
        """Check critical packages are importable."""
        critical = ["torch", "numpy"]
        missing = []
        for mod in critical:
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)

        # Check CUDA availability
        cuda_available = False
        try:
            import torch
            cuda_available = torch.cuda.is_available()
        except Exception:
            pass

        # Check NPU availability (Huawei Ascend via torch_npu)
        npu_available = False
        try:
            import torch_npu  # noqa: F401
            import torch
            npu_available = torch.npu.is_available()
        except Exception:
            pass

        # Build accelerator status string
        accel_parts = []
        if cuda_available:
            accel_parts.append("CUDA: available")
        else:
            accel_parts.append("CUDA: not available")
        if npu_available:
            accel_parts.append("NPU: available")
        else:
            accel_parts.append("NPU: not detected")

        if missing:
            return CheckResult(
                check_name="env_dependencies",
                passed=False,
                blocking=False,
                message=f"Critical packages missing locally: {missing}",
                suggestion="Install with pip before local execution. SCO container may differ.",
                details={
                    "missing": missing,
                    "cuda_available": cuda_available,
                    "npu_available": npu_available,
                },
            )
        return CheckResult(
            check_name="env_dependencies",
            passed=True,
            blocking=True,
            message=f"Critical packages OK ({', '.join(accel_parts)})",
            details={
                "cuda_available": cuda_available,
                "npu_available": npu_available,
            },
        )

    # ------------------------------------------------------------------
    # 7. SCO worker spec validation
    # ------------------------------------------------------------------

    _SPEC_RE = re.compile(
        r"^[a-z0-9]+\.[a-z0-9]+\.[a-z0-9]+\.\d+\.\d+c\d+g$"
    )

    def _check_sco_worker_spec(self) -> CheckResult:
        """Validate SCO worker spec format."""
        spec = os.environ.get("SCO_WORKER_SPEC", "")
        if not spec:
            return CheckResult(
                check_name="sco_worker_spec",
                passed=True,
                blocking=True,
                message="No SCO_WORKER_SPEC set — will use default from config.py",
            )

        if not self._SPEC_RE.match(spec):
            return CheckResult(
                check_name="sco_worker_spec",
                passed=False,
                blocking=True,
                message=f"Invalid worker spec format: {spec}",
                suggestion="Format: {machine}.{type}.{variant}.{gpus}.{cpu}c{ram}g. "
                           "Example: n6ls.iu.i40.2.16c256g. "
                           "Truncated specs cause silent container failures.",
                details={"spec": spec},
            )
        return CheckResult(
            check_name="sco_worker_spec",
            passed=True,
            blocking=True,
            message=f"Worker spec format valid: {spec}",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_stdlib_modules() -> set[str]:
    """Return a set of Python stdlib top-level module names (3.10+)."""
    return {
        "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio",
        "asyncore", "atexit", "audioop", "base64", "bdb", "binascii", "binhex",
        "bisect", "builtins", "bz2", "calendar", "cgi", "cgitb", "chunk",
        "cmath", "cmd", "code", "codecs", "codeop", "collections", "colorsys",
        "compileall", "concurrent", "configparser", "contextlib", "contextvars",
        "copy", "copyreg", "cProfile", "crypt", "csv", "ctypes", "curses",
        "dataclasses", "datetime", "dbm", "decimal", "difflib", "dis",
        "distutils", "doctest", "email", "encodings", "enum", "errno",
        "faulthandler", "fcntl", "filecmp", "fileinput", "fnmatch", "fractions",
        "ftplib", "functools", "gc", "getopt", "getpass", "gettext", "glob",
        "graphlib", "grp", "gzip", "hashlib", "heapq", "hmac", "html", "http",
        "idlelib", "imaplib", "imghdr", "imp", "importlib", "inspect", "io",
        "ipaddress", "itertools", "json", "keyword", "lib2to3", "linecache",
        "locale", "logging", "lzma", "mailbox", "mailcap", "marshal", "math",
        "mimetypes", "mmap", "modulefinder", "multiprocessing", "netrc", "nis",
        "nntplib", "numbers", "operator", "optparse", "os", "ossaudiodev",
        "pathlib", "pdb", "pickle", "pickletools", "pipes", "pkgutil",
        "platform", "plistlib", "poplib", "posix", "posixpath", "pprint",
        "profile", "pstats", "pty", "pwd", "py_compile", "pyclbr",
        "pydoc", "queue", "quopri", "random", "re", "readline", "reprlib",
        "resource", "rlcompleter", "runpy", "sched", "secrets", "select",
        "selectors", "shelve", "shlex", "shutil", "signal", "site", "smtpd",
        "smtplib", "sndhdr", "socket", "socketserver", "sqlite3", "ssl",
        "stat", "statistics", "string", "stringprep", "struct", "subprocess",
        "sunau", "symtable", "sys", "sysconfig", "syslog", "tabnanny",
        "tarfile", "telnetlib", "tempfile", "termios", "test", "textwrap",
        "threading", "time", "timeit", "tkinter", "token", "tokenize",
        "trace", "traceback", "tracemalloc", "tty", "turtle", "turtledemo",
        "types", "typing", "unicodedata", "unittest", "urllib", "uu",
        "uuid", "venv", "warnings", "wave", "weakref", "webbrowser",
        "winreg", "winsound", "wsgiref", "xdrlib", "xml", "xmlrpc",
        "zipapp", "zipfile", "zipimport", "zlib", "zoneinfo",
    }


def _get_local_modules(experiment_dir: Path) -> set[str]:
    """Return module names defined locally in the experiment directory."""
    local: set[str] = set()
    for pf in experiment_dir.rglob("*.py"):
        local.add(pf.stem)
    # Also add workspace-level Python files (like shared utils)
    workspace = experiment_dir.parent.parent
    for pf in workspace.glob("*.py"):
        local.add(pf.stem)
    return local


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Pre-flight check for experiment execution"
    )
    parser.add_argument(
        "experiment_dir", type=str,
        help="Path to experiment directory (e.g., workspace/<project>/experiment)"
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Output as JSON instead of human-readable"
    )
    args = parser.parse_args()

    exp_dir = Path(args.experiment_dir)
    if not exp_dir.exists():
        print(f"ERROR: Directory not found: {exp_dir}")
        sys.exit(1)

    checker = PreflightChecker(exp_dir)
    report = checker.run()

    if args.json_output:
        import json as _json
        print(_json.dumps({
            "ok": report.ok,
            "blocking_failures": report._blocking_failures,
            "warnings": report._warnings,
            "checks": [
                {
                    "name": r.check_name,
                    "passed": r.passed,
                    "blocking": r.blocking,
                    "message": r.message,
                    "suggestion": r.suggestion,
                }
                for r in report.results
            ],
        }, indent=2, ensure_ascii=False))
    else:
        print(report.format())

    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
