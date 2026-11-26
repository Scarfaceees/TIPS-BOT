"""Phase 3.5.2 / F2 – build s presnym commitem: odmitnuti necisteho stromu (docasne Git repo), nic se nestavi."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build_release.py")
BR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BR)


def _repo(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    (tmp_path / "notes.txt").write_text("mimo build\n")
    git("add", "src", "Dockerfile", ".gitignore")
    git("commit", "-q", "-m", "x")
    return tmp_path, git


def test_clean_tree_passes_and_head_is_full_commit(tmp_path):
    repo, _ = _repo(tmp_path)
    assert BR.dirty_paths(repo) == []  # nesledovany notes.txt mimo cesty buildu nevadi
    rev = BR.head_rev(repo)
    assert len(rev) == 40 and rev == subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                                                    text=True).stdout.strip()


@pytest.mark.parametrize("change", ["modified", "untracked", "staged", "dockerfile"])
def test_dirty_build_paths_are_rejected(tmp_path, change):
    repo, git = _repo(tmp_path)
    if change == "modified":
        (repo / "src" / "a.py").write_text("x = 2\n")
    elif change == "untracked":
        (repo / "src" / "new.py").write_text("y = 1\n")
    elif change == "staged":
        (repo / "src" / "a.py").write_text("x = 3\n")
        git("add", "src/a.py")
    else:
        (repo / "Dockerfile").write_text("FROM python\n")
    assert BR.dirty_paths(repo)


def test_ignored_cache_files_do_not_block(tmp_path):
    repo, _ = _repo(tmp_path)
    (repo / "src" / "__pycache__").mkdir()
    (repo / "src" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0")
    assert BR.dirty_paths(repo) == []


def test_main_refuses_dirty_tree_without_running_docker(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(BR, "dirty_paths", lambda repo=None: ["src/x.py"])
    monkeypatch.setattr(BR.subprocess, "run", lambda *a, **k: calls.append(a))
    assert BR.main([]) == 2 and calls == []
    assert "ODMÍTNUTO" in capsys.readouterr().err


def test_main_passes_exact_commit_to_compose_build(monkeypatch, capsys):
    rev = "0123456789abcdef0123456789abcdef01234567"
    runs = []
    monkeypatch.setattr(BR, "dirty_paths", lambda repo=None: [])
    monkeypatch.setattr(BR, "head_rev", lambda repo=None: rev)
    monkeypatch.setattr(BR, "verify_image", lambda r, image=BR.IMAGE: [] if r == rev else ["x"])
    monkeypatch.setattr(BR.subprocess, "run", lambda cmd, **k: runs.append((cmd, k.get("env", {}).get("TIPSBOT_CODE_REV"))))
    assert BR.main([]) == 0
    assert runs == [(["docker", "compose", "build"], rev)]
    assert BR.main(["--dry-run"]) == 0 and len(runs) == 1  # dry-run nic nespousti


def test_measurement_stores_the_image_commit(monkeypatch):
    """Ulozene mereni nese presne TIPSBOT_CODE_REV z prostredi kontejneru (nastavene v image pri buildu)."""
    import test_nhl_measure as T

    rev = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setenv("TIPSBOT_CODE_REV", rev)
    from dataclasses import replace

    from tipsbot import nhl_data, pipeline

    tmp = Path(__import__("tempfile").mkdtemp())
    (tmp / "nhl").mkdir()
    T._moneypuck().to_csv(tmp / "nhl" / "all_teams.csv", index=False)
    st = replace(pipeline.SETTINGS, data_dir=tmp)
    monkeypatch.setattr(nhl_data, "SETTINGS", st)
    snap, *_ = T.snapshot(st)
    assert snap["code_rev"] == rev


def test_compose_gives_both_services_the_same_build_arg():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert text.count("TIPSBOT_CODE_REV: ${TIPSBOT_CODE_REV:-}") == 2
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG TIPSBOT_CODE_REV" in dockerfile and "ENV TIPSBOT_CODE_REV=$TIPSBOT_CODE_REV" in dockerfile
    assert "org.opencontainers.image.revision=$TIPSBOT_CODE_REV" in dockerfile
