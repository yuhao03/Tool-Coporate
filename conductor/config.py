"""配置: 数据结构、加载、合并、默认值。

加载顺序(后者覆盖前者):
    内置默认  ->  用户配置 ~/.conductor/config.toml  ->  项目级 ./conductor.toml
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# tomllib 仅 3.11+ 内置; 3.10 用 tomli 回退.
try:  # pragma: no cover - 环境相关
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml  # type: ignore[no-redef]


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class BackendConfig:
    """单个后端(适配器)的配置."""

    name: str = ""
    # claude-cli | codex-cli | openai-compatible
    type: str = ""
    # 可选模型覆盖; 留空则用后端默认
    model: str = ""
    # openai-compatible 相关
    base_url: str = ""
    api_key_env: str = ""
    api_key: str = ""
    temperature: float | None = None
    # CLI 后端相关
    executable: str = ""           # 覆盖二进制路径
    extra_args: list[str] = field(default_factory=list)
    extra_config: dict[str, str] = field(default_factory=dict)  # codex -c key=val
    full_auto: bool = True         # codex 全自动(绕过审批/沙箱)
    timeout: int = 900             # 秒

    @property
    def resolved_api_key(self) -> str:
        """先取明文 api_key, 否则取 api_key_env 指向的环境变量."""
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""


@dataclass
class OrchestrationConfig:
    max_debug_rounds: int = 2
    verify_command: str = ""
    plan_fallback_role: str = "coder"


@dataclass
class Config:
    backends: dict[str, BackendConfig] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    orchestration: OrchestrationConfig = field(default_factory=OrchestrationConfig)
    sources: list[Path] = field(default_factory=list)  # 实际加载过的文件

    def role_for(self, role: str) -> str:
        if role not in self.roles:
            raise KeyError(f"未定义角色: {role!r} (已配置: {list(self.roles)})")
        backend_name = self.roles[role]
        if backend_name not in self.backends:
            raise KeyError(f"角色 {role!r} 指向了未配置的后端 {backend_name!r}")
        return backend_name

    def backend_for(self, role: str) -> BackendConfig:
        return self.backends[self.role_for(role)]


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def app_dir() -> Path:
    """配置目录 ~/.conductor ."""
    env = os.environ.get("CONDUCTOR_HOME")
    return Path(env) if env else Path.home() / ".conductor"


def user_config_path() -> Path:
    return app_dir() / "config.toml"


def project_config_path(start: Path | None = None) -> Path | None:
    """从 start(默认 cwd) 向上找仓库根的 conductor.toml."""
    here = (start or Path.cwd()).resolve()
    for cur in [here, *here.parents]:
        candidate = cur / "conductor.toml"
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# 默认配置
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG_TOML = """\
# Conductor 配置 — 见 DESIGN.md
# 全局配置放 ~/.conductor/config.toml; 仓库根的 conductor.toml 可做项目级覆盖.

[backends.claude]
type = "claude-cli"
# model = "claude-fable-5"          # 留空则用 claude 默认模型

[backends.codex]
type = "codex-cli"
full_auto = true                   # 用 --dangerously-bypass-approvals-and-sandbox 实现全自动

[backends.glm]
type = "openai-compatible"
base_url = "https://open.bigmodel.cn/api/paas/v4/"
model = "glm-5.2"
api_key_env = "ZHIPU_API_KEY"
# api_key = "sk-..."               # 或直接写死密钥

[roles]
planner  = "claude"
coder    = "codex"
debugger = "glm"
designer = "glm"

[orchestration]
max_debug_rounds = 2
verify_command = ""                # 例: "pytest -q" / "npm run build"
plan_fallback_role = "coder"
"""


def _default_config() -> Config:
    """从内置 TOML 构造默认 Config(保证与 init 写出的文件一致)."""
    return _build_config(_toml.loads(DEFAULT_CONFIG_TOML))


# --------------------------------------------------------------------------- #
# 合并 / 构造
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _dataclass_from(cls: type, data: dict[str, Any]) -> Any:
    """按字段名构造 dataclass, 忽略未知键."""
    known = {f.name for f in fields(cls)}
    filtered = {k: v for k, v in data.items() if k in known}
    return cls(**filtered)


def _build_config(data: dict[str, Any]) -> Config:
    backends: dict[str, BackendConfig] = {}
    for name, raw in (data.get("backends") or {}).items():
        bc = _dataclass_from(BackendConfig, raw or {})
        bc.name = name
        backends[name] = bc
    roles = dict(data.get("roles") or {})
    orch = _dataclass_from(OrchestrationConfig, data.get("orchestration") or {})
    return Config(backends=backends, roles=roles, orchestration=orch)


def load_config(project_dir: Path | None = None) -> Config:
    """加载并合并配置. 缺少文件不报错, 一律有合理默认."""
    merged: dict[str, Any] = _toml.loads(DEFAULT_CONFIG_TOML)
    sources: list[Path] = []
    user = user_config_path()
    if user.is_file():
        merged = _deep_merge(merged, _toml.loads(user.read_text(encoding="utf-8")))
        sources.append(user)
    proj = project_config_path(project_dir)
    if proj:
        merged = _deep_merge(merged, _toml.loads(proj.read_text(encoding="utf-8")))
        sources.append(proj)
    cfg = _build_config(merged)
    cfg.sources = sources
    return cfg


def write_default_config(path: Path | None = None) -> Path:
    """写出默认配置(若不存在). 返回路径."""
    target = path or user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return target
