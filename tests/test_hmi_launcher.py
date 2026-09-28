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


def test_project_python_preferred_without_changing_global_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("HMI_PYTHON", raising=False)
    interpreter = tmp_path / ".venv/Scripts/python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    (tmp_path / ".venv/Lib/site-packages/torch").mkdir(parents=True)
    assert python_for(tmp_path) == interpreter


def test_lite_venv_without_torch_falls_back_to_launcher_python(tmp_path, monkeypatch):
    # 精简 .venv 没有 torch 会隐藏训练页，此时用启动器自身的完整 Python
    monkeypatch.delenv("HMI_PYTHON", raising=False)
    venv_python = tmp_path / ".venv/Scripts/python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    assert python_for(tmp_path) != venv_python
    monkeypatch.setenv("HMI_PYTHON", str(venv_python))
    assert python_for(tmp_path) == venv_python
