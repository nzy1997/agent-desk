from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import shutil


MACOS_CODEX_CANDIDATES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("~/Applications/ChatGPT.app/Contents/Resources/codex"),
)


def _executable_path(value: str, *, search_path: str) -> str:
    candidate = Path(value).expanduser()
    if (candidate.is_absolute() or len(candidate.parts) > 1) and candidate.is_file():
        resolved = candidate.resolve()
        if os.access(resolved, os.X_OK):
            return str(resolved)
    found = shutil.which(value, path=search_path)
    if found:
        return str(Path(found).resolve())
    return ""


def resolve_codex_executable(
    *,
    environ: Mapping[str, str] | None = None,
    fallback_candidates: Sequence[Path] | None = None,
) -> str:
    env = os.environ if environ is None else environ
    search_path = str(env.get("PATH") or "")
    override = str(env.get("AGENT_DESK_CODEX") or "").strip()
    if override:
        resolved = _executable_path(override, search_path=search_path)
        if resolved:
            return resolved
        raise FileNotFoundError(
            f"AGENT_DESK_CODEX does not identify an executable: {override}"
        )
    resolved = _executable_path("codex", search_path=search_path)
    if resolved:
        return resolved
    candidates = (
        MACOS_CODEX_CANDIDATES if fallback_candidates is None else fallback_candidates
    )
    for raw_candidate in candidates:
        candidate = Path(raw_candidate).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise FileNotFoundError(
        "Codex executable not found; set AGENT_DESK_CODEX to its absolute path"
    )


def resolve_codex_argv(argv: Sequence[str]) -> list[str]:
    resolved = list(argv)
    if resolved and resolved[0] == "codex":
        resolved[0] = resolve_codex_executable()
    return resolved
