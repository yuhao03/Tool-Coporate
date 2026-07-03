"""Conductor CLI 入口(typer).

命令: init / config / backends / who / plan / ask / run
v0.2: resume / sessions / session
v0.3: board / mcp  +  全局 --stream / --jobs / --board
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .backends import BackendRequest, make_backend
from .config import (
    app_dir,
    load_config,
    user_config_path,
    write_default_config,
)
from .cost import format_cost
from .orchestrator import Orchestrator
from .render import Renderer, STATUS_ICON
from .roles import ROLES, title_for
from .session import SessionStore

app = typer.Typer(
    name="conductor",
    help="多 AI 角色编排: Claude 出方案 / Codex 干活 / GLM 排错与做前端",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()


def _renderer(verbose: bool = False, board: bool = False) -> Renderer:
    return Renderer(console=console, verbose=verbose, use_board=board)


def _orch(
    verbose: bool = False, work_dir: str | None = None,
    jobs: int = 1, board: bool = False,
) -> Orchestrator:
    cfg = load_config()
    orch = Orchestrator(cfg, work_dir=work_dir or Path.cwd(), max_workers=jobs)
    orch.emit = _renderer(verbose, board=board)
    return orch


def _store() -> SessionStore:
    return SessionStore()


# --------------------------------------------------------------------------- #
@app.command()
def init(
    force: bool = typer.Option(False, "--force", "-f", help="已存在时也覆盖"),
) -> None:
    """生成默认配置文件 ~/.conductor/config.toml."""
    path = user_config_path()
    if path.exists() and not force:
        console.print(f"配置已存在: {path} (用 --force 覆盖)")
        return
    write_default_config(path)
    console.print(Panel(
        f"已写入: {path}\n\n下一步:\n"
        "  1) (GLM 需要) export ZHIPU_API_KEY=sk-...\n"
        "  2) conductor backends   # 检查各后端是否就绪\n"
        "  3) conductor run \"<你的任务>\" --dry-run",
        title="✅ 初始化完成", border_style="green"))


@app.command()
def config() -> None:
    """打印当前合并后的配置与加载来源."""
    cfg = load_config()
    console.print(f"[bold]配置目录:[/] {app_dir()}")
    console.print(f"[bold]用户配置:[/] {user_config_path()}"
                  + (" [green](存在)[/]" if user_config_path().exists()
                     else " [yellow](不存在, 用 init 生成)[/]"))
    srcs = cfg.sources or []
    console.print(f"[bold]实际加载:[/] {', '.join(map(str, srcs)) or '(仅默认值)'}")

    t = Table(title="后端", show_header=True, header_style="bold")
    t.add_column("名称"); t.add_column("类型"); t.add_column("model")
    for name, b in cfg.backends.items():
        t.add_row(name, b.type, b.model or "-")
    console.print(t)

    t2 = Table(title="角色 → 后端", show_header=True, header_style="bold")
    t2.add_column("角色"); t2.add_column("后端"); t2.add_column("职责")
    for role in ROLES:
        t2.add_row(role, cfg.roles.get(role, "[未配置]"), title_for(role))
    console.print(t2)

    o = cfg.orchestration
    console.print(Panel(
        f"max_debug_rounds   = {o.max_debug_rounds}\n"
        f"verify_command     = {o.verify_command or '(未配置)'}\n"
        f"plan_fallback_role = {o.plan_fallback_role}\n"
        f"pricing overrides  = {len(cfg.pricing)} 项",
        title="编排 / 成本设置"))


@app.command(name="backends")
def backends() -> None:
    """列出后端并做健康检查(二进制/密钥是否就绪)."""
    cfg = load_config()
    t = Table(title="后端健康检查", show_header=True, header_style="bold")
    t.add_column("后端"); t.add_column("类型"); t.add_column("就绪"); t.add_column("说明")
    for name, bc in cfg.backends.items():
        try:
            ok, note = make_backend(bc).health()
        except ValueError as e:
            ok, note = False, str(e)
        status = "[green]✓[/]" if ok else "[red]✗[/]"
        t.add_row(name, bc.type, status, note)
    console.print(t)


@app.command()
def who(role: str = typer.Argument(..., help="角色: " + "/".join(ROLES))) -> None:
    """查询某角色由哪个后端负责."""
    cfg = load_config()
    if role not in ROLES:
        console.print(f"[red]未知角色 {role!r}[/]; 可用: {', '.join(ROLES)}")
        raise typer.Exit(2)
    be_name = cfg.roles.get(role)
    if not be_name:
        console.print(f"[yellow]角色 {role!r} 未配置后端[/]")
        raise typer.Exit(1)
    console.print(f"[bold]{role}[/] ({title_for(role)})  →  [cyan]{be_name}[/]")
    bc = cfg.backends[be_name]
    ok, note = make_backend(bc).health()
    console.print((f"[green]✓ {note}[/]" if ok else f"[red]✗ {note}[/]")
                  + f"  [dim]type={bc.type}[/]")


@app.command()
def plan(
    task: str = typer.Argument(..., help="任务描述"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示 planner 原始输出"),
) -> None:
    """只跑 planner, 打印分步计划(不执行)."""
    orch = _orch(verbose=verbose)
    steps = orch.plan(task)
    if not steps:
        console.print("[yellow]未能解析出计划(检查 planner 后端是否可用, 见 conductor backends)[/]")
        raise typer.Exit(1)
    orch.emit({"type": "steps", "task": task, "source": "planner",
               "steps": [{"title": s.title, "role": s.role, "instruction": s.instruction}
                         for s in steps]})
    console.print(f"\n共 [b]{len(steps)}[/] 步. 用 [cyan]conductor run[/] 执行.")


@app.command()
def ask(
    role: str = typer.Argument(..., help="角色: " + "/".join(ROLES)),
    prompt: str = typer.Argument(..., help="要问的内容"),
    stream: bool = typer.Option(False, "--stream", help="流式输出(HTTP 后端逐字)"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="只显示将调用谁, 不真正请求"),
) -> None:
    """单角色直问: 把一段话路由到该角色的后端."""
    cfg = load_config()
    if role not in ROLES:
        console.print(f"[red]未知角色 {role!r}[/]; 可用: {', '.join(ROLES)}")
        raise typer.Exit(2)
    orch = _orch()
    be, req = orch.request_for(role, prompt)
    if dry_run:
        console.print(f"[dim](dry-run) {be.describe(req)}[/]")
        return
    console.print(f"[bold]{role}[/] → [cyan]{be.name}[/]\n")
    if stream:
        usage = None
        for ev in be.stream(req):
            if ev.type == "delta" and ev.text:
                console.print(ev.text, style="cyan", end="", markup=False, highlight=False)
            elif ev.type == "done":
                usage = ev.usage
            elif ev.type == "error":
                console.print(Panel(ev.error or "(无)", title="❌ 失败", border_style="red"))
                raise typer.Exit(1)
        console.print()
        if usage:
            console.print(f"[dim]{usage.input_tokens}↑ {usage.output_tokens}↓ tokens[/]")
        return
    res = be.complete(req)
    if res.ok:
        console.print(res.text or "(无输出)")
    else:
        console.print(Panel(res.error or "(无错误信息)", title="❌ 失败", border_style="red"))
        raise typer.Exit(1)


@app.command()
def run(
    task: str = typer.Argument(..., help="任务描述"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="只规划与展示, 不真正执行"),
    workdir: str = typer.Option("", "--workdir", "-C", help="工作目录(默认当前目录)"),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="并发数(>1 时无依赖步骤并发; 注意 acting 步骤改同一目录可能冲突)"),
    board: bool = typer.Option(False, "--board", "-b", help="TUI 看板实时刷新"),
    stream: bool = typer.Option(False, "--stream", help="流式输出(HTTP 后端逐字)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示 planner 原始输出与各步命令"),
) -> None:
    """全自动编排: plan → (依赖图并发) execute → verify → debug 循环."""
    orch = _orch(verbose=verbose, work_dir=workdir or None, jobs=jobs, board=board)
    report = orch.run(task, dry_run=dry_run, stream=stream)
    if report.verify_ok is False:
        raise typer.Exit(1)


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="会话 id (见 conductor sessions)"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1),
    board: bool = typer.Option(False, "--board", "-b"),
    stream: bool = typer.Option(False, "--stream"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """续跑某次会话: 复用已完成步骤, 重跑未完成/失败的步骤."""
    s = _store().load(session_id)
    if not s:
        console.print(f"[red]找不到会话 {session_id}[/] (用 conductor sessions 查看)")
        raise typer.Exit(1)
    orch = _orch(verbose=verbose, jobs=jobs, board=board)
    report = orch.run(s.task, dry_run=dry_run, stream=stream, resume_id=session_id)
    if report.verify_ok is False:
        raise typer.Exit(1)


@app.command()
def sessions(
    limit: int = typer.Option(20, "--limit", "-n", help="最多列出多少条"),
) -> None:
    """列出最近的会话."""
    items = _store().list(limit=limit)
    if not items:
        console.print("[dim]还没有会话. 用 conductor run 开始一次任务.[/]")
        return
    t = Table(title=f"最近会话 (共 {len(items)})", show_header=True, header_style="bold")
    t.add_column("id"); t.add_column("任务", ratio=1); t.add_column("状态", width=12)
    t.add_column("步骤", width=6); t.add_column("成本", width=9); t.add_column("更新时间")
    for s in items:
        n_done = sum(1 for r in s.records.values() if r.get("status") in ("done", "skipped"))
        cost = format_cost(s.cost_total_usd)
        t.add_row(s.id, s.task[:40], s.status, f"{n_done}/{len(s.records)}",
                  cost, (s.updated_at or "")[5:16])
    console.print(t)
    console.print("[dim]用 conductor session <id> 看详情, conductor resume <id> 续跑.[/]")


@app.command()
def session(session_id: str = typer.Argument(..., help="会话 id")) -> None:
    """查看某次会话的详情."""
    s = _store().load(session_id)
    if not s:
        console.print(f"[red]找不到会话 {session_id}[/]")
        raise typer.Exit(1)
    console.print(Panel(
        f"任务: {s.task}\n状态: {s.status}  |  plan: {s.plan_source}\n"
        f"创建: {s.created_at}  |  更新: {s.updated_at}\n"
        f"校验: {s.verify_ok}  |  debug 轮数: {s.debug_rounds}  |  成本: {format_cost(s.cost_total_usd)}",
        title=f"会话 {s.id}", border_style="cyan"))
    if s.records:
        t = Table(title="步骤", show_header=True, header_style="bold")
        t.add_column("状态", width=4); t.add_column("角色", width=10)
        t.add_column("步骤", ratio=1); t.add_column("模型", width=14)
        t.add_column("tokens", width=12); t.add_column("成本", width=9)
        for rec in s.records.values():
            from .session import DONE, FAILED, SKIPPED
            icon = STATUS_ICON.get(rec.get("status"), "•")
            usage = rec.get("usage") or {}
            tok = f"{usage.get('input_tokens',0)}↑{usage.get('output_tokens',0)}↓" if usage else "—"
            t.add_row(icon, rec.get("role", ""), rec.get("title", ""),
                      (rec.get("model") or "—")[:14], tok,
                      format_cost(rec.get("cost_usd")))
        console.print(t)
    if s.final:
        console.print(Panel(s.final, title="总结", border_style="green"))


@app.command()
def board(
    session_id: str = typer.Argument("", help="会话 id, 默认最近一次"),
) -> None:
    """把某次会话渲染成步骤看板(静态)."""
    store = _store()
    s = store.load(session_id) if session_id else store.latest()
    if not s:
        console.print("[dim]没有可展示的会话.[/]")
        raise typer.Exit(1)
    from .render import ROLE_STYLE
    t = Table(title=f"📋 {s.task[:50]}", title_style="bold cyan", border_style="cyan")
    t.add_column("#", width=3); t.add_column("角色", width=10)
    t.add_column("步骤"); t.add_column("状态", width=4); t.add_column("成本", width=9)
    for i, rec in enumerate(s.records.values(), 1):
        style = ROLE_STYLE.get(rec.get("role", ""), "white")
        icon = STATUS_ICON.get(rec.get("status"), "•")
        t.add_row(str(i), f"[{style}]{rec.get('role','')}[/]", rec.get("title", ""),
                  icon, format_cost(rec.get("cost_usd")))
    console.print(t)


@app.command()
def mcp() -> None:
    """以 MCP server(stdio) 形式运行, 供 Claude Code 等 MCP 客户端调用.

    需先安装可选依赖: uv pip install 'conductor[mcp]'
    在 Claude Code 里: claude mcp add conductor -- conductor mcp
    """
    from .mcp_server import main as mcp_main

    raise typer.Exit(mcp_main())


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="显示版本"),
) -> None:
    """Conductor — 多 AI 角色编排."""
    if version:  # pragma: no cover
        console.print(f"conductor {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
