"""Read fixed, role-specific system prompts from the filesystem."""

from __future__ import annotations

from pathlib import Path


def load_system_prompt(root: Path | str, role: str) -> str:
    """Load one non-blank ``<root>/<role>/SYSTEM.md`` prompt."""

    if (
        not isinstance(role, str)
        or not role.strip()
        or role in {".", ".."}
        or Path(role).name != role
        or "/" in role
        or "\\" in role
        or "\x00" in role
    ):
        raise ValueError("system prompt role must be one valid path component")

    prompt_path = Path(root) / role / "SYSTEM.md"
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"system prompt file cannot be read: {prompt_path}") from exc
    if not prompt:
        raise ValueError(f"system prompt file is blank: {prompt_path}")
    return prompt


__all__ = ["load_system_prompt"]
