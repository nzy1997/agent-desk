from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
from typing import Any


@dataclass(frozen=True)
class PullRequestChecksStatus:
    state: str
    summary: str
    head_sha: str
    checks: list[dict[str, Any]]


@dataclass(frozen=True)
class PullRequestViewStatus:
    head_sha: str
    mergeable: str
    merge_state_status: str


class GitHubClient:
    def list_open_issues(self, repo: str, limit: int = 200) -> list[dict[str, Any]]:
        completed = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number,title,body,url,labels",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        return json.loads(completed.stdout or "[]")

    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        completed = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                "number,title,body,url,state,stateReason,closedAt",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        return json.loads(completed.stdout or "{}")

    def add_label(self, repo: str, issue_number: int, label: str) -> None:
        owner, name = repo.split("/", 1)
        completed = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{owner}/{name}/issues/{issue_number}/labels",
                "-X",
                "POST",
                "-f",
                f"labels[]={label}",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        owner, name = repo.split("/", 1)
        completed = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{owner}/{name}/issues/{issue_number}/labels/{label}",
                "-X",
                "DELETE",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0 and "Not Found" not in completed.stderr:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())

    def pr_checks_status(self, repo: str, pr_url: str) -> PullRequestChecksStatus:
        pr_number = parse_pr_number(pr_url)
        if not pr_number:
            return PullRequestChecksStatus(state="unknown", summary="No pull request URL", head_sha="", checks=[])

        pr_view = self._pr_view_status(repo, pr_number)
        if pr_has_merge_conflict(pr_view):
            return PullRequestChecksStatus(
                state="failure",
                summary="Pull request has merge conflicts",
                head_sha=pr_view.head_sha,
                checks=[
                    {
                        "name": "mergeable",
                        "state": pr_view.mergeable or pr_view.merge_state_status,
                        "bucket": "fail",
                        "description": "Pull request has merge conflicts with the base branch.",
                        "link": pr_url,
                        "workflow": "",
                    }
                ],
            )
        completed = subprocess.run(
            [
                "gh",
                "pr",
                "checks",
                pr_number,
                "--repo",
                repo,
                "--json",
                "name,state,bucket,description,link,workflow",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if not completed.stdout.strip():
            detail = completed.stderr.strip() or "No checks reported"
            return PullRequestChecksStatus(state="unknown", summary=detail, head_sha=pr_view.head_sha, checks=[])
        try:
            raw_checks = json.loads(completed.stdout)
        except json.JSONDecodeError:
            detail = completed.stderr.strip() or "Could not parse PR checks"
            return PullRequestChecksStatus(state="unknown", summary=detail, head_sha=pr_view.head_sha, checks=[])
        checks = normalize_checks(raw_checks)
        state, summary = summarize_checks(checks)
        return PullRequestChecksStatus(state=state, summary=summary, head_sha=pr_view.head_sha, checks=checks)

    def pull_request_exists(self, repo: str, pr_url: str) -> bool:
        pr_number = parse_pr_number(pr_url)
        if not pr_number:
            return False
        completed = subprocess.run(
            ["gh", "pr", "view", pr_number, "--repo", repo, "--json", "url"],
            text=True,
            capture_output=True,
            check=False,
        )
        if not completed.stdout.strip():
            return False
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return False
        return bool(payload.get("url"))

    def _pr_view_status(self, repo: str, pr_number: str) -> PullRequestViewStatus:
        completed = subprocess.run(
            ["gh", "pr", "view", pr_number, "--repo", repo, "--json", "headRefOid,mergeable,mergeStateStatus"],
            text=True,
            capture_output=True,
            check=False,
        )
        if not completed.stdout.strip():
            return PullRequestViewStatus(head_sha="", mergeable="", merge_state_status="")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return PullRequestViewStatus(head_sha="", mergeable="", merge_state_status="")
        return PullRequestViewStatus(
            head_sha=str(payload.get("headRefOid") or ""),
            mergeable=str(payload.get("mergeable") or ""),
            merge_state_status=str(payload.get("mergeStateStatus") or ""),
        )


def parse_pr_number(pr_url: str) -> str:
    match = re.search(r"/pull/(\d+)(?:\D|$)", pr_url)
    return match.group(1) if match else ""


def normalize_checks(raw_checks: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_checks, list):
        return []
    checks = []
    for item in raw_checks:
        if not isinstance(item, dict):
            continue
        checks.append(
            {
                "name": str(item.get("name") or ""),
                "state": str(item.get("state") or ""),
                "bucket": str(item.get("bucket") or ""),
                "description": str(item.get("description") or ""),
                "link": str(item.get("link") or ""),
                "workflow": str(item.get("workflow") or ""),
            }
        )
    return checks


def pr_has_merge_conflict(pr_view: PullRequestViewStatus) -> bool:
    mergeable = pr_view.mergeable.upper()
    merge_state_status = pr_view.merge_state_status.upper()
    return mergeable == "CONFLICTING" or merge_state_status == "DIRTY"


def summarize_checks(checks: list[dict[str, Any]]) -> tuple[str, str]:
    if not checks:
        return "unknown", "No checks reported"
    failed = sum(1 for check in checks if check_failed(check))
    pending = sum(1 for check in checks if check_pending(check))
    passed = sum(1 for check in checks if check_passed(check))
    skipped = sum(1 for check in checks if check_skipped(check))
    if failed:
        state = "failure"
    elif pending:
        state = "pending"
    else:
        state = "success"
    parts = []
    if failed:
        parts.append(count_phrase(failed, "failed"))
    if passed:
        parts.append(count_phrase(passed, "passed"))
    if pending:
        parts.append(count_phrase(pending, "pending"))
    if skipped:
        parts.append(count_phrase(skipped, "skipped"))
    return state, ", ".join(parts) or "No checks reported"


def check_failed(check: dict[str, Any]) -> bool:
    state = str(check.get("state") or "").upper()
    bucket = str(check.get("bucket") or "").lower()
    return bucket in {"fail", "failing"} or state in {
        "FAILURE",
        "ERROR",
        "CANCELLED",
        "TIMED_OUT",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
    }


def check_pending(check: dict[str, Any]) -> bool:
    state = str(check.get("state") or "").upper()
    bucket = str(check.get("bucket") or "").lower()
    return bucket in {"pending", "running"} or state in {
        "PENDING",
        "QUEUED",
        "IN_PROGRESS",
        "WAITING",
        "REQUESTED",
        "EXPECTED",
    }


def check_skipped(check: dict[str, Any]) -> bool:
    state = str(check.get("state") or "").upper()
    bucket = str(check.get("bucket") or "").lower()
    return bucket in {"skip", "skipping"} or state in {"SKIPPED", "NEUTRAL"}


def check_passed(check: dict[str, Any]) -> bool:
    state = str(check.get("state") or "").upper()
    bucket = str(check.get("bucket") or "").lower()
    return bucket in {"pass", "passing"} or state == "SUCCESS"


def count_phrase(count: int, word: str) -> str:
    return f"{count} {word}"
