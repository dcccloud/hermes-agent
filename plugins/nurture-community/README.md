# nurture-community plugin

Avatar-Hermes 社区侧插件 — 多设备 Recipe/Graph/Trace/Event 聚合 + 任务调度。

**当前状态:** Phase 0 脚手架。

## 架构

详见 `docs/avatar-hermes/`:
- [community-agent](../../docs/avatar-hermes/community-agent.md)
- [community-memory-evolution](../../docs/avatar-hermes/community-memory-evolution.md)
- [mcp-http-protocol](../../docs/avatar-hermes/mcp-http-protocol.md)

## 启用(社区端 Hermes 实例)

```yaml
# ~/.hermes/profiles/community/config.yaml
plugins:
  enabled:
    - nurture-community
  nurture-community:
    enabled: true
    host: 127.0.0.1
    port: 18790
    databaseUrl: ""   # 留空 = JSON 后端;生产填 PG URL
```

## CLI

```bash
hermes nurture-community status     # Phase 0 已可用
hermes nurture-community serve      # Phase 2
hermes nurture-community agents     # Phase 2
hermes nurture-community recipes ...# Phase 2
hermes nurture-community tasks ...  # Phase 3
```

## 实现路线

| Phase | 内容 |
|---|---|
| 0 ★ | 脚手架(本仓库现状) |
| 2 | 7 大 store TS→Python 翻译 + KnowledgeEngine 双后端 + FastAPI |
| 3 | MCP server endpoint + JWT 鉴权 |
| 5 | analytics 聚合 + 周报投递 + Langfuse |
