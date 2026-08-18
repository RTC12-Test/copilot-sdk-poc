"""
Self-Healing Build Pipeline — GitHub Copilot SDK + PyGitHub

Monitors GitHub Actions workflow runs for failures, uses Copilot to
generate the fix, then opens a PR with the remediation.

Usage:
  Set environment variables:
    GITHUB_TOKEN  - GitHub PAT with repo + workflow permissions
    GITHUB_REPO   - owner/repo to monitor

  python src/healer.py
"""

import os
import re
import asyncio
import logging
from typing import Optional

from github import Github, Auth, GithubException
from copilot import CopilotClient
from copilot.session import PermissionHandler
from copilot.session_events import SessionEventType

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("self-healer")


# ---------------------------------------------------------------------------
# Error extraction (parse annotations from GitHub API)
# ---------------------------------------------------------------------------

def parse_error_annotations(log_text: str) -> list[dict]:
    """
    Extract structured error info from check-run annotation lines.
    Returns list of { file, line, message } dicts.
    """
    errors = []
    for line in log_text.splitlines():
        m = re.match(r"FILE:\s*(\S+)\s+LINE:\s*(\d+)\s+MSG:\s*(.+)", line)
        if m:
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": m.group(3),
            })
    return errors


# ---------------------------------------------------------------------------
# Copilot-powered fix generation
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a build-fix bot. You receive a CI/CD build error and the full
source file that contains the error. You must return ONLY the corrected
file content — no explanation, no markdown fences, no commentary.
The output must be the exact file content that should be committed.
"""


def _build_fix_prompt(file_path: str, file_content: str, errors: list[dict]) -> str:
    error_block = "\n".join(
        f"  - {e['file']}:{e['line']}: {e['message']}" for e in errors
    )
    return f"""\
The following file has build errors:

File: {file_path}
Errors:
{error_block}

Current file content:
```{file_path.rsplit('.', 1)[-1]}
{file_content}
```

Return the complete corrected file content. Only the file content, nothing else."""


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
        # Remove opening fence (```lang or ```)
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        # Remove closing fence
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    return cleaned if cleaned else None


# ---------------------------------------------------------------------------
# GitHub interaction (PyGitHub)
# ---------------------------------------------------------------------------

class BuildHealer:
    def __init__(self, token: str, oauth_token: str, repo_name: str):
        self.token = token
        self.oauth_token = oauth_token
        self.github = Github(auth=Auth.Token(token))
        self.repo = self.github.get_repo(repo_name)
        logger.info("Initialised healer for repo %s", repo_name)

    def get_failed_run(self) -> Optional[dict]:
        """Return the latest failed workflow run, or None."""
        runs = self.repo.get_workflow_runs(status="failure", branch="main")
        for run in runs:
            if run.conclusion == "failure":
                return {
                    "id": run.id,
                    "head_sha": run.head_sha,
                    "head_branch": run.head_branch,
                    "html_url": run.html_url,
                    "name": run.name,
                }
        return None

    def get_failure_annotations(self, run_id: int) -> tuple[str, list[dict]]:
        """
        Fetch raw logs from failed jobs and parse source-file errors.
        Returns (raw_log_text, parsed_errors).
        """
        import io
        import zipfile
        import requests

        run = self.repo.get_workflow_run(run_id)
        log_text = ""

        # Download raw logs via REST API (logs_url returns a zip)
        headers = {"Authorization": f"Bearer {self.token}"}
        resp = requests.get(run.logs_url, headers=headers, allow_redirects=True)
        logger.info("Logs download: HTTP %d, %d bytes", resp.status_code, len(resp.content))
        if resp.status_code == 200 and len(resp.content) > 0:
            try:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    for name in zf.namelist():
                        content = zf.read(name).decode(errors="replace")
                        log_text += content + "\n"
            except zipfile.BadZipFile:
                # Might be a plain text redirect, not a zip
                log_text = resp.text
        else:
            logger.warning("Could not download logs (HTTP %d)", resp.status_code)

        logger.info("Raw log length: %d chars", len(log_text))
        if log_text:
            # Print first 500 chars for debugging
            logger.info("Log preview:\n%s", log_text[:500])

        # Strip ANSI escape codes that GitHub adds to logs
        log_text = re.sub(r"\x1b\[[0-9;]*m", "", log_text)

        # Parse Python errors: File "app.py", line N ... SyntaxError: ...
        for m in re.finditer(
            r'File "(?P<file>[^"]+)", line (?P<line>\d+)',
            log_text,
        ):
            path = m.group("file")
            if path.startswith(".github"):
                continue
            # Look for SyntaxError or IndentationError nearby
            after = log_text[m.end():m.end()+500]
            err_match = re.search(r"(SyntaxError|IndentationError|NameError|TypeError|AttributeError|ImportError): (?P<msg>.+)", after)
            if err_match:
                log_text += f"FILE: {path} LINE: {m.group('line')} MSG: {err_match.group(0)}\n"

        # Parse Terraform errors: Error: ... \n\n  on s3.tf line N
        for m in re.finditer(
            r'Error: (?P<msg>.+)\n\n\s*on (?P<file>\S+) line (?P<line>\d+)',
            log_text,
        ):
            path = m.group("file")
            if not path.startswith(".github"):
                log_text += f"FILE: {path} LINE: {m.group('line')} MSG: {m.group('msg')}\n"

        # Also try the simpler "on FILE line N" pattern for Terraform
        for m in re.finditer(
            r'\x1b\[31m│\x1b\[0m \x1b\[0m\x1b\[31mError: (?P<msg>.+?)\x1b\[0m',
            log_text,
        ):
            msg = re.sub(r"\x1b\[[0-9;]*m", "", m.group("msg"))
            # Find the file/line that follows
            after = log_text[m.end():m.end()+500]
            file_match = re.search(r"on (?P<file>\S+) line (?P<line>\d+)", after)
            if file_match:
                path = file_match.group("file")
                if not path.startswith(".github"):
                    log_text += f"FILE: {path} LINE: {file_match.group('line')} MSG: {msg}\n"

        errors = [e for e in parse_error_annotations(log_text) if not e["file"].startswith(".github")]
        logger.info("Parsed %d source-file error(s) from logs", len(errors))
        return log_text, errors

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

        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base.commit.sha)

        file_data = repo.get_contents(path, ref=branch_name)
        if isinstance(file_data, list):
            raise ValueError(f"Path {path} is a directory")
        repo.update_file(
            path=path,
            message=f"self-heal: fix build error in {path}",
            content=new_content,
            sha=file_data.sha,
            branch=branch_name,
        )

        error_summary = "\n".join(f"- `{e['file']}:{e['line']}` — {e['message']}" for e in errors)

        pr = repo.create_pull(
            title=f"[Self-Heal] Fix build error in `{path}`",
            body=(
                f"## Automated Remediation\n\n"
                f"**Detected errors:**\n{error_summary}\n\n"
                f"Fix generated by **GitHub Copilot SDK** and auto-committed."
            ),
            head=branch_name,
            base=default_branch,
        )

        logger.info("Created PR: %s", pr.html_url)
        return pr.html_url

    def run(self):
        """Main entry: poll for failures → Copilot fix → PR."""
        logger.info("Polling for failed workflow runs...")

        failed = self.get_failed_run()
        if not failed:
            logger.info("No failed runs detected. All green!")
            return

        logger.info("Detected failed run %d (%s)", failed["id"], failed["html_url"])

        log_text, errors = self.get_failure_annotations(failed["id"])
        if not errors:
            logger.warning("No parseable errors in annotations. Manual intervention needed.")
            return

        logger.info("Found %d error(s):", len(errors))
        for e in errors:
            logger.info("  %s:%d — %s", e["file"], e["line"], e["message"])

        # Process each errored file
        for error in errors:
            path = error["file"]
            file_content = self.fetch_file(path)
            if not file_content:
                continue

            logger.info("Asking Copilot to fix %s ...", path)
            fixed_content = asyncio.run(
                copilot_fix(path, file_content, [error], self.oauth_token)
            )

            if not fixed_content:
                logger.warning("Copilot returned empty fix for %s", path)
                continue

            if fixed_content == file_content:
                logger.info("Copilot returned unchanged content for %s — skipping", path)
                continue

            branch = f"self-heal/{path.replace('/', '-')}"
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

