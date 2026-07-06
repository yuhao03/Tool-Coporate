# Conductor

> 让不同 AI 各司其职：**Claude 出方案、Codex 干活、GLM 排错与做前端**。
> 一个跨平台的多 AI 角色编排 CLI。

Conductor 把你的开发任务拆成"角色"，每个角色路由到你指定的 AI 后端，并自动编排成一条流水线：
方案 → 执行 → 校验 → 排错修复。你也可以随时单角色直问，手动分工。

## 为什么

不同 AI 各有所长。用一个模型包揽一切，往往既贵又平庸。Conductor 的思路是**分工**：

| 角色 | 默认后端 | 干什么 |
|------|----------|--------|
| `planner` | Claude | 需求拆解、架构、产出计划 |
| `coder` | Codex | 写代码、改文件、跑命令 |
| `debugger` | GLM-5.2 | 分析报错、定位根因 |
| `designer` | GLM-5.2 | 前端 / UI |

映射全部可配置，想怎么分工你说了算。

## 工作原理

- **Claude / Codex**：直接复用你本机已安装并认证的 `claude` / `codex` CLI（无头模式），无需额外 API key。
- **GLM**：走智谱 BigModel 的 OpenAI 兼容 API（`https://open.bigmodel.cn/api/paas/v4/`，模型 `glm-5.2`），需要一把 `ZHIPU_API_KEY`。

详细架构见 [`DESIGN.md`](./DESIGN.md)。

## 安装

```bash
# 核心 CLI（推荐 uv）
uv tool install -e .
# 或 pipx / pip
pipx install -e .

# 可选 extras（按需）
uv pip install 'conductor[web]'   # Web UI 看板 (fastapi + uvicorn)
uv pip install 'conductor[mcp]'   # 作为 MCP server 接入 Claude Code
uv pip install 'conductor[all]'   # 全部
```

三种使用形态：**CLI**（终端）/ **Web UI**（浏览器看板，实时 SSE）/ **MCP**（在 Claude Code 里直接调用）。

## 快速开始

```bash
# 1) 初始化配置（生成 ~/.conductor/config.toml）
conductor init

# 2) 填入 GLM 密钥
export ZHIPU_API_KEY=sk-xxxxxxxx

# 3) 看看各角色由谁负责、后端是否就绪
conductor backends

# 4) 全自动跑一个任务（先看计划，--dry-run 不真正执行）
conductor run "给这个项目加一个带表单校验的登录页" --dry-run
conductor run "给这个项目加一个带表单校验的登录页"

# 5) 手动单角色直问
conductor ask debugger "报错: KeyError 'user' at auth.py:42，帮我定位根因"
conductor ask designer "设计一个深色风格的数据看板布局"
```

## 配置

全局配置：`~/.conductor/config.toml`；项目级覆盖：仓库根的 `conductor.toml`。

```toml
[backends.claude]
type = "claude-cli"

[backends.codex]
type = "codex-cli"
full_auto = true

[backends.glm]
type = "openai-compatible"
base_url = "https://open.bigmodel.cn/api/paas/v4/"
model = "glm-5.2"
api_key_env = "ZHIPU_API_KEY"

[roles]
planner  = "claude"
coder    = "codex"
debugger = "glm"
designer = "glm"

[orchestration]
max_debug_rounds = 2
verify_command = ""              # 例如 "pytest -q"
```

## 命令一览

| 命令 | 作用 |
|------|------|
| `conductor init` | 生成默认配置 |
| `conductor config` | 打印当前合并后的配置与路径 |
| `conductor backends` | 列出后端 + 健康检查（二进制/密钥是否就绪） |
| `conductor doctor` | 环境诊断（Python/git/CLI/密钥/可选依赖） |
| `conductor who <role>` | 查询某角色由哪个后端负责 |
| `conductor plan "<task>"` | 只跑 planner，打印分步计划 |
| `conductor ask <role> "<prompt>"` | 单角色直问（`--stream` 逐字） |
| `conductor run "<task>"` | 全自动编排（plan→并发 execute→verify→debug 循环） |
| `conductor resume <id>` | 续跑某次会话（复用已完成步骤） |
| `conductor sessions` / `session <id>` | 列出 / 查看会话（含成本） |
| `conductor board [id]` | 步骤看板（静态） |
| `conductor memory list/add/remove` | 跨会话记忆管理 |
| `conductor web` | 启动 Web UI 看板（实时 SSE，浏览器访问） |
| `conductor mcp` | 以 MCP server 运行，供 Claude Code 调用 |

`run` / `resume` 常用选项：`--dry-run`、`--jobs N`（并发）、`--isolate`（worktree 隔离）、`--board`（TUI 看板）、`--stream`（流式）。

## 进阶用法

**Web UI 看板**：`conductor web` → 浏览器打开 `http://127.0.0.1:8765`，可视化新建任务、看步骤实时状态/成本/token、管理记忆，SSE 实时推送。

**跨会话记忆**：`conductor memory add 技术栈 "FastAPI + React"` 记住项目事实，后续 `run` 会自动注入任务上下文。分项目级（`<项目>/.conductor/memory.json`）与全局级。

**隔离并发**：`conductor run "..." --jobs 3 --isolate` —— 每个 acting 步骤在独立 git worktree 改文件，完成后顺序合并回主树，冲突自动上报（让并发真正安全）。

**会话续跑**：`conductor run` 每次落盘一个会话；中断或失败后 `conductor resume <id>` 只重跑未完成步骤。

**流式 + 看板**：`conductor run "..." --board --stream` 终端实时看步骤状态、GLM 输出逐字浮现。

**成本统计**：自动收集 token 用量并估算 USD，会话详情与总结里可见；可在配置 `[cost.pricing]` 覆盖定价。

**作为 MCP 工具接入 Claude Code**：
```bash
uv pip install 'conductor[mcp]'            # 装可选依赖
claude mcp add conductor -- conductor mcp   # 注册到 Claude Code
# 之后在 Claude Code 里直接用 conductor_plan / conductor_ask / conductor_run
```

## 状态

v0.4（产品化）：CLI + Web UI + MCP 三形态；v0.1–0.3 全部能力 + 跨会话记忆 + git worktree 隔离并发 + 诊断命令。32 个单测通过。
