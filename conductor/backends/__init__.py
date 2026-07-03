"""后端适配器: claude CLI / codex CLI / OpenAI 兼容(GLM 等)."""

from __future__ import annotations

from .base import Backend, BackendRequest, BackendResult, make_backend

__all__ = ["Backend", "BackendRequest", "BackendResult", "make_backend"]
