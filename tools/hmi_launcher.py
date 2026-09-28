"""HMI --tree main|codex|claude：按 Git worktree 启动，不切换或修改分支。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def read_worktrees(base):
    result = subprocess.run(
        ["git", "-C", str(base), "-c", "core.quotePath=false", "worktree", "list", "--porcelain", "-z"],
        capture_output=True, check=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    entries, current = [], {}
    for token in result.stdout.decode("utf-8").split("\0"):
        if not token:
            if current:
                entries.append(current)
                current = {}
        elif token.startswith("worktree "):
            current["path"] = token[9:]
        elif token.startswith("branch "):
            current["branch"] = token[7:].removeprefix("refs/heads/")
    if current:
        entries.append(current)
    return entries


def select_worktree(entries, selector, base):
    if selector == "main":
        return Path(base).resolve()
    exact = [e for e in entries if e.get("branch") == selector]
    matches = exact or [e for e in entries if e.get("branch", "").startswith(selector + "/")]
    if not matches:
        raise ValueError(f"找不到 {selector} 的工作目录。请先创建 Git worktree，或用 HMI --list 查看。")
    if len(matches) != 1:
        branches = ", ".join(e.get("branch", "detached") for e in matches)
        raise ValueError(f"{selector} 对应多个目录，请指定完整分支名：{branches}")
    return Path(matches[0]["path"]).resolve()


def _venv_has_torch(venv):
    return any((venv / sub / "torch").is_dir()
               for sub in ("Lib/site-packages", *(p.relative_to(venv) for p in venv.glob("lib/python*/site-packages"))))


def python_for(project):
    """HMI_PYTHON 指定的解释器 > 装有 torch 的项目 .venv > 启动器自身的 Python > 精简 .venv。

    精简 .venv 缺 torch 时训练页会被自动隐藏，因此只在它功能完整时优先使用。
    """
    override = os.environ.get("HMI_PYTHON")
    if override:
        return Path(override)
    venv_pythons = [c for c in (project / ".venv/Scripts/python.exe", project / ".venv/bin/python")
                    if c.is_file()]
    if venv_pythons and _venv_has_torch(project / ".venv"):
        return venv_pythons[0]
    interpreter = Path(sys.executable)
    interpreter = interpreter.with_name("python.exe") if interpreter.name.lower() == "pythonw.exe" else interpreter
    if interpreter.is_file():
        return interpreter
    return venv_pythons[0] if venv_pythons else interpreter


def show_message(message, error=False):
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(message, file=stream)
    elif os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "HMI 启动器", 0x10 if error else 0x40)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", default="main", help="main / codex / claude / 完整分支名")
    parser.add_argument("--list", action="store_true", help="列出工作目录")
    parser.add_argument("--check", action="store_true", help="仅检查 Python 和 C++ 核路径")
    parser.add_argument("--dry-run", action="store_true", help="显示启动目标，不运行程序")
    options, app_args = parser.parse_known_args(argv)
    base = Path(os.environ.get("HMI_BASE_PROJECT", Path.home() / "AI工作文件夹" / "上位机"))
    try:
        entries = read_worktrees(base) if options.tree != "main" or options.list else []
        if options.list:
            show_message("\n".join(f"{e.get('branch', 'detached')}\n  {e['path']}" for e in entries))
            return 0
        project = select_worktree(entries, options.tree, base)
        entry = project / "main.py"
        python = python_for(project)
        if not entry.is_file() or not python.is_file():
            raise FileNotFoundError(f"找不到程序或 Python：{entry}；{python}")
        command = [str(python), str(entry)]
        command += ["--runtime-check"] if options.check else app_args
        if options.dry_run:
            show_message(json.dumps({"tree": options.tree, "cwd": str(project), "command": command},
                                    ensure_ascii=False, indent=2))
            return 0
        log_path = project / "logs/hmi_launcher.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["YUHENG_WORKTREE"] = options.tree
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"Tree={options.tree} cwd={project}\nLaunching: {command}\n")
            log.flush()
            child = subprocess.Popen(command, cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log.write(f"Started PID {child.pid}\n")
            if options.check:
                code = child.wait(timeout=30)
                log.write(f"Check exit code: {code}\n")
                return code
        return 0
    except Exception as exc:
        show_message(str(exc), error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
