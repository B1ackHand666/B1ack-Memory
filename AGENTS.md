# B1ack Memory AI 维护指南

## 项目目标

B1ack Memory 是面向个人的 local-first Hermes Memory Provider。优先级依次是：记忆可控与可解释、单人可维护、低运维成本、兼容低成本 OpenAI-compatible 模型。代码应保持足够小、可读、可修改。

## 不可破坏的边界

- SQLite `memory.db` 是唯一事实源；`MEMORY.md` 和 `DREAMS.md` 是生成镜像，不直接编辑。
- 全文检索必须无需外部服务即可工作；embeddings 只能是可选增强，失败时仍可回退。
- 不引入 ORM、外部数据库、Node 构建链或新的常驻服务。
- WebUI 应覆盖日常配置、审核、删除、备份与维护操作。
- API Key 不回显明文；WebUI 默认仅监听 loopback；敏感候选不得自动晋升。
- 永久删除必须处理关联证据、召回、索引和适用的隐私残留，并明确备份后果。
- 数据库只能使用向前兼容迁移；已有个人数据库不得要求手工重建。

## 代码导航

- `b1ack_memory/db.py`：schema、迁移、SQLite 读写、生命周期和删除。
- `b1ack_memory/dream.py`：Light → REM → Deep、去重、评分和自动晋升。
- `b1ack_memory/retrieval.py`：FTS、可选向量召回与召回轨迹。
- `b1ack_memory/service.py`：业务编排、后台调度、备份、镜像和设置。
- `b1ack_memory/provider.py`：Hermes Memory Provider 适配。
- `b1ack_memory/web.py`、`b1ack_memory/static/`：API 与无构建步骤的 WebUI。
- `tests/`：核心、Hermes 集成和 Web API 回归测试。
- `b1ack_memory/version.py`、`pyproject.toml`、两份 `plugin.yaml` 和两份 Dashboard manifest：发布版本来源。

## 跨会话工作流程

1. 先阅读本文件、`README.md` 和 `CHANGELOG.md`，再检查 `git status`、相关实现和测试。
2. 以当前代码、测试、Git diff 和数据库迁移为事实来源，不依赖上一段聊天记录的口头状态。
3. 修改前确认现有兼容行为；避免覆盖不属于当前任务的工作树变更。
4. schema 变更必须提供从上一版本升级的测试；删除与隐私行为必须测试关联数据和备份。
5. 行为变化同步更新 README 和 CHANGELOG；WebUI 变化执行浏览器或截图检查。

## 完成标准

至少运行：

```bash
python -m unittest discover -s tests -v
python -m compileall -q b1ack_memory
python -m build
```

发布前还应确认：

- 所有版本来源一致，构建产物内包含 Dashboard、静态页面和插件清单。
- Hermes Dashboard 与独立 WebUI 路径均可加载，写操作仍受认证保护。
- README、CHANGELOG、安装/升级说明和必要截图已更新。
- 没有把密钥、个人数据库、备份、日志或临时调试产物提交进仓库。

本文件只记录稳定约束和工作方法，不记录临时进度、待办或某次会话状态。
