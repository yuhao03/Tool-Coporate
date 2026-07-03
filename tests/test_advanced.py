"""v0.2/v0.3 高级特性测试: resume 续跑 / 流式 / TUI 看板 / MCP."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from conductor.backends import Backend, BackendRequest, BackendResult, StreamEvent
from conductor.config import BackendConfig, Config, OrchestrationConfig
from conductor.orchestrator import Orchestrator
from conductor.render import Renderer
from conductor.session import DONE, SKIPPED, SessionStore

PLAN_JSON = ('{"steps":[{"title":"A","role":"coder","instruction":"do A"},'
             '{"title":"B","role":"designer","instruction":"do B"}]}')


class FakeBackend(Backend):
    kind = "fake"

    def __init__(self, name: str, text: str, stream: bool = False) -> None:
        self.cfg = BackendConfig(name=name, type="fake")
        self._text = text
        self._stream = stream
        self.calls = 0

    def complete(self, req: BackendRequest) -> BackendResult:
        self.calls += 1
        return BackendResult(ok=True, text=self._text, model="fake")

    def stream(self, req: BackendRequest):
        self.calls += 1
        if self._stream and self._text:
            n = max(1, len(self._text) // 3)
            for i in range(0, len(self._text), n):
                yield StreamEvent(type="delta", text=self._text[i:i + n])
        yield StreamEvent(type="done", text="", model="fake")  # done 不重复文本

    def health(self):
        return True, "ok"


def _cfg() -> Config:
    return Config(
        backends={
            "claude": BackendConfig(name="claude", type="claude-cli"),
            "codex": BackendConfig(name="codex", type="codex-cli"),
            "glm": BackendConfig(name="glm", type="openai-compatible",
                                 base_url="http://x/v4/", model="glm-5.2", api_key_env="ZHIPU_API_KEY"),
        },
        roles={"planner": "claude", "coder": "codex", "debugger": "glm", "designer": "glm"},
        orchestration=OrchestrationConfig(max_debug_rounds=2, verify_command=""),
    )


def _orch(tmp_path, stream=False):
    store = SessionStore(base_dir=tmp_path / "sessions")
    orch = Orchestrator(_cfg(), work_dir=tmp_path, session_store=store)
    orch._backends = {
        "claude": FakeBackend("claude", PLAN_JSON, stream=stream),
        "codex": FakeBackend("codex", "实现了A", stream=stream),
        "glm": FakeBackend("glm", "设计了B", stream=stream),
    }
    return orch


# --------------------------------------------------------------------------- #
def test_resume_reuses_completed_steps(tmp_path):
    orch = _orch(tmp_path)
    rep = orch.run("task X")
    sid = rep.session_id
    assert len(rep.records) == 2
    assert all(r.status == DONE for r in rep.records.values())

    # 第二次: 全新 orchestrator + 全新假后端, 用 resume 续跑
    orch2 = _orch(tmp_path)
    rep2 = orch2.run("task X", resume_id=sid)
    assert orch2._backends["claude"].calls == 0   # 不再规划
    assert orch2._backends["codex"].calls == 0    # 已完成, 复用
    assert orch2._backends["glm"].calls == 0
    assert len(rep2.records) == 2
    assert all(r.status in (DONE, SKIPPED) for r in rep2.records.values())


def test_run_streaming_collects_text(tmp_path):
    orch = _orch(tmp_path, stream=True)
    rep = orch.run("task", stream=True)
    assert len(rep.records) == 2
    # 流式增量应被正确拼回完整文本
    assert "实现了A" in rep.records["A"].text
    assert "设计了B" in rep.records["B"].text


def test_renderer_board_does_not_crash():
    out = io.StringIO()
    r = Renderer(console=Console(file=out, width=100), use_board=True)
    step = {"title": "A", "role": "coder", "instruction": "x"}
    events = [
        {"type": "plan_start", "task": "demo"},
        {"type": "steps", "task": "demo", "source": "planner", "steps": [step]},
        {"type": "step_start", "step": step, "describe": "[codex] exec …"},
        {"type": "step_delta", "step": step, "text": "hel"},
        {"type": "step_delta", "step": step, "text": "lo"},
        {"type": "step_done", "ok": True, "step": step, "text": "hello",
         "model": "fake", "usage": {"input_tokens": 10, "output_tokens": 5},
         "cost_usd": 0.0001},
        {"type": "report", "report": {"final": "✅ 完成", "verify_ok": None,
                                      "cost_total_usd": 0.0001, "session_id": "s1"}},
    ]
    for ev in events:
        r(ev)  # 不应抛异常
    assert "完成" in out.getvalue()


def test_mcp_build_server_missing_dep():
    """未安装 mcp 包时, build_server 应抛带提示的 ModuleNotFoundError."""
    try:
        import mcp  # noqa: F401
    except ModuleNotFoundError:
        from conductor.mcp_server import build_server
        with pytest.raises(ModuleNotFoundError):
            build_server()
        return
    pytest.skip("已安装 mcp 包, 跳过缺依赖路径测试")
