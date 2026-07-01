from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class Dependency:
    repo: str
    number: int
    evidence: str = ""
    confidence: str = ""

    def as_payload(self) -> dict[str, str | int]:
        return {
            "repo": self.repo,
            "number": self.number,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class IssueDependencies:
    number: int
    depends_on: list[Dependency]
    notes: str = ""


@dataclass(frozen=True)
class DependencyGraph:
    repo: str
    issues: list[IssueDependencies]
    warnings: list[str]


def render_dependency_prompt(
    repo_name: str,
    issues: list[dict[str, Any]],
    *,
    known_issue_states: list[dict[str, Any]] | None = None,
) -> str:
    payload = {
        "repo": repo_name,
        "known_issue_states": [
            {
                "repo": str(state.get("repo") or repo_name),
                "number": int(state.get("number") or 0),
                "local_state": str(state.get("local_state") or ""),
                "github_state": str(state.get("github_state") or state.get("state") or ""),
                "state_reason": str(state.get("state_reason") or state.get("stateReason") or ""),
                "closed_at": str(state.get("closed_at") or state.get("closedAt") or ""),
            }
            for state in known_issue_states or []
            if int(state.get("number") or 0) > 0
        ],
        "issues": [
            {
                "number": int(issue["number"]),
                "title": str(issue.get("title") or issue.get("issue_title") or ""),
                "body": str(issue.get("body") or issue.get("issue_body") or ""),
                "url": str(issue.get("url") or issue.get("issue_url") or ""),
            }
            for issue in issues
        ],
    }
    return f"""You are Agent Desk's unresolved dependency extractor.

Given selected GitHub issues plus known issue states, extract only explicit dependencies that are still unsatisfied.
A dependency is satisfied when known_issue_states says local_state is "done", or github_state is "closed" and state_reason is "completed".

Do not infer dependencies from vague wording, roadmap order, numbering, milestones, or implementation intuition.
Only use explicit text such as "depends on", "blocked by", "requires", "after #N", checklist dependency sections, or direct issue references in a dependency context.
Do not include satisfied dependencies in depends_on. Mention satisfied dependencies in notes if useful.
If an explicit dependency has unknown status, include it in depends_on and note that the status is unknown.
Do not treat historical context like "Issue #335 added ..." as a blocker unless the issue also uses explicit dependency wording.

Return JSON only, matching this schema:
{{
  "repo": "OWNER/REPO",
  "issues": [
    {{
      "number": 123,
      "depends_on": [
        {{
          "repo": "OWNER/REPO",
          "number": 120,
          "evidence": "Depends on #120",
          "confidence": "high"
        }}
      ],
      "notes": ""
    }}
  ],
  "warnings": []
}}

Use the current repo for #N references.
Use confidence:
- high: explicit dependency wording
- medium: dependency section or checklist strongly implies ordering
- low: ambiguous; include only if the text still clearly indicates blocking

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def parse_dependency_result(text: str, *, default_repo: str) -> DependencyGraph:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("dependency extractor returned invalid JSON") from error
    if not isinstance(raw, dict):
        raise ValueError("dependency extractor result must be an object")
    repo = str(raw.get("repo") or default_repo)
    raw_issues = raw.get("issues") or []
    if not isinstance(raw_issues, list):
        raise ValueError("dependency extractor issues must be a list")
    issues: list[IssueDependencies] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        number = int(item.get("number") or 0)
        deps = []
        raw_deps = item.get("depends_on") or []
        if isinstance(raw_deps, list):
            for dep in raw_deps:
                if not isinstance(dep, dict):
                    continue
                dep_number = int(dep.get("number") or 0)
                if dep_number <= 0:
                    continue
                deps.append(
                    Dependency(
                        repo=str(dep.get("repo") or repo),
                        number=dep_number,
                        evidence=str(dep.get("evidence") or ""),
                        confidence=str(dep.get("confidence") or ""),
                    )
                )
        if number > 0:
            issues.append(
                IssueDependencies(
                    number=number,
                    depends_on=deps,
                    notes=str(item.get("notes") or ""),
                )
            )
    raw_warnings = raw.get("warnings") or []
    warnings = [str(warning) for warning in raw_warnings] if isinstance(raw_warnings, list) else []
    return DependencyGraph(repo=repo, issues=issues, warnings=warnings)
