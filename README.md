# AShare Monitor · CandidateCard 候选工作台

面向个人研究流程的 A 股候选工作台：公开市场数据负责广度发现，确定性规则生成 CandidateCard，用户再选择 KEEP / IGNORE / PROMOTE。

## 当前主流程

```text
公开市场数据 → 确定性候选扫描 → CandidateCard → KEEP / IGNORE / PROMOTE → ThesisCard
                                              └→ Claude 深研（后台、受限 MCP）
```

候选由确定性规则产生，不是 AI 研究结论，也不构成投资建议。首页中的旧版连板、情绪和五维评分内容已降级为历史参考，不代表当前 CandidateCard 逻辑。

`research_pool.json` 保存用户明确指定的研究股票。“我的关注”与“市场发现”名额彼此独立；研究池身份只表示“用户希望研究”，不表示市场信号或推荐。

Candidate 的普通“转入研究”使用现有 OpenAI/Recorded 路径；“Claude 深研”可从 Monitor 启动受限 Claude Code 后台任务，结果仍须在“深度研究”页人工审核。该路径使用 Claude 订阅中单独的月度 Agent SDK 额度，配置与安全边界见 [docs/claude_code_runner.md](docs/claude_code_runner.md)。Claude Desktop + MCP 仍是另一条手动交互入口。

## 数据与更新

- 数据源：同花顺涨停池、新浪财经 MoneyFlow、腾讯财经行情（均为公开接口）
- 日级公开数据：GitHub Actions 每交易日 15:35（北京时间）运行旧收盘流程并提交 `data/*.json`
- 本地候选扫描：`.\.venv\Scripts\python.exe scripts/run_realtime_scan.py --once`
- 盘中循环扫描：`.\.venv\Scripts\python.exe scripts/run_realtime_scan.py --loop --interval-minutes 4`
- 页面启动：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start_local_app.ps1`
- 完整测试：`.\.venv\Scripts\python.exe -m pytest -q`

项目依赖只保证安装在仓库 `.venv`；不要使用裸 `python`、裸 `streamlit` 或全局 Anaconda 运行项目与测试。

分钟扫描使用独立、无副作用的公开数据客户端和确定性规则，不调用旧荐股脚本、LLM 或同花顺本地账户接口。候选和每轮 `ScanRun` 审计记录保存在本地 `data/thesis.db`，与 ThesisCard 共用同一个工作台数据文件，但来源 Observation 保持隔离；不同来源冲突时标记 `CONFLICTED`，不会静默覆盖。

候选页可按需调用 `get_price_volume_context`，读取腾讯公开端点的 5/10 日前复权 OHLCV，计算收益、完整日成交量相对前 5 日均量、距 10 日最高价和 10 日最大收盘回撤。该调用只在用户点击“查看价格行为”时发生，不会随分钟扫描批量抓取。公开端点当前不提供 amount/turnover 时保持缺失，不填零。

`--once` 只代表一次手动刷新；`--loop` 才代表持续扫描。页面会分别显示最新单次运行与 LOOP 健康状态。打开 Streamlit 页面本身不会启动后台扫描；盘中若超过实际 LOOP 间隔的两倍没有新运行，页面会提示持续扫描可能已经停止。

Streamlit Cloud 不运行本地分钟扫描，也没有本地 SQLite 持久化保证。云端页面只展示部署时已有的公开数据或演示内容，不代表持续实时模式。

## 免责声明

本项目内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。
