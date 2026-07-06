"""v0.4 引擎测试: 跨会话记忆 + git worktree 隔离."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conductor.memory import GLOBAL_SCOPE, PROJECT_SCOPE, MemoryStore
from conductor.worktree import WorktreeIsolator, is_git_repo


# --------------------------------------------------------------------------- #
# memory
# --------------------------------------------------------------------------- #
def test_memory_add_list_remove_context(tmp_path):
    store = MemoryStore(work_dir=tmp_path / "proj", base_dir=tmp_path / "cfg")
    store.add("lang", "项目用 Python 3.10", tags=["env"], scope=PROJECT_SCOPE)
    store.add("pref", "偏好函数式风格", scope=GLOBAL_SCOPE)

    items = store.list()
    assert len(items) == 2
    by_key = {i.key: i for i in items}
    assert by_key["lang"].scope == PROJECT_SCOPE
    assert by_key["pref"].scope == GLOBAL_SCOPE

    ctx = store.context_text()
    assert "[跨会话记忆]" in ctx and "Python 3.10" in ctx

    assert store.remove(by_key["lang"].id) is True
    assert len(store.list()) == 1


def test_memory_empty_context(tmp_path):
    assert MemoryStore(work_dir=tmp_path, base_dir=tmp_path).context_text() == ""


def test_memory_filter_by_tag(tmp_path):
    store = MemoryStore(work_dir=tmp_path, base_dir=tmp_path)
    store.add("a", "内容a", tags=["env"])
    store.add("b", "内容b", tags=["ui"])
    assert len(store.list(tag="env")) == 1


# --------------------------------------------------------------------------- #
# worktree
# --------------------------------------------------------------------------- #
def _git_repo(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)

    def g(*args):
        p = subprocess.run(["git"] + list(args), cwd=str(d),
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        return p

    g("init", "-b", "main")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "init")
    return d


def test_worktree_isolation_and_merge(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    assert is_git_repo(repo)
    iso = WorktreeIsolator(repo)
    assert iso.available()

    prepared = iso.prepare("stepA")
    assert prepared is not None
    wt, branch = prepared
    (wt / "new.txt").write_text("hello", encoding="utf-8")
    assert iso.commit(wt, "stepA changes") is True

    ok, conflict = iso.merge_back(branch)
    assert ok is True and conflict == []
    assert (repo / "new.txt").read_text(encoding="utf-8") == "hello"
    iso.cleanup()


def test_worktree_conflict_detected(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-m", "f"], check=True)

    iso = WorktreeIsolator(repo)
    # 两个 worktree 都基于同一 HEAD, 改同一文件
    wt1, b1 = iso.prepare("s1")
    wt2, b2 = iso.prepare("s2")
    (wt1 / "f.txt").write_text("from-wt1", encoding="utf-8")
    (wt2 / "f.txt").write_text("from-wt2", encoding="utf-8")
    iso.commit(wt1, "s1")
    iso.commit(wt2, "s2")

    assert iso.merge_back(b1)[0] is True          # 第一次合并成功
    ok, conflict = iso.merge_back(b2)             # 第二次必然冲突
    assert ok is False
    assert isinstance(conflict, list) and "f.txt" in conflict
    iso.cleanup()


def test_worktree_degrades_without_git(tmp_path):
    # 非 git 目录: available 为 False, prepare 返回 None
    repo = tmp_path / "notgit"
    repo.mkdir()
    iso = WorktreeIsolator(repo)
    assert iso.available() is False
    assert iso.prepare("x") is None


# --------------------------------------------------------------------------- #
# web (FastAPI TestClient) — 缺 fastapi 则跳过
# --------------------------------------------------------------------------- #
def test_web_app_endpoints(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "cond"))
    from conductor.web import create_app

    app = create_app(work_dir=tmp_path)
    client = TestClient(app)

    r = client.get("/api/config")
    assert r.status_code == 200
    j = r.json()
    assert "roles" in j and "backends" in j

    assert client.get("/api/sessions").status_code == 200
    assert client.get("/api/memory").status_code == 200

    r = client.post("/api/memory", json={"key": "栈", "content": "FastAPI",
                                         "scope": "project", "tags": []})
    assert r.status_code == 200 and r.json()["key"] == "栈"

    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()

