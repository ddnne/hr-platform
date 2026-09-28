import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("boundary", Path("scripts/check_public_boundary.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_tracked_data_and_secrets_detected_even_if_ignore_bypassed(tmp_path):
    (tmp_path / "innocent.txt").write_text("ghp_" + "x" * 36)
    (tmp_path / "normal.py").write_text("value = 1")
    problems = module.audit(
        tmp_path, ["private/book.json", ".dev.vars", "raw.zip", "innocent.txt", "normal.py"]
    )
    assert len(problems) == 4
    assert all("ghp_" not in reason for _, reason in problems)


def test_git_index_secret_is_detected_after_worktree_is_cleaned(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "innocent.txt"
    path.write_text("ghp_" + "x" * 36)
    subprocess.run(["git", "add", "innocent.txt"], cwd=tmp_path, check=True)
    path.write_text("clean working tree")
    assert module.audit(tmp_path, ["innocent.txt"]) == []
    assert module.audit_index(tmp_path) == [("innocent.txt", "credential pattern")]
