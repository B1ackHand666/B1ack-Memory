# 更新日志

本文件记录 B1ack Memory 的重要变更。版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)，内容格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [未发布]

## [0.2.0] - 2026-08-05

### 新增

- 候选记忆增加待审核、已过期、已拒绝生命周期，以及恢复、单条隐私删除和按状态批量清理。
- 默认 14 天无活动后过期；已过期与人工拒绝候选再保留 30 天后自动清理。
- WebUI 显示候选最后活动、清理时间、REM 判断和两条自动晋升通道进度。
- Dream 日志新增候选新增、合并、过滤和过期计数。
- 新增根目录 `AGENTS.md`，固定架构边界、跨会话接手流程和发布完成标准。

### 变更

- Light 只提取适合长期保留的稳定信息，置信度下限为 0.75，每次 Dream 最多新增 8 条候选。
- REM 在原有调用中识别同义项、已有长期记忆、近期人工拒绝内容和临时噪声，无需新增模型或 embeddings。
- 自动晋升改为“跨日重复”或“实际作用”双通道；综合评分只用于排序，每天最多自动晋升 3 条。
- 长期记忆必须先进入回收站才能永久删除。
- 设置面板增加候选数量上限和三项候选保留策略。

### 安全与维护

- 手动永久删除候选会清理关联会话、Dream/model 日志、召回、索引和旧托管备份，并生成干净备份。
- 自动到期清理仅删除在线候选和派生数据，旧备份继续按照现有数量自然轮换。
- 数据库升级到 schema v3；v0.1.x 旧候选保留并初始化最后活动时间，不会在升级时立即删除。

### 测试

- 覆盖双通道晋升、每日上限、严格提取上限、REM 同义合并和噪声过滤。
- 覆盖候选过期、拒绝抑制、恢复、自动清理、隐私删除、Web API 和 schema v2 升级。

## [0.1.7] - 2026-08-05

### 修复

- Light 阶段限制每批最多提取 20 条候选，并限制候选文本长度，避免模型生成超长 JSON。
- REM 阶段限制主题与冲突数量，并要求紧凑输出。
- JSON 格式错误或尾部截断时自动调用模型进行一次压缩修复，丢弃不完整的尾部记录而不编造内容。

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

[未发布]: https://github.com/B1ackHand666/B1ack-Memory/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.2.0
[0.1.7]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.7
[0.1.6]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.6
[0.1.5]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.5
[0.1.4]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.4
[0.1.3]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.3
[0.1.2]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.2
[0.1.1]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.1
[0.1.0]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.0
