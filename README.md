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

- **Claude（规划）/ Codex（执行）**：复用本机已认证的 `claude` / `codex` CLI（无头模式），无需额外 key。
- **GLM（审核/设计）**：**也走 claude CLI**，通过 env 把它指向智谱的 Anthropic 兼容端点（`https://open.bigmodel.cn/api/anthropic`，模型 `glm-5.2`）——等于"GLM 的 CLI"。只需 `export ZHIPU_API_KEY=sk-...`。三家全 CLI，env 进程级隔离（规划用你的 Claude 登录、GLM 用智谱端点，互不串）。

> **两个 claude 能同时跑吗？** 能。同一个 `claude` 二进制起两个进程：规划进程不带 env 覆盖（→ Anthropic，你的登录），GLM 进程带 `ANTHROPIC_BASE_URL`+`ANTHROPIC_API_KEY`（→ 智谱）。env 是进程级隔离，互不污染，可并发。

详细架构见 [`DESIGN.md`](./DESIGN.md)。

## 核心玩法：开发闭环（推荐）

把一个任务交给三个 AI 自动闭环，直到 GLM 审核通过：

```
给任务 → ① Claude 规划，产出「执行文档」
       → ② Codex 按文档执行、写代码
       → ③ GLM-5.2 审核代码、找 bug
       → 有 bug？→ ④ 把 bug 回 feed 给 Claude「重新出方案」→ 回到 ②
       → GLM 认为没问题（且测试通过）→ 停下 ✅
```

```bash
conductor loop "给项目加一个带表单校验的登录页" --rounds 5
# 想每轮跑测试再交给 GLM 审核:
conductor loop "..." --verify "pytest -q" --rounds 5
```

关键语义：**bug 是回给 Claude 重新规划**（而不是直接让 Codex 盲改），每轮都是有的放矢的修订。

## 架构解惑：真 Claude 怎么摆？

"我本来就活在 claude code 里，再来个调 claude 的工具不就嵌套冲突了？" —— 两种用法互不冲突：

- **独立进程（`conductor loop`）**：在普通终端或另开一个终端标签运行。它内部调的是 `claude -p`——一个**无头规划实例**，**不是**你正在交互的 claude code 会话，两者互不干扰。
- **待在 claude code 里（MCP 桥）**：`uv pip install 'conductor[mcp]'` 后 `claude mcp add conductor -- conductor mcp`，你的 claude code 就多了 `glm_review` / `codex_run` / `glm_chat` 工具，需要时手动调 GLM 或把活交给 Codex——真 Claude 始终是大脑，零嵌套。

## 安装

```bash
# 核心 CLI（推荐 uv）
uv tool install -e .
# 或 pipx / pip
pipx install -e .

# 可选 extras（按需）
uv pip install 'conductor[tui]'   # 终端原生交互式 TUI 仪表盘 (textual)
uv pip install 'conductor[mcp]'   # 作为 MCP server 接入 Claude Code
uv pip install 'conductor[all]'   # 全部
```

主力形态（终端优先）：
- **CLI / TUI**（日常）：`conductor run`、`conductor tui` 满屏看板、`memory/sessions/resume` …
- **MCP**：在 Claude Code 里直接调用 `conductor_plan/ask/run`。

## 快速开始

```bash
# 1) 初始化配置（生成 ~/.conductor/config.toml）
conductor init

# 2) 填入 GLM 密钥
export ZHIPU_API_KEY=sk-xxxxxxxx

# 3) 看看各角色由谁负责、后端是否就绪
conductor backends

# 4) 开发闭环（推荐）：Claude规划→Codex执行→GLM审核→循环到通过
conductor loop "给这个项目加一个带表单校验的登录页"
# 或全自动多步编排(可并发, 先 --dry-run 看计划)
conductor run "给这个项目加一个带表单校验的登录页" --dry-run

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
# GLM = claude CLI 指向智谱 Anthropic 端点(env 隔离 key)。需 export ZHIPU_API_KEY
type = "claude-cli"
model = "glm-5.2"
[backends.glm.env]
ANTHROPIC_BASE_URL = "https://open.bigmodel.cn/api/anthropic"
ANTHROPIC_API_KEY = "${ZHIPU_API_KEY}"

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
| `conductor loop "<task>"` | **开发闭环**（claude规划→codex执行→glm审核→重规划，循环到通过） |
| `conductor resume <id>` | 续跑某次会话（复用已完成步骤） |
| `conductor sessions` / `session <id>` | 列出 / 查看会话（含成本） |
| `conductor board [id]` | 步骤看板（静态） |
| `conductor memory list/add/remove` | 跨会话记忆管理 |
| `conductor tui` | 终端原生交互式仪表盘（满屏看板，键盘驱动） |
| `conductor mcp` | 以 MCP server 运行，供 Claude Code 调用 |

`run` / `resume` 常用选项：`--dry-run`、`--jobs N`（并发）、`--isolate`（worktree 隔离）、`--board`（TUI 看板）、`--stream`（流式）。

## 进阶用法

**终端原生 TUI 仪表盘**：`conductor tui` 进入满屏交互界面（类 lazygit），键盘发起任务、实时看步骤状态/成本/token、翻会话、管记忆，全程不用记子命令。需 `conductor[tui]`。

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
