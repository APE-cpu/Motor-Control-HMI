from pathlib import Path
import pytest
from tools.hmi_launcher import select_worktree, python_for


def test_tree_alias_resolves_checked_out_branch_and_detects_ambiguity(tmp_path):
    entries = [
        {"path": str(tmp_path / "codex"), "branch": "codex/ui-refinement"},
        {"path": str(tmp_path / "claude"), "branch": "claude/command-palette"},
    ]
    assert select_worktree(entries, "codex", tmp_path) == tmp_path / "codex"
    assert select_worktree(entries, "claude", tmp_path) == tmp_path / "claude"
    assert select_worktree(entries, "main", tmp_path) == tmp_path
    with pytest.raises(ValueError, match="找不到"):
        select_worktree(entries, "missing", tmp_path)
    entries.append({"path": str(tmp_path / "other"), "branch": "codex/other"})
    with pytest.raises(ValueError, match="多个"):
        select_worktree(entries, "codex", tmp_path)
    assert select_worktree(entries, "codex/ui-refinement", tmp_path) == tmp_path / "codex"


def test_project_python_preferred_without_changing_global_environment(tmp_path):
    interpreter = tmp_path / ".venv/Scripts/python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    assert python_for(tmp_path) == interpreter
