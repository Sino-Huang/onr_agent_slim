from __future__ import annotations

from pathlib import Path

import pytest

from onr.adapters.system_prompts import load_system_prompt


def test_load_system_prompt_reads_role_file(tmp_path: Path) -> None:
    prompt_path = tmp_path / "hyper-agent" / "SYSTEM.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Use the configured tools.", encoding="utf-8")

    assert load_system_prompt(tmp_path, "hyper-agent") == "Use the configured tools."


@pytest.mark.parametrize(
    "role", ["", " ", ".", "..", "../hyper-agent", "nested/hyper-agent", "/tmp"]
)
def test_load_system_prompt_rejects_invalid_role_components(
    tmp_path: Path, role: str
) -> None:
    with pytest.raises(ValueError, match="one valid path component"):
        load_system_prompt(tmp_path, role)


def test_load_system_prompt_rejects_missing_unreadable_and_blank_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="cannot be read"):
        load_system_prompt(tmp_path, "missing")

    unreadable = tmp_path / "unreadable" / "SYSTEM.md"
    unreadable.mkdir(parents=True)
    with pytest.raises(ValueError, match="cannot be read"):
        load_system_prompt(tmp_path, "unreadable")

    blank = tmp_path / "blank" / "SYSTEM.md"
    blank.parent.mkdir()
    blank.write_text(" \n\t", encoding="utf-8")
    with pytest.raises(ValueError, match="is blank"):
        load_system_prompt(tmp_path, "blank")
