# Conductor — 多 AI 角色编排工具（设计文档）

> 一个把不同 AI 编码助手按"特长"分工协作的跨平台编排器。
> 你定义 **角色**，工具把每个角色路由到对应的 **后端**，并按需自动编排成一条流水线。

---

## 1. 核心理念

不同的 AI 各有所长。与其用一个模型干所有事，不如让每个模型只做自己最擅长的事：

| 角色 | 默认后端 | 职责 |
|------|----------|------|
| `planner`（方案） | Claude | 需求拆解、架构设计、产出可执行计划 |
| `coder`（执行） | Codex | 写代码、改文件、跑命令 |
| `debugger`（排错） | GLM-5.2 | 分析报错、定位根因、给修复建议 |
| `designer`（前端/UI） | GLM-5.2 | UI 设计、前端实现 |

这套映射**完全可配置**——你想让 Claude 来排错、或者 Codex 来做前端，改一行配置即可。

关键洞察：本机已经装好并认证了 `claude`（2.1.x）和 `codex`（0.142.x）两个 CLI。
所以我们**直接复用这两个 CLI 的认证与 agent 循环**（无头模式调用），而不必自己实现一遍代码执行能力。
GLM 没有 CLI，则走智谱 OpenAI 兼容 API。

---

## 2. 架构总览

```
                 ┌───────────────────────────────────────────────┐
   用户任务 ───▶ │                  Orchestrator                  │
                 │                                               │
                 │  1) Plan:  planner(claude) 拆解 → JSON 计划     │
                 │  2) Execute: 每步路由到对应角色后端              │
                 │  3) Verify: 跑校验命令(test/build)              │
                 │  4) Debug Loop: 失败 → debugger(glm) 找根因      │
                 │                  → coder(codex) 按建议修复      │
                 └───────────────────────────────────────────────┘
                                          │
            ┌─────────────────────────────┼─────────────────────────────┐
            ▼                             ▼                             ▼
   ┌─────────────────┐         ┌─────────────────────┐        ┌─────────────────────┐
   │ ClaudeCliBackend│         │ CodexCliBackend     │        │ OpenAICompatible    │
   │ `claude -p ...` │         │ `codex exec ...`    │        │ (GLM / OpenAI / …)  │
   │ 复用现有认证     │         │ 全自动改文件         │        │ Bearer key HTTP     │
   └─────────────────┘         └─────────────────────┘        └─────────────────────┘
```

所有后端实现同一个抽象 `Backend.complete(request) -> BackendResult`，因此：
- 换后端 = 换一个适配器；
- 新增后端 = 实现一个 `Backend` 子类 + 在 config 里注册。

---

## 3. 三种使用模式

1. **全自动编排** `conductor run "<任务>"`
   planner 拆任务 → 逐角色执行 → 失败时 debug 循环 → 汇报。

2. **单角色直问** `conductor ask debugger "<报错信息>"`
   把一段话直接路由到某个角色的后端，适合你已经有明确子任务时手动分工。

3. **只看计划** `conductor plan "<任务>"`
   只跑 planner，打印它产出的分步计划（含每步角色），不执行。便于审阅。

所有命令都支持 `--dry-run`：只打印「会调用谁、传什么」，不真正请求/执行。

---

## 4. 编排流程详解（`run`）

```
task
  │
  ▼
[Plan 阶段]  planner(claude) 收到 meta-prompt，返回 JSON:
             {"steps":[{"title","role","instruction","depends_on"}]}
             解析失败 → 降级为单个 coder 步骤
  │
  ▼
[Execute 阶段]  顺序执行每一步:
   - coder/designer 是"行动型"后端: 真正改文件
   - planner/debugger 是"顾问型"后端: 产出分析，喂给下一步
   - 每步都带上前面所有步的标题+摘要作为上下文
  │
  ▼
[Verify 阶段]  跑配置里的校验命令(默认 None, 可设 `verify_command = "pytest -q"`)
  │
  ├─ 通过 → 结束
  │
  ▼ 失败
[Debug 循环]  (最多 max_debug_rounds 轮, 默认 2)
   - debugger(glm) 拿到「校验输出 + 最近改动 diff」→ 给根因+修复建议
   - coder(codex) 拿到「建议 + 失败信息」→ 修复
   - 重新 Verify
  │
  ▼
[Report]  汇总每步做了什么、耗时、最终成败
```

---

## 5. 后端抽象

```python
@dataclass
class BackendRequest:
    prompt: str
    system: str | None = None
    role: str = ""
    cwd: Path | None = None
    json_mode: bool = False
    timeout: int = 600

@dataclass
class BackendResult:
    ok: bool
    text: str
    error: str | None = None
    model: str | None = None

class Backend(ABC):
    name: str
    def complete(self, req: BackendRequest) -> BackendResult: ...
    def health(self) -> tuple[bool, str]: ...   # 检查二进制/密钥是否就绪
```

三个具体后端：

### 5.1 `ClaudeCliBackend` (type = `claude-cli`)
- 命令：`claude -p "<prompt>"`（print/headless 模式），可选 `--model`、`--output-format`。
- 认证：复用 `~/.claude` 现有登录，**无需 API key**。
- health：`shutil.which("claude")` 是否存在。

### 5.2 `CodexCliBackend` (type = `codex-cli`)
- 命令：`codex exec [flags] "<prompt>"`，`-C/--cd` 指定工作目录。
- 全自动：`--dangerously-bypass-approvals-and-sandbox`（可关闭，改用 `-s workspace-write`），
  配合 `--skip-git-repo-check`、`-o/--output-last-message <tmpfile>` 干净捕获结果。
- 认证：复用 `~/.codex` 现有登录，**无需 API key**。
- health：`shutil.which("codex")` 是否存在。

### 5.3 `OpenAICompatibleBackend` (type = `openai-compatible`)
- 智谱 GLM 等任何 OpenAI 兼容服务。POST `{base_url}/chat/completions`，`Authorization: Bearer <key>`。
- GLM 预设：`base_url=https://open.bigmodel.cn/api/paas/v4/`，`model=glm-5.2`。
- 密钥来源：`api_key`（明文，可选）或 `api_key_env`（环境变量名，默认 `ZHIPU_API_KEY`）。
- health：密钥是否存在。

---

## 6. 配置（`~/.conductor/config.toml`）

声明式、纯 TOML。全局配置在 `~/.conductor/config.toml`，项目级覆盖在仓库根 `conductor.toml`。

```toml
[backends.claude]
type = "claude-cli"
# model = "claude-fable-5"      # 可选，留空则用 claude 默认

[backends.codex]
type = "codex-cli"
full_auto = true                 # 用 --dangerously-bypass-approvals-and-sandbox

[backends.glm]
type = "openai-compatible"
base_url = "https://open.bigmodel.cn/api/paas/v4/"
model = "glm-5.2"
api_key_env = "ZHIPU_API_KEY"
# api_key = "sk-..."            # 或直接写死

[roles]
planner  = "claude"
coder    = "codex"
debugger = "glm"
designer = "glm"

[orchestration]
max_debug_rounds = 2
verify_command = ""              # 如 "pytest -q" / "npm run build"
plan_fallback_role = "coder"     # 计划解析失败时降级为该角色单步
```

---

## 7. 跨平台策略

- **纯 Python 3.10+**，依赖全为跨平台纯 Python 包（typer / rich / httpx / tomli）。
- 子进程调用一律用 `subprocess` + **参数列表**（不用 shell 字符串拼接），Win/Mac/Linux 行为一致。
- 路径用 `pathlib` / `os.path.expanduser`，配置目录 `~/.conductor`。
- 安装方式：`uv tool install -e .` / `pipx install -e .` / `pip install -e .`，任选其一。
- 三个执行后端的可执行文件路径可配置（`executable = "..."`），便于不同平台/版本环境。

---

## 8. 目录结构

```
tool-coporate/
├── pyproject.toml
├── README.md
├── DESIGN.md                 ← 本文档
├── conductor/
│   ├── __init__.py
│   ├── config.py             配置加载/合并/默认 + 定价覆盖
│   ├── roles.py              角色定义 + 系统提示词
│   ├── router.py             planner meta-prompt + JSON 计划解析
│   ├── graph.py              依赖图 + 环检测 + 波次化并发调度
│   ├── session.py            会话/步骤记录 + JSON 持久化(resume)
│   ├── cost.py               token 用量 + 定价表 + 成本估算
│   ├── memory.py             跨会话记忆(全局/项目两级)
│   ├── worktree.py           git worktree 隔离 + 合并 + 冲突检测
│   ├── orchestrator.py       run/ask 主流程 + debug + 会话 + 流式 + 隔离
│   ├── render.py             rich 渲染: 行内 / TUI 看板(live)
│   ├── mcp_server.py         MCP server(让 Claude Code 直接调用)
│   ├── tui.py                终端原生交互式仪表盘(textual)
│   ├── cli.py                typer 入口
│   └── backends/
│       ├── __init__.py
│       ├── base.py           Backend 抽象 + StreamEvent + 工厂
│       ├── cli_backends.py   claude(--output-format json) / codex
│       └── openai_compat.py  GLM/OpenAI/DeepSeek …(SSE 流式 + usage)
├── tests/
│   ├── test_smoke.py
│   ├── test_units.py
│   ├── test_advanced.py
│   └── test_v04.py
└── examples/
    └── example-task.md
```

---

## 9. 路线图

- **v0.1 ✅**：配置 + 4 后端 + run/ask/plan + debug 循环 + dry-run + 冒烟测试。
- **v0.2 ✅**：
  - **依赖图并发**：`graph.py` 按 `depends_on` 构 DAG、环检测、波次化执行；`--jobs N` 开启同波次并发（默认 1 串行，因 acting 步骤改同一目录可能冲突）。
  - **流式输出**：HTTP 后端走 SSE 真流式逐字；`--stream` 开启；`ask --stream` 也可。
  - **会话续跑**：每次 run 持久化为 `~/.conductor/sessions/<id>.json`；`conductor resume <id>` 复用已完成步骤。
- **v0.3 ✅**：
  - **TUI 看板**：`--board` 实时刷新步骤状态表；`conductor board [id]` 看静态看板。
  - **成本统计**：收集 `usage`，按定价表估 USD（claude 直读 `total_cost_usd`）；会话/总结/`conductor session` 均展示。
  - **MCP server**：`conductor mcp` 以 stdio 暴露 `conductor_plan/ask/run/who/backends` 工具，Claude Code 可 `claude mcp add conductor -- conductor mcp` 直接调用。
- **v0.4 ✅（产品化）**：
  - **跨会话记忆**：`memory.py` 全局/项目两级存储，自动注入 planner 与各步上下文；`conductor memory` 管理。
  - **git worktree 隔离并发**：`worktree.py` 为 acting 步骤建独立 worktree → 提交 → 顺序合并回主树，冲突自动上报；`--isolate` 让 `--jobs>1` 真正安全。
  - **终端原生 TUI 仪表盘**：`tui.py`(textual) 满屏键盘驱动 App，运行/会话/记忆三视图，实时步骤看板+流式；`conductor tui` 启动。
  - **产品化**：`conductor doctor` 诊断；`[tui]`/`[mcp]`/`[all]` 可选 extras；pyproject URLs/classifiers。
- **v0.5（计划）**：步骤级回滚、token 成本预算熔断、远程执行(SSH/Codex Cloud)、插件化角色市场。
