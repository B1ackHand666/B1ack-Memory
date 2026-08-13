# 更新日志

本文件记录 B1ack Memory 的重要变更。版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)，内容格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [未发布]

## [0.5.2] - 2026-08-13

### 修复

- Dashboard 部署在反向代理路径前缀下时，iframe 现在保留当前路由前缀再请求插件 `/ui/`；不再错误请求域名根目录的 API 并显示 Not Found。

## [0.5.1] - 2026-08-13

### 修复

- Hermes Dashboard 不再通过 `iframe srcDoc` 注入内联 WebUI 脚本，改为加载宿主鉴权下的外部 `/ui/` 资源，避免 CSP 拦截脚本后页面永久停留在骨架屏。
- 首次数据加载失败时，概览页会保留错误说明和“重试”操作，不再只显示短暂通知与无限骨架占位。

## [0.5.0] - 2026-08-13

### 新增

- 数据库升级至 schema v7，加入 `subjects`、别名与关系、`work_items` 及修订、版本化 `summary_versions`、session 项目关联和可重试 `projection_jobs`。
- Light 的 `observe` 进入有生命周期的项目工作记忆；决定、当前状态和开放问题仅在用户原话确认且项目可靠识别时注入，proposal、里程碑、待确认和过期项不注入。
- 新增项目识别优先级、session 纠正、cwd/workspace 映射和有界 context pack；召回轨迹记录项目置信度、识别来源、排除原因与字符预算。
- 新增 current / historical / disputed 时间化长期记忆、有效区间和替代链；默认召回只使用当前且处于有效区间的事实。
- 新增带 source IDs、来源损失保护、手工锁定和一键回滚的项目/主题摘要；摘要不可用时自动回退到原子记忆与工作项。
- 新增可重建 `vault/` 与 `indexes/`，以及包含 manifest、schema 版本和 SHA-256 的 ZIP 整包备份、校验和恢复预演。
- 新增项目、主题、工作项、摘要、上下文预览、存储校验、投影重建与备份恢复预演 API。

### 变更

- WebUI 固定为“概览、记忆库、项目、审核、系统”五个一级工作区，使用紧凑列表、二级标签和统一右侧详情抽屉承载版本、证据和低频操作。
- 审核默认拆分为“需要决定”和“整理建议”；项目归属、过期工作项与摘要建议不再挤占冲突、替代和敏感内容队列。
- Provider 的搜索、明确记住与 prefetch 增加可选 `project_id`，兼容旧调用；模糊项目不会产生跨项目工作记忆注入。
- SQLite `memory.db` 继续作为唯一可写事实源；没有引入 ORM、外部数据库、Node 构建链或新的常驻服务。

### 迁移与安全

- v6 → v7 前自动创建 `pre-schema-v7` 在线数据库备份；现有长期记忆保持全局 current，不自动归属或重写。
- 最近 30 天仍有证据的旧 observe 日志转换为 suggested 工作项，必须确认项目和状态后才能参与注入。
- 永久删除同步清理新增实体关联、投影任务与包含被删来源 ID 的派生摘要；删除前后备份继续执行隐私清理规则。
- 恢复先验证归档哈希、SQLite integrity 和 schema，再展示数据量差异；执行前创建当前状态备份，恢复后以数据库重建 vault 和索引。
- 统一数据库层“当前有效记忆”谓词，FTS、项目上下文、摘要、投影和审计共同遵守 status、时间状态及有效区间；`valid_to` 日期在当天结束前仍有效。
- 修复记忆/候选晋升的项目链接、证据和事件事务一致性；subject 合并会迁移全部引用、关系、摘要和投影任务，并在提交前执行外键检查。
- 临时 schema v7 数据库会通过列检测补齐审核 proposal、摘要来源 revision 和 raw turn ingestion 状态，不要求降级或手工重建。
- 独立 WebUI 改为 HttpOnly SameSite=Strict 会话与终端 Bearer 双通道，所有数据读取均需鉴权，写入另校验同源 Origin；Dashboard 明确复用 Hermes 宿主鉴权。
- POSIX 数据目录/文件分别收紧到 `0700`/`0600`；Windows 在系统页明确显示为 ACL 管理。恢复使用同目录 `.restore.tmp` 完整迁移校验、fsync 后原子替换。
- raw turn 支持按字符预算在句子边界分片、游标续跑、失败退避及三次后隔离；系统页可查看并单条重新处理，避免失败 turn 每轮重复消耗模型。
- LLM HTTP 错误不再保存上游响应正文；密钥检测覆盖 16 字符起的高熵 token，检测和脱敏共用同一规则，设置页只显示是否已配置。

## [0.4.0] - 2026-08-13

### 新增

- 新增统一审核中心与 `GET /reviews`、`POST /reviews/scan`、`POST /reviews/{id}/resolve`、`POST /reviews/{id}/dismiss`；集中处理准入、重复、替代、冲突和长期池质量问题。
- 数据库升级至 schema v6，新增 `admission_decisions`、`memory_review_items`、`memory_audit_runs`，Dream 运行增加 admit、observe、discard 和审核提案计数。
- 新增写入后增量审计、每 7 天全池审计和 WebUI 手动全池扫描；既有长期记忆只生成建议，不自动改写、合并或回收。
- 审核支持创建、合并到 canonical、替代旧记忆、保留两者、编辑后执行、拒绝/过期候选及保留/编辑/回收长期记忆；保留现状按内容指纹抑制重复提示。

### 变更

- Light 改为结构化 `admit / observe / discard` 严格准入。自动候选至少需要 `0.85` 置信度、合法类型、有效来源 turn，并引用可在脱敏用户消息中验证的原话；助手文本仅作不可信上下文。
- 候选证据保存用户原话，不再把候选摘要当作证据。项目进度、迁移记录、完成操作、待落实方案和临时状态只观察，不创建候选。
- 默认 prefetch 在检索层强制排除候选。显式搜索仍返回 `UNVERIFIED` 候选，但不再增加召回计数、更新活动时间或影响晋升。
- 移除“实际作用”自动晋升通道。自动晋升固定要求两个不同本地日期的用户证据、最新且未失效的 REM durable 结论、无敏感/冲突/开放审核、通过 Deep 全池整合和每日额度。
- REM 只审核未审核或证据变化后失效的严格候选，按最久未审排序；相关项来自完整 FTS 池，可选 embeddings 仅作补充。
- Deep 固定输出 `create / duplicate / supersede / conflict / defer`。仅无相关项的 `create` 自动新增，其余动作生成审核提案；整批先校验后在单一事务中提交。
- 明确记住、候选人工晋升、Hermes 写入、长期记忆编辑和回收站恢复统一经过全池整合关口；完全相同内容作为幂等例外。
- 长期记忆合并保留 canonical 记录并将其他记录标记为 superseded；候选、证据和演化链显式保留。取消一条长期记忆只能关联一个已晋升候选的限制。
- WebUI 候选页区分严格合格、待准入和整合待审，晋升进度改为用户原话、不同日期证据、REM、整合状态和阻塞原因；长期记忆显示开放审核数量。

### 迁移与安全

- v5 → v6 前自动创建 `pre-schema-v6` 在线备份。旧 pending 候选迁移为 `legacy_review`，停止自动晋升并由首次治理扫描生成建议；已有长期记忆状态和内容保持不变。
- 候选或长期记忆永久删除同步清理新增的准入日志、审核项及关联隐私残留；旧托管备份清理和删除后干净备份规则保持不变。
- 模型不可用、输出缺项、引用未知 ID 或校验失败时不写长期池；Deep 批次错误不会产生部分晋升。

### 测试

- 覆盖准入分类、伪造原话拒绝、候选预取隔离、显式搜索无晋升信号、REM 队列公平、跨日证据和每日额度。
- 覆盖直接记住/人工晋升/编辑/恢复的统一整合、Deep 原子回滚、审核合并和指纹抑制、全池审计、v5 → v6 备份迁移及新增表隐私删除。

## [0.3.1] - 2026-08-09

### 变更

- 候选页面移除“已晋升”分区和存量统计；晋升历史统一通过长期记忆的“查看演化”访问。
- 记忆演化的“最近变化”固定返回并显示所选范围内最新 20 条，完整 `memory_events` 历史继续用于单条时间线。
- 状态分布不再把已晋升候选作为待维护存量，但每日晋升趋势和晋升通道统计保持不变。

### 迁移与修复

- 数据库升级至 schema v5，为候选增加唯一的长期记忆关联；自动和人工晋升均在同一事务内写入关联。
- v4 升级前自动创建数据库备份，并依次通过晋升事件、证据、共享模型调用和严格内容/来源/时间匹配修复旧关联。
- 无法唯一关联的旧已晋升候选会被安全清理；不会使用模糊语义猜测长期记忆归属。
- 长期记忆移入回收站时保留演化链；永久删除时同步清除关联候选、证据、召回、索引、事件和适用的隐私残留。

### 测试

- 覆盖 v4 → v5 的四种安全关联、歧义与孤儿清理、迁移幂等性及升级前备份。
- 覆盖晋升关联、回收/恢复/永久删除一致性，以及最近变化 20 条上限与完整历史保留。

## [0.3.0] - 2026-08-09

### 新增

- 新增可配置的 IANA 记忆时区；每日 Dream、自动晋升额度、证据日期和分析图表统一按该时区计算，WebUI 可一键采用浏览器检测结果。
- 数据库升级至 schema v4，加入追加式 `memory_events`，记录候选创建、证据、REM、合并、过期、恢复、拒绝、晋升以及长期记忆创建、编辑、回收和恢复。
- 新增“记忆演化”面板：7/30/90 天或全部范围的每日变化、状态分布、晋升通道和最近事件。
- 新增候选与长期记忆的单条演化时间线，以及 `GET /analytics/memory-flow`、`GET /lineage/candidate/{id}` 和 `GET /lineage/memory/{id}` 接口。
- 候选页面新增“已晋升”分区；长期记忆来源改为面向使用者的“Dream 自动晋升”“Hermes 写入”“人工保存”或“人工晋升”。

### 变更

- “跨日 1/2”改为“不同日期证据 1/2”；证据新增、同义合并、关联会话清理或时区变更后都会重新计算。
- Light 可向 REM 提示可能匹配的已有候选；REM 对每条候选返回耐久、同义候选、已有记忆、近期拒绝、噪声、冲突或暂缓之一，并接收证据日期，不新增模型调用或 embeddings 依赖。
- Deep 只接收已满足晋升条件的候选 ID；候选原文、整理后内容、REM 审查、晋升通道、Dream 运行和晋升时间均可追溯。
- WebUI 重设计为黑白灰个人记忆控制台，统一侧边栏、卡片、表单、标签、空状态、反馈和危险操作弹窗；状态仅使用低饱和绿/黄/红辅助色。
- 增加页面淡入、卡片错峰、数字递增、图表生长、时间线绘制及 Dream Light → REM → Deep 阶段动画，并遵循 `prefers-reduced-motion`。

### 迁移与安全

- v3 数据库自动幂等回填已有候选、证据、晋升、长期记忆和修订历史；无法还原的旧晋升通道标记为“历史未知”。
- 候选或长期记忆的隐私永久删除同步清除可关联演化事件；长期记忆仍需先移入回收站。
- 修复没有证据行的人工晋升候选无法关联长期记忆、时间线和隐私删除的问题。

### 测试

- 覆盖 UTC/北京时间边界、相同本地日期、夏令时、时区变更重算、中文同义证据合并和 Deep 血缘。
- 覆盖 v3 → v4 回填幂等、晋升通道、分析/血缘 API、已晋升候选和永久删除事件清理。

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

[未发布]: https://github.com/B1ackHand666/B1ack-Memory/compare/v0.5.2...HEAD
[0.5.2]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.5.2
[0.5.1]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.5.1
[0.5.0]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.5.0
[0.4.0]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.4.0
[0.3.1]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.3.1
[0.3.0]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.3.0
[0.2.0]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.2.0
[0.1.7]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.7
[0.1.6]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.6
[0.1.5]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.5
[0.1.4]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.4
[0.1.3]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.3
[0.1.2]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.2
[0.1.1]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.1
[0.1.0]: https://github.com/B1ackHand666/B1ack-Memory/releases/tag/v0.1.0
