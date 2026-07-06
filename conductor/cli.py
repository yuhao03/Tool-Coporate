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
from .memory import GLOBAL_SCOPE, PROJECT_SCOPE, MemoryStore
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
    jobs: int = 1, board: bool = False, isolate: bool = False,
    gather_memory: bool = True,
) -> Orchestrator:
    cfg = load_config()
    wd = work_dir or Path.cwd()
    mem = MemoryStore(work_dir=wd).context_text() if gather_memory else ""
    orch = Orchestrator(cfg, work_dir=wd, max_workers=jobs,
                        isolate=isolate, memory_context=mem)
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
    isolate: bool = typer.Option(False, "--isolate", help="acting 步骤在独立 git worktree 执行(并发安全, 需干净 git 仓库)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示 planner 原始输出与各步命令"),
) -> None:
    """全自动编排: plan → (依赖图并发) execute → verify → debug 循环."""
    if jobs > 1 and not isolate:
        console.print("[yellow]提示: 并发(--jobs>1)且未 --isolate 时, acting 步骤改同一目录可能冲突; 建议加 --isolate[/]")
    orch = _orch(verbose=verbose, work_dir=workdir or None, jobs=jobs, board=board, isolate=isolate)
    report = orch.run(task, dry_run=dry_run, stream=stream)
    if report.verify_ok is False:
        raise typer.Exit(1)


@app.command()
def loop(
    task: str = typer.Argument(..., help="任务描述"),
    rounds: int = typer.Option(5, "--rounds", "-r", min=1, max=20, help="最大循环轮数"),
    verify: str = typer.Option("", "--verify", help="每轮校验命令, 例: pytest -q"),
    workdir: str = typer.Option("", "--workdir", "-C", help="工作目录(默认当前)"),
    stream: bool = typer.Option(False, "--stream", help="流式输出"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示执行文档等详情"),
) -> None:
    """开发闭环: Claude规划→Codex执行→GLM审核→(有bug)Claude重规划, 循环到通过。

    独立进程运行; 内部调的是无头 claude -p(规划实例), 不是你交互式的 claude code,
    两者不冲突。建议在普通终端(或另开终端)运行。
    """
    orch = _orch(verbose=verbose, work_dir=workdir or None)
    report = orch.dev_loop(task, max_rounds=rounds, verify_command=verify, stream=stream)
    if not report.verify_ok:
        raise typer.Exit(1)


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="会话 id (见 conductor sessions)"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1),
    board: bool = typer.Option(False, "--board", "-b"),
    stream: bool = typer.Option(False, "--stream"),
    isolate: bool = typer.Option(False, "--isolate"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """续跑某次会话: 复用已完成步骤, 重跑未完成/失败的步骤."""
    s = _store().load(session_id)
    if not s:
        console.print(f"[red]找不到会话 {session_id}[/] (用 conductor sessions 查看)")
        raise typer.Exit(1)
    orch = _orch(verbose=verbose, jobs=jobs, board=board, isolate=isolate)
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


# ---- 跨会话记忆 ----
mem_app = typer.Typer(help="跨会话记忆: 记住项目约定/偏好, 自动注入任务上下文",
                      no_args_is_help=True)
app.add_typer(mem_app, name="memory")


@mem_app.command("list")
def memory_list(
    scope: str = typer.Option("", "--scope", "-s", help="project/global, 默认全部"),
) -> None:
    """列出记忆条目."""
    items = MemoryStore().list(scope=scope or None)
    if not items:
        console.print("[dim]暂无记忆. 用 conductor memory add 添加.[/]")
        return
    t = Table(title=f"记忆 ({len(items)})", show_header=True, header_style="bold")
    t.add_column("id", width=10); t.add_column("范围", width=8)
    t.add_column("键", width=16); t.add_column("内容", ratio=1)
    for m in items:
        t.add_row(m.id[:8], m.scope, m.key, m.content[:80])
    console.print(t)


@mem_app.command("add")
def memory_add(
    key: str = typer.Argument(..., help="键, 如 技术栈"),
    content: str = typer.Argument(..., help="内容"),
    scope: str = typer.Option(PROJECT_SCOPE, "--scope", "-s", help="project / global"),
) -> None:
    """添加一条记忆(默认项目级)."""
    if scope not in (PROJECT_SCOPE, GLOBAL_SCOPE):
        console.print(f"[red]scope 必须是 project 或 global[/]")
        raise typer.Exit(2)
    item = MemoryStore().add(key, content, scope=scope)
    console.print(f"[green]✓ 已记 ({item.scope})[/] {item.key}: {item.content}  [dim]{item.id}[/]")


@mem_app.command("remove")
def memory_remove(
    item_id: str = typer.Argument(..., help="记忆 id (见 memory list)"),
) -> None:
    """删除一条记忆."""
    if MemoryStore().remove(item_id):
        console.print(f"[green]✓ 已删除 {item_id}[/]")
    else:
        console.print(f"[yellow]未找到 {item_id}[/]")
        raise typer.Exit(1)


# ---- 诊断 ----
@app.command()
def doctor() -> None:
    """诊断环境: Python / git / CLI / 密钥 / 可选依赖."""
    import shutil
    import sys

    from . import __version__

    def check(label: str, ok: bool, detail: str) -> None:
        console.print(f"  [{'[green]✓[/]' if ok else '[red]✗[/]'}] {label}: {detail}")

    console.print(f"[bold]Conductor {__version__} 环境诊断[/]\n")
    check("Python", True, f"{sys.version.split()[0]}  ({sys.executable})")
    check("git", shutil.which("git") is not None,
          shutil.which("git") or "未安装(隔离并发需要)")
    cfg = load_config()
    for name, bc in cfg.backends.items():
        try:
            ok, note = make_backend(bc).health()
        except ValueError as e:
            ok, note = False, str(e)
        check(f"后端 {name}", ok, note)
    # 可选依赖
    try:
        import mcp  # noqa: F401
        check("可选 mcp", True, "已安装 (conductor mcp 可用)")
    except ModuleNotFoundError:
        check("可选 mcp", False, "未安装: uv pip install 'conductor\\[mcp]'")
    try:
        import textual  # noqa: F401
        check("可选 tui", True, "已安装 (conductor tui 可用)")
    except ModuleNotFoundError:
        check("可选 tui", False, "未安装: uv pip install 'conductor\\[tui]'")
    check("配置", user_config_path().exists(),
          str(user_config_path()) + (" (存在)" if user_config_path().exists() else " (用 init 生成)"))


# ---- 终端原生 TUI 仪表盘 ----
@app.command()
def tui(
    workdir: str = typer.Option("", "--workdir", "-C", help="项目工作目录(默认当前)"),
) -> None:
    """启动终端原生交互式 TUI 仪表盘(满屏看板, 键盘驱动)。需 conductor\\[tui]。"""
    try:
        from .tui import create_app
    except ModuleNotFoundError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    create_app(work_dir=workdir or None).run()


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
        # 裸 conductor: 装了 [tui] 就进交互式仪表盘, 否则显示帮助
        try:
            from .tui import create_app
            create_app().run()
        except ModuleNotFoundError:
            console.print(ctx.get_help())
            console.print("\n[dim]提示: 安装 conductor[tui] 后, 直接运行 [b]conductor[/b]"
                          " 即可进入交互式仪表盘; 或用 [b]conductor run[/b] 等子命令。[/]")
        raise typer.Exit()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
