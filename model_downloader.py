#!/usr/bin/env python3
"""
Three-layer model / dataset downloader with automatic fallback.

Layer 1: Direct — clear proxy, connect to HuggingFace directly
Layer 2: Mirror — hf-mirror.com → ModelScope fallback
Layer 3: VPN  — start mihomo proxy, route through 127.0.0.1:7890

Usage:
    from model_downloader import ThreeLayerDownloader

    dl = ThreeLayerDownloader()
    result = dl.download_model("prajjwal1/bert-tiny")
    if result.success:
        print(f"Downloaded to {result.path} via {result.layer_used}")
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("model_downloader")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DownloadResult:
    """Result of a download attempt."""
    success: bool
    path: str = ""
    layer_used: str = ""  # "direct", "mirror", "vpn"
    error: str = ""
    model_id: str = ""


# ---------------------------------------------------------------------------
# Three-Layer Downloader
# ---------------------------------------------------------------------------


class ThreeLayerDownloader:
    """Download models/datasets/files with automatic multi-layer fallback."""

    # HuggingFace mirror endpoints (tried in order)
    HF_MIRRORS = [
        "https://hf-mirror.com",
        "https://huggingface-mirror.com",
    ]

    # ModelScope as alternative backend
    MODELSCOPE_ENDPOINT = "https://modelscope.cn"

    def __init__(
        self,
        cache_dir: str = "",
        timeout: int = 300,
        retry_count: int = 2,
        proxy_url: str = "http://127.0.0.1:7890",
        vpn_script: str = "",
    ):
        self.timeout = timeout
        self.retry_count = retry_count
        self.proxy_url = proxy_url

        # Cache dir
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        else:
            try:
                from config import DOWNLOAD_CACHE_DIR
                self.cache_dir = Path(DOWNLOAD_CACHE_DIR)
            except ImportError:
                self.cache_dir = Path("workspace/.shared/cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # VPN script path
        if vpn_script:
            self.vpn_script = Path(vpn_script)
        else:
            self.vpn_script = Path(__file__).resolve().parent / "env" / "vpn" / "proxy.sh"
            if not self.vpn_script.exists():
                self.vpn_script = Path("/data/AutoResearch/ChenResearch/env/vpn/proxy.sh")

        self._load_hf_token()

    def _load_hf_token(self) -> None:
        """Load HuggingFace token from config or env."""
        try:
            from config import HF_TOKEN
            if HF_TOKEN:
                os.environ.setdefault("HF_TOKEN", HF_TOKEN)
                os.environ.setdefault("HUGGINGFACE_HUB_TOKEN", HF_TOKEN)
        except ImportError:
            pass

    # ------------------------------------------------------------------
    # Main download entry points
    # ------------------------------------------------------------------

    def download_model(self, model_id: str) -> DownloadResult:
        """Download a HuggingFace / ModelScope model with fallback."""
        model_dir = self.cache_dir / "models" / model_id.replace("/", "--")
        if model_dir.exists() and any(model_dir.iterdir()):
            logger.info("Model already cached: %s", model_dir)
            return DownloadResult(success=True, path=str(model_dir),
                                  layer_used="cache", model_id=model_id)

        for layer in ["direct", "mirror", "vpn"]:
            logger.info("Downloading %s via layer: %s", model_id, layer)
            result = self._download_hf_model(model_id, model_dir, layer)
            if result.success:
                return result
            logger.warning("Layer %s failed for %s: %s", layer, model_id, result.error)

        return DownloadResult(
            success=False, path=str(model_dir), layer_used="",
            error=f"All three layers failed for {model_id}", model_id=model_id,
        )

    def download_file(self, url: str, filename: str = "") -> DownloadResult:
        """Download a generic file with fallback."""
        if not filename:
            filename = hashlib.sha256(url.encode()).hexdigest()[:16]
        file_path = self.cache_dir / "files" / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if file_path.exists():
            return DownloadResult(success=True, path=str(file_path),
                                  layer_used="cache")

        for layer in ["direct", "mirror", "vpn"]:
            result = self._download_generic_file(url, file_path, layer)
            if result.success:
                return result

        return DownloadResult(
            success=False, path=str(file_path), layer_used="",
            error=f"All layers failed for {url}",
        )

    def download_dataset(
        self, dataset_path: str, subset: str | None = None,
    ) -> DownloadResult:
        """Download a HuggingFace dataset with three-layer fallback.

        Uses datasets.load_dataset() with cache_dir pointed at shared storage
        so SCO containers can load datasets offline.
        """
        dataset_cache_dir = self.cache_dir / "datasets"
        dataset_cache_dir.mkdir(parents=True, exist_ok=True)

        # Quick cache check: datasets stores at {cache_dir}/{sanitized}/...
        sanitized = dataset_path.replace("/", "--")
        if subset:
            sanitized = f"{sanitized}__{subset}"
        cached_dir = dataset_cache_dir / sanitized
        if cached_dir.exists() and any(cached_dir.iterdir()):
            logger.info("Dataset already cached: %s (subset=%s)", dataset_path, subset)
            return DownloadResult(success=True, path=str(cached_dir),
                                  layer_used="cache", model_id=dataset_path)

        for layer in ["direct", "mirror", "vpn"]:
            logger.info("Downloading dataset %s (subset=%s) via layer: %s",
                        dataset_path, subset, layer)
            result = self._download_hf_dataset(dataset_path, subset,
                                                dataset_cache_dir, layer)
            if result.success:
                return result
            logger.warning("Layer %s failed for dataset %s: %s",
                           layer, dataset_path, result.error)

        return DownloadResult(
            success=False, path=str(dataset_cache_dir), layer_used="",
            error=f"All three layers failed for dataset {dataset_path}",
            model_id=dataset_path,
        )

    # ------------------------------------------------------------------
    # Layer: Direct
    # ------------------------------------------------------------------

    def _download_hf_model(
        self, model_id: str, target_dir: Path, layer: str,
    ) -> DownloadResult:
        """Download a HuggingFace model using Python API."""
        self._setup_layer_env(layer)

        for attempt in range(self.retry_count + 1):
            try:
                from huggingface_hub import snapshot_download

                target_dir.mkdir(parents=True, exist_ok=True)
                snapshot_path = snapshot_download(
                    repo_id=model_id,
                    cache_dir=str(target_dir.parent),
                    local_dir=str(target_dir),
                    local_dir_use_symlinks=False,
                    resume_download=True,
                    max_workers=4,
                )
                if snapshot_path and target_dir.exists():
                    return DownloadResult(
                        success=True, path=str(target_dir),
                        layer_used=layer, model_id=model_id,
                    )
            except ImportError:
                # Fall back to git-based download
                return self._download_hf_git(model_id, target_dir, layer)
            except Exception as e:
                logger.debug("HF download attempt %d failed: %s", attempt + 1, e)
                if attempt < self.retry_count:
                    time.sleep(2 ** attempt)
                continue

        return DownloadResult(
            success=False, path=str(target_dir), layer_used=layer,
            error=f"HF Python download failed after {self.retry_count + 1} attempts",
            model_id=model_id,
        )

    def _download_hf_git(
        self, model_id: str, target_dir: Path, layer: str,
    ) -> DownloadResult:
        """Download via git LFS (fallback when huggingface_hub unavailable)."""
        try:
            url = f"https://huggingface.co/{model_id}"
            target_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", url, str(target_dir)],
                capture_output=True, text=True,
                timeout=self.timeout, check=True,
            )
            return DownloadResult(
                success=True, path=str(target_dir),
                layer_used=layer, model_id=model_id,
            )
        except subprocess.TimeoutExpired:
            return DownloadResult(
                success=False, error="Git clone timed out",
                layer_used=layer, model_id=model_id,
            )
        except Exception as e:
            return DownloadResult(
                success=False, error=str(e)[:200],
                layer_used=layer, model_id=model_id,
            )

    def _download_hf_dataset(
        self, dataset_path: str, subset: str | None,
        cache_dir: Path, layer: str,
    ) -> DownloadResult:
        """Download a HuggingFace dataset using datasets.load_dataset().

        Runs in a subprocess so that environment variables (proxy, HF_ENDPOINT)
        are picked up fresh by the datasets/huggingface_hub libraries.
        """
        import json as _json

        self._setup_layer_env(layer)

        # Build a self-contained Python script that loads the dataset.
        # Subprocess execution ensures env vars take effect for the
        # requests/urllib3 libraries (which cache env at import time).
        subset_arg = repr(subset)
        script = f'''
import os, json, sys
os.environ.update({_json.dumps(dict(os.environ))})
from datasets import load_dataset
try:
    ds = load_dataset(
        {repr(dataset_path)},
        name={subset_arg},
        cache_dir={repr(str(cache_dir))},
    )
    if ds is not None:
        print("OK:" + str(len(ds)))
    else:
        print("FAIL:None")
except Exception as e:
    print("FAIL:" + str(e))
'''

        for attempt in range(self.retry_count + 1):
            try:
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    capture_output=True, text=True,
                    timeout=self.timeout,
                    env=os.environ.copy(),
                )
                output = result.stdout.strip()
                if output.startswith("OK:"):
                    return DownloadResult(
                        success=True,
                        path=str(cache_dir),
                        layer_used=layer,
                        model_id=dataset_path,
                    )
                else:
                    error_msg = output.replace("FAIL:", "", 1).strip()[:200]
                    logger.debug("Dataset download attempt %d: %s", attempt + 1, error_msg)
            except subprocess.TimeoutExpired:
                logger.debug("Dataset download attempt %d timed out", attempt + 1)
            except Exception as e:
                logger.debug("Dataset download attempt %d failed: %s", attempt + 1, e)

            if attempt < self.retry_count:
                time.sleep(2 ** attempt)

        return DownloadResult(
            success=False, path=str(cache_dir), layer_used=layer,
            error=f"Dataset download failed after {self.retry_count + 1} attempts",
            model_id=dataset_path,
        )

    # ------------------------------------------------------------------
    # Layer: Mirror (hf-mirror.com → ModelScope)
    # ------------------------------------------------------------------

    def _download_generic_file(
        self, url: str, file_path: Path, layer: str,
    ) -> DownloadResult:
        """Download a generic file using wget/curl."""
        self._setup_layer_env(layer)

        # If mirror layer, try to rewrite URL
        download_url = url
        if layer == "mirror":
            download_url = self._mirror_url(url)

        for attempt in range(self.retry_count + 1):
            try:
                # Prefer wget for resume capability
                result = subprocess.run(
                    ["wget", "-q", "--show-progress", "-c",
                     "-O", str(file_path), download_url],
                    capture_output=True, text=True,
                    timeout=self.timeout,
                )
                if result.returncode == 0 and file_path.exists():
                    return DownloadResult(
                        success=True, path=str(file_path), layer_used=layer,
                    )
            except subprocess.TimeoutExpired:
                logger.debug("wget timed out for %s", download_url)
            except FileNotFoundError:
                # wget not available, try curl
                pass

            # Fallback: Python urllib
            try:
                import urllib.request
                req = urllib.request.Request(download_url)
                req.add_header("User-Agent", "ChenResearch/1.0")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    file_path.write_bytes(resp.read())
                if file_path.exists():
                    return DownloadResult(
                        success=True, path=str(file_path), layer_used=layer,
                    )
            except Exception as e:
                logger.debug("urllib attempt %d failed: %s", attempt + 1, e)

            if attempt < self.retry_count:
                time.sleep(2 ** attempt)

        return DownloadResult(
            success=False, path=str(file_path), layer_used=layer,
            error=f"Download failed after {self.retry_count + 1} attempts",
        )

    # ------------------------------------------------------------------
    # Environment setup per layer
    # ------------------------------------------------------------------

    def _setup_layer_env(self, layer: str) -> None:
        """Configure environment variables and proxy for each layer."""
        # Clear any existing proxy and HF endpoint vars
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                     "all_proxy", "ALL_PROXY"):
            os.environ.pop(var, None)
        for var in ("HF_ENDPOINT", "HF_HUB_ENDPOINT", "HF_HUB_DOWNLOAD_ENDPOINT"):
            os.environ.pop(var, None)

        if layer == "direct":
            # Ensure clean environment — direct connection (no HF endpoint set)
            pass

        elif layer == "mirror":
            # Set HuggingFace mirror endpoint (both old and new env var names)
            mirror_url = self.HF_MIRRORS[0]
            os.environ["HF_ENDPOINT"] = mirror_url
            os.environ["HF_HUB_ENDPOINT"] = mirror_url
            # Also route through VPN if available (mirrors may need proxy)
            if self.vpn_script.exists():
                self._ensure_vpn()
                os.environ["http_proxy"] = self.proxy_url
                os.environ["https_proxy"] = self.proxy_url
                os.environ["HTTP_PROXY"] = self.proxy_url
                os.environ["HTTPS_PROXY"] = self.proxy_url

        elif layer == "vpn":
            self._ensure_vpn()
            os.environ["http_proxy"] = self.proxy_url
            os.environ["https_proxy"] = self.proxy_url
            os.environ["HTTP_PROXY"] = self.proxy_url
            os.environ["HTTPS_PROXY"] = self.proxy_url

    def _ensure_vpn(self) -> bool:
        """Start the VPN proxy daemon if not already running."""
        if not self.vpn_script.exists():
            logger.warning("VPN script not found: %s", self.vpn_script)
            return False

        try:
            result = subprocess.run(
                ["bash", str(self.vpn_script), "ensure"],
                capture_output=True, text=True,
                timeout=30,
            )
            if result.returncode == 0:
                logger.info("VPN proxy ready")
                return True
            logger.warning("VPN ensure failed: %s", result.stderr[:200])
            return False
        except subprocess.TimeoutExpired:
            logger.warning("VPN startup timed out")
            return False
        except Exception as e:
            logger.warning("VPN error: %s", e)
            return False

    def _mirror_url(self, url: str) -> str:
        """Rewrite a URL to use mirror endpoints."""
        # HuggingFace → hf-mirror.com
        if "huggingface.co" in url:
            for mirror in self.HF_MIRRORS:
                rewritten = url.replace("https://huggingface.co", mirror)
                return rewritten
        # GitHub raw → mirror
        if "raw.githubusercontent.com" in url:
            return url  # GitHub raw usually accessible
        return url

    # ------------------------------------------------------------------
    # ModelScope backend (alternative to HuggingFace)
    # ------------------------------------------------------------------

    def download_from_modelscope(self, model_id: str) -> DownloadResult:
        """Download a model from ModelScope as an alternative backend."""
        model_dir = self.cache_dir / "models" / f"ms--{model_id.replace('/', '--')}"
        if model_dir.exists() and any(model_dir.iterdir()):
            return DownloadResult(success=True, path=str(model_dir),
                                  layer_used="cache", model_id=model_id)

        try:
            from modelscope import snapshot_download as ms_snapshot

            model_dir.mkdir(parents=True, exist_ok=True)
            ms_snapshot(
                model_id=model_id,
                cache_dir=str(model_dir.parent),
                local_dir=str(model_dir),
            )
            return DownloadResult(
                success=True, path=str(model_dir),
                layer_used="modelscope", model_id=model_id,
            )
        except ImportError:
            return DownloadResult(
                success=False, error="modelscope package not installed",
                model_id=model_id,
            )
        except Exception as e:
            return DownloadResult(
                success=False, error=str(e)[:200],
                model_id=model_id,
            )

    def cache_status(self) -> dict:
        """Report what's currently in the shared cache.

        Returns a dict with counts and sizes per category (models, datasets, wheels).
        All download_*() methods use this cache — they check here first before
        attempting any network download.
        """
        status = {"models": {"count": 0, "size_mb": 0}, "datasets": {"count": 0, "size_mb": 0}, "wheels": {"count": 0, "size_mb": 0}}
        for category, dir_name in [("models", "models"), ("datasets", "datasets")]:
            cat_dir = self.cache_dir / dir_name
            if cat_dir.exists():
                entries = [p for p in cat_dir.iterdir() if p.is_dir()]
                status[category]["count"] = len(entries)
                total = sum(sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) for d in entries)
                status[category]["size_mb"] = round(total / (1024 * 1024), 1)
        wheels_dir = self.cache_dir / "wheels"
        if wheels_dir.exists():
            whls = list(wheels_dir.glob("*.whl"))
            status["wheels"]["count"] = len(whls)
            total = sum(f.stat().st_size for f in whls)
            status["wheels"]["size_mb"] = round(total / (1024 * 1024), 1)
        return status

    @staticmethod
    def print_cache_status(cache_dir: str | Path | None = None) -> None:
        """Print a human-readable cache status report.

        Can be called standalone:
            python model_downloader.py --cache-status
        """
        dl = ThreeLayerDownloader(cache_dir=str(cache_dir) if cache_dir else "")
        status = dl.cache_status()
        print("Shared cache: " + str(dl.cache_dir))
        for cat, info in status.items():
            count = info["count"]
            size = info["size_mb"]
            if count:
                print(f"  {cat}: {count} items, {size} MB")
            else:
                print(f"  {cat}: (empty)")

    # ------------------------------------------------------------------
    # Bulk download pre-check (for SCO submission)
    # ------------------------------------------------------------------

    def pre_cache_from_script(self, script_path: str | Path) -> list[DownloadResult]:
        """Scan a run_experiment.sh for download_hf_model calls and pre-download.

        Call this before SCO submission so models are in shared storage.

        Resolves shell variables by scanning for VAR=VALUE assignments first,
        then substituting them in matched model references.
        """
        script = Path(script_path)
        if not script.exists():
            return []

        content = script.read_text()

        # ── 1. Extract shell variable assignments ──
        import re as _re
        shell_vars: dict[str, str] = {}
        for m in _re.finditer(r'^(\w+)=["\']?([^"\'\n]+)["\']?\s*(?:#.*)?$', content, _re.MULTILINE):
            shell_vars[m.group(1)] = m.group(2).strip()

        def _resolve(raw: str) -> str | None:
            """Resolve a potentially shell-variable reference to a concrete model ID."""
            # Shell variable reference like ${T5_BASE} or $T5_BASE
            m = _re.match(r'^\$\{?(\w+)\}?$', raw)
            if m:
                var_name = m.group(1)
                # Variable names used as function parameters (not real env vars)
                if var_name in ("MODEL_ID", "model_id", "MODEL", "model"):
                    return None
                return shell_vars.get(var_name)
            # Already a concrete model ID (contains /)
            if "/" in raw and not raw.startswith("$"):
                return raw
            return None

        # ── 2. Scan for model references ──
        model_ids: set[str] = set()

        for pat in [
            r'download_hf_model\s+["\']([^"\']+)["\']',
            r'download_model\.sh\s+.*--model\s+["\']([^"\']+)["\']',
            r'huggingface\.co/([^/\s]+/[^/\s"]+)',
            r'modelscope\.cn/([^/\s]+/[^/\s"]+)',
        ]:
            for m in _re.finditer(pat, content):
                raw = m.group(1)
                # Skip pip/install flags and shell variable references
                if raw.startswith("-") or raw.startswith("$") or "=" in raw:
                    continue
                resolved = _resolve(raw)
                if resolved:
                    model_ids.add(resolved)

        # ── 3. Also scan for direct model string assignments ──
        # e.g. T5_BASE="google-t5/t5-base"
        for m in _re.finditer(r'(\w+)=["\']([^"\']+/[^"\']+)["\']', content):
            val = m.group(2)
            if "/" in val and not val.startswith("$") and not val.startswith("/"):
                # Skip pip/python command fragments
                if any(kw in val.lower() for kw in ["find-links", "pip ", "index-url", "trusted-host"]):
                    continue
                model_ids.add(val)

        results = []
        for mid in sorted(model_ids):
            logger.info("Pre-caching model: %s", mid)
            if "modelscope" in mid.lower() or "/" not in mid:
                result = self.download_from_modelscope(mid)
            else:
                result = self.download_model(mid)
            results.append(result)

        return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Download models/datasets with three-layer fallback",
    )
    parser.add_argument("model_id", nargs="?", help="HuggingFace model ID")
    parser.add_argument("--file", help="Download a generic file URL")
    parser.add_argument("--dataset", help="Download a HuggingFace dataset")
    parser.add_argument("--dataset-subset", help="Subset/config name for the dataset")
    parser.add_argument("--cache-dir", help="Cache directory override")
    parser.add_argument("--cache-status", action="store_true", help="Print cache inventory")
    parser.add_argument("--scan-script", help="Pre-cache models referenced in script")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    kwargs: dict[str, Any] = {}
    if args.cache_dir:
        kwargs["cache_dir"] = args.cache_dir
    if args.timeout:
        kwargs["timeout"] = args.timeout

    dl = ThreeLayerDownloader(**kwargs)

    if args.cache_status:
        dl.print_cache_status(args.cache_dir)
        return

    if args.scan_script:
        results = dl.pre_cache_from_script(args.scan_script)
        for r in results:
            status = "OK" if r.success else "FAIL"
            print(f"[{status}] {r.model_id} via {r.layer_used or 'none'}")
            if r.error:
                print(f"  Error: {r.error}")
        return

    if args.file:
        result = dl.download_file(args.file)
        print(f"[{'OK' if result.success else 'FAIL'}] {args.file}")
        print(f"  Layer: {result.layer_used}, Path: {result.path}")
        if result.error:
            print(f"  Error: {result.error}")
        return

    if args.dataset:
        result = dl.download_dataset(args.dataset, args.dataset_subset)
        print(f"[{'OK' if result.success else 'FAIL'}] dataset={args.dataset} "
              f"subset={args.dataset_subset or 'none'}")
        print(f"  Layer: {result.layer_used}, Path: {result.path}")
        if result.error:
            print(f"  Error: {result.error}")
        return

    if not args.model_id:
        parser.error("Either model_id, --file, --dataset, or --scan-script is required")

    result = dl.download_model(args.model_id)
    print(f"[{'OK' if result.success else 'FAIL'}] {args.model_id}")
    print(f"  Layer: {result.layer_used}, Path: {result.path}")
    if result.error:
        print(f"  Error: {result.error}")


if __name__ == "__main__":
    main()
