from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from onr.runtime_host.artifacts import (
    PlannerArtifactInventory,
    PlannerArtifactSnapshot,
)

if TYPE_CHECKING:
    from _typeshed import StrPath

_NAMES = ("plan.json", "domain.pddl")


def _refs(root: Path, snapshot: PlannerArtifactSnapshot) -> list[str]:
    return [path.relative_to(root).as_posix() for path, _ in snapshot.files]


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _age_tree(root: Path) -> None:
    """Make every directory look long settled so its listing is reusable."""
    old = time.time() - 60
    for directory, _, _ in os.walk(root):
        os.utime(directory, (old, old))


def test_unchanged_tree_is_one_stable_snapshot_and_new_entries_appear(
    tmp_path: Path,
) -> None:
    root = tmp_path / "planner"
    _write(root / "r1" / "plan.json", b"{}")
    _write(root / "r1" / "notes.txt", b"ignored")
    _age_tree(root)
    inventory = PlannerArtifactInventory(root, _NAMES)

    first = inventory.snapshot()
    assert _refs(root, first) == ["r1/plan.json"]
    assert inventory.snapshot() is first

    _write(root / "r2" / "deep" / "domain.pddl", b"(define)")
    (root / "r3" / "plan.json").mkdir(parents=True)
    second = inventory.snapshot()
    assert second.version > first.version
    assert _refs(root, second) == ["r1/plan.json", "r2/deep/domain.pddl"]

    # A listing taken right after a change must not hide a same-tick sibling.
    _write(root / "r2" / "deep" / "plan.json", b"{}")
    assert _refs(root, inventory.snapshot()) == [
        "r1/plan.json",
        "r2/deep/domain.pddl",
        "r2/deep/plan.json",
    ]


def test_in_place_overwrites_replacements_and_deletes_change_version(
    tmp_path: Path,
) -> None:
    root = tmp_path / "planner"
    plan = _write(root / "r1" / "plan.json", b'{"a": 1}')
    _age_tree(root)
    inventory = PlannerArtifactInventory(root, _NAMES)
    first = inventory.snapshot()
    before = plan.stat()

    # Same size, same inode, mtime restored: only ctime records the overwrite.
    time.sleep(0.05)
    with plan.open("r+b") as handle:
        handle.write(b'{"a": 2}')
    os.utime(plan, ns=(before.st_atime_ns, before.st_mtime_ns))
    second = inventory.snapshot()
    assert second.version > first.version
    assert second.files[0][1].st_ino == before.st_ino
    assert second.files[0][1].st_ctime_ns != before.st_ctime_ns

    replacement = _write(root / "r1" / ".plan.tmp", b'{"a": 3}')
    os.replace(replacement, plan)
    third = inventory.snapshot()
    assert third.version > second.version
    assert third.files[0][1].st_ino == plan.stat().st_ino

    plan.unlink()
    fourth = inventory.snapshot()
    assert fourth.version > third.version
    assert fourth.files == ()
    assert inventory.snapshot() is fourth


def test_symlinked_leaf_ancestor_and_root_never_expose_outside_metadata(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    _write(outside / "plan.json", b"{}")
    _write(outside / "nested" / "plan.json", b"{}")
    root = tmp_path / "planner"
    _write(root / "r1" / "plan.json", b"{}")
    (root / "leaf").mkdir()
    (root / "leaf" / "plan.json").symlink_to(outside / "plan.json")
    _age_tree(root)
    inventory = PlannerArtifactInventory(root, _NAMES)
    assert _refs(root, inventory.snapshot()) == ["r1/plan.json"]

    # Ancestor replaced by a symlink to a directory with an accepted file.
    shutil.rmtree(root / "r1")
    (root / "r1").symlink_to(outside)
    assert inventory.snapshot().files == ()

    # Root replaced by a symlink to that directory.
    shutil.rmtree(root)
    root.symlink_to(outside)
    linked = inventory.snapshot()
    assert linked.files == ()
    assert inventory.snapshot() is linked


def test_ancestor_replaced_mid_scan_never_exposes_outside_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    _write(outside / "plan.json", b"{}")
    root = tmp_path / "planner"
    plan = _write(root / "r1" / "plan.json", b"{}")
    _write(root / "r2" / "plan.json", b"{}")
    inventory = PlannerArtifactInventory(root, _NAMES)
    real_lstat = os.lstat
    swapped = False

    def swap_ancestor_before_file(
        path: StrPath, *, dir_fd: int | None = None
    ) -> os.stat_result:
        nonlocal swapped
        if not swapped and path == plan:
            swapped = True
            shutil.rmtree(root / "r1")
            (root / "r1").symlink_to(outside)
        return real_lstat(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "lstat", swap_ancestor_before_file)
    snapshot = inventory.snapshot()
    assert swapped
    assert _refs(root, snapshot) == ["r2/plan.json"]


def test_replaced_root_rebuilds_and_missing_root_is_stable_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "planner"
    _write(root / "r1" / "plan.json", b"{}")
    _age_tree(root)
    inventory = PlannerArtifactInventory(root, _NAMES)
    assert _refs(root, inventory.snapshot()) == ["r1/plan.json"]

    staged = tmp_path / "staged"
    _write(staged / "r1" / "domain.pddl", b"(define)")
    _age_tree(staged)
    shutil.rmtree(root)
    staged.rename(root)
    assert _refs(root, inventory.snapshot()) == ["r1/domain.pddl"]

    shutil.rmtree(root)
    empty = inventory.snapshot()
    assert empty.files == ()
    assert inventory.snapshot() is empty

    _write(root / "plan.json", b"{}")
    assert _refs(root, inventory.snapshot()) == ["plan.json"]


def test_inventories_for_different_roots_are_isolated(tmp_path: Path) -> None:
    first_root = tmp_path / "a"
    second_root = tmp_path / "b"
    _write(first_root / "plan.json", b"{}")
    _write(second_root / "domain.pddl", b"(define)")
    first = PlannerArtifactInventory(first_root, _NAMES)
    second = PlannerArtifactInventory(second_root, _NAMES)

    assert _refs(first_root, first.snapshot()) == ["plan.json"]
    assert _refs(second_root, second.snapshot()) == ["domain.pddl"]
    (first_root / "plan.json").unlink()
    assert first.snapshot().files == ()
    assert _refs(second_root, second.snapshot()) == ["domain.pddl"]
