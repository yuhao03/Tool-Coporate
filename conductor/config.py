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
    # 注入子进程的环境变量(值支持 ${VAR} 展开, 引用 os.environ)
    # 用途: 把 claude CLI 指向智谱 Z.ai/BigModel 的 Anthropic 兼容端点 -> GLM 也走 claude CLI
    env: dict[str, str] = field(default_factory=dict)

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
    # 成本定价覆盖: {model: (input_usd_per_Mtok, output_usd_per_Mtok)}; 空则用内置默认
    pricing: dict[str, tuple[float, float]] = field(default_factory=dict)
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


def overrides_path() -> Path:
    """模型选择覆盖文件(由 conductor model 写入), 与主配置分离, 保护主配置注释。"""
    return app_dir() / "overrides.toml"


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

# GLM: 复用 claude CLI, 通过 env 指向智谱 Anthropic 兼容端点 = "GLM 的 CLI"。
# 需 export ZHIPU_API_KEY=sk-...(智谱开放平台 API Key)。
# 海外账号把 BASE_URL 换成 https://api.z.ai/api/anthropic, key 用 Z.ai 的。
[backends.glm]
type = "claude-cli"
model = "glm-5.2"
[backends.glm.env]
ANTHROPIC_BASE_URL = "https://open.bigmodel.cn/api/anthropic"
ANTHROPIC_API_KEY = "${ZHIPU_API_KEY}"

# —— 备选: 直接走 HTTP(OpenAI 兼容), 不经 claude CLI ——
# [backends.glm]
# type = "openai-compatible"
# base_url = "https://open.bigmodel.cn/api/paas/v4/"
# model = "glm-5.2"
# api_key_env = "ZHIPU_API_KEY"

[roles]
planner  = "claude"
coder    = "codex"
debugger = "glm"
designer = "glm"

[orchestration]
max_debug_rounds = 2
verify_command = ""                # 例: "pytest -q" / "npm run build"
plan_fallback_role = "coder"

# [cost] 可选: 覆盖内置定价(USD / 每百万 token). 不写则用内置默认估算.
# [cost.pricing]
# "glm-5.2" = { input = 0.6, output = 2.2 }
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
    # 可选定价覆盖: [cost.pricing] 下 model = {input=.., output=..}
    pricing: dict[str, tuple[float, float]] = {}
    for model, vals in ((data.get("cost") or {}).get("pricing") or {}).items():
        try:
            pricing[model] = (float(vals.get("input", 0)), float(vals.get("output", 0)))
        except (AttributeError, TypeError, ValueError):
            continue
    return Config(backends=backends, roles=roles, orchestration=orch, pricing=pricing)


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
    # 应用模型选择覆盖(conductor model 写入的 overrides.toml)
    for name, model in _load_model_overrides().items():
        if name in cfg.backends:
            cfg.backends[name].model = model
    cfg.sources = sources
    return cfg


def _load_model_overrides() -> dict[str, str]:
    import json

    p = overrides_path()
    if not p.is_file():
        return {}
    try:
        data = _toml.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for name, sec in (data.get("backends") or {}).items():
        if isinstance(sec, dict) and sec.get("model"):
            out[name] = str(sec["model"])
    return out


def save_model_overrides(models: dict[str, str]) -> Path:
    """把 {backend: model} 写入 overrides.toml(覆盖主配置里的 model 字段)。"""
    import json

    p = overrides_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    parts = ["# 由 conductor model 写入; 覆盖主配置里的 model 字段\n"]
    for name, model in models.items():
        parts.append(f"[backends.{name}]\nmodel = {json.dumps(str(model))}\n\n")
    p.write_text("".join(parts), encoding="utf-8")
    return p


def write_default_config(path: Path | None = None) -> Path:
    """写出默认配置(若不存在). 返回路径."""
    target = path or user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return target
