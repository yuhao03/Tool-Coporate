"""rich 渲染: 把 orchestrator 的进度事件转成美观的终端输出.

两种模式:
- 行内模式(默认): 每个事件即时打印面板/文本, 流式增量逐字显示.
- 看板模式(use_board=True): 用 rich.live 实时刷新一个步骤状态表(TUI 看板),
  结束后再打印各步完整输出.

与编排逻辑解耦: 只消费事件 dict, 不改任何状态. 线程安全(并发执行时)。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .session import DONE, FAILED, PENDING, RUNNING, SKIPPED

ROLE_STYLE: dict[str, str] = {
    "planner": "cyan", "coder": "green", "debugger": "red", "designer": "magenta",
}
ROLE_EMOJI: dict[str, str] = {
    "planner": "🧠", "coder": "🛠️", "debugger": "🔎", "designer": "🎨",
}
STATUS_ICON = {DONE: "✅", FAILED: "❌", RUNNING: "🔄", PENDING: "⏸️ ", SKIPPED: "⏭️"}


def _clip(text: str, n: int = 6000) -> str:
    t = text or ""
    return t if len(t) <= n else t[:n] + "\n…(已截断)"


class Renderer:
    """事件 -> rich 渲染. 作为 orchestrator.emit 注入."""

    def __init__(
        self,
        console: Console | None = None,
        verbose: bool = False,
        use_board: bool = False,
    ) -> None:
        self.console = console or Console()
        self.verbose = verbose
        self.use_board = use_board
        self._lock = threading.RLock()
        # 看板状态
        self._live: Live | None = None
        self._rows: list[dict] = []
        self._texts: dict[str, str] = {}
        self._cost_total: float | None = None

    def __call__(self, ev: dict[str, Any]) -> None:
        with self._lock:
            handler: Callable[[dict], None] | None = getattr(
                self, f"_on_{ev.get('type')}", None)
            if handler:
                handler(ev)

    # 看板控制 ------------------------------------------------------------
    def _board_active(self) -> bool:
        return self._live is not None

    def _start_board(self, task: str, steps: list[dict]) -> None:
        self._rows = [{"title": s["title"], "role": s["role"], "status": PENDING,
                       "preview": "", "cost": None} for s in steps]
        self._texts = {}
        self._live = Live(self._render_board(task), console=self.console,
                          refresh_per_second=10, transient=False)
        self._live.start()

    def _refresh(self, task: str = "") -> None:
        if self._live:
            self._live.update(self._render_board(task))

    def _stop_board(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None
        # 结束后打印各步完整输出
        for row in self._rows:
            text = self._texts.get(row["title"], "")
            if not text:
                continue
            style = ROLE_STYLE.get(row["role"], "white")
            border = "green" if row["status"] == DONE else "red"
            self.console.print(Panel(
                _clip(text), title=f"{ROLE_EMOJI.get(row['role'],'•')} [{row['role']}] {row['title']}",
                border_style=border, title_align="left"))
            self.console.print()

    def _render_board(self, task: str) -> Any:
        t = Table(title=f"📋 {task[:50]}", title_style="bold cyan",
                  border_style="cyan", show_lines=False)
        t.add_column("#", width=3)
        t.add_column("角色", width=10)
        t.add_column("步骤")
        t.add_column("状态", width=4)
        t.add_column("预览", overflow="fold", ratio=1)
        t.add_column("成本", width=9)
        for i, r in enumerate(self._rows, 1):
            style = ROLE_STYLE.get(r["role"], "white")
            icon = STATUS_ICON.get(r["status"], "•")
            preview = r["preview"][-70:] if r["preview"] else ""
            cost = f"${r['cost']:.4f}" if r.get("cost") is not None else "—"
            t.add_row(str(i), f"[{style}]{r['role']}[/]", r["title"],
                      icon, preview, cost)
        return t

    # ---- 计划 ----
    def _on_plan_start(self, ev: dict) -> None:
        if self._board_active():
            return
        self.console.print(Rule("🧠 planner 制定计划中", style="cyan"))

    def _on_plan_done(self, ev: dict) -> None:
        if self._board_active():
            return
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

    def _on_waves(self, ev: dict) -> None:
        if self.verbose and not self._board_active():
            waves = ev.get("waves", [])
            self.console.print(Text(f"依赖图: {len(waves)} 个波次 → {waves}", style="dim"))

    def _on_steps(self, ev: dict) -> None:
        if self.use_board:
            # task 未直接带在事件里; 用第一条指令近似, 或留空让 start 用 steps
            self._start_board(ev.get("task", "执行计划"), ev.get("steps", []))
            return
        lines = []
        for i, s in enumerate(ev.get("steps", []), 1):
            role = s["role"]
            style = ROLE_STYLE.get(role, "white")
            lines.append(f"[bold {style}]{i}. [{role}][/] [b]{s['title']}[/]\n    {s['instruction']}")
        src = "由 planner 拆解" if ev.get("source") == "planner" else "降级为单步(planner 不可用)"
        self.console.print(Panel(
            "\n".join(lines), title=f"📋 执行计划 · {src}", border_style="cyan"))

    # ---- 步骤 ----
    def _row_index(self, title: str) -> int:
        for i, r in enumerate(self._rows):
            if r["title"] == title:
                return i
        return -1

    def _on_step_start(self, ev: dict) -> None:
        title = ev["step"]["title"]
        if self._board_active():
            idx = self._row_index(title)
            if idx >= 0:
                self._rows[idx]["status"] = RUNNING
                self._refresh()
            return
        role = ev["step"]["role"]
        style = ROLE_STYLE.get(role, "white")
        tag = "  [dim](dry-run)[/]" if ev.get("dry_run") else ""
        self.console.print(Rule(
            f"{ROLE_EMOJI.get(role, '•')} [{role}] {title}{tag}", style=style))
        if ev.get("dry_run") or self.verbose:
            self.console.print(Text(ev.get("describe", ""), style="dim"))

    def _on_step_delta(self, ev: dict) -> None:
        text = ev.get("text", "")
        if not text:
            return
        if self._board_active():
            idx = self._row_index(ev["step"]["title"])
            if idx >= 0:
                self._rows[idx]["preview"] = (self._rows[idx]["preview"] + text)[-200:]
                self._refresh()
            return
        # 行内流式: 不换行, 逐字打印
        self.console.print(text, style="dim", end="", markup=False, highlight=False)

    def _on_step_skip(self, ev: dict) -> None:
        if self._board_active():
            idx = self._row_index(ev["step"]["title"])
            if idx >= 0:
                self._rows[idx]["status"] = ev.get("status", DONE)
                self._refresh()
            return
        self.console.print(Text(
            f"⏭️  跳过已完成步骤: {ev['step']['title']} (resume 复用)", style="dim"))

    def _on_step_done(self, ev: dict) -> None:
        title = ev["step"]["title"]
        cost = ev.get("cost_usd")
        if self._board_active():
            idx = self._row_index(title)
            if idx >= 0:
                self._rows[idx]["status"] = DONE if ev.get("ok") else FAILED
                self._rows[idx]["cost"] = cost
                if ev.get("text"):
                    self._texts[title] = ev["text"]
                self._refresh()
            elif ev.get("text"):  # debug 修复步等动态步骤
                self._texts[title] = ev["text"]
            return
        if ev.get("dry_run"):
            self.console.print(Text("(dry-run: 未实际调用后端)", style="dim italic"))
            return
        if ev.get("ok"):
            model = ev.get("model", "")
            usage = ev.get("usage")
            title_suffix = f" · {model}".rstrip(" ·")
            if usage:
                title_suffix += f" · {usage.get('input_tokens',0)}↑{usage.get('output_tokens',0)}↓"
            if cost is not None:
                title_suffix += f" · ${cost:.4f}"
            self.console.print(Panel(
                _clip(ev.get("text", "")), title=f"✅ 完成{title_suffix}", border_style="green"))
        else:
            self.console.print(Panel(
                ev.get("error", "") or ev.get("text", "") or "(无输出)",
                title="❌ 失败", border_style="red"))

    # ---- 校验 / debug ----
    def _on_verify_skip(self, ev: dict) -> None:
        if not self._board_active():
            self.console.print(Text(f"(dry-run: 跳过校验 `{ev.get('cmd')}`)", style="dim"))

    def _on_verify_done(self, ev: dict) -> None:
        if self._board_active():
            return
        if ev.get("ok"):
            self.console.print(Text("✅ 校验通过", style="bold green"))
        else:
            self.console.print(Panel(
                _clip(ev.get("output", "")), title="❌ 校验失败", border_style="red"))

    def _on_debug_start(self, ev: dict) -> None:
        if not self._board_active():
            self.console.print(Rule(f"🔎 debugger 排查 · 第 {ev.get('round')} 轮", style="red"))

    def _on_debug_done(self, ev: dict) -> None:
        if self._board_active():
            return
        if ev.get("ok"):
            self.console.print(Panel(
                _clip(ev.get("text", "")), title="debugger 根因分析", border_style="yellow"))
        else:
            self.console.print(Panel(
                ev.get("error", "") or "(无输出)", title="debugger 失败", border_style="red"))

    def _on_error(self, ev: dict) -> None:
        self.console.print(Panel(str(ev.get("error", "")), title="错误", border_style="red"))

    # ---- 汇总 ----
    def _on_report(self, ev: dict) -> None:
        r = ev["report"]
        self._cost_total = r.get("cost_total_usd")
        if self._board_active():
            self._stop_board()
        ok = r.get("verify_ok")
        color = "green" if ok in (True, None) else "red"
        extra = ""
        if r.get("session_id"):
            extra = f"  [dim](session {r['session_id']})[/]"
        self.console.print(Panel(r.get("final", ""), title="🏁 总结" + extra, border_style=color))
