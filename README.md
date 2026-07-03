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
# 任选一种（推荐 uv）
uv tool install -e .
# 或
pipx install -e .
# 或
pip install -e .
```

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
| `conductor who <role>` | 查询某角色由哪个后端负责 |
| `conductor plan "<task>"` | 只跑 planner，打印分步计划 |
| `conductor ask <role> "<prompt>"` | 单角色直问（`--stream` 逐字） |
| `conductor run "<task>"` | 全自动编排（plan→并发 execute→verify→debug 循环） |
| `conductor resume <id>` | 续跑某次会话（复用已完成步骤） |
| `conductor sessions` / `session <id>` | 列出 / 查看会话（含成本） |
| `conductor board [id]` | 步骤看板（静态） |
| `conductor mcp` | 以 MCP server 运行，供 Claude Code 调用 |

`run` / `resume` 常用选项：`--dry-run`、`--jobs N`（并发）、`--board`（TUI 看板）、`--stream`（流式）。

## 进阶用法

**并发执行**：planner 在计划里用 `depends_on` 标注依赖，无依赖步骤可并发（注意：多个 coder/designer 改同一目录可能冲突，故默认串行，`--jobs 2+` 开启）。

**会话续跑**：`conductor run` 每次落盘一个会话；中断或失败后 `conductor resume <id>` 只重跑未完成步骤。

**流式 + 看板**：`conductor run "..." --board --stream` 实时看步骤状态、GLM 输出逐字浮现。

**成本统计**：自动收集 token 用量并估算 USD，会话详情与总结里可见；可在配置 `[cost.pricing]` 覆盖定价。

**作为 MCP 工具接入 Claude Code**：
```bash
uv pip install 'conductor[mcp]'          # 装可选依赖
claude mcp add conductor -- conductor mcp  # 注册到 Claude Code
# 之后在 Claude Code 里直接: 用 conductor_plan / conductor_ask / conductor_run
```

## 状态

v0.3：v0.1 全部能力 + 依赖图并发 + 流式输出 + 会话续跑 + TUI 看板 + 成本统计 + MCP server。25 个单测通过。
