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


def render_dependency_prompt(repo_name: str, issues: list[dict[str, Any]]) -> str:
    payload = {
        "repo": repo_name,
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
    return f"""You are Agent Desk's dependency extractor.

Given a JSON list of GitHub issues, extract only explicit dependencies between issues.
A dependency means the current issue should not be worked on until the referenced issue is complete.

Do not infer dependencies from vague wording, roadmap order, numbering, milestones, or implementation intuition.
Only use explicit text such as "depends on", "blocked by", "requires", "after #N", checklist dependency sections, or direct issue references in a dependency context.

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
