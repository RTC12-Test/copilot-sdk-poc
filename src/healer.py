"""
Self-Healing Build Pipeline using GitHub SDK (PyGitHub)

This script monitors GitHub Actions workflow runs for failures,
analyzes the error logs, determines remediation, and raises PRs to fix them.

Supported error categories:
  - Python syntax/compilation errors
  - Terraform syntax/validation errors
  - Docker build errors (future)

Usage:
  Set environment variables:
    GITHUB_TOKEN  - GitHub personal access token with repo + workflow permissions
    GITHUB_REPO   - owner/repo to monitor

  python src/healer.py
"""

import os
import re
import json
import logging
from typing import Optional
from github import Github, GithubException

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("self-healer")

# ---------------------------------------------------------------------------
# Error pattern detectors
# ---------------------------------------------------------------------------

PYTHON_SYNTAX_PATTERN = re.compile(
    r'File "(?P<file>[^"]+)", line (?P<line>\d+)\n'
    r'(?P<context>.*)\n'
    r'(?P<marker>[\s\^]+)\n'
    r'SyntaxError: (?P<msg>.+)',
    re.MULTILINE,
)

TERRAFORM_SYNTAX_PATTERN = re.compile(
    r'Error: (?P<msg>.+)\n\n\s*on (?P<file>[^ ]+) line (?P<line>\d+)',
    re.MULTILINE,
)

TERRAFORM_MISSING_CLOSING = re.compile(
    r'Error: Missing closing quote',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Fix generators
# ---------------------------------------------------------------------------

def fix_python_syntax(content: str, error_msg: str, line_number: int) -> Optional[str]:
    """
    Attempt to fix common Python syntax errors based on error message.
    Returns fixed content or None if unrecognised.
    """
    lines = content.splitlines()

    if "unexpected EOF while parsing" in error_msg or "EOF while scanning" in error_msg:
        # Missing closing paren / bracket / brace
        # Find the open paren on the offending line and close it
        target = line_number - 1
        if 0 <= target < len(lines):
            line = lines[target]
            opens = line.count("(") + line.count("[") + line.count("{")
            closes = line.count(")") + line.count("]") + line.count("}")
            diff = opens - closes
            if diff > 0:
                closing = ")" * line.count("("") + "]" * line.count("[") + "}" * line.count("{")
                # Append closing characters
                stripped = line.rstrip()
                lines[target] = stripped + closing
                return "\n".join(lines)

    if "invalid syntax" in error_msg.lower():
        # Try removing the offending character (common for trailing commas before closing paren)
        target = line_number - 1
        if 0 <= target < len(lines):
            line = lines[target]
            # Common fix: trailing operator
            fixed = re.sub(r'([+\-*/])\s*\)', r')', line)
            if fixed != line:
                lines[target] = fixed
                return "\n".join(lines)

    return None


def fix_terraform_syntax(content: str, error_msg: str, line_number: int) -> Optional[str]:
    """
    Attempt to fix common Terraform syntax errors.
    Returns fixed content or None if unrecognised.
    """
    lines = content.splitlines()

    if "Missing closing quote" in error_msg or "expected a" in error_msg.lower():
        # Find the line with the unclosed string
        target = line_number - 1
        if 0 <= target < len(lines):
            line = lines[target]
            stripped = line.rstrip()
            # If the line ends with an open quote scenario, add closing quote
            if stripped.count('"') % 2 != 0:
                stripped += '"'
                lines[target] = stripped
                return "\n".join(lines)

    if "Unsupported argument" in error_msg:
        # Try to find a similar attribute that exists - simplistic heuristic
        return None

    return None


# ---------------------------------------------------------------------------
# Core healer
# ---------------------------------------------------------------------------

class BuildHealer:
    def __init__(self, token: str, repo_name: str):
        self.github = Github(token)
        self.repo = self.github.get_repo(repo_name)
        logger.info("Initialised healer for repo %s", repo_name)

    # ----- log retrieval -----

    def get_failed_run(self) -> Optional[dict]:
        """Return the latest failed workflow run, or None."""
        runs = self.repo.get_workflow_runs(status="failure", branch="main", per_page=1)
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

    def get_failure_logs(self, run_id: int) -> str:
        """Download and concatenate logs from a failed run."""
        run = self.repo.get_workflow_run(run_id)
        logs = run.logs_url
        # PyGitHub exposes logs via download - use requests under the hood
        # For simplicity we fetch the raw text via the API
        import requests
        headers = {"Authorization": f"token {self.github._Github__requester._Requester__authToken}",
                   "Accept": "application/vnd.github.v3+json"}
        # Use the checks API to grab job-level logs
        jobs = run.get_jobs()
        log_text = ""
        for job in jobs:
            for step in job.steps:
                if step.conclusion == "failure":
                    # The step name and number help us narrow down
                    log_text += f"FAILED STEP: {step.name}\n"
        # Fallback: grab the annotations / check-run output
        check_runs = self.repo.get_check_runs(self.repo.get_commit(run.head_sha))
        for cr in check_runs:
            if cr.conclusion == "failure":
                annotations = cr.get_annotations()
                for ann in annotations:
                    log_text += f"FILE: {ann.path} LINE: {ann.start_line} MSG: {ann.message}\n"
        return log_text

    # ----- error classification -----

    def classify_error(self, log_text: str) -> Optional[dict]:
        """
        Parse log text and return { type, file, line, message } or None.
        """
        # Python
        m = PYTHON_SYNTAX_PATTERN.search(log_text)
        if m:
            return {
                "type": "python_syntax",
                "file": m.group("file"),
                "line": int(m.group("line")),
                "message": m.group("msg"),
            }

        # Terraform
        m = TERRAFORM_SYNTAX_PATTERN.search(log_text)
        if m:
            return {
                "type": "terraform_syntax",
                "file": m.group("file"),
                "line": int(m.group("line")),
                "message": m.group("msg"),
            }

        return None

    # ----- file remediation -----

    def remediate(self, error_info: dict) -> Optional[str]:
        """Fetch the offending file from the repo, apply a fix, return new content."""
        path = error_info["file"]
        try:
            file_data = self.repo.get_contents(path)
            content = file_data.decoded_content.decode()
        except GithubException:
            logger.warning("Could not fetch %s", path)
            return None

        if error_info["type"] == "python_syntax":
            return fix_python_syntax(content, error_info["message"], error_info["line"])
        elif error_info["type"] == "terraform_syntax":
            return fix_terraform_syntax(content, error_info["message"], error_info["line"])
        return None

    # ----- PR creation -----

    def raise_fix_pr(self, branch_name: str, path: str, new_content: str, error_info: dict) -> str:
        """Create a new branch, commit the fix, open a PR. Returns PR URL."""
        repo = self.repo
        default_branch = repo.default_branch

        # Get the base commit SHA
        base = repo.get_branch(default_branch)

        # Create a new branch
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base.commit.sha)

        # Update the file on the new branch
        file_data = repo.get_contents(path, ref=branch_name)
        repo.update_file(
            path=path,
            message=f"self-heal: fix {error_info['type']} in {path}",
            content=new_content,
            sha=file_data.sha,
            branch=branch_name,
        )

        # Create pull request
        pr = repo.create_pull(
            title=f"[Self-Heal] Fix {error_info['type']} in `{path}`",
            body=(
                f"## Automated Remediation\n\n"
                f"**Error type:** `{error_info['type']}`\n"
                f"**File:** `{path}` line {error_info['line']}`\n"
                f"**Message:** {error_info['message']}\n\n"
                f"This PR was auto-generated by the self-healing pipeline."
            ),
            head=branch_name,
            base=default_branch,
        )

        logger.info("Created PR: %s", pr.html_url)
        return pr.html_url

    # ----- orchestration -----

    def run(self):
        """Main entry: poll for failures, remediate, raise PR."""
        logger.info("Polling for failed workflow runs...")

        failed = self.get_failed_run()
        if not failed:
            logger.info("No failed runs detected. All green!")
            return

        logger.info("Detected failed run %d (%s)", failed["id"], failed["html_url"])

        log_text = self.get_failure_logs(failed["id"])
        logger.info("Collected logs (%d chars)", len(log_text))

        error_info = self.classify_error(log_text)
        if not error_info:
            logger.warning("Could not classify error from logs. Manual intervention needed.")
            return

        logger.info("Classified: %s in %s:%d — %s",
                     error_info["type"], error_info["file"],
                     error_info["line"], error_info["message"])

        new_content = self.remediate(error_info)
        if not new_content:
            logger.warning("No automated fix available for this error.")
            return

        branch = f"self-heal/{error_info['type']}/{error_info['file'].replace('/', '-')}"
        pr_url = self.raise_fix_pr(branch, error_info["file"], new_content, error_info)
        logger.info("Remediation PR ready: %s", pr_url)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("GITHUB_REPO")

    if not token or not repo_name:
        logger.error("Set GITHUB_TOKEN and GITHUB_REPO environment variables.")
        raise SystemExit(1)

    healer = BuildHealer(token, repo_name)
    healer.run()
