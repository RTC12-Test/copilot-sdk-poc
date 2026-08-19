"""
Self-Healing Build Pipeline — GitHub Copilot SDK + PyGitHub

Scans client_code/ for ALL .py files with syntax errors,
uses Copilot SDK to generate fixes, opens ONE PR with all fixes.

Usage:
  Set environment variables:
    GITHUB_TOKEN  - GitHub PAT with repo + workflow permissions
    GH_TOKEN      - GitHub OAuth token for Copilot SDK (gho_...)
    GITHUB_REPO   - owner/repo to monitor

  python src/healer.py
"""

import os
import re
import ast
import time
import asyncio
import logging
from pathlib import Path
from typing import Optional

from github import Github, Auth, GithubException
from copilot import CopilotClient
from copilot.session import PermissionHandler
from copilot.session_events import AssistantMessageData, SessionIdleData

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("self-healer")

SCAN_DIR = "client_code"


# ---------------------------------------------------------------------------
# Local file scanning — find Python syntax errors in ALL .py files
# ---------------------------------------------------------------------------

def scan_python_files(directory: str) -> list[dict]:
    """
    Scan directory for ALL .py files — syntax errors AND runtime errors.
    Returns list of { file, line, message, content } for files with errors.
    """
    import subprocess

    results = []
    seen = set()
    py_files = list(Path(directory).glob("**/*.py"))
    logger.info("Scanned %d .py file(s) in %s/", len(py_files), directory)

    for filepath in py_files:
        rel_path = str(filepath)
        with open(filepath) as f:
            source = f.read()

        # 1. Check syntax
        try:
            ast.parse(source, filename=rel_path)
        except SyntaxError as e:
            if rel_path not in seen:
                results.append({
                    "file": rel_path,
                    "line": e.lineno or 1,
                    "message": f"{e.msg}",
                    "content": source,
                })
                seen.add(rel_path)
            continue

        # 2. Check runtime errors by actually running the file
        proc = subprocess.run(
            ["python3", str(filepath)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip()
            err_match = re.search(r"(\w+Error|\w+Exception):\s*(.+)", err)
            line_match = re.search(r'File "[^"]+", line (\d+)', err)

            if err_match:
                error_type = err_match.group(1)
                error_msg = err_match.group(2)
                line_num = int(line_match.group(1)) if line_match else 1

                if rel_path not in seen:
                    results.append({
                        "file": rel_path,
                        "line": line_num,
                        "message": f"{error_type}: {error_msg}",
                        "content": source,
                    })
                    seen.add(rel_path)

    return results


# ---------------------------------------------------------------------------
# Copilot-powered fix generation
# ---------------------------------------------------------------------------

def _build_fix_prompt(file_path: str, file_content: str, errors: list[dict]) -> str:
    error_block = "\n".join(
        f"  - line {e['line']}: {e['message']}" for e in errors
    )
    return f"""\
The following Python file has syntax errors. Fix ALL errors and return the
complete corrected file content. Return ONLY the file content — no markdown
fences, no explanation, no commentary.

File: {file_path}
Errors:
{error_block}

Current file content:
```python
{file_content}
```

Return the complete corrected file content."""


async def copilot_fix_all(
    file_errors: dict[str, tuple[str, list[dict]]],
    oauth_token: str,
) -> dict[str, str]:
    """
    Fix all files in one Copilot session.
    file_errors = { file_path: (file_content, errors) }
    Returns { file_path: fixed_content } for successful fixes only.
    """
    fixes = {}

    async with CopilotClient(github_token=oauth_token) as client:
        async with await client.create_session(
            on_permission_request=PermissionHandler.approve_all,
            model="gpt-5",
        ) as session:

            for file_path, (file_content, errors) in file_errors.items():
                prompt = _build_fix_prompt(file_path, file_content, errors)
                done = asyncio.Event()
                response_text = ""

                def on_event(event):
                    nonlocal response_text
                    match event.data:
                        case AssistantMessageData() as data:
                            response_text = data.content
                        case SessionIdleData():
                            done.set()

                session.on(on_event)
                await session.send(prompt)
                await done.wait()

                cleaned = response_text.strip()
                if cleaned.startswith("```"):
                    cleaned = re.sub(r"^```\w*\n?", "", cleaned)
                    cleaned = re.sub(r"\n?```\s*$", "", cleaned)

                if not cleaned:
                    logger.warning("Copilot returned empty fix for %s — skipping", file_path)
                    continue

                if cleaned.strip() == file_content.strip():
                    logger.warning("Copilot returned UNCHANGED content for %s — skipping", file_path)
                    continue

                try:
                    ast.parse(cleaned)
                    logger.info("Fix verified OK for %s", file_path)
                    fixes[file_path] = cleaned
                except SyntaxError as e:
                    logger.warning("Copilot fix STILL HAS ERRORS in %s: %s — skipping", file_path, e)

    return fixes


# ---------------------------------------------------------------------------
# GitHub interaction (PyGitHub) — single PR with all fixes
# ---------------------------------------------------------------------------

class BuildHealer:
    def __init__(self, token: str, oauth_token: str, repo_name: str):
        self.token = token
        self.oauth_token = oauth_token
        self.github = Github(auth=Auth.Token(token))
        self.repo = self.github.get_repo(repo_name)
        logger.info("Initialised healer for repo %s", repo_name)

    def raise_fix_pr(self, fixes: dict[str, str], all_errors: list[dict], source_branch: str = None) -> str:
        """
        Create one branch, commit all fixes, open one PR.
        fixes = { file_path: new_content }
        source_branch = the branch that failed CI (e.g., feature/test)
        """
        repo = self.repo
        target_branch = source_branch or repo.default_branch
        base = repo.get_branch(target_branch)

        branch = f"self-heal/run-{int(time.time())}"
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base.commit.sha)

        for path, new_content in fixes.items():
            try:
                file_data = repo.get_contents(path, ref=branch)
                if isinstance(file_data, list):
                    continue
                repo.update_file(
                    path=path,
                    message=f"self-heal: fix syntax error in {path}",
                    content=new_content,
                    sha=file_data.sha,
                    branch=branch,
                )
            except GithubException:
                repo.create_file(
                    path=path,
                    message=f"self-heal: fix syntax error in {path}",
                    content=new_content,
                    branch=branch,
                )

        files_list = "\n".join(f"- `{p}`" for p in fixes.keys())
        error_summary = "\n".join(
            f"- `{e['file']}:{e['line']}` — {e['message']}" for e in all_errors
        )

        pr = repo.create_pull(
            title=f"[Self-Heal] Fix syntax errors in {len(fixes)} file(s)",
            body=(
                f"## Automated Remediation\n\n"
                f"**Fixed files:**\n{files_list}\n\n"
                f"**Errors:**\n{error_summary}\n\n"
                f"Fix generated by **GitHub Copilot SDK** and auto-committed."
            ),
            head=branch,
            base=target_branch,
        )

        logger.info("Created PR: %s", pr.html_url)
        return pr.html_url

    def run(self):
        """Scan ALL .py files → Copilot fix each → ONE PR with all fixes."""
        logger.info("Scanning %s/ for ALL Python files...", SCAN_DIR)

        errors = scan_python_files(SCAN_DIR)
        if not errors:
            logger.info("No syntax errors found. All files OK!")
            return

        logger.info("Found %d file(s) with errors:", len(errors))
        for e in errors:
            logger.info("  %s:%d — %s", e["file"], e["line"], e["message"])

        file_errors = {}
        for error in errors:
            file_errors[error["file"]] = (error["content"], [error])

        fixes = asyncio.run(copilot_fix_all(file_errors, self.oauth_token))

        if not fixes:
            logger.warning("No fixes generated. Nothing to commit.")
            return

        source_branch = os.environ.get("SOURCE_BRANCH")
        logger.info("Creating single PR with %d fixed file(s) targeting %s...", len(fixes), source_branch or "default")
        pr_url = self.raise_fix_pr(fixes, errors, source_branch)
        logger.info("Done! PR: %s", pr_url)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    token = os.environ.get("GITHUB_TOKEN")
    oauth_token = os.environ.get("GH_TOKEN")
    repo_name = os.environ.get("GITHUB_REPO")

    if not token or not oauth_token or not repo_name:
        logger.error("Set GITHUB_TOKEN, GH_TOKEN, and GITHUB_REPO environment variables.")
        raise SystemExit(1)

    healer = BuildHealer(token, oauth_token, repo_name)
    healer.run()
