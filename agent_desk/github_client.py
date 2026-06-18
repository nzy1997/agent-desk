from __future__ import annotations

import json
import subprocess
from typing import Any


class GitHubClient:
    def list_ready_issues(self, repo: str, label: str, limit: int = 10) -> list[dict[str, Any]]:
        completed = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--label",
                label,
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
