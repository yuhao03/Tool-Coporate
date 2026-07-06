"""开发闭环(dev_loop)测试: 用 fake 后端模拟 claude/codex/glm 的循环。"""

from __future__ import annotations

from pathlib import Path

from conductor.backends import Backend, BackendRequest, BackendResult
from conductor.config import BackendConfig, Config, OrchestrationConfig
from conductor.orchestrator import Orchestrator
from conductor.session import SessionStore

PLAN_DOC = "# 执行文档\n- 新增 a.py, 实现 X"
REPLAN_DOC = "# 修订执行文档\n- a.py 增加边界处理"
REVIEW_BAD = '{"approved": false, "bugs": ["a.py 缺边界处理"], "summary": "有问题"}'
REVIEW_GOOD = '{"approved": true, "bugs": [], "summary": "通过"}'


class LoopFake(Backend):
    """按 role 路由的假后端: planner 出文档/重规划, coder 执行, debugger 审核(可控通过)。"""

    kind = "fake"

    def __init__(self, approve_on_round: int = 2) -> None:
        self.cfg = BackendConfig(name="x", type="fake")
        self.approve_on_round = approve_on_round
        self.review_calls = 0

    def complete(self, req: BackendRequest) -> BackendResult:
        role = req.role
        if role == "planner":
            text = REPLAN_DOC if "修订" in req.prompt else PLAN_DOC
            return BackendResult(ok=True, text=text, model="claude")
        if role == "coder":
            return BackendResult(ok=True, text="已实现 a.py", model="codex")
        if role == "debugger":
            self.review_calls += 1
            ok = self.review_calls >= self.approve_on_round
            return BackendResult(ok=True,
                                 text=REVIEW_GOOD if ok else REVIEW_BAD, model="glm")
        return BackendResult(ok=True, text="ok", model="x")

    def health(self):
        return True, "ok"


def _orch_with_fake(tmp_path: Path, approve_on_round: int) -> Orchestrator:
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
    orch = Orchestrator(cfg, work_dir=tmp_path,
                        session_store=SessionStore(base_dir=tmp_path / "s"))
    fake = LoopFake(approve_on_round=approve_on_round)
    # 三个角色共用同一个 fake, 按 role 路由
    orch._backends = {"claude": fake, "codex": fake, "glm": fake}
    return orch, fake


def test_dev_loop_approves_after_replan(tmp_path):
    orch, fake = _orch_with_fake(tmp_path, approve_on_round=2)
    report = orch.dev_loop("实现 X", max_rounds=5)
    assert report.verify_ok is True            # 第 2 轮通过
    assert fake.review_calls == 2
    # 第 1 轮审核失败 → 应触发过一次重规划
    assert report.session is not None
    titles = list(report.session.records.keys())
    assert any("重规划" in t for t in titles)
    assert any("审核" in t for t in titles)


def test_dev_loop_exhausts_rounds_when_never_approved(tmp_path):
    orch, fake = _orch_with_fake(tmp_path, approve_on_round=99)  # 永不通过
    report = orch.dev_loop("实现 X", max_rounds=3)
    assert report.verify_ok is False
    assert fake.review_calls == 3               # 跑满 3 轮
    assert report.session.status == "failed"


def test_dev_loop_first_round_approve(tmp_path):
    orch, fake = _orch_with_fake(tmp_path, approve_on_round=1)
    report = orch.dev_loop("实现 X", max_rounds=5)
    assert report.verify_ok is True
    assert fake.review_calls == 1               # 一轮即过, 无需重规划
