# 更新日志

本文件记录 B1ack Memory 的重要变更。版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)，内容格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [未发布]

## [0.1.6] - 2026-08-04

### 修复

- DeepSeek V4 的非思考模式判断改为基于模型 ID，不再限定 DeepSeek 官方 API 域名。
- 修复通过 OpenCode Go 等兼容网关使用 `deepseek-v4-flash` 时，响应只含 `reasoning_content`、普通 `content` 为空并导致 JSON 解析失败的问题。
- 对空 completion 和仅思考内容的响应提供明确错误信息。

## [0.1.5] - 2026-08-04

### 修复

- OpenAI-compatible 请求优先改用 Hermes 环境已有的 `httpx` 传输，同时保留标准库 `urllib` 回退。
- 修复部分 VPS 出口下 Cloudflare 根据 Python `urllib` 的 HTTP/TLS 客户端指纹持续返回 `403 / 1010` 的问题。

## [0.1.4] - 2026-08-04

### 修复

- 为所有 OpenAI-compatible 请求添加明确的 `User-Agent` 和 `Accept` 请求头。
- 修复 OpenCode Go/Zen 经 Cloudflare 访问时，Python 默认客户端特征触发 `HTTP 403 / error code 1010` 的问题。

## [0.1.3] - 2026-08-04

### 修复

- Dashboard 面板改用 Hermes Plugin SDK 的认证请求桥接，不再让 iframe 直接导航到受保护的插件 API。
- 修复启用 Hermes Dashboard 会话认证时，B1ack Memory 面板显示 `Unauthorized` 的问题。
- 保持独立 WebUI 的本机访问限制；嵌入 Dashboard 时改由 Hermes 的会话认证保护。

## [0.1.2] - 2026-08-04

### 修复

- Dashboard iframe 现在读取 Hermes 注入的 `window.__HERMES_BASE_PATH__`。
- 修复 Hermes Dashboard 经反向代理子路径访问时，B1ack Memory 面板请求根路径并显示 `Not Found` 的问题。

## [0.1.1] - 2026-08-04

### 修复

- 适配 Hermes Agent 0.20.x 的目录型 Memory Provider 发现机制。
- 修复 Hermes 合成包命名空间下的 provider 与 CLI 相对导入。
- 修复 Dashboard 后端以独立模块加载时无法找到 `b1ack_memory` 包的问题。
- 移除 Hermes 0.20.x 不再用于 Memory Provider 发现的 Python entry point，避免产生错误的安装预期。

### 变更

- 插件清单升级为 `manifest_version: 1`，并声明 `kind: exclusive`。
- 推荐安装方式改为 `hermes plugins install B1ackHand666/B1ack-Memory --enable`。
- 明确 Git 安装使用 `hermes plugins update b1ack-memory` 升级，非 Git 安装可使用 `--force` 重新安装。
- 最低支持版本更新为 Hermes Agent 0.20.0、Python 3.11。

### 测试

- 新增 Hermes 目录插件发现与加载集成测试。
- 新增 Dashboard API 独立模块加载测试。
- 使用 Hermes 最新源码验证 provider、插件 CLI 和 Dashboard API 的真实加载流程。
- 验证 wheel 可在隔离目录安装并加载全部 Dashboard 路由。

## [0.1.0] - 2026-08-04

### 新增

- 首个可用版本：面向个人的轻量级、local-first Hermes Memory Provider。
- 以 SQLite 和 FTS5 为默认存储与全文检索方案，无需外部数据库或向量服务。
- Light → REM → Deep 三阶段 Dream 整理流程，以及保守的候选记忆晋升规则。
- 自动捕获主 Agent 会话，并排除 subagent 内部会话。
- 支持 DeepSeek、OpenAI、Ollama、LM Studio 等 OpenAI-compatible Chat Completions API。
- embeddings 可选增强，并在服务不可用时自动回退到全文检索。
- 本地 WebUI：记忆、候选、召回轨迹、Dream 记录、模型配置、备份与维护管理。
- Hermes Dashboard 集成，以及不依赖前端构建工具的静态页面。
- 记忆回收、永久删除、SQLite 在线备份与恢复、WAL 清理和数据库压缩。
- API Key 独立保存与掩码显示、会话密钥脱敏、敏感记忆人工审核。
- 自动生成 `MEMORY.md` 与 `DREAMS.md` 可读镜像。
- 独立 CLI 与 Hermes 插件 CLI 命令。

[未发布]: https://github.com/B1ackHand666/B1ack-Memory/compare/v0.1.6...HEAD
[0.1.6]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.6
[0.1.5]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.5
[0.1.4]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.4
[0.1.3]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.3
[0.1.2]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.2
[0.1.1]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.1
[0.1.0]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.0
