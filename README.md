
## 演示账号
user_id=00000000-0000-4000-8000-000000000001

trip_id=00000000-0000-4000-8000-000000000002

## 本地日志

应用启动后，项目运行日志会同时输出到控制台和 `logs/tourism-agent.log`。日志文件按
10 MB 轮转，最多保留 5 个历史文件。可在根目录 `.env` 中覆盖默认配置：

```dotenv
TOURISM_LOG_LEVEL=INFO
TOURISM_LOG_FILE=logs/tourism-agent.log
```

日志记录 API、根图、Planning 节点、LLM Tool Calls、Tool 结果预览和外部供应商调用，
但不会记录 API Key、数据库密码或完整 System Prompt。较长内容最多保留 2000 个字符。
