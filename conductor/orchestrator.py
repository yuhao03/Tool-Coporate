"""编排: plan → execute → verify → debug 循环.

通过 emit(event) 把进度事件发给上层(CLI 负责渲染), 自身不依赖 rich, 便于测试.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import router
from .backends import BackendRequest, BackendResult, make_backend
from .config import Config
from .roles import system_prompt_for

log = logging.getLogger("conductor.orch")

SUMMARY_LEN = 600
VERIFY_TIMEOUT = 600
Event = dict[str, Any]


# --------------------------------------------------------------------------- #
# 报告结构
# --------------------------------------------------------------------------- #
@dataclass
class StepRun:
    step: router.Step
    result: BackendResult | None = None
    describe: str = ""
    skipped: bool = False


@dataclass
class RunReport:
    task: str
    steps: list[router.Step] = field(default_factory=list)
    runs: list[StepRun] = field(default_factory=list)
    plan_ok: bool = True
    plan_source: str = "planner"   # planner | fallback
    verify_ok: bool | None = None
    verify_output: str = ""
    debug_rounds: int = 0
    final: str = ""


# --------------------------------------------------------------------------- #
# 编排器
# --------------------------------------------------------------------------- #
class Orchestrator:
    def __init__(self, config: Config, work_dir: Path | str | None = None) -> None:
        self.cfg = config
        self.work_dir = Path(work_dir) if work_dir else Path.cwd()
        self._backends: dict = {}
        # 进度回调; 默认静默. CLI 注入 rich 渲染器.
        self.emit: Callable[[Event], None] = lambda e: None

    # ---- 后端缓存 ----
    def get_backend(self, name: str):
        if name not in self._backends:
            self._backends[name] = make_backend(self.cfg.backends[name])
        return self._backends[name]

    def _backend_for_role(self, role: str):
        return self.get_backend(self.cfg.role_for(role))

    # ---- 单角色直问 ----
    def ask(self, role: str, prompt: str, json_mode: bool = False) -> BackendResult:
        be = self._backend_for_role(role)
        req = BackendRequest(
            prompt=prompt, system=system_prompt_for(role), role=role,
            cwd=self.work_dir, json_mode=json_mode,
        )
        return be.complete(req)

    # ---- 计划 ----
    def plan(self, task: str, context: str = "") -> list[router.Step] | None:
        self.emit({"type": "plan_start", "task": task})
        be = self._backend_for_role(router.PLANNER_ROLE)
        req = BackendRequest(
            prompt=router.build_plan_prompt(task, context),
            system=router.system_prompt(), role=router.PLANNER_ROLE,
            cwd=self.work_dir, json_mode=True,
        )
        res = be.complete(req)
        self.emit({"type": "plan_done", "ok": res.ok, "raw": (res.text or "")[:2000],
                   "error": res.error})
        if not res.ok:
            return None
        return router.parse_plan(res.text)

    # ---- 全流程 ----
    def run(self, task: str, dry_run: bool = False, context: str = "") -> RunReport:
        report = RunReport(task=task)
        steps: list[router.Step] | None = None
        try:
            steps = self.plan(task, context)
        except KeyError as e:
            self.emit({"type": "plan_error", "error": str(e)})
        if steps:
            report.steps = steps
            report.plan_source = "planner"
        else:
            fb = self.cfg.orchestration.plan_fallback_role
            report.steps = [router.Step(title=task[:40] or "任务", role=fb, instruction=task)]
            report.plan_ok = False
            report.plan_source = "fallback"
        self.emit({
            "type": "steps", "source": report.plan_source,
            "steps": [_step_view(s) for s in report.steps],
        })

        trail: list[tuple[str, str, str]] = []
        for st in report.steps:
            sr = self._exec_step(st, trail, dry_run)
            report.runs.append(sr)
            if sr.skipped:
                trail.append((st.title, st.role, "(dry-run 未执行)"))
            elif sr.result and sr.result.ok:
                trail.append((st.title, st.role, _summarize(sr.result.text)))

        vcmd = (self.cfg.orchestration.verify_command or "").strip()
        if vcmd and not dry_run:
            self._verify_and_debug(report, vcmd)
        elif vcmd and dry_run:
            self.emit({"type": "verify_skip", "cmd": vcmd})

        report.final = self._compose_final(report)
        self.emit({"type": "report", "report": _report_view(report)})
        return report

    def _exec_step(self, step: router.Step, trail, dry_run: bool) -> StepRun:
        be = self._backend_for_role(step.role)
        req = BackendRequest(
            prompt=_build_step_prompt(step, trail),
            system=system_prompt_for(step.role), role=step.role, cwd=self.work_dir,
        )
        if dry_run:
            desc = be.describe(req)
            self.emit({"type": "step_start", "step": _step_view(step),
                       "describe": desc, "dry_run": True})
            self.emit({"type": "step_done", "ok": True, "dry_run": True})
            return StepRun(step=step, describe=desc, skipped=True)
        self.emit({"type": "step_start", "step": _step_view(step), "describe": be.describe(req)})
        res = be.complete(req)
        self.emit({"type": "step_done", "ok": res.ok, "text": res.text,
                   "error": res.error, "model": res.model})
        return StepRun(step=step, result=res)

    def _verify_and_debug(self, report: RunReport, vcmd: str) -> None:
        ok, out = _run_verify(vcmd, self.work_dir)
        report.verify_ok = ok
        report.verify_output = out
        self.emit({"type": "verify_done", "ok": ok, "output": out})

        can_loop = "debugger" in self.cfg.roles and "coder" in self.cfg.roles
        rounds = 0
        while not ok and can_loop and rounds < self.cfg.orchestration.max_debug_rounds:
            rounds += 1
            self.emit({"type": "debug_start", "round": rounds})
            trail = [(r.step.title, r.step.role, _summarize(r.result.text))
                     for r in report.runs if r.result and r.result.ok]
            dres = self._backend_for_role("debugger").complete(BackendRequest(
                prompt=_build_debug_prompt(vcmd, out, trail),
                system=system_prompt_for("debugger"), role="debugger", cwd=self.work_dir,
            ))
            self.emit({"type": "debug_done", "ok": dres.ok, "text": (dres.text or "")[:4000],
                       "error": dres.error})
            if not dres.ok:
                break
            cres = self._backend_for_role("coder").complete(BackendRequest(
                prompt=_build_fix_prompt(dres.text, out),
                system=system_prompt_for("coder"), role="coder", cwd=self.work_dir,
            ))
            report.runs.append(StepRun(step=router.Step(
                title=f"[debug 修复 第{rounds}轮]", role="coder",
                instruction="按 debugger 分析修复代码"), result=cres))
            self.emit({"type": "step_done", "ok": cres.ok, "text": cres.text,
                       "error": cres.error, "model": cres.model})
            ok, out = _run_verify(vcmd, self.work_dir)
            report.verify_output = out
            self.emit({"type": "verify_done", "ok": ok, "output": out})
        report.debug_rounds = rounds
        report.verify_ok = ok

    def _compose_final(self, report: RunReport) -> str:
        executed = sum(1 for r in report.runs if not r.skipped)
        if report.verify_ok is False:
            return (f"校验仍未通过(debug {report.debug_rounds} 轮). "
                    f"见上文 debugger 根因分析, 可手动跟进.")
        if report.verify_ok is True:
            return (f"✅ 完成: 校验通过. 共 {executed} 步, debug {report.debug_rounds} 轮.")
        return f"✅ 完成: 共执行 {executed} 步(未配置校验命令)."


# --------------------------------------------------------------------------- #
# 提示词构造 / 工具
# --------------------------------------------------------------------------- #
def _trail_block(trail: list[tuple[str, str, str]]) -> str:
    if not trail:
        return "(无)"
    lines = []
    for i, (title, role, summary) in enumerate(trail, 1):
        lines.append(f"{i}. [{role}] {title}\n   {summary}")
    return "\n".join(lines)


def _build_step_prompt(step: router.Step, trail) -> str:
    return (
        f"[已完成步骤]\n{_trail_block(trail)}\n\n"
        f"[你的角色] {step.role}\n"
        f"[本步目标] {step.title}\n"
        f"[具体指令] {step.instruction}\n\n"
        "请直接执行: 若你是 coder/designer(行动型), 直接改文件/写代码; "
        "若是 planner/debugger(顾问型), 输出结构化分析或建议. "
        "完成后用 3~5 行给出改动/结论摘要."
    )


def _build_debug_prompt(vcmd: str, fail_out: str, trail) -> str:
    out = fail_out if len(fail_out) <= 4000 else fail_out[:4000] + " …"
    return (
        f"校验命令 `{vcmd}` 失败. 请定位根因并给出可执行的修复步骤.\n\n"
        f"[最近改动摘要]\n{_trail_block(trail)}\n\n"
        f"[失败输出]\n{out}\n\n"
        "输出: 1) 根因  2) 依据  3) 具体修复步骤."
    )


def _build_fix_prompt(analysis: str, fail_out: str) -> str:
    out = fail_out if len(fail_out) <= 2000 else fail_out[:2000] + " …"
    return (
        "校验失败, debugger 已给出如下分析与修复建议:\n"
        f"{analysis}\n\n"
        f"[原始失败输出(节选)]\n{out}\n\n"
        "请据此修复代码(直接动手改文件/跑命令). 修完给出简短改动摘要."
    )


def _summarize(text: str, n: int = SUMMARY_LEN) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[:n] + " …"


def _run_verify(cmd: str, cwd: Path) -> tuple[bool, str]:
    """运行校验命令(shell), 返回 (是否通过, 合并输出)."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), shell=True, capture_output=True, text=True,
            timeout=VERIFY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"校验超时({VERIFY_TIMEOUT}s)"
    out = (proc.stdout or "") + (proc.stderr or "")
    out = out.strip()
    if len(out) > 6000:
        out = out[:6000] + " …"
    return proc.returncode == 0, out


def _step_view(step: router.Step) -> dict:
    return {"title": step.title, "role": step.role, "instruction": step.instruction}


def _report_view(report: RunReport) -> dict:
    return {
        "task": report.task,
        "plan_source": report.plan_source,
        "plan_ok": report.plan_ok,
        "n_steps": len(report.steps),
        "n_runs": len(report.runs),
        "verify_ok": report.verify_ok,
        "debug_rounds": report.debug_rounds,
        "final": report.final,
    }
