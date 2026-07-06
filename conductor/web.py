"""Conductor Web UI 后端(FastAPI).

提供:
- GET  /                     SPA 首页
- GET  /api/config           角色/后端健康/版本
- GET  /api/sessions         会话列表
- GET  /api/sessions/{id}    会话详情
- POST /api/run              启动一次编排(后台线程), 返回 session_id
- GET  /api/sessions/{id}/events  SSE 实时事件流
- GET/POST/DELETE /api/memory[/{id}]  跨会话记忆

可选依赖: pip install 'conductor[web]'  (fastapi + uvicorn)
启动: conductor web [--host --port]
"""

# 注意: 本模块不使用 `from __future__ import annotations` —— FastAPI 需要运行时
# 解析 endpoint 的类型注解(局部定义的 Pydantic 模型), 字符串化注解会解析失败。

import asyncio
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATIC_DIR = Path(__file__).parent / "web_static"


@dataclass
class _WebState:
    work_dir: Path
    queues: dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def create_app(work_dir: Path | str | None = None):
    """构造 FastAPI 应用. 延迟导入, 未装 web 依赖时由调用方提示."""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel

    from . import __version__
    from .backends import make_backend
    from .config import load_config
    from .memory import MemoryStore
    from .orchestrator import Orchestrator
    from .session import Session, SessionStore

    state = _WebState(work_dir=Path(work_dir) if work_dir else Path.cwd())
    app = FastAPI(title="Conductor", version=__version__)

    class RunReq(BaseModel):
        task: str
        dry_run: bool = False
        jobs: int = 1
        stream: bool = False
        isolate: bool = False
        workdir: str | None = None
        memory: bool = True

    class MemoryReq(BaseModel):
        key: str
        content: str
        tags: list[str] = []
        scope: str = "project"

    @app.get("/")
    def index():
        idx = STATIC_DIR / "index.html"
        if idx.is_file():
            return FileResponse(idx)
        return JSONResponse({"error": "web_static/index.html 缺失"}, status_code=500)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/api/config")
    def api_config():
        cfg = load_config()
        backends: dict[str, Any] = {}
        for name, bc in cfg.backends.items():
            try:
                ok, note = make_backend(bc).health()
            except Exception as e:  # noqa: BLE001
                ok, note = False, str(e)
            backends[name] = {"type": bc.type, "model": bc.model, "ready": ok, "note": note}
        return {"version": __version__, "roles": cfg.roles, "backends": backends}

    @app.get("/api/sessions")
    def api_sessions():
        items = SessionStore().list(limit=50)
        return {"sessions": [_session_summary(s) for s in items]}

    @app.get("/api/sessions/{sid}")
    def api_session(sid: str):
        s = SessionStore().load(sid)
        if not s:
            raise HTTPException(status_code=404, detail="session not found")
        return _session_detail(s)

    @app.post("/api/run")
    async def api_run(req: RunReq):
        cfg = load_config()
        wd = Path(req.workdir) if req.workdir else state.work_dir
        mem = MemoryStore(work_dir=wd).context_text() if req.memory else ""
        session = Session(task=req.task)
        SessionStore().save(session)

        loop = asyncio.get_running_loop()
        q: "asyncio.Queue" = asyncio.Queue()
        with state.lock:
            state.queues[session.id] = q

        def emit(ev: dict):
            try:
                loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError:
                pass  # 事件循环已关闭

        def worker():
            try:
                orch = Orchestrator(cfg, work_dir=wd, max_workers=req.jobs,
                                    isolate=req.isolate, memory_context=mem)
                orch.emit = emit
                orch.run(req.task, dry_run=req.dry_run, stream=req.stream,
                         resume_id=session.id)
            except Exception as e:  # noqa: BLE001
                emit({"type": "error", "error": f"运行异常: {e}"})
            finally:
                emit({"type": "_end"})

        threading.Thread(target=worker, daemon=True).start()
        return {"session_id": session.id}

    @app.get("/api/sessions/{sid}/events")
    async def api_events(sid: str, request: Request):
        q = state.queues.get(sid)

        async def gen():
            if q is None:
                s = SessionStore().load(sid)
                if s:
                    yield _sse({"type": "snapshot", "session": _session_detail(s)})
                yield _sse({"type": "_end"})
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keepalive
                    continue
                yield _sse(ev)
                if ev.get("type") == "_end":
                    break

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/api/memory")
    def api_memory():
        items = MemoryStore(work_dir=state.work_dir).list()
        return {"items": [i.to_dict() for i in items]}

    @app.post("/api/memory")
    def api_memory_add(req: MemoryReq):
        return MemoryStore(work_dir=state.work_dir).add(
            req.key, req.content, req.tags, req.scope).to_dict()

    @app.delete("/api/memory/{item_id}")
    def api_memory_del(item_id: str):
        return {"removed": MemoryStore(work_dir=state.work_dir).remove(item_id)}

    return app


def run_server(host: str = "127.0.0.1", port: int = 8765,
               work_dir: Path | str | None = None) -> None:
    import uvicorn

    uvicorn.run(create_app(work_dir=work_dir), host=host, port=port, log_level="info")


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


def _session_summary(s) -> dict:
    n_done = sum(1 for r in s.records.values()
                 if r.get("status") in ("done", "skipped"))
    return {"id": s.id, "task": s.task, "status": s.status,
            "steps": f"{n_done}/{len(s.records)}",
            "cost_usd": s.cost_total_usd, "updated_at": s.updated_at}


def _session_detail(s) -> dict:
    records = []
    for title, r in s.records.items():
        records.append({
            "title": title, "role": r.get("role"), "status": r.get("status"),
            "text": r.get("text", ""), "error": r.get("error"),
            "model": r.get("model"), "usage": r.get("usage"),
            "cost_usd": r.get("cost_usd"),
        })
    return {
        "id": s.id, "task": s.task, "status": s.status,
        "plan_source": s.plan_source, "steps": s.steps, "records": records,
        "verify_ok": s.verify_ok, "debug_rounds": s.debug_rounds,
        "cost_total_usd": s.cost_total_usd, "final": s.final,
        "created_at": s.created_at, "updated_at": s.updated_at,
    }
