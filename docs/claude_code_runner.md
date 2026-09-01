# AShare Monitor + Claude Code 外接深研

Candidate 页面中的“Claude 深研”会在本机启动一次受限的 Claude Code 后台任务。它与 Claude Desktop 的手动 MCP 对话是两条入口：前者从 Monitor 一键调度，后者仍由用户在 Desktop 中主动对话。

## 使用方式

1. 使用 `scripts/start_local_app.ps1` 启动 AShare Monitor。
2. 在 Candidate 卡片点击“Claude 深研”。
3. 页面显示排队、运行、完成或失败状态；MVP 全局有任意活动 Claude Job 时不会创建第二个。
4. 成功后进入“深度研究”页面审核 pending proposal，再由用户 Accept / Modify / Reject。

如果应用没有自动找到 Claude Code，可在启动 Streamlit 前设置：

```powershell
$env:CLAUDE_CODE_EXECUTABLE = "C:\path\to\claude.exe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\start_local_app.ps1
```

启动 Job 前会运行轻量 `claude --version` 预检。退出码必须为 0，且输出必须明确包含 Claude Code 标识；`python.exe`、Claude Desktop GUI 和无法识别的 `claude.exe` 都会被拒绝。预检失败时不创建 QUEUED Job、不弹出外部窗口，建议改用 Claude Desktop + MCP 手动路径。

该入口使用 Claude Code 的 `-p`/Agent SDK 模式，消耗 Claude 订阅中单独的月度 Agent SDK 额度；它不等同于 Claude Desktop 普通对话额度，也不会使用 OpenAI API。

## 安全边界

- 每次任务只允许调用 `ashare-thesis-workbench` 的七个 MCP 工具；明确禁止 Bash、文件读写、搜索、Web 和子任务工具。
- MCP 使用项目 `.venv`、本地 SQLite 和 stdio，不开放网络监听端口。
- 子进程环境移除 Anthropic API、Bedrock、Vertex 和 Foundry 凭据变量，避免意外切换到按 API 计费。
- 原始 Claude stdout/stderr 只在 worker 内存中短暂捕获和分类，解析后立即丢弃，不写 SQLite、Git 或长期日志；正式结果只能通过 `submit_thesis_proposal` 写入。
- proposal 必须通过现有 Hard Validator，成功后仍是 `accepted=false`，不会替用户 Accept。
- 失败或超时不会改变 Candidate 决定，可以重试；同 Candidate 活动任务和已有 Thesis 会复用。全局单 Job 限制避免连续点击多只股票意外消耗额度。
- ResearchJob 仅保存 `cli_version`、`executable_kind`、`return_code`、`duration_ms`、`failure_category`、worker 起止时间等非敏感诊断。
- 来源固定标记为 `claude-code:<模型信息>`，不会与 `claude-mcp:` 或 OpenAI/Recorded 混淆。

## 已知限制

- 这是本机后台子进程，不是远程任务队列；关闭电脑或强制结束进程会中断研究。
- 页面轮询任务状态，不提供逐 token 输出。
- 命令超时统一为 300 秒；页面 stale 收口从同一配置推导为 360 秒（含 60 秒 worker 写回宽限）。任何未处理异常尽最大可能收口为 FAILED，终态不允许被旧 Worker 覆盖。
- 实际研究速度取决于 Claude 服务响应和工具调用轮数，通常比 Recorded 路径慢，但 Streamlit 页面不会被同步阻塞。
