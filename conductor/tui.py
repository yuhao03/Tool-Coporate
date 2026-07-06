"""Conductor 终端原生交互式 TUI 仪表盘 (textual)。

满屏键盘驱动的 App，无需记子命令:
- 运行: 输入任务 + 选项 → 实时步骤看板(DataTable) + 流式输出(RichLog)
- 会话: 浏览历史会话, 选中看详情
- 记忆: 查看 / 新增 / 删除跨会话记忆

启动: conductor tui   (需可选依赖: uv pip install 'conductor[tui]')

编排在后端线程运行, 通过 call_from_thread 把事件送回 UI 线程, 保证界面流畅。
orch_factory 可注入, 便于测试。
"""

import threading
from pathlib import Path
from typing import Callable

from .cost import format_cost

ROLE_LABEL = {"planner": "方案", "coder": "执行", "debugger": "排错", "designer": "前端"}
ROLE_COLOR = {"planner": "cyan", "coder": "green", "debugger": "red", "designer": "magenta"}
ICON = {"pending": "⏸️ ", "running": "🔄", "done": "✅", "failed": "❌", "skipped": "⏭️"}

STATUS_TEXT = {"pending": "等待", "running": "运行", "done": "完成", "failed": "失败", "skipped": "跳过"}

# textual 样式(模块级, 供 App 类体引用 —— 类体不闭包外层函数局部)
_TUI_CSS = """
Screen { layout: vertical; }
#topbar { background: $boost; padding: 0 1; height: 1; }
TabbedContent { height: 1fr; }
.row { height: auto; layout: horizontal; padding: 0; }
.row Input { margin-right: 1; }
#task-input { height: 3; }
#steps { height: 1fr; border: round $accent; }
#stream { height: 1fr; border: round $primary; }
#session-detail, #mem-list { height: 1fr; border: round $primary; }
.muted { color: $text-muted; }
Button { margin-right: 1; }
"""


def _default_orch_factory() -> Callable:
    """返回一个构造 Orchestrator 的闭包(读取本地配置 + 项目记忆)."""
    from .config import load_config
    from .memory import MemoryStore
    from .orchestrator import Orchestrator

    def build():
        cfg = load_config()
        wd = Path.cwd()
        mem = MemoryStore(work_dir=wd).context_text()
        return Orchestrator(cfg, work_dir=wd, memory_context=mem)

    return build


class ConductorTUI:
    """占位: 真正的 textual App 在 _ConductorApp(下方延迟定义)。"""


def create_app(orch_factory: Callable[[], object] | None = None, work_dir: Path | str | None = None):
    """构造 textual App。延迟导入 textual, 未安装时由调用方提示。"""
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, Vertical, VerticalScroll
        from textual.widgets import (
            Button, Checkbox, DataTable, Footer, Header, Input, Label,
            ListItem, ListView, RichLog, Select, TabbedContent, TabPane,
        )
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            "缺少 tui 依赖。请安装: uv pip install 'conductor[tui]'  或  pip install textual"
        ) from e

    from .memory import GLOBAL_SCOPE, PROJECT_SCOPE, MemoryStore
    from .session import SessionStore

    factory = orch_factory or _default_orch_factory()
    base_dir = Path(work_dir) if work_dir else Path.cwd()

    class _App(App):
        TITLE = "🎼 Conductor"
        CSS = _TUI_CSS
        BINDINGS = [
            Binding("q", "quit", "退出"),
            Binding("1", "tab('run')", "运行"),
            Binding("2", "tab('sessions')", "会话"),
            Binding("3", "tab('memory')", "记忆"),
            Binding("r", "focus('task-input')", "聚焦输入"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._factory = factory
            self._work_dir = base_dir
            self._step_order: list[str] = []
            self._step_meta: dict[str, dict] = {}

        # ---- compose ----
        def compose(self) -> "ComposeResult":
            yield Header(show_clock=False)
            yield Footer()
            yield Label(self._backends_line(), id="topbar")
            with TabbedContent():
                with TabPane("▶ 运行 [1]", id="run"):
                    yield Input(placeholder="描述任务, 回车或点开始… 例: 加一个带校验的登录页",
                                id="task-input")
                    with Horizontal(classes="row"):
                        yield Checkbox("流式", id="opt-stream", value=True)
                        yield Checkbox("预演", id="opt-dryrun", value=False)
                        yield Checkbox("隔离", id="opt-isolate", value=False)
                        yield Label("并发:", classes="muted")
                        yield Input(value="1", id="opt-jobs")
                        yield Button("开始编排 ▶", id="btn-start", variant="success")
                    with Vertical():
                        yield DataTable(id="steps")
                        yield RichLog(id="stream", highlight=False, markup=True)
                with TabPane("📂 会话 [2]", id="sessions"):
                    with Horizontal(classes="row"):
                        yield Button("刷新", id="btn-refresh-sessions", variant="primary")
                    with Vertical():
                        yield ListView(id="sessions-list")
                        yield RichLog(id="session-detail", highlight=False, markup=True)
                with TabPane("🧠 记忆 [3]", id="memory"):
                    yield Input(placeholder="键, 如 技术栈", id="mem-key")
                    yield Input(placeholder="内容, 如 FastAPI + React", id="mem-content")
                    with Horizontal(classes="row"):
                        yield Select(
                            [("项目", PROJECT_SCOPE), ("全局", GLOBAL_SCOPE)],
                            id="mem-scope", value=PROJECT_SCOPE,
                        )
                        yield Button("＋ 加入", id="btn-mem-add", variant="success")
                        yield Button("删除选中", id="btn-mem-del", variant="error")
                    yield ListView(id="mem-list")

        # ---- mount ----
        def on_mount(self) -> None:
            table = self.query_one("#steps", DataTable)
            table.add_columns("#", "角色", "步骤", "状态", "成本")
            table.cursor_type = "none"
            self._refresh_sessions()
            self._refresh_memory()

        def _backends_line(self) -> str:
            try:
                from .backends import make_backend
                from .config import load_config
                cfg = load_config()
                parts = []
                for role in ("planner", "coder", "debugger", "designer"):
                    be = cfg.roles.get(role, "?")
                    parts.append(f"[{ROLE_LABEL.get(role, role)}]{role}→{be}")
                return "  ·  ".join(parts)
            except Exception:
                return "Conductor"

        # ---- keybindings ----
        def action_tab(self, tab: str) -> None:
            self.query_one(TabbedContent).active = tab

        # ---- run ----
        def on_input_submitted(self, event: Input.Submitted) -> None:
            if event.input.id == "task-input":
                self._start_run()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            bid = event.button.id
            if bid == "btn-start":
                self._start_run()
            elif bid == "btn-refresh-sessions":
                self._refresh_sessions()
            elif bid == "btn-mem-add":
                self._add_memory()
            elif bid == "btn-mem-del":
                self._del_memory()

        def _start_run(self) -> None:
            task = self.query_one("#task-input", Input).value.strip()
            log = self.query_one("#stream", RichLog)
            if not task:
                log.write("[yellow]请先输入任务描述[/]")
                return
            stream = self.query_one("#opt-stream", Checkbox).value
            dry = self.query_one("#opt-dryrun", Checkbox).value
            isolate = self.query_one("#opt-isolate", Checkbox).value
            try:
                jobs = max(1, int(self.query_one("#opt-jobs", Input).value or "1"))
            except ValueError:
                jobs = 1
            self.query_one("#btn-start", Button).disabled = True
            log.clear()
            table = self.query_one("#steps", DataTable)
            table.clear()
            self._step_order = []
            self._step_meta = {}
            log.write(f"[bold cyan]▶ 开始编排:[/] {task}\n")

            orch = self._factory()
            orch.max_workers = jobs
            orch.isolate = isolate
            orch.emit = self._emit

            def worker():
                try:
                    orch.run(task, dry_run=dry, stream=stream)
                except Exception as e:  # noqa: BLE001
                    self.call_from_thread(self._run_error, str(e))
                finally:
                    self.call_from_thread(self._run_done)

            threading.Thread(target=worker, daemon=True).start()

        def _emit(self, ev: dict) -> None:
            try:
                self.call_from_thread(self.handle_event, ev)
            except RuntimeError:
                pass

        def _run_done(self) -> None:
            self.query_one("#btn-start", Button).disabled = False
            self.query_one("#stream", RichLog).write("[dim]— 编排结束 —[/]")
            self._refresh_sessions()

        def _run_error(self, msg: str) -> None:
            self.query_one("#stream", RichLog).write(f"[red]错误: {msg}[/]")

        # ---- 事件 → UI ----
        def handle_event(self, ev: dict) -> None:
            t = ev.get("type")
            table = self.query_one("#steps", DataTable)
            log = self.query_one("#stream", RichLog)
            if t == "steps":
                table.clear()
                self._step_order = [s["title"] for s in ev.get("steps", [])]
                self._step_meta = {s["title"]: s for s in ev.get("steps", [])}
                for i, title in enumerate(self._step_order, 1):
                    s = self._step_meta[title]
                    table.add_row(str(i), ROLE_LABEL.get(s["role"], s["role"]),
                                  title, STATUS_TEXT["pending"], "—", key=title)
            elif t == "step_start":
                self._set_step(ev["step"]["title"], status="running")
            elif t == "step_delta":
                role = ev["step"].get("role", "")
                log.write(f"[{ROLE_COLOR.get(role,'white')}]{ev.get('text','')}[/]", shrink=False)
            elif t == "step_done":
                title = ev["step"]["title"]
                status = "done" if ev.get("ok") else "failed"
                self._set_step(title, status=status, cost=ev.get("cost_usd"))
                if ev.get("ok"):
                    log.write(f"\n[green]✅ {title}[/] "
                              + (f"({ev.get('model','')})" if ev.get("model") else "") + "\n")
                    if ev.get("text"):
                        log.write(ev["text"][:1200] + ("\n…" if len(ev.get("text", "")) > 1200 else ""))
                else:
                    log.write(f"\n[red]❌ {title}: {ev.get('error','')}[/]\n")
            elif t == "step_skip":
                self._set_step(ev["step"]["title"], status="skipped")
            elif t == "verify_done":
                log.write(("[green]✅ 校验通过[/]\n") if ev.get("ok")
                          else f"[red]❌ 校验失败[/]\n{ev.get('output','')[:600]}\n")
            elif t == "debug_done":
                if ev.get("text"):
                    log.write(f"\n[yellow]🔎 debugger:[/]\n{ev['text'][:1500]}\n")
            elif t == "report":
                r = ev.get("report", {})
                color = "green" if r.get("verify_ok") in (True, None) else "red"
                cost = format_cost(r.get("cost_total_usd"))
                log.write(f"\n[bold {color}]🏁 {r.get('final','')}[/]  [dim]成本 {cost}[/]\n")

        def _set_step(self, title: str, status: str = "pending",
                      cost: float | None = None) -> None:
            table = self.query_one("#steps", DataTable)
            if title not in self._step_meta:
                self._step_meta[title] = {"role": "", "title": title}
                self._step_order.append(title)
                table.add_row(str(len(self._step_order)), "?", title,
                              STATUS_TEXT.get(status, "·"), "—", key=title)
            try:
                table.update_cell(title, "状态", f"{ICON.get(status,'')} {STATUS_TEXT.get(status,status)}")
                if cost is not None:
                    table.update_cell(title, "成本", format_cost(cost))
            except Exception:
                pass

        # ---- 会话 ----
        def _refresh_sessions(self) -> None:
            lv = self.query_one("#sessions-list", ListView)
            lv.clear()
            for s in SessionStore().list(limit=50):
                item = ListItem(Label(
                    f"{s.id[:13]}  [{s.status}]  {s.task[:30]}  {format_cost(s.cost_total_usd)}"))
                item.session_id = s.id  # type: ignore[attr-defined]
                lv.append(item)

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            if event.list_view.id != "sessions-list":
                return
            sid = getattr(event.item, "session_id", None)
            if not sid:
                return
            s = SessionStore().load(sid)
            detail = self.query_one("#session-detail", RichLog)
            detail.clear()
            if not s:
                detail.write("[red]会话不存在[/]")
                return
            detail.write(f"[bold]{s.task}[/]\n[dim]{s.status} · {s.plan_source} · "
                         f"debug {s.debug_rounds} · {format_cost(s.cost_total_usd)}[/]\n")
            for title, r in s.records.items():
                icon = ICON.get(r.get("status"), "·")
                detail.write(f"{icon} [{r.get('role','')}] {title} "
                             f"[dim]{format_cost(r.get('cost_usd'))}[/]")
            if s.final:
                detail.write(f"\n[green]{s.final}[/]")

        # ---- 记忆 ----
        def _refresh_memory(self) -> None:
            lv = self.query_one("#mem-list", ListView)
            lv.clear()
            store = MemoryStore(work_dir=self._work_dir)
            for m in store.list():
                item = ListItem(Label(f"[{m.scope}] {m.key}: {m.content[:50]}"))
                item.mem_id = m.id  # type: ignore[attr-defined]
                lv.append(item)

        def _add_memory(self) -> None:
            key = self.query_one("#mem-key", Input).value.strip()
            content = self.query_one("#mem-content", Input).value.strip()
            if not key or not content:
                return
            scope = self.query_one("#mem-scope", Select).value
            MemoryStore(work_dir=self._work_dir).add(key, content, scope=scope)
            self.query_one("#mem-key", Input).value = ""
            self.query_one("#mem-content", Input).value = ""
            self._refresh_memory()

        def _del_memory(self) -> None:
            lv = self.query_one("#mem-list", ListView)
            if lv.highlighted_child is None:
                return
            mid = getattr(lv.highlighted_child, "mem_id", None)
            if mid and MemoryStore(work_dir=self._work_dir).remove(mid):
                self._refresh_memory()

    return _App()


def run(work_dir: Path | str | None = None) -> None:
    create_app(work_dir=work_dir).run()
