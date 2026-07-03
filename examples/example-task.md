# 示例任务

下面演示 Conductor 如何把一个真实需求分给不同 AI 完成。

## 场景：给一个 Web 项目加「带表单校验的登录页」

```bash
# 先看它会怎么分工(不真正执行)
conductor run "给本项目加一个登录页: 邮箱+密码, 前端做表单校验(邮箱格式、密码≥8位), 风格深色现代" --dry-run
```

预期 planner(Claude) 会拆出类似计划：

```
📋 执行计划 · 由 planner 拆解
1. [designer] 设计登录页 UI — 深色现代风格, 邮箱+密码两栏, 含校验态视觉
2. [coder]    实现登录页组件 — 接入表单校验逻辑(邮箱格式、密码长度)
3. [coder]    接入后端登录接口 — 提交、错误提示、loading 态
4. [debugger] 自测边界情况 — 空值/非法邮箱/超短密码/网络失败
```

正式执行（会真正调用 codex 改文件、glm 设计/排错）：

```bash
conductor run "给本项目加一个登录页: 邮箱+密码, 前端做表单校验, 风格深色现代"
```

## 单角色直问

```bash
# 让 GLM 排查一个报错
conductor ask debugger "TypeError: Cannot read properties of undefined (reading 'map') at UserList.tsx:23, 帮我定位根因"

# 让 GLM 设计一个界面
conductor ask designer "设计一个深色风格的监控看板: 顶部 KPI 卡片, 中部折线图, 底部告警列表"

# 让 Claude 出方案
conductor ask planner "我想给 CLI 加插件机制, 给我架构方案与拆步"
```

## 配置校验命令后, 自动 debug 循环

在 `~/.conductor/config.toml` 里：

```toml
[orchestration]
verify_command = "npm run build && npm test -- --run"
max_debug_rounds = 3
```

这样 `conductor run` 执行完会自动跑校验；失败时 GLM 分析根因、Codex 据此修复，最多循环 3 轮直到通过。
