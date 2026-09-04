# 贡献说明

欢迎提交缺陷报告、文档修正和有明确使用场景的功能改进。

## 开发原则

- 优先完成一条可运行的旅行流程，不把项目扩展为通用 Agent 平台。
- 根图只编排；旅行领域能力优先通过子图内的 Tool 实现。
- 涉及行程、订单、支付等副作用时，必须由确定性流程和明确确认控制。
- 新增说明、注释和面向用户的异常信息请使用中文；避免不必要的防御性包装。

## 提交前检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check src tests

Set-Location frontend
pnpm lint
pnpm test
pnpm build
```

请在 Pull Request 中说明问题背景、修改范围、验证结果，以及尚未覆盖的边界。不要提交 `.env`、密钥、真实用户数据、运行日志或构建产物。
