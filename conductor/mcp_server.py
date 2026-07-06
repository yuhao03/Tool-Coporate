"""MCP server: 把 Conductor 暴露为 MCP 工具, 让 Claude Code 等 MCP 客户端直接调用.

依赖可选: pip install 'conductor[mcp]' (即安装 mcp 包).
启动: conductor mcp  (stdio 传输)

暴露的工具:
- conductor_backends()        列出后端 + 健康检查
- conductor_who(role)         查询角色 -> 后端
- conductor_plan(task)        planner 拆解任务为分步计划(不执行)
- conductor_ask(role, prompt) 单角色直问
- conductor_run(task, dry_run) 全自动编排
"""

from __future__ import annotations

from typing import Any


def _new_orchestrator():
    from .config import load_config
    from .orchestrator import Orchestrator

    return Orchestrator(load_config())


def build_server():
    """构造 FastMCP server. (延迟导入 mcp, 以便未安装时给出友好提示.)"""
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            "缺少 mcp 依赖。请安装: uv pip install 'conductor[mcp]'  或  pip install mcp"
        ) from e

    from . import router
    from .backends import BackendRequest, make_backend
    from .config import load_config
    from .roles import ROLES, title_for
    from pathlib import Path

    mcp = FastMCP("conductor")

    @mcp.tool()
    def conductor_backends() -> str:
        """列出 Conductor 配置的所有后端, 并做健康检查(二进制/密钥是否就绪)。"""
        cfg = load_config()
        lines = []
        for name, bc in cfg.backends.items():
            try:
                ok, note = make_backend(bc).health()
            except ValueError as exc:
                ok, note = False, str(exc)
            lines.append(f"[{'OK' if ok else 'X'}] {name} ({bc.type}) — {note}")
        return "\n".join(lines) or "(无后端)"

    @mcp.tool()
    def conductor_who(role: str) -> str:
        """查询某个角色由哪个后端负责。role ∈ planner / coder / debugger / designer。"""
        cfg = load_config()
        if role not in ROLES:
            return f"未知角色 {role!r}; 可用: {', '.join(ROLES)}"
        be = cfg.roles.get(role, "(未配置)")
        return f"{role} ({title_for(role)}) → {be}"

    @mcp.tool()
    def conductor_plan(task: str) -> str:
        """让 planner(默认 Claude) 把任务拆成分步计划。仅规划, 不执行, 不改文件。"""
        orch = _new_orchestrator()
        steps = orch.plan(task)
        if not steps:
            return "(未能生成计划; 检查 planner 后端是否可用)"
        return "\n".join(
            f"{i}. [{s.role}] {s.title} — {s.instruction}"
            for i, s in enumerate(steps, 1)
        )

    @mcp.tool()
    def conductor_ask(role: str, prompt: str) -> str:
        """单角色直问: 把 prompt 路由到该角色的后端。role ∈ planner/coder/debugger/designer。"""
        orch = _new_orchestrator()
        try:
            res = orch.ask(role, prompt)
        except KeyError as e:
            return f"[配置错误] {e}"
        if not res.ok:
            return f"[失败] {res.error}"
        return res.text or "(无输出)"

    @mcp.tool()
    def conductor_run(task: str, dry_run: bool = True) -> str:
        """全自动编排: plan → execute → verify → debug 循环。

        dry_run=True(默认) 时只规划不执行、不改文件;
        dry_run=False 会真正调用执行后端(可能修改工作目录文件)。
        """
        orch = _new_orchestrator()
        report = orch.run(task, dry_run=dry_run)
        lines = [report.final]
        for title, rec in report.records.items():
            cost = f" ${rec.cost_usd:.4f}" if rec.cost_usd is not None else ""
            lines.append(f"- [{rec.role}] {title}: {rec.status}{cost}")
        return "\n".join(lines)

    # ---- 桥接工具: 供 Claude Code / Codex 在会话内手动调用 GLM / Codex ----
    @mcp.tool()
    def glm_chat(prompt: str, system: str = "") -> str:
        """直接调用 GLM-5.2。prompt=问题; system=可选系统提示(留空用通用风格)。"""
        cfg = load_config()
        be = make_backend(cfg.backends[cfg.role_for("debugger")])
        res = be.complete(BackendRequest(prompt=prompt, system=system or None,
                                         role="glm", cwd=Path.cwd()))
        return res.text if res.ok else f"[失败] {res.error}"

    @mcp.tool()
    def glm_review(code: str, task: str = "", focus: str = "") -> str:
        """让 GLM 审核代码找 bug。code=要审核的代码; task=它应实现的需求; focus=重点关注点。"""
        cfg = load_config()
        be = make_backend(cfg.backends[cfg.role_for("debugger")])
        prompt = router.build_review_prompt(
            task or "(未提供具体需求)", focus or "按需求与代码质量审核", "", code, "")
        res = be.complete(BackendRequest(prompt=prompt, role="glm",
                                         json_mode=True, cwd=Path.cwd()))
        return res.text if res.ok else f"[失败] {res.error}"

    @mcp.tool()
    def codex_run(task: str, workdir: str = "") -> str:
        """把一个编码任务交给原生 Codex 执行(它会改文件)。task=具体指令; workdir=工作目录。"""
        cfg = load_config()
        be = make_backend(cfg.backends[cfg.role_for("coder")])
        res = be.complete(BackendRequest(
            prompt=task, role="coder",
            cwd=Path(workdir) if workdir else Path.cwd()))
        return res.text if res.ok else f"[失败] {res.error}"

    return mcp


def main() -> int:
    try:
        mcp = build_server()
    except ModuleNotFoundError as e:
        print(str(e), flush=True)
        return 1
    mcp.run()
    return 0
