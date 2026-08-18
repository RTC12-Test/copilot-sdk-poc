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
from copilot.session_events import SessionEventType

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("self-healer")

SCAN_DIR = "client_code"


# ---------------------------------------------------------------------------
# Local file scanning — find Python syntax errors in ALL .py files
# ---------------------------------------------------------------------------

def scan_python_files(directory: str) -> list[dict]:
    """
    Scan directory for ALL .py files and check for syntax errors.
    Returns list of { file, line, message, content } for files with errors.
    """
    results = []
    py_files = list(Path(directory).glob("**/*.py"))
    logger.info("Scanned %d .py file(s) in %s/", len(py_files), directory)

    for filepath in py_files:
        rel_path = str(filepath)
        try:
            with open(filepath) as f:
                source = f.read()
            ast.parse(source, filename=rel_path)
        except SyntaxError as e:
            results.append({
                "file": rel_path,
                "line": e.lineno or 1,
                "message": f"{e.msg}",
                "content": source,
            })

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


async def copilot_fix(
    file_path: str,
    file_content: str,
    errors: list[dict],
    oauth_token: str,
) -> Optional[str]:
    """
    Ask Copilot to generate a fix for the given file and errors.
    Returns the fixed file content or None.
    """
    prompt = _build_fix_prompt(file_path, file_content, errors)
    response_text = ""

    client = CopilotClient(github_token=oauth_token)
    await client.start()

    try:
        session = await client.create_session(
            on_permission_request=PermissionHandler.approve_all,
            model="gpt-5",
        )

        def on_event(event):
            nonlocal response_text
            if event.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
                response_text += event.data.delta_content

        session.on(on_event)
        await session.send_and_wait(prompt)
    finally:
        await client.stop()

    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    return cleaned if cleaned else None


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

    def raise_fix_pr(self, fixes: dict[str, str], all_errors: list[dict]) -> str:
        """
        Create one branch, commit all fixes, open one PR.
        fixes = { file_path: new_content }
        """
        repo = self.repo
        default_branch = repo.default_branch
        base = repo.get_branch(default_branch)

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
            base=default_branch,
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

        fixes = {}
        for error in errors:
            path = error["file"]
            file_content = error["content"]
            logger.info("Asking Copilot to fix %s ...", path)

            fixed_content = asyncio.run(
                copilot_fix(path, file_content, [error], self.oauth_token)
            )

            if not fixed_content:
                logger.warning("Copilot returned empty fix for %s — skipping", path)
                continue

            if fixed_content.strip() == file_content.strip():
                logger.warning("Copilot returned UNCHANGED content for %s — skipping", path)
                continue

            try:
                ast.parse(fixed_content)
                logger.info("Fix verified OK for %s", path)
            except SyntaxError as e:
                logger.warning("Copilot fix STILL HAS ERRORS in %s: %s — skipping", path, e)
                continue

            fixes[path] = fixed_content

        if not fixes:
            logger.warning("No fixes generated. Nothing to commit.")
            return

        logger.info("Creating single PR with %d fixed file(s)...", len(fixes))
        pr_url = self.raise_fix_pr(fixes, errors)
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
