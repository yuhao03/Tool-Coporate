"""跨会话记忆: 让 Conductor 在多次任务间记住项目约定/偏好/事实.

两级存储:
- global: ~/.conductor/memory.json  (用户级, 跨项目)
- project: <work_dir>/.conductor/memory.json  (项目级, 建议加入 .gitignore)

记忆会在 run/plan 时作为上下文注入 planner 与各执行步骤.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .config import app_dir

GLOBAL_SCOPE = "global"
PROJECT_SCOPE = "project"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class MemoryItem:
    id: str
    key: str
    content: str
    tags: list[str] = field(default_factory=list)
    scope: str = PROJECT_SCOPE
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


class MemoryStore:
    def __init__(self, work_dir: Path | str | None = None, base_dir: Path | None = None) -> None:
        self.work_dir = Path(work_dir) if work_dir else Path.cwd()
        self.global_path = (base_dir or app_dir()) / "memory.json"
        self.project_path = self.work_dir / ".conductor" / "memory.json"

    def _path(self, scope: str) -> Path:
        return self.global_path if scope == GLOBAL_SCOPE else self.project_path

    def _load(self, scope: str) -> list[dict]:
        p = self._path(scope)
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        items = data.get("items", []) if isinstance(data, dict) else []
        return [i for i in items if isinstance(i, dict)]

    def _save(self, scope: str, items: list[dict]) -> None:
        p = self._path(scope)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2),
                     encoding="utf-8")

    def add(self, key: str, content: str, tags: list[str] | None = None,
            scope: str = PROJECT_SCOPE) -> MemoryItem:
        if scope not in (GLOBAL_SCOPE, PROJECT_SCOPE):
            scope = PROJECT_SCOPE
        items = self._load(scope)
        item = MemoryItem(id=secrets.token_hex(4), key=key, content=content,
                          tags=tags or [], scope=scope)
        items.append(item.to_dict())
        self._save(scope, items)
        return item

    def list(self, scope: str | None = None, tag: str | None = None) -> list[MemoryItem]:
        out: list[MemoryItem] = []
        scopes = [scope] if scope else [GLOBAL_SCOPE, PROJECT_SCOPE]
        for sc in scopes:
            for raw in self._load(sc):
                if tag and tag not in raw.get("tags", []):
                    continue
                try:
                    out.append(MemoryItem(**{k: raw.get(k) for k in
                                             MemoryItem.__dataclass_fields__}))  # type: ignore[attr-defined]
                except TypeError:
                    continue
        return out

    def remove(self, item_id: str) -> bool:
        removed = False
        for sc in (GLOBAL_SCOPE, PROJECT_SCOPE):
            items = self._load(sc)
            new = [i for i in items if i.get("id") != item_id]
            if len(new) != len(items):
                self._save(sc, new)
                removed = True
        return removed

    def context_text(self, max_items: int = 12, max_chars: int = 2000) -> str:
        """拼成可注入提示词的文本; 无记忆返回空串."""
        items = self.list()
        if not items:
            return ""
        lines = [f"- [{i.scope}] {i.key}: {i.content}" for i in items[-max_items:]]
        text = "[跨会话记忆]\n" + "\n".join(lines)
        return text[:max_chars]
