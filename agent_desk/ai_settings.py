from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping


DEFAULT_AI_MODEL = "gpt-5.5"
DEFAULT_AI_REASONING_EFFORT = "xhigh"


@dataclass(frozen=True)
class AIModelOption:
    id: str
    label: str
    default_reasoning_effort: str
    reasoning_efforts: tuple[str, ...]


AI_MODEL_CATALOG = (
    AIModelOption(
        "gpt-5.6-sol",
        "GPT-5.6 Sol",
        "low",
        ("low", "medium", "high", "xhigh", "max", "ultra"),
    ),
    AIModelOption(
        "gpt-5.6-terra",
        "GPT-5.6 Terra",
        "medium",
        ("low", "medium", "high", "xhigh", "max", "ultra"),
    ),
    AIModelOption(
        "gpt-5.6-luna",
        "GPT-5.6 Luna",
        "medium",
        ("low", "medium", "high", "xhigh", "max"),
    ),
    AIModelOption("gpt-5.5", "GPT-5.5", "medium", ("low", "medium", "high", "xhigh")),
    AIModelOption("gpt-5.4", "GPT-5.4", "medium", ("low", "medium", "high", "xhigh")),
    AIModelOption(
        "gpt-5.4-mini",
        "GPT-5.4 Mini",
        "medium",
        ("low", "medium", "high", "xhigh"),
    ),
    AIModelOption(
        "gpt-5.3-codex-spark",
        "GPT-5.3 Codex Spark",
        "high",
        ("low", "medium", "high", "xhigh"),
    ),
)
AI_MODEL_BY_ID = {item.id: item for item in AI_MODEL_CATALOG}


def normalize_ai_settings(
    model: str | None,
    reasoning_effort: str | None,
) -> tuple[str, str]:
    model_value = str(model or DEFAULT_AI_MODEL).strip() or DEFAULT_AI_MODEL
    effort_value = str(reasoning_effort or DEFAULT_AI_REASONING_EFFORT).strip()
    option = AI_MODEL_BY_ID.get(model_value)
    if option and effort_value not in option.reasoning_efforts:
        effort_value = option.default_reasoning_effort
    if not effort_value:
        effort_value = option.default_reasoning_effort if option else DEFAULT_AI_REASONING_EFFORT
    return model_value, effort_value


def ai_model_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "label": item.label,
            "default_reasoning_effort": item.default_reasoning_effort,
            "reasoning_efforts": list(item.reasoning_efforts),
        }
        for item in AI_MODEL_CATALOG
    ]


def codex_ai_args(run: Mapping[str, Any]) -> list[str]:
    model = str(run.get("ai_model") or "").strip()
    effort = str(run.get("ai_reasoning_effort") or "").strip()
    args: list[str] = []
    if model:
        args.extend(["-m", model])
    if effort:
        args.extend(["-c", f"model_reasoning_effort={json.dumps(effort)}"])
    return args
