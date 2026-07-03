"""后端抽象与工厂.

所有后端实现同一个接口 Backend.complete(request)->BackendResult,
因此路由/编排层无需关心底层是 CLI 还是 HTTP.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import BackendConfig

log = logging.getLogger("conductor.backend")


@dataclass
class BackendRequest:
    prompt: str
    system: str | None = None
    role: str = ""
    cwd: Path | None = None
    # 强制 JSON 输出(planner 解析计划时用)
    json_mode: bool = False
    timeout: int | None = None
    # 透传给调用方/渲染层
    label: str = ""


@dataclass
class BackendResult:
    ok: bool
    text: str
    error: str | None = None
    model: str | None = None
    # 各后端可放诊断信息, 如实际执行的命令 / 耗时
    meta: dict[str, Any] = field(default_factory=dict)


class Backend(ABC):
    """后端基类."""

    kind: str = "abstract"

    def __init__(self, cfg: BackendConfig) -> None:
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.cfg.name

    @abstractmethod
    def complete(self, req: BackendRequest) -> BackendResult:
        """执行一次请求."""

    @abstractmethod
    def health(self) -> tuple[bool, str]:
        """健康检查: 返回 (是否就绪, 说明)."""

    # 供渲染/调试用: 这个后端"会怎么跑"这次请求(不真正执行)
    def describe(self, req: BackendRequest) -> str:
        prompt = req.prompt
        if len(prompt) > 200:
            prompt = prompt[:200] + " …(截断)"
        return f"[{self.name}/{self.kind}] {prompt}"


def make_backend(cfg: BackendConfig) -> Backend:
    """根据 BackendConfig.type 构造对应后端."""
    t = cfg.type
    if t == "claude-cli":
        from .cli_backends import ClaudeCliBackend

        return ClaudeCliBackend(cfg)
    if t == "codex-cli":
        from .cli_backends import CodexCliBackend

        return CodexCliBackend(cfg)
    if t == "openai-compatible":
        from .openai_compat import OpenAICompatibleBackend

        return OpenAICompatibleBackend(cfg)
    raise ValueError(f"未知后端类型: {t!r} (后端 {cfg.name!r})")
