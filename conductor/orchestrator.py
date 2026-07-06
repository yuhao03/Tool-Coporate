"""编排: plan → (依赖图并发执行) → verify → debug 循环.

特性:
- 依赖图: planner 产出的 steps 带 depends_on, 由 Scheduler 按波次并发执行.
- 会话: 每次 run 持久化为 Session, 可 resume 续跑.
- 流式: stream=True 时 HTTP 后端边收边出; CLI 后端走默认(完成时一次给出).
- 成本: 收集 usage, 估算 USD, 汇总到会话与报告.

通过 emit(event) 把进度事件发给上层(CLI 负责渲染), 自身不依赖 rich, 便于测试.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import router
from .backends import BackendRequest, BackendResult, make_backend
from .config import Config
from .cost import estimate_cost_usd
from .graph import CycleError, Scheduler
from .roles import ACTING_ROLES, system_prompt_for
from .session import (
    DONE, FAILED, PENDING, RUNNING, SKIPPED, S_DONE, S_FAILED, S_INTERRUPTED,
    S_PLANNING, S_RUNNING, Session, SessionStore, StepRecord,
)
from .worktree import WorktreeIsolator

log = logging.getLogger("conductor.orch")

VERIFY_TIMEOUT = 600
Event = dict[str, Any]


@dataclass
class RunReport:
    task: str
    session_id: str = ""
    steps: list[router.Step] = field(default_factory=list)
    records: dict[str, StepRecord] = field(default_factory=dict)
    plan_ok: bool = True
    plan_source: str = "planner"
    verify_ok: bool | None = None
    verify_output: str = ""
    debug_rounds: int = 0
    cost_total_usd: float | None = None
    final: str = ""
    session: Session | None = None


class Orchestrator:
    def __init__(
        self,
        config: Config,
        work_dir: Path | str | None = None,
        session_store: SessionStore | None = None,
        max_workers: int = 1,
        isolate: bool = False,
        memory_context: str = "",
    ) -> None:
        self.cfg = config
        self.work_dir = Path(work_dir) if work_dir else Path.cwd()
        self.store = session_store or SessionStore()
        self.max_workers = max(1, max_workers)
        self.isolate = isolate
        self.memory_context = memory_context or ""
        self._isolator: WorktreeIsolator | None = None
        self._backends: dict = {}
        self._lock = threading.RLock()
        # 进度回调; 默认静默. CLI 注入 rich 渲染器.
        self.emit: Callable[[Event], None] = lambda e: None

    # ---- 后端 ----
    def get_backend(self, name: str):
        with self._lock:
            if name not in self._backends:
                self._backends[name] = make_backend(self.cfg.backends[name])
            return self._backends[name]

    def _backend_for_role(self, role: str):
        return self.get_backend(self.cfg.role_for(role))

    def request_for(self, role: str, prompt: str, json_mode: bool = False) -> tuple:
        """构造 (backend, BackendRequest), 复用角色系统提示词与工作目录."""
        be = self._backend_for_role(role)
        req = BackendRequest(
            prompt=prompt, system=system_prompt_for(role), role=role,
            cwd=self.work_dir, json_mode=json_mode,
        )
        return be, req

    def _emit(self, ev: Event) -> None:
        """线程安全的 emit."""
        with self._lock:
            self.emit(ev)

    # ---- 单角色直问 ----
    def ask(self, role: str, prompt: str, json_mode: bool = False) -> BackendResult:
        be, req = self.request_for(role, prompt, json_mode=json_mode)
        res = be.complete(req)
        self._apply_cost(res)
        return res

    def _apply_cost(self, res: BackendResult) -> None:
        if res.cost_usd is None and res.usage:
            res.cost_usd = estimate_cost_usd(res.usage, self.cfg.pricing or None)

    def _combined_context(self, context: str) -> str:
        return "\n\n".join(p for p in [self.memory_context, context] if p)

    def _get_isolator(self) -> WorktreeIsolator | None:
        if not self.isolate:
            return None
        if self._isolator is None:
            self._isolator = WorktreeIsolator(self.work_dir)
        return self._isolator

    # ---- 计划 ----
    def plan(self, task: str, context: str = "") -> list[router.Step] | None:
        self._emit({"type": "plan_start", "task": task})
        be, req = self.request_for(router.PLANNER_ROLE,
                                   router.build_plan_prompt(task, context), json_mode=True)
        res = be.complete(req)
        self._apply_cost(res)
        self._emit({"type": "plan_done", "ok": res.ok, "raw": (res.text or "")[:2000],
                    "error": res.error, "usage": res.usage.to_dict() if res.usage else None,
                    "cost_usd": res.cost_usd})
        if not res.ok:
            return None
        return router.parse_plan(res.text)

    # ---- 全流程 ----
    def run(
        self,
        task: str,
        dry_run: bool = False,
        context: str = "",
        stream: bool = False,
        resume_id: str | None = None,
    ) -> RunReport:
        report = RunReport(task=task)
        # 会话: resume 则加载已有, 否则新建
        if resume_id:
            session = self.store.load(resume_id)
            if session is None:
                self._emit({"type": "error", "error": f"找不到会话 {resume_id}"})
                report.final = f"找不到会话 {resume_id}"
                return report
            session.status = S_RUNNING
        else:
            session = Session(task=task, status=S_PLANNING)
        report.session_id = session.id
        report.session = session
        self.store.save(session)

        # ---- 计划 ----
        if session.steps and resume_id:
            steps = [router.Step(title=s["title"], role=s["role"],
                                 instruction=s.get("instruction", ""),
                                 depends_on=s.get("depends_on", []))
                     for s in session.steps]
            report.plan_source = session.plan_source
            report.plan_ok = session.plan_ok
        else:
            steps = None
            try:
                steps = self.plan(task, self._combined_context(context))
            except KeyError as e:
                self._emit({"type": "plan_error", "error": str(e)})
            if steps:
                session.steps = [{"title": s.title, "role": s.role,
                                  "instruction": s.instruction,
                                  "depends_on": s.depends_on} for s in steps]
                session.plan_source = report.plan_source = "planner"
            else:
                fb = self.cfg.orchestration.plan_fallback_role
                steps = [router.Step(title=task[:40] or "任务", role=fb, instruction=task)]
                session.steps = [{"title": s.title, "role": s.role,
                                  "instruction": s.instruction, "depends_on": []}
                                 for s in steps]
                session.plan_ok = report.plan_ok = False
                session.plan_source = report.plan_source = "fallback"
            self.store.save(session)

        report.steps = steps
        self._emit({
            "type": "steps", "task": task, "source": report.plan_source,
            "steps": [_step_view(s) for s in steps],
        })

        # ---- 依赖图调度 ----
        try:
            scheduler = Scheduler(steps, max_workers=self.max_workers)
            waves = scheduler.waves_preview()
        except CycleError as e:
            self._emit({"type": "error", "error": f"依赖图错误: {e}"})
            session.status = S_FAILED
            session.final = f"依赖图错误: {e}"
            self.store.save(session)
            report.final = session.final
            return report
        self._emit({"type": "waves", "waves": waves})

        def exec_fn(step: router.Step, dep_results: dict[str, StepRecord]) -> StepRecord:
            return self._exec_step(step, dep_results, dry_run, stream, session, resume=bool(resume_id))

        records = scheduler.run(exec_fn)
        report.records = records

        # ---- 校验 + debug 循环 ----
        vcmd = (self.cfg.orchestration.verify_command or "").strip()
        if vcmd and not dry_run:
            self._verify_and_debug(report, session, vcmd)
        elif vcmd and dry_run:
            self._emit({"type": "verify_skip", "cmd": vcmd})

        # 清理 worktree 隔离产生的临时工作树
        if self._isolator is not None:
            try:
                self._isolator.cleanup()
            except Exception as e:  # noqa: BLE001
                log.warning("worktree 清理失败: %s", e)

        # ---- 收尾 ----
        report.cost_total_usd = _sum_costs(records)
        session.cost_total_usd = report.cost_total_usd
        report.final = self._compose_final(report)
        session.final = report.final
        if dry_run:
            session.status = S_INTERRUPTED
        elif report.verify_ok is False:
            session.status = S_FAILED
        else:
            session.status = S_DONE
        self.store.save(session)
        self._emit({"type": "report", "report": _report_view(report)})
        return report

    def _exec_step(
        self, step: router.Step, dep_results: dict[str, StepRecord],
        dry_run: bool, stream: bool, session: Session, resume: bool,
    ) -> StepRecord:
        # resume: 复用已完成步骤
        if resume and session.is_step_done(step.title):
            rec = session.record_for(step.title)
            self._emit({"type": "step_skip", "step": _step_view(step), "status": rec.status})
            return rec
        be, req = self.request_for(
            step.role, _build_step_prompt(step, dep_results, self.memory_context))
        if dry_run:
            self._emit({"type": "step_start", "step": _step_view(step),
                        "describe": be.describe(req), "dry_run": True})
            rec = StepRecord(title=step.title, role=step.role, instruction=step.instruction,
                             status=SKIPPED, skipped=True)
            session.upsert_record(rec)
            self._emit({"type": "step_done", "ok": True, "dry_run": True,
                        "step": _step_view(step)})
            return rec

        self._emit({"type": "step_start", "step": _step_view(step), "describe": be.describe(req)})
        rec = StepRecord(title=step.title, role=step.role, instruction=step.instruction,
                         status=RUNNING)
        with self._lock:
            session.upsert_record(rec)
            self.store.save(session)

        # worktree 隔离: acting 步骤在独立 worktree 执行, 之后提交并合并回主树
        wt_branch: str | None = None
        wt_dir: Path | None = None
        if self.isolate and step.role in ACTING_ROLES:
            isol = self._get_isolator()
            if isol and isol.available():
                prepared = isol.prepare(_slug(step.title))
                if prepared is not None:
                    wt_dir, wt_branch = prepared
                    req.cwd = wt_dir
                    self._emit({"type": "isolate", "step": _step_view(step),
                                "worktree": str(wt_dir)})

        usage = None
        model = None
        cost = None
        text = ""
        error = None
        if stream:
            parts: list[str] = []
            for ev in be.stream(req):
                if ev.type == "delta" and ev.text:
                    parts.append(ev.text)
                    self._emit({"type": "step_delta", "step": _step_view(step), "text": ev.text})
                elif ev.type == "done":
                    usage, model = ev.usage, ev.model
                    if ev.text:
                        parts.append(ev.text)
                elif ev.type == "error":
                    error = ev.error
            text = "".join(parts).strip()
            ok = error is None and bool(text)
        else:
            res = be.complete(req)
            self._apply_cost(res)
            ok, text, error = res.ok, res.text, res.error
            usage, model, cost = res.usage, res.model, res.cost_usd

        # 合并 worktree 改动回主工作树(失败则标记步骤失败)
        if wt_branch is not None and wt_dir is not None and ok:
            isol = self._get_isolator()
            if isol:
                isol.commit(wt_dir, f"conductor: {step.title}")
                merged, conflict = isol.merge_back(wt_branch)
                if not merged:
                    ok = False
                    error = (f"worktree 合并冲突: {conflict}"
                             if isinstance(conflict, list) else f"worktree 合并失败: {conflict}")

        rec.text = text
        rec.error = error
        rec.model = model
        rec.usage = usage.to_dict() if usage else None
        rec.status = DONE if ok else FAILED
        rec.finished_at = _now_iso()
        if cost is None and usage:
            cost = estimate_cost_usd(usage, self.cfg.pricing or None)
        rec.cost_usd = cost
        with self._lock:
            session.upsert_record(rec)
            self.store.save(session)
        self._emit({
            "type": "step_done", "ok": ok, "step": _step_view(step),
            "text": text, "error": error, "model": model,
            "usage": usage.to_dict() if usage else None, "cost_usd": cost,
        })
        return rec

    def _verify_and_debug(self, report: RunReport, session: Session, vcmd: str) -> None:
        ok, out = _run_verify(vcmd, self.work_dir)
        report.verify_ok = ok
        report.verify_output = out
        session.verify_ok = ok
        session.verify_output = out
        self._emit({"type": "verify_done", "ok": ok, "output": out})

        can_loop = "debugger" in self.cfg.roles and "coder" in self.cfg.roles
        rounds = 0
        while not ok and can_loop and rounds < self.cfg.orchestration.max_debug_rounds:
            rounds += 1
            session.debug_rounds = rounds
            self._emit({"type": "debug_start", "round": rounds})
            trail = [(t, r.role, (r.text or "")[:600])
                     for t, r in report.records.items() if r.status == DONE]
            dbe, dreq = self.request_for(
                "debugger", _build_debug_prompt(vcmd, out, trail))
            dres = dbe.complete(dreq)
            self._apply_cost(dres)
            self._emit({"type": "debug_done", "ok": dres.ok,
                        "text": (dres.text or "")[:4000], "error": dres.error})
            if not dres.ok:
                break
            cbe, creq = self.request_for("coder", _build_fix_prompt(dres.text, out))
            cres = cbe.complete(creq)
            self._apply_cost(cres)
            fix_step = router.Step(title=f"[debug 修复 第{rounds}轮]", role="coder",
                                   instruction="按 debugger 分析修复代码")
            rec = StepRecord(title=fix_step.title, role="coder", status=DONE if cres.ok else FAILED,
                             text=cres.text, error=cres.error, model=cres.model,
                             usage=cres.usage.to_dict() if cres.usage else None,
                             cost_usd=cres.cost_usd, finished_at=_now_iso())
            session.upsert_record(rec)
            report.records[fix_step.title] = rec
            self._emit({"type": "step_done", "ok": cres.ok, "step": _step_view(fix_step),
                        "text": cres.text, "error": cres.error, "model": cres.model})
            ok, out = _run_verify(vcmd, self.work_dir)
            report.verify_output = out
            session.verify_output = out
            self._emit({"type": "verify_done", "ok": ok, "output": out})
        report.debug_rounds = rounds
        report.verify_ok = ok
        session.verify_ok = ok
        session.debug_rounds = rounds

    # ---- 开发闭环: 规划(claude) → 执行(codex) → 审核(glm) → 重规划(claude) ----
    def dev_loop(
        self,
        task: str,
        max_rounds: int = 5,
        verify_command: str = "",
        context: str = "",
        stream: bool = False,
    ) -> RunReport:
        """按用户语义的开发闭环运行, 直到 GLM 审核通过或达到 max_rounds。"""
        from .worktree import current_diff as _diff

        session = Session(task=task, status=S_RUNNING)
        self.store.save(session)
        self._emit({"type": "loop_start", "task": task, "max_rounds": max_rounds,
                    "session_id": session.id})

        # 0. Claude 产出执行文档
        doc = self._exec_doc(task, self._combined_context(context)) or task
        self._emit({"type": "exec_doc", "doc": doc[:4000]})

        approved = False
        last_review: dict | None = None
        rounds = 0
        vcmd = (verify_command or self.cfg.orchestration.verify_command or "").strip()

        for r in range(1, max_rounds + 1):
            rounds = r
            self._emit({"type": "round_start", "round": r})
            # 1. Codex 执行
            exec_step = {"title": f"第{r}轮·执行", "role": "coder"}
            self._emit({"type": "step_start", "step": exec_step})
            exec_res = self._codex_exec(doc)
            self._emit({"type": "step_done", "ok": exec_res.ok, "step": exec_step,
                        "text": exec_res.text, "error": exec_res.error, "model": exec_res.model})
            self._record(session, exec_step["title"], "coder", exec_res)
            # 2. 可选校验
            verify_ok, verify_out = True, ""
            if vcmd:
                verify_ok, verify_out = _run_verify(vcmd, self.work_dir)
                self._emit({"type": "verify_done", "ok": verify_ok, "output": verify_out})
            # 3. GLM 审核
            diff = _diff(self.work_dir)
            review = self._glm_review(task, doc, diff, exec_res.text, verify_out)
            last_review = review
            self._emit({"type": "review_done", "round": r,
                        "step": {"title": f"第{r}轮·审核", "role": "debugger"}, "review": review})
            self._record(session, f"第{r}轮·审核", "debugger", BackendResult(
                ok=True, text=(review or {}).get("summary", ""), model="glm"))
            if review and review.get("approved") and verify_ok:
                approved = True
                break
            # 4. Claude 基于审核问题重规划
            replan_step = {"title": f"第{r}轮·重规划", "role": "planner"}
            self._emit({"type": "step_start", "step": replan_step})
            new_doc = self._claude_replan(task, doc, review or {}, verify_out)
            self._emit({"type": "step_done", "ok": bool(new_doc), "step": replan_step,
                        "text": (new_doc or "")[:4000]})
            if new_doc:
                doc = new_doc
                self._record(session, replan_step["title"], "planner",
                             BackendResult(ok=True, text=new_doc, model="claude"))

        if approved:
            final = f"✅ 第 {rounds} 轮通过 GLM 审核, 任务完成。"
            session.status = S_DONE
        else:
            bugs = (last_review or {}).get("bugs", [])
            final = (f"⚠️ 达到 {max_rounds} 轮仍未通过。最后问题: "
                     + ("; ".join(bugs[:3]) if bugs else "审核未通过"))
            session.status = S_FAILED
        session.final = final
        self.store.save(session)
        self._emit({"type": "loop_done", "approved": approved, "rounds": rounds,
                    "final": final, "session_id": session.id})
        return RunReport(task=task, session_id=session.id, final=final,
                         verify_ok=approved, session=session)

    # ---- 闭环用的小后端调用 ----
    def _exec_doc(self, task: str, ctx: str) -> str | None:
        be, req = self.request_for(router.PLANNER_ROLE, router.build_exec_doc_prompt(task, ctx))
        res = be.complete(req)
        self._apply_cost(res)
        return res.text.strip() if res.ok else None

    def _codex_exec(self, doc: str) -> BackendResult:
        be, req = self.request_for(
            "coder",
            "请严格按以下「执行文档」完成实现(直接改文件/写代码, 遵循既有风格):\n\n"
            f"{doc}\n\n完成后用 3~5 行给改动摘要。")
        res = be.complete(req)
        self._apply_cost(res)
        return res

    def _glm_review(self, task: str, doc: str, diff: str, exec_summary: str,
                    verify_out: str) -> dict | None:
        be, req = self.request_for(
            "debugger",
            router.build_review_prompt(task, doc, diff, exec_summary, verify_out),
            json_mode=True)
        res = be.complete(req)
        self._apply_cost(res)
        if not res.ok:
            return None
        return router.parse_review(res.text)

    def _claude_replan(self, task: str, prev_doc: str, review: dict, verify_out: str) -> str | None:
        be, req = self.request_for(
            router.PLANNER_ROLE, router.build_replan_prompt(task, prev_doc, review, verify_out))
        res = be.complete(req)
        self._apply_cost(res)
        return res.text.strip() if res.ok else None

    def _record(self, session: Session, title: str, role: str, res: BackendResult) -> None:
        rec = StepRecord(title=title, role=role, status=DONE if res.ok else FAILED,
                         text=res.text, error=res.error, model=res.model,
                         usage=res.usage.to_dict() if res.usage else None,
                         cost_usd=res.cost_usd, finished_at=_now_iso())
        session.upsert_record(rec)
        self.store.save(session)

    def _compose_final(self, report: RunReport) -> str:
        executed = sum(1 for r in report.records.values() if r.status == DONE)
        failed = sum(1 for r in report.records.values() if r.status == FAILED)
        cost = f" 成本约 {report.cost_total_usd:.4f}$" if report.cost_total_usd else ""
        if report.verify_ok is False:
            return (f"校验仍未通过(debug {report.debug_rounds} 轮, 失败 {failed} 步). "
                    f"见上文 debugger 根因分析.{cost}")
        if report.verify_ok is True:
            return f"✅ 完成: 校验通过. 成功 {executed} 步, debug {report.debug_rounds} 轮.{cost}"
        return f"✅ 完成: 成功 {executed} 步" + (f", 失败 {failed} 步" if failed else "") + \
               "(未配置校验命令)" + cost


# --------------------------------------------------------------------------- #
# 提示词构造 / 工具
# --------------------------------------------------------------------------- #
def _trail_from_deps(dep_results: dict[str, StepRecord]) -> str:
    if not dep_results:
        return "(无)"
    lines = []
    for title, rec in dep_results.items():
        summary = (rec.text or "").strip()
        if len(summary) > 600:
            summary = summary[:600] + " …"
        lines.append(f"[{rec.role}] {title}\n   {summary or '(无输出)'}")
    return "\n".join(lines)


def _build_step_prompt(step: router.Step, dep_results: dict[str, StepRecord],
                       memory: str = "") -> str:
    mem_block = f"{memory}\n\n" if memory else ""
    return (
        f"{mem_block}"
        f"[前置依赖的产出]\n{_trail_from_deps(dep_results)}\n\n"
        f"[你的角色] {step.role}\n"
        f"[本步目标] {step.title}\n"
        f"[具体指令] {step.instruction}\n\n"
        "请直接执行: 若你是 coder/designer(行动型), 直接改文件/写代码; "
        "若是 planner/debugger(顾问型), 输出结构化分析或建议. "
        "完成后用 3~5 行给出改动/结论摘要."
    )


def _slug(text: str) -> str:
    """把步骤标题转成适合做 git 分支名的 slug."""
    import re
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "step")).strip("-")
    return s[:32] or "step"


def _build_debug_prompt(vcmd: str, fail_out: str, trail) -> str:
    out = fail_out if len(fail_out) <= 4000 else fail_out[:4000] + " …"
    trail_block = "\n".join(f"[{r}] {t}\n   {(s or '')[:400]}" for t, r, s in trail) or "(无)"
    return (
        f"校验命令 `{vcmd}` 失败. 请定位根因并给出可执行的修复步骤.\n\n"
        f"[最近改动摘要]\n{trail_block}\n\n"
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


def _run_verify(cmd: str, cwd: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), shell=True, capture_output=True, text=True,
            timeout=VERIFY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"校验超时({VERIFY_TIMEOUT}s)"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if len(out) > 6000:
        out = out[:6000] + " …"
    return proc.returncode == 0, out


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _sum_costs(records: dict[str, StepRecord]) -> float | None:
    total = 0.0
    any_cost = False
    for r in records.values():
        if r.cost_usd is not None:
            total += r.cost_usd
            any_cost = True
    return round(total, 6) if any_cost else None


def _step_view(step: router.Step) -> dict:
    return {"title": step.title, "role": step.role, "instruction": step.instruction}


def _report_view(report: RunReport) -> dict:
    return {
        "task": report.task,
        "session_id": report.session_id,
        "plan_source": report.plan_source,
        "plan_ok": report.plan_ok,
        "n_steps": len(report.steps),
        "verify_ok": report.verify_ok,
        "debug_rounds": report.debug_rounds,
        "cost_total_usd": report.cost_total_usd,
        "final": report.final,
    }
