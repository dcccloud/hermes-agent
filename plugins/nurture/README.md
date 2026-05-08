# nurture plugin

Avatar-Hermes 设备侧插件 — 把任意 Hermes 实例变成设备自动化引擎。

**当前状态:** Phase 0 脚手架(register、CLI subcommand、生命周期 hook 占位)。

## 架构

详见 `docs/avatar-hermes/`:
- [README](../../docs/avatar-hermes/README.md) — 文档索引
- [decisions](../../docs/avatar-hermes/decisions.md) — 7 大决策记录
- [boundary-contracts](../../docs/avatar-hermes/boundary-contracts.md) — 系统不变量
- [device-agent](../../docs/avatar-hermes/device-agent.md) — 设备侧设计

## 启用

在 `~/.hermes/config.yaml` 加入:

```yaml
plugins:
  enabled:
    - nurture
  nurture:
    enabled: true
    # ... 详见 docs/avatar-hermes/configuration.md
```

## CLI

```bash
hermes nurture status   # 当前 Phase 0 已可用(占位输出)
hermes nurture list     # Phase 1 实现
hermes nurture migrate  # Phase 6 实现
```

## 实现路线

| Phase | 内容 |
|---|---|
| 0 ★ | 脚手架(本仓库现状) |
| 1 | Python 设备服务 lift + 设备工具 |
| 3 | MCP + HTTP 协议层 + 社区连接器 |
| 4 | Kanban worker 任务系统 |
| 6 | OpenClaw 迁移工具 |
