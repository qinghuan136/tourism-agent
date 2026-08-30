
## 演示账号
user_id=00000000-0000-4000-8000-000000000001

trip_id=00000000-0000-4000-8000-000000000002
3e33e7c2-6dcc-435e-abdd-6157c1697ced

## 本地日志

应用启动后，项目运行日志会同时输出到控制台和 `logs/tourism-agent.log`。日志文件按
10 MB 轮转，最多保留 5 个历史文件。可在根目录 `.env` 中覆盖默认配置：

```dotenv
TOURISM_LOG_LEVEL=INFO
TOURISM_LOG_FILE=logs/tourism-agent.log
```

日志记录 API、根图、Planning 节点、LLM Tool Calls、Tool 结果预览和外部供应商调用，
但不会记录 API Key、数据库密码或完整 System Prompt。较长内容最多保留 2000 个字符。

## 启动 API 服务

项目通过 `npx` 启动 Tavily MCP，因此运行前需要本机已安装 Node.js 和 `npx`。

使用项目统一入口启动，Windows 下会自动选择与 Psycopg 异步连接池兼容的事件循环：

```powershell
.\.venv\Scripts\python.exe -m tourism_agent.server
```
