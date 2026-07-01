#!/usr/bin/env python3
"""
Baseline GitHub repository finder — clones reference implementations for
structural context that informs LLM experiment design.

Does NOT run baseline code. Extracts file tree, README, requirements, and key
imports so the LLM can design fair experiments with awareness of real codebases.

Usage:
    from baseline_finder import BaselineFinder

    bf = BaselineFinder(cache_dir="workspace/.shared/baselines")
    contexts = bf.find_and_extract(paper_entries, method_names)
    prompt_block = bf.format_for_prompt(contexts)
"""

from __future__ import annotations

import os
import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("baseline_finder")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class BaselineSource:
    """Origin of a found baseline repository."""
    method_name: str
    paper_title: str
    repo_url: str
    source: str = ""  # "paper_code_url", "github_search", "paperswithcode"


@dataclass
class BaselineContext:
    """Extracted structural context from a cloned baseline repo."""
    method_name: str
    repo_url: str
    slug: str
    local_path: str = ""     # filesystem path where repo is cloned
    file_tree: str = ""
    readme_summary: str = ""
    requirements: str = ""
    key_imports: str = ""
    entry_points: str = ""  # main scripts or __main__ modules
    error: str = ""


# ---------------------------------------------------------------------------
# Baseline Finder
# ---------------------------------------------------------------------------


class BaselineFinder:
    """Search, clone, and extract structural context from baseline repos."""

    SEARCH_CACHE_FILE = "baseline_search_cache.json"
    SEARCH_CACHE_TTL = 7 * 86400  # 7 days in seconds

    GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"

    # Known GitHub mirrors (tried in order when direct clone fails)
    _GIT_MIRRORS: list[str] = []

    def __init__(
        self,
        cache_dir: str | Path = "",
        max_repos: int = 5,
        clone_timeout: int = 60,
        github_token: str = "",
    ):
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        else:
            from config import BASELINE_CACHE_DIR
            self.cache_dir = Path(BASELINE_CACHE_DIR) if BASELINE_CACHE_DIR else Path("workspace/.shared/baselines")

        self.max_repos = max_repos
        self.clone_timeout = clone_timeout
        self.github_token = github_token or self._load_github_token()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Build mirror list: env override + built-in fallbacks
        custom_mirror = os.environ.get("GIT_MIRROR_PREFIX", "")
        self._GIT_MIRRORS = []
        if custom_mirror:
            self._GIT_MIRRORS.append(custom_mirror)
        self._GIT_MIRRORS.extend([
            "https://gitclone.com/github.com",
            "https://ghproxy.com/https://github.com",
        ])

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def find_and_extract(
        self,
        papers: list[Any] = None,
        method_names: list[str] = None,
    ) -> list[BaselineContext]:
        """Find baseline repos and extract structural context.

        Args:
            papers: PaperEntry objects (with optional code_url).
            method_names: Baseline method names to search for.

        Returns:
            List of BaselineContext for each successfully cloned repo.
        """
        papers = papers or []
        method_names = method_names or []

        sources = self._find_baselines(papers, method_names)
        contexts: list[BaselineContext] = []

        for src in sources:
            slug = self._repo_slug(src.repo_url, src.method_name)
            target_dir = self.cache_dir / slug

            # Only use cache if it contains files beyond .git (failed clones
            # often leave an empty directory or a bare .git with no source).
            if target_dir.exists() and any(
                p.name != ".git" for p in target_dir.iterdir()
            ):
                logger.info("Using cached repo: %s", target_dir)
            else:
                # Clean up empty/stale directory before cloning
                if target_dir.exists():
                    import shutil
                    shutil.rmtree(target_dir, ignore_errors=True)
                ok = self._clone_repo(src.repo_url, target_dir)
                if not ok:
                    continue

            ctx = self._extract_context(target_dir, src)
            contexts.append(ctx)

        return contexts

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _find_baselines(
        self, papers: list[Any], method_names: list[str]
    ) -> list[BaselineSource]:
        """Find GitHub repos for baseline methods.

        Priority order:
        1. code_url already on the PaperEntry
        2. GitHub API search
        3. PapersWithCode-style heuristic
        """
        found: dict[str, BaselineSource] = {}  # keyed by lower method name

        # 1. Direct code URLs from paper entries
        for p in papers:
            code_url = getattr(p, "code_url", "")
            if not code_url:
                continue
            method = getattr(p, "title", "") or ""
            # Try to extract method name from paper title
            method_short = self._guess_method_name(method, method_names)
            if not method_short:
                continue
            key = method_short.lower()
            if key not in found:
                found[key] = BaselineSource(
                    method_name=method_short,
                    paper_title=getattr(p, "title", ""),
                    repo_url=code_url,
                    source="paper_code_url",
                )
            if len(found) >= self.max_repos:
                return list(found.values())

        # 2. GitHub API search for remaining methods
        for name in method_names:
            key = name.lower()
            if key in found:
                continue
            url = self._github_search(name)
            if url:
                found[key] = BaselineSource(
                    method_name=name,
                    paper_title="",
                    repo_url=url,
                    source="github_search",
                )
            if len(found) >= self.max_repos:
                break

        # 3. PapersWithCode heuristic (construct URL pattern)
        for name in method_names:
            key = name.lower()
            if key in found:
                continue
            url = self._paperswithcode_guess(name)
            if url:
                found[key] = BaselineSource(
                    method_name=name,
                    paper_title="",
                    repo_url=url,
                    source="paperswithcode",
                )
            if len(found) >= self.max_repos:
                break

        return list(found.values())

    def _github_search(self, method_name: str) -> str:
        """Search GitHub for a method's implementation repo."""
        cache = self._load_search_cache()
        cache_key = f"github:{method_name.lower()}"
        if cache_key in cache:
            entry = cache[cache_key]
            if time.time() - entry.get("ts", 0) < self.SEARCH_CACHE_TTL:
                return entry.get("url", "")

        # Try GitHub API
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        queries = [
            f"{method_name} implementation",
            f"{method_name} pytorch",
            f"{method_name} official",
        ]

        for query in queries:
            try:
                import urllib.request
                import urllib.parse

                params = urllib.parse.urlencode({
                    "q": query,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": "3",
                })
                req = urllib.request.Request(
                    f"{self.GITHUB_SEARCH_URL}?{params}", headers=headers
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                    items = data.get("items", [])
                    if items:
                        url = items[0]["html_url"]
                        cache[cache_key] = {"url": url, "ts": time.time()}
                        self._save_search_cache(cache)
                        return url
            except Exception as e:
                logger.debug("GitHub search failed for %s: %s", query, e)
                continue

        return ""

    def _paperswithcode_guess(self, method_name: str) -> str:
        """Construct a best-guess GitHub URL from method name.

        Checks common naming patterns: MethodName-official, MethodName-pytorch, etc.
        Uses `gh search repos` CLI or a HEAD request to verify existence.
        """
        slug = re.sub(r'[^a-zA-Z0-9]', '', method_name).lower()
        candidates = [
            f"https://github.com/{slug}/{slug}",
        ]

        # Try to verify with a quick HEAD request
        for url in candidates:
            try:
                import urllib.request
                req = urllib.request.Request(url)
                req.get_method = lambda: "HEAD"
                urllib.request.urlopen(req, timeout=5)
                return url
            except Exception:
                continue

        return ""

    def _guess_method_name(self, paper_title: str, method_names: list[str]) -> str:
        """Try to map a paper title to one of the known method names."""
        title_lower = paper_title.lower()
        for name in method_names:
            if name.lower() in title_lower:
                return name
        # Return first word group as fallback
        return ""

    # ------------------------------------------------------------------
    # Clone
    # ------------------------------------------------------------------

    @staticmethod
    def _is_valid_clone(target_dir: Path) -> bool:
        """Check that a cloned dir has real source files, not just .git."""
        return target_dir.exists() and any(
            p.name != ".git" for p in target_dir.iterdir()
        )

    def _clone_repo(self, url: str, target_dir: Path) -> bool:
        """Shallow clone with mirror fallback + integrity check.

        Cleans partial clones before each retry.  Verifies the result has
        source files (not just a bare .git from a timed-out clone)."""
        urls_to_try = [url]
        if "github.com" in url:
            for mirror in self._GIT_MIRRORS:
                urls_to_try.append(url.replace("https://github.com", mirror))
        import shutil
        for attempt_url in urls_to_try:
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", "--single-branch",
                     "--no-tags", attempt_url, str(target_dir)],
                    capture_output=True, text=True, timeout=self.clone_timeout,
                    check=True,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
                if self._is_valid_clone(target_dir):
                    logger.info("Cloned %s → %s", attempt_url, target_dir)
                    return True
                else:
                    logger.warning("Clone incomplete (no source files): %s", attempt_url)
                    shutil.rmtree(target_dir, ignore_errors=True)
            except subprocess.TimeoutExpired:
                logger.warning("Clone timed out (%ss): %s", self.clone_timeout, attempt_url)
            except subprocess.CalledProcessError as e:
                logger.warning("Clone failed: %s — %s", attempt_url, e.stderr[:150])
        return False

    # ------------------------------------------------------------------
    # Extract structural context
    # ------------------------------------------------------------------

    def _extract_context(
        self, repo_dir: Path, source: BaselineSource
    ) -> BaselineContext:
        """Extract file tree, README, requirements, and key imports."""
        slug = self._repo_slug(source.repo_url, source.method_name)
        ctx = BaselineContext(
            method_name=source.method_name,
            repo_url=source.repo_url,
            slug=slug,
            local_path=str(repo_dir),
        )

        try:
            ctx.file_tree = self._get_file_tree(repo_dir)
        except Exception as e:
            ctx.error = str(e)[:200]

        try:
            ctx.readme_summary = self._get_readme_summary(repo_dir)
        except Exception:
            pass

        try:
            ctx.requirements = self._get_requirements(repo_dir)
        except Exception:
            pass

        try:
            ctx.key_imports = self._get_key_imports(repo_dir)
        except Exception:
            pass

        try:
            ctx.entry_points = self._get_entry_points(repo_dir)
        except Exception:
            pass

        return ctx

    def _get_file_tree(self, repo_dir: Path, max_depth: int = 3) -> str:
        """Get a condensed file tree of the repo."""
        try:
            result = subprocess.run(
                ["find", str(repo_dir), "-maxdepth", str(max_depth),
                 "-not", "-path", "*/.git/*", "-not", "-path", "*/__pycache__/*",
                 "-not", "-path", "*.egg-info*"],
                capture_output=True, text=True, timeout=10,
            )
            lines = result.stdout.strip().split("\n")
            # Truncate to reasonable size
            return "\n".join(lines[:200])
        except Exception:
            return ""

    def _get_readme_summary(self, repo_dir: Path) -> str:
        """Extract and summarize the README file."""
        for name in ["README.md", "README.rst", "readme.md", "Readme.md"]:
            readme = repo_dir / name
            if readme.exists():
                text = readme.read_text()[:3000]
                # Extract key sections: title, description, installation, usage
                summary_parts = []
                for line in text.split("\n")[:60]:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("<!--"):
                        summary_parts.append(stripped)
                return "\n".join(summary_parts[:40])
        return ""

    def _get_requirements(self, repo_dir: Path) -> str:
        """Extract dependency info."""
        for name in ["requirements.txt", "requirements/dev.txt",
                      "setup.py", "pyproject.toml", "environment.yml"]:
            req_file = repo_dir / name
            if req_file.exists():
                content = req_file.read_text()
                if name == "pyproject.toml":
                    # Extract just the dependencies section
                    m = re.search(
                        r'(\[tool\.poetry\.dependencies\].*?)(\n\[|\Z)',
                        content, re.DOTALL,
                    )
                    if m:
                        return m.group(1)[:1000]
                return content[:1200]
        return ""

    def _get_key_imports(self, repo_dir: Path) -> str:
        """Extract key import statements from Python files."""
        imports: set[str] = set()
        for py_file in list(repo_dir.glob("*.py"))[:10] + list(repo_dir.glob("*/*.py"))[:10]:
            try:
                for line in py_file.read_text().split("\n")[:100]:
                    stripped = line.strip()
                    if stripped.startswith(("import ", "from ")) and len(stripped) < 120:
                        imports.add(stripped)
            except Exception:
                pass
        return "\n".join(sorted(imports)[:40])

    def _get_entry_points(self, repo_dir: Path) -> str:
        """Identify main entry-point scripts."""
        entry_candidates = [
            "main.py", "train.py", "run.py", "__main__.py",
            "train_dqn.py", "train_agent.py", "agent.py",
        ]
        entries = []
        for name in entry_candidates:
            for path in repo_dir.glob(f"**/{name}"):
                if ".git" not in str(path):
                    entries.append(str(path.relative_to(repo_dir)))
        return "\n".join(entries[:10])

    # ------------------------------------------------------------------
    # Format for prompt injection
    # ------------------------------------------------------------------

    def format_for_prompt(self, contexts: list[BaselineContext]) -> str:
        """Format baseline contexts as a Markdown block for LLM prompts.

        Output is capped at 8000 chars to fit within prompt budget.
        """
        if not contexts:
            return ""

        blocks: list[str] = []
        total_chars = 0
        max_chars = 8000

        for ctx in contexts:
            header = f"### {ctx.method_name} — {ctx.repo_url}"
            parts = [header, "", f"**本地路径**: `{ctx.local_path}`", ""]

            if ctx.entry_points:
                parts.append(f"**Entry points**: {ctx.entry_points}")
                parts.append("")

            if ctx.requirements:
                req_short = ctx.requirements[:600]
                parts.append(f"**Dependencies**:\n```\n{req_short}\n```")
                parts.append("")

            if ctx.key_imports:
                imports_short = ctx.key_imports[:500]
                parts.append(f"**Key imports**:\n```python\n{imports_short}\n```")
                parts.append("")

            if ctx.file_tree:
                tree_short = ctx.file_tree[:800]
                parts.append(f"**File tree**:\n```\n{tree_short}\n```")
                parts.append("")

            if ctx.readme_summary:
                readme_short = ctx.readme_summary[:500]
                parts.append(f"**README excerpt**:\n{readme_short}")
                parts.append("")

            if ctx.error:
                parts.append(f"**Note**: {ctx.error}")
                parts.append("")

            block = "\n".join(parts)
            if total_chars + len(block) > max_chars:
                # Add truncated notice and stop
                blocks.append("\n*(Baseline contexts truncated — reached prompt budget)*")
                break

            blocks.append(block)
            total_chars += len(block)

        blocks.insert(0, (
            "## Baseline Implementation References\n\n"
            "The following are structural references from real baseline repositories. "
            "Use these to inform experiment design:\n"
        ))

        return "\n---\n".join(blocks)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _repo_slug(self, url: str, method_name: str) -> str:
        """Generate a filesystem-safe slug for a repo."""
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', method_name.lower())[:30]
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        return f"{safe_name}_{url_hash}"

    def _load_github_token(self) -> str:
        """Load GitHub token from config or env."""
        try:
            from config import GITHUB_API_TOKEN
            if GITHUB_API_TOKEN:
                return GITHUB_API_TOKEN
        except ImportError:
            pass
        import os
        return os.environ.get("GITHUB_API_TOKEN", "")

    def _load_search_cache(self) -> dict:
        """Load the search cache with TTL awareness."""
        cache_path = self.cache_dir / self.SEARCH_CACHE_FILE
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_search_cache(self, cache: dict) -> None:
        """Save the search cache."""
        cache_path = self.cache_dir / self.SEARCH_CACHE_FILE
        # Prune expired entries
        now = time.time()
        cache = {
            k: v for k, v in cache.items()
            if now - v.get("ts", 0) < self.SEARCH_CACHE_TTL
        }
        try:
            cache_path.write_text(json.dumps(cache, indent=2))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Find and clone baseline repos for structural reference",
    )
    parser.add_argument("--methods", nargs="*", help="Baseline method names")
    parser.add_argument("--cache-dir", default="workspace/.shared/baselines",
                        help="Cache directory for cloned repos")
    parser.add_argument("--max", type=int, default=5, help="Max repos to clone")
    parser.add_argument("--output", "-o", help="Write baseline context to file")
    args = parser.parse_args()

    bf = BaselineFinder(
        cache_dir=args.cache_dir,
        max_repos=args.max,
    )

    contexts = bf.find_and_extract(method_names=args.methods or [])
    prompt_block = bf.format_for_prompt(contexts)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(prompt_block)
        print(f"Baseline context saved to {args.output} ({len(prompt_block)} chars)")
    else:
        print(prompt_block)

    if not contexts:
        print("\nNo baseline repos found — experiment design will use literature only.")


if __name__ == "__main__":
    main()
