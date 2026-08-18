"""
Self-Healing Build Pipeline — GitHub Copilot SDK + PyGitHub

Scans client_code/ for Python files with syntax errors,
uses Copilot SDK to generate fixes, opens separate PRs per file.

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
import subprocess
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
# Local file scanning — find Python syntax errors
# ---------------------------------------------------------------------------

def scan_python_files(directory: str) -> list[dict]:
    """
    Scan directory for .py files and check for syntax errors.
    Returns list of { file, line, message, content } for files with errors.
    """
    results = []
    py_files = list(Path(directory).glob("**/*.py"))

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
            logger.info("  Found error in %s:%d — %s", rel_path, e.lineno, e.msg)

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

    # Strip markdown fences if Copilot wrapped them
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    return cleaned if cleaned else None


# ---------------------------------------------------------------------------
# GitHub interaction (PyGitHub) — one PR per file
# ---------------------------------------------------------------------------

class BuildHealer:
    def __init__(self, token: str, oauth_token: str, repo_name: str):
        self.token = token
        self.oauth_token = oauth_token
        self.github = Github(auth=Auth.Token(token))
        self.repo = self.github.get_repo(repo_name)
        logger.info("Initialised healer for repo %s", repo_name)

    def fetch_file(self, path: str, ref: str = "main") -> Optional[str]:
        """Fetch file content from the repo."""
        try:
            data = self.repo.get_contents(path, ref=ref)
            if isinstance(data, list):
                logger.warning("Path %s is a directory, not a file", path)
                return None
            return data.decoded_content.decode()
        except GithubException:
            logger.warning("Could not fetch %s", path)
            return None

    def raise_fix_pr(self, branch_name: str, path: str, new_content: str, errors: list[dict]) -> str:
        """Create branch, commit fix, open PR. Returns PR URL."""
        repo = self.repo
        default_branch = repo.default_branch
        base = repo.get_branch(default_branch)

        # Branch name: self-heal/client-code-discount-py
        safe_name = branch_name.replace("/", "-")
        repo.create_git_ref(ref=f"refs/heads/{safe_name}", sha=base.commit.sha)

        # Check if file exists on main, create or update
        try:
            file_data = repo.get_contents(path, ref=safe_name)
            if isinstance(file_data, list):
                raise ValueError(f"Path {path} is a directory")
            repo.update_file(
                path=path,
                message=f"self-heal: fix syntax error in {path}",
                content=new_content,
                sha=file_data.sha,
                branch=safe_name,
            )
        except GithubException:
            # File doesn't exist yet, create it
            repo.create_file(
                path=path,
                message=f"self-heal: fix syntax error in {path}",
                content=new_content,
                branch=safe_name,
            )

        error_summary = "\n".join(f"- line `{e['line']}`: {e['message']}" for e in errors)

        pr = repo.create_pull(
            title=f"[Self-Heal] Fix syntax error in `{path}`",
            body=(
                f"## Automated Remediation\n\n"
                f"**File:** `{path}`\n"
                f"**Errors:**\n{error_summary}\n\n"
                f"Fix generated by **GitHub Copilot SDK** and auto-committed."
            ),
            head=safe_name,
            base=default_branch,
        )

        logger.info("Created PR: %s", pr.html_url)
        return pr.html_url

    def run(self):
        """Scan local files → Copilot fix → PR per file."""
        logger.info("Scanning %s/ for Python files with errors...", SCAN_DIR)

        errors = scan_python_files(SCAN_DIR)
        if not errors:
            logger.info("No syntax errors found. All files OK!")
            return

        logger.info("Found %d file(s) with errors", len(errors))

        # Process each errored file — separate PR for each
        for error in errors:
            path = error["file"]
            file_content = error["content"]

            logger.info("Asking Copilot to fix %s ...", path)
            fixed_content = asyncio.run(
                copilot_fix(path, file_content, [error], self.oauth_token)
            )

            if not fixed_content:
                logger.warning("Copilot returned empty fix for %s", path)
                continue

            if fixed_content.strip() == file_content.strip():
                logger.info("Copilot returned unchanged content for %s — skipping", path)
                continue

            # Verify the fix actually resolves the syntax error
            try:
                ast.parse(fixed_content)
                logger.info("Fix verified OK for %s", path)
            except SyntaxError as e:
                logger.warning("Copilot fix still has errors in %s: %s", path, e)
                continue

            branch = f"self-heal/{path.replace('/', '-').replace('.py', '')}"
            pr_url = self.raise_fix_pr(branch, path, fixed_content, [error])
            logger.info("Remediation PR: %s", pr_url)


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
