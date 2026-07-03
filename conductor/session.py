"""会话持久化: 让一次 run 可被中断后 resume, 也便于事后查看.

会话以 JSON 存于 ~/.conductor/sessions/<id>.json. 时间戳用 ISO 字符串保证可序列化.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import app_dir

# 步骤状态
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

# 会话状态
S_PLANNING = "planning"
S_RUNNING = "running"
S_DONE = "done"
S_FAILED = "failed"
S_INTERRUPTED = "interrupted"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


@dataclass
class StepRecord:
    title: str
    role: str
    instruction: str = ""
    status: str = PENDING
    text: str = ""
    error: str | None = None
    model: str | None = None
    usage: dict | None = None
    cost_usd: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    skipped: bool = False

    @property
    def duration_s(self) -> float | None:
        if self.started_at and self.finished_at:
            try:
                return (datetime.fromisoformat(self.finished_at)
                        - datetime.fromisoformat(self.started_at)).total_seconds()
            except ValueError:
                return None
        return None


@dataclass
class Session:
    id: str = field(default_factory=_new_id)
    task: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    status: str = S_PLANNING
    plan_source: str = "planner"        # planner | fallback
    plan_ok: bool = True
    steps: list[dict] = field(default_factory=list)   # step 视图(标题/角色/指令/depends_on)
    records: dict[str, dict] = field(default_factory=dict)  # title -> StepRecord.asdict
    verify_ok: bool | None = None
    verify_output: str = ""
    debug_rounds: int = 0
    cost_total_usd: float | None = None
    final: str = ""

    def touch(self) -> None:
        self.updated_at = _now()

    def upsert_record(self, rec: StepRecord) -> None:
        self.records[rec.title] = asdict(rec)
        self.touch()

    def record_for(self, title: str) -> StepRecord | None:
        d = self.records.get(title)
        if not d:
            return None
        return StepRecord(**d)

    def is_step_done(self, title: str) -> bool:
        """该步骤是否已完成(resume 时跳过)."""
        rec = self.record_for(title)
        return bool(rec and rec.status in (DONE, SKIPPED))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        valid = set(cls.__dataclass_fields__.keys())
        return cls(**{k: v for k, v in d.items() if k in valid})


class SessionStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.dir = (base_dir or (app_dir() / "sessions"))
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, sid: str) -> Path:
        return self.dir / f"{sid}.json"

    def save(self, session: Session) -> Path:
        session.touch()
        p = self.path(session.id)
        p.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p

    def load(self, sid: str) -> Session | None:
        p = self.path(sid)
        if not p.is_file():
            return None
        try:
            return Session.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            return None

    def list(self, limit: int = 20) -> list[Session]:
        sessions: list[Session] = []
        for p in sorted(self.dir.glob("*.json"), reverse=True):
            s = self.load(p.stem)
            if s:
                sessions.append(s)
            if len(sessions) >= limit:
                break
        return sessions

    def latest(self) -> Session | None:
        items = self.list(limit=1)
        return items[0] if items else None
