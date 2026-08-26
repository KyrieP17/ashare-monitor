# Claude Desktop + MCP 盘后深研入口

这是一个**手动、交互式**入口。Claude Desktop 不会监听 Candidate 的 PROMOTE，也不会自动开始研究；只有用户在 Claude Desktop 中主动发起对话、允许调用本地工具并提交 proposal，结果才会进入 AShare Monitor 的“深度研究”页面。

本地 MCP Server 使用 Claude Desktop 启动的 stdio 子进程通信，不开放端口，不需要 OpenAI、Anthropic 或其他 API Key。它不引入单独的 API 按次计费；Claude 的对话与工具使用仍受用户现有 Claude 订阅套餐和使用限额约束。

## 1. 前置检查

在 PowerShell 中执行：

```powershell
Set-Location "C:\Users\Kyrie\Documents\ChatGPT\New project\ashare-monitor"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -c "import mcp; print('MCP import: OK')"
```

手动启动检查：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\Kyrie\Documents\ChatGPT\New project\ashare-monitor\scripts\start_claude_mcp.ps1"
```

命令启动后保持安静并等待输入是正常现象，因为 stdout 属于 MCP 协议。按 `Ctrl+C` 退出。

## 2. Claude Desktop 配置

1. 完全退出 Claude Desktop，包括系统托盘中的后台进程。
2. 打开 `%APPDATA%\Claude\claude_desktop_config.json`。
3. 保留文件中已有的其他 `mcpServers`，将下面的 `ashare-thesis-workbench` 合并进去。
4. 保存后重新启动 Claude Desktop。

```json
{
  "mcpServers": {
    "ashare-thesis-workbench": {
      "command": "C:\\Users\\Kyrie\\Documents\\ChatGPT\\New project\\ashare-monitor\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "thesis.mcp_server"
      ],
      "env": {
        "PYTHONPATH": "C:\\Users\\Kyrie\\Documents\\ChatGPT\\New project\\ashare-monitor",
        "PYTHONNOUSERSITE": "1",
        "THESIS_DB_PATH": "C:\\Users\\Kyrie\\Documents\\ChatGPT\\New project\\ashare-monitor\\data\\thesis.db"
      }
    }
  }
}
```

没有任何 Key 或 Token 配置项。Claude Desktop 会在本机启动项目 `.venv` 中的 Python，并通过 stdin/stdout 与它通信。

如果服务没有出现，检查 `%APPDATA%\Claude\logs\mcp-server-ashare-thesis-workbench.log`。最常见原因是 JSON 合并错误、路径不是绝对路径，或 Claude Desktop 没有完全重启。

## 3. 建议的真实研究操作

先在 AShare Monitor Candidate 页面确认股票、交易日和它确实存在于当前候选箱。然后在 Claude Desktop 发起类似对话：

```text
请对 Candidate CN.SZ.002295 在 2026-08-26 做一次盘后研究。

这是手动交互研究，不要给买卖指令。请依次调用本地 MCP 工具：
1. get_market_snapshot；
2. get_stock_observation；
3. 有板块证据时调用 get_sector_observations 和 get_fund_flow_observations；
4. 需要价格行为时按需调用 get_price_volume_context；
5. 只使用工具返回的 observation_ref_id 和 source_refs 形成完整 ThesisRevision。

请创建一个新的 UUID 同时作为 submit_thesis_proposal 的 thesis_id 和 proposal.thesis_id。
proposal 必须引用 get_market_snapshot 返回的 snapshot_id，version=1，
revision_type=agent_proposal，accepted=false，derived_from_revision_id=null。
最后调用 submit_thesis_proposal，并把当前 Claude 模型名称放到 claude_model。
如果 Hard Validator 拒绝，请展示结构化 issues，不要编造数据绕过。
```

上述股票和日期只是示例，实际应替换为 Candidate 页面当前真实条目。

提交成功后：

1. 打开本地 Streamlit 主应用；
2. 进入“深度研究”；
3. 确认显示 `Claude Desktop + MCP · 交互式研究` 来源；
4. 检查支持/反对证据、Price In、失效条件、来源和 Reviewer issues；
5. 再由用户选择“接受研究”“修改后接受”或“拒绝研究”。

`submit_thesis_proposal` 永远只创建 `accepted=false` 的 pending proposal，不会替用户接受研究。

## 4. 暴露的工具

- `get_market_snapshot`
- `get_stock_observation`
- `get_sector_observations`
- `get_fund_flow_observations`
- `get_price_volume_context`（M3a，按需访问公开行情）
- `submit_thesis_proposal`

前四个工具直接复用 `ReadOnlyMarketTools`；价格行为直接复用 M3a 的 `PublicPriceVolumeTool`。MCP 层没有复制采集或指标业务逻辑，也不会修改代理环境变量。

## 5. 安全与产品边界

- 这是 Claude Desktop 中的手动深研，不等同于 PROMOTE 的自动研究路径。
- 不接同花顺账户、持仓或交易接口。
- 所有提交先经过项目现有 `HardProposalValidator`。
- 校验失败返回结构化 issues，数据库不写 ThesisCard 或 revision。
- 校验通过后仍保持 pending，必须在工作台中由用户 Accept / Modify / Reject。
- `generator_kind` 使用 `claude-mcp:<模型信息>`，不会与 `openai:*` 或 `recorded` 混淆。
