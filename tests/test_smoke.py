"""冒烟测试: 配置加载、计划解析、编排主流程(用假后端, 无网络/无 CLI 调用)."""

from __future__ import annotations

from pathlib import Path

from conductor import router
from conductor.backends import Backend, BackendRequest, BackendResult
from conductor.config import (
    BackendConfig,
    Config,
    OrchestrationConfig,
    load_config,
)
from conductor.orchestrator import Orchestrator
from conductor.session import DONE, SKIPPED, SessionStore


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_load_config_has_defaults():
    cfg = load_config()
    # 默认应包含四个角色与至少三种后端
    for role in ("planner", "coder", "debugger", "designer"):
        assert role in cfg.roles, f"缺少角色 {role}"
    assert "claude" in cfg.backends
    assert "codex" in cfg.backends
    assert "glm" in cfg.backends
    # 角色能解析到后端
    assert cfg.role_for("planner") == "claude"
    assert cfg.role_for("debugger") == "glm"


def test_glm_key_resolution_prefers_inline_then_env(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "env-key")
    bc = BackendConfig(name="glm", type="openai-compatible", api_key_env="ZHIPU_API_KEY")
    assert bc.resolved_api_key == "env-key"
    bc2 = BackendConfig(name="glm", type="openai-compatible", api_key="inline", api_key_env="ZHIPU_API_KEY")
    assert bc2.resolved_api_key == "inline"  # 明文优先


# --------------------------------------------------------------------------- #
# 计划解析
# --------------------------------------------------------------------------- #
def test_parse_plan_plain_json():
    text = '{"steps":[{"title":"A","role":"coder","instruction":"do A"},' \
           '{"title":"B","role":"designer","instruction":"do B"}]}'
    steps = router.parse_plan(text)
    assert steps is not None and len(steps) == 2
    assert steps[0].role == "coder" and steps[1].role == "designer"


def test_parse_plan_with_fences_and_chatter():
    text = '好的, 计划如下:\n```json\n{"steps":[{"title":"调研","role":"planner",' \
           '"instruction":"先调研"}]}\n```\n以上.'
    steps = router.parse_plan(text)
    assert steps is not None and steps[0].role == "planner"


def test_parse_plan_invalid_role_coerced_to_coder():
    text = '{"steps":[{"title":"X","role":"architect","instruction":"x"}]}'
    steps = router.parse_plan(text)
    assert steps is not None and steps[0].role == "coder"  # 非法角色兜底


def test_parse_plan_garbage_returns_none():
    assert router.parse_plan("完全不是 JSON 的文本") is None
    assert router.parse_plan("") is None


# --------------------------------------------------------------------------- #
# 编排主流程(假后端)
# --------------------------------------------------------------------------- #
class _FakeBackend(Backend):
    kind = "fake"

    def __init__(self, name: str, text: str, ok: bool = True) -> None:
        # name 走 cfg.name(Base.name 是只读 property)
        self.cfg = BackendConfig(name=name, type="fake")
        self._text = text
        self._ok = ok
        self.calls = 0

    def complete(self, req: BackendRequest) -> BackendResult:
        self.calls += 1
        return BackendResult(ok=self._ok, text=self._text, model="fake")

    def health(self):
        return True, "fake ok"


PLAN_JSON = '{"steps":[{"title":"设计登录页","role":"designer","instruction":"UI"},' \
            '{"title":"实现组件","role":"coder","instruction":"写代码"}]}'


def _orch_with_fakes(tmp_path: Path) -> Orchestrator:
    cfg = Config(
        backends={
            "claude": BackendConfig(name="claude", type="claude-cli"),
            "codex": BackendConfig(name="codex", type="codex-cli"),
            "glm": BackendConfig(name="glm", type="openai-compatible",
                                 base_url="http://x/v4/", model="glm-5.2", api_key_env="ZHIPU_API_KEY"),
        },
        roles={"planner": "claude", "coder": "codex", "debugger": "glm", "designer": "glm"},
        orchestration=OrchestrationConfig(max_debug_rounds=2, verify_command=""),
    )
    # 用临时会话存储, 避免污染真实 ~/.conductor/sessions
    orch = Orchestrator(cfg, work_dir=tmp_path,
                        session_store=SessionStore(base_dir=tmp_path / "sessions"))
    orch._backends = {
        "claude": _FakeBackend("claude", PLAN_JSON),
        "codex": _FakeBackend("codex", "已实现"),
        "glm": _FakeBackend("glm", "已设计"),
    }
    return orch


def test_run_dry_run_skips_execution(tmp_path):
    orch = _orch_with_fakes(tmp_path)
    report = orch.run("做一个登录页", dry_run=True)
    assert len(report.steps) == 2
    assert len(report.records) == 2
    assert all(r.status == SKIPPED for r in report.records.values())
    # dry-run 不应真正执行非 planner 后端
    assert orch._backends["codex"].calls == 0
    # 但 planner 仍被调用以生成计划
    assert orch._backends["claude"].calls == 1


def test_run_real_executes_all_steps(tmp_path):
    orch = _orch_with_fakes(tmp_path)
    report = orch.run("做一个登录页", dry_run=False)
    assert report.plan_source == "planner"
    assert len(report.records) == 2
    assert all(r.status == DONE for r in report.records.values())
    assert orch._backends["codex"].calls == 1
    assert orch._backends["glm"].calls == 1
    assert report.verify_ok is None  # 未配置校验命令


def test_run_falls_back_when_plan_unparseable(tmp_path):
    orch = _orch_with_fakes(tmp_path)
    # 让 planner 返回无法解析的文本
    orch._backends["claude"] = _FakeBackend("claude", "我没法给出 JSON")
    report = orch.run("奇怪任务", dry_run=False)
    assert report.plan_source == "fallback"
    assert report.plan_ok is False
    assert len(report.steps) == 1
    assert report.steps[0].role == "coder"  # plan_fallback_role
