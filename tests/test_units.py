"""v0.2/v0.3 基础模块单测: graph / session / cost."""

from __future__ import annotations

import threading
import time

from conductor.cost import Usage, estimate_cost_usd, format_cost
from conductor.graph import Scheduler, build_graph, detect_cycle, topological_waves
from conductor.router import Step
from conductor.session import DONE, Session, SessionStore, StepRecord


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #
def _steps(*tuples):
    return [Step(title=t, role=r, instruction=i, depends_on=d)
            for t, r, i, d in tuples]


def test_graph_linear_waves():
    steps = _steps(
        ("A", "coder", "a", []),
        ("B", "coder", "b", ["A"]),
        ("C", "designer", "c", ["B"]),
    )
    g = build_graph(steps)
    assert [w for w in topological_waves(g)] == [["A"], ["B"], ["C"]]


def test_graph_parallel_waves():
    steps = _steps(
        ("A", "coder", "a", []),
        ("B", "coder", "b", []),
        ("C", "designer", "c", ["A", "B"]),
    )
    waves = topological_waves(build_graph(steps))
    assert waves[0] == ["A", "B"]
    assert waves[1] == ["C"]


def test_graph_detects_cycle():
    steps = _steps(
        ("A", "coder", "a", ["C"]),
        ("B", "coder", "b", ["A"]),
        ("C", "coder", "c", ["B"]),
    )
    assert detect_cycle(build_graph(steps)) is True
    import pytest
    with pytest.raises(Exception):
        topological_waves(build_graph(steps))


def test_graph_unknown_dep_ignored():
    steps = _steps(
        ("A", "coder", "a", ["不存在的步骤"]),
    )
    g = build_graph(steps)
    assert g["A"].deps == set()  # 未知依赖被忽略


def test_scheduler_runs_concurrently():
    """max_workers>1 时, 同波次步骤应并发执行(用事件证明)."""
    steps = _steps(
        ("A", "planner", "a", []),
        ("B", "planner", "b", []),
    )
    running: list[str] = []
    lock = threading.Lock()
    overlap = {"hit": False}

    def exec_fn(step, deps):
        with lock:
            running.append(step.title)
            if len(running) > 1:
                overlap["hit"] = True
        time.sleep(0.1)
        with lock:
            running.remove(step.title)
        return f"done-{step.title}"

    sched = Scheduler(steps, max_workers=2)
    results = sched.run(exec_fn)
    assert results == {"A": "done-A", "B": "done-B"}
    assert overlap["hit"] is True  # 确实并发了


def test_scheduler_serial_by_default():
    steps = _steps(("A", "planner", "a", []), ("B", "planner", "b", []))
    order: list[str] = []

    def exec_fn(step, deps):
        order.append(step.title)
        return "ok"

    Scheduler(steps, max_workers=1).run(exec_fn)
    assert order == ["A", "B"]  # 串行, 有序


# --------------------------------------------------------------------------- #
# session
# --------------------------------------------------------------------------- #
def test_session_roundtrip(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    s = Session(task="demo")
    s.steps = [{"title": "A", "role": "coder", "instruction": "x", "depends_on": []}]
    s.upsert_record(StepRecord(title="A", role="coder", status=DONE,
                               text="ok", model="codex"))
    store.save(s)
    loaded = store.load(s.id)
    assert loaded is not None
    assert loaded.task == "demo"
    assert loaded.record_for("A").status == DONE
    assert loaded.is_step_done("A") is True


def test_session_list(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    for t in ("t1", "t2", "t3"):
        store.save(Session(task=t))
    items = store.list()
    assert len(items) == 3


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #
def test_cost_estimate_known_model():
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000, model="glm-5.2")
    c = estimate_cost_usd(u)
    assert c is not None and c > 0


def test_cost_estimate_unknown_model_none():
    assert estimate_cost_usd(Usage(model="some-random")) is None
    assert estimate_cost_usd(None) is None
    assert estimate_cost_usd(Usage(model="glm-5.2")) is None  # 0 tokens


def test_cost_model_prefix_match():
    u = Usage(input_tokens=100_000, output_tokens=50_000,
              model="claude-fable-5-20260101")
    assert estimate_cost_usd(u) is not None


def test_cost_format():
    assert format_cost(None) == "—"
    assert "$" in format_cost(0.5)


def test_model_overrides_save_load(tmp_path, monkeypatch):
    """conductor model 写的 overrides.toml 应被 load_config 合并, 覆盖主配置 model。"""
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "cond"))
    from conductor import config as C

    C.save_model_overrides({"claude": "claude-opus-4.6", "glm": "glm-5-turbo"})
    assert C._load_model_overrides() == {"claude": "claude-opus-4.6", "glm": "glm-5-turbo"}

    cfg = C.load_config()
    assert cfg.backends["claude"].model == "claude-opus-4.6"
    assert cfg.backends["glm"].model == "glm-5-turbo"

