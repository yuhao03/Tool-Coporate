"""Conductor CLI 入口(typer).

命令: init / config / backends / who / plan / ask / run
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
    DEFAULT_CONFIG_TOML,
    app_dir,
    load_config,
    user_config_path,
    write_default_config,
)
from .orchestrator import Orchestrator
from .render import ROLE_STYLE, Renderer
from .roles import ROLES, title_for

app = typer.Typer(
    name="conductor",
    help="多 AI 角色编排: Claude 出方案 / Codex 干活 / GLM 排错与做前端",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _renderer(verbose: bool = False) -> Renderer:
    return Renderer(console=console, verbose=verbose)


def _orch(verbose: bool = False, work_dir: str | None = None) -> Orchestrator:
    cfg = load_config()
    orch = Orchestrator(cfg, work_dir=work_dir or Path.cwd())
    orch.emit = _renderer(verbose)
    return orch


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
    proj = app_dir()
    console.print(f"[bold]配置目录:[/] {proj}")
    console.print(f"[bold]用户配置:[/] {user_config_path()}"
                  + (" [green](存在)[/]" if user_config_path().exists() else " [yellow](不存在, 用 init 生成)[/]"))
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
        be = cfg.roles.get(role, "[未配置]")
        t2.add_row(role, be, title_for(role))
    console.print(t2)

    o = cfg.orchestration
    console.print(Panel(
        f"max_debug_rounds = {o.max_debug_rounds}\n"
        f"verify_command   = {o.verify_command or '(未配置)'}\n"
        f"plan_fallback_role = {o.plan_fallback_role}",
        title="编排设置"))


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
    orch.emit({
        "type": "steps", "source": "planner",
        "steps": [{"title": s.title, "role": s.role, "instruction": s.instruction}
                  for s in steps],
    })
    console.print(f"\n共 [b]{len(steps)}[/] 步. 用 [cyan]conductor run[/] 执行.")


@app.command()
def ask(
    role: str = typer.Argument(..., help="角色: " + "/".join(ROLES)),
    prompt: str = typer.Argument(..., help="要问的内容"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="只显示将调用谁, 不真正请求"),
) -> None:
    """单角色直问: 把一段话路由到该角色的后端."""
    cfg = load_config()
    if role not in ROLES:
        console.print(f"[red]未知角色 {role!r}[/]; 可用: {', '.join(ROLES)}")
        raise typer.Exit(2)
    orch = _orch()
    be_name = cfg.role_for(role)
    be = orch.get_backend(be_name)
    from .roles import system_prompt_for
    req = BackendRequest(prompt=prompt, system=system_prompt_for(role), role=role,
                         cwd=orch.work_dir)
    if dry_run:
        console.print(f"[dim](dry-run) {be.describe(req)}[/]")
        return
    console.print(f"[bold]{role}[/] → [cyan]{be_name}[/]\n")
    res = be.complete(req)
    if res.ok:
        console.print(res.text or "(无输出)")
    else:
        console.print(Panel(res.error or "(无错误信息)", title="❌ 失败", border_style="red"))
        raise typer.Exit(1)


@app.command()
def run(
    task: str = typer.Argument(..., help="任务描述"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="只展示计划与将调用的后端, 不真正执行"),
    workdir: str = typer.Option("", "--workdir", "-C", help="工作目录(默认当前目录)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示 planner 原始输出与各步命令"),
) -> None:
    """全自动编排: plan → execute → verify → debug 循环."""
    orch = _orch(verbose=verbose, work_dir=workdir or None)
    report = orch.run(task, dry_run=dry_run)
    if report.verify_ok is False:
        raise typer.Exit(1)


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
