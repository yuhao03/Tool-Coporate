"""rich 渲染: 把 orchestrator 的进度事件转成美观的终端输出.

与编排逻辑解耦: 只消费事件 dict, 不改任何状态.
"""

from __future__ import annotations

from typing import Any, Callable

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

ROLE_STYLE: dict[str, str] = {
    "planner": "cyan", "coder": "green", "debugger": "red", "designer": "magenta",
}
ROLE_EMOJI: dict[str, str] = {
    "planner": "🧠", "coder": "🛠️", "debugger": "🔎", "designer": "🎨",
}


class Renderer:
    """事件 -> rich 渲染. 作为 orchestrator.emit 注入."""

    def __init__(self, console: Console | None = None, verbose: bool = False) -> None:
        self.console = console or Console()
        self.verbose = verbose

    def __call__(self, ev: dict[str, Any]) -> None:
        handler: Callable[[dict], None] | None = getattr(self, f"_on_{ev.get('type')}", None)
        if handler:
            handler(ev)

    # ---- 计划 ----
    def _on_plan_start(self, ev: dict) -> None:
        self.console.print(Rule("🧠 planner 制定计划中", style="cyan"))

    def _on_plan_done(self, ev: dict) -> None:
        if ev.get("ok"):
            if self.verbose:
                self.console.print(Panel(
                    _clip(ev.get("raw", "")), title="planner 原始输出", border_style="dim"))
        else:
            self.console.print(Panel(
                f"planner 调用失败: {ev.get('error')}", title="计划失败", border_style="red"))

    def _on_plan_error(self, ev: dict) -> None:
        self.console.print(Panel(
            str(ev.get("error")), title="计划阶段错误(将降级为单步执行)", border_style="yellow"))

    def _on_steps(self, ev: dict) -> None:
        lines = []
        for i, s in enumerate(ev.get("steps", []), 1):
            role = s["role"]
            style = ROLE_STYLE.get(role, "white")
            lines.append(f"[bold {style}]{i}. [{role}][/] [b]{s['title']}[/]\n    {s['instruction']}")
        src = "由 planner 拆解" if ev.get("source") == "planner" else "降级为单步(planner 不可用)"
        self.console.print(Panel(
            "\n".join(lines), title=f"📋 执行计划 · {src}", border_style="cyan"))

    # ---- 步骤 ----
    def _on_step_start(self, ev: dict) -> None:
        role = ev["step"]["role"]
        style = ROLE_STYLE.get(role, "white")
        tag = "  [dim](dry-run)[/]" if ev.get("dry_run") else ""
        self.console.print(Rule(
            f"{ROLE_EMOJI.get(role, '•')} [{role}] {ev['step']['title']}{tag}", style=style))
        if ev.get("dry_run") or self.verbose:
            self.console.print(Text(ev.get("describe", ""), style="dim"))

    def _on_step_done(self, ev: dict) -> None:
        if ev.get("dry_run"):
            self.console.print(Text("(dry-run: 未实际调用后端)", style="dim italic"))
            return
        if ev.get("ok"):
            self.console.print(Panel(
                _clip(ev.get("text", "")),
                title=f"✅ 完成 · {ev.get('model', '')}".rstrip(" ·"),
                border_style="green"))
        else:
            self.console.print(Panel(
                ev.get("error", "") or ev.get("text", "") or "(无输出)",
                title="❌ 失败", border_style="red"))

    # ---- 校验 / debug ----
    def _on_verify_skip(self, ev: dict) -> None:
        self.console.print(Text(f"(dry-run: 跳过校验 `{ev.get('cmd')}`)", style="dim"))

    def _on_verify_done(self, ev: dict) -> None:
        if ev.get("ok"):
            self.console.print(Text("✅ 校验通过", style="bold green"))
        else:
            self.console.print(Panel(
                _clip(ev.get("output", "")), title="❌ 校验失败", border_style="red"))

    def _on_debug_start(self, ev: dict) -> None:
        self.console.print(Rule(f"🔎 debugger 排查 · 第 {ev.get('round')} 轮", style="red"))

    def _on_debug_done(self, ev: dict) -> None:
        if ev.get("ok"):
            self.console.print(Panel(
                _clip(ev.get("text", "")), title="debugger 根因分析", border_style="yellow"))
        else:
            self.console.print(Panel(
                ev.get("error", "") or "(无输出)", title="debugger 失败", border_style="red"))

    # ---- 汇总 ----
    def _on_report(self, ev: dict) -> None:
        r = ev["report"]
        ok = r.get("verify_ok")
        color = "green" if ok in (True, None) else "red"
        self.console.print(Panel(r.get("final", ""), title="🏁 总结", border_style=color))


def _clip(text: str, n: int = 6000) -> str:
    t = text or ""
    return t if len(t) <= n else t[:n] + "\n…(已截断)"
