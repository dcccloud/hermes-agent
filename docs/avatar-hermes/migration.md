# OpenClaw → Avatar-Hermes 迁移

`hermes nurture migrate` 命令把现有 OpenClaw nurture 数据迁移到 Hermes profile 下。

---

## 1. 迁移原则

由 [decisions.md ADR-005](decisions.md#adr-005) 确定:**纯文件复制,无 schema 转换**。

OpenClaw 与 Avatar-Hermes 的设备侧文件 schema 完全一致(见 [device-memory-evolution.md](device-memory-evolution.md))。迁移工具只做路径映射 + 文件复制,零转换风险。

---

## 2. 命令

```bash
# 预览(dry-run)
hermes nurture migrate --source ~/.openclaw --dry-run

# 完整迁移(默认目标 = 当前 hermes profile 的 nurture 目录)
hermes nurture migrate --source ~/.openclaw

# 指定目标 profile
hermes --profile agent-A nurture migrate --source ~/.openclaw

# 跳过 recipes(只迁人设/任务)
hermes nurture migrate --source ~/.openclaw --skip-recipes

# 强制覆盖现有文件
hermes nurture migrate --source ~/.openclaw --overwrite

# 仅迁移指定 app
hermes nurture migrate --source ~/.openclaw --app douyin
```

---

## 3. 迁移内容

| OpenClaw 来源 | Hermes 目标 | 转换 |
|---|---|---|
| `<src>/nurture-workspaces/agent/persona/*.md` | `<dst>/agent/persona/*.md` | 复制 |
| `<src>/nurture-workspaces/agent/goals.md` | `<dst>/agent/goals.md` | 复制 |
| `<src>/nurture-workspaces/agent/community-tasks.json` | `<dst>/agent/community-tasks.json` | 复制 |
| `<src>/nurture-workspaces/agent/community-recipes-cache.json` | `<dst>/agent/community-recipes-cache.json` | 复制 |
| `<src>/nurture-workspaces/agent/value-schema.json` | `<dst>/agent/value-schema.json` | 复制 |
| `<src>/nurture-workspaces/agent/advice.json` | `<dst>/agent/advice.json` | 复制 |
| `<src>/nurture-workspaces/agent/directives.json` | `<dst>/agent/directives.json` | 复制 |
| `<src>/nurture-workspaces/data/recipes/<app>/<op>/<step>/*` | `<dst>/data/recipes/<app>/<op>/<step>/*` | 复制(★ schema 不变) |
| `<src>/nurture-workspaces/data/app_graphs/*.json` | `<dst>/data/app_graphs/*.json` | 复制 |
| `<src>/nurture-workspaces/data/traces/<app>/*.jsonl` | `<dst>/data/traces/<app>/*.jsonl` | 复制 |
| `<src>/nurture-workspaces/data/events/*` | `<dst>/data/events/*` | 复制 |
| `<src>/history/profiles/<account>/*` | `<dst>/history/profiles/<account>/*` | 复制 |

`<src>` 默认 `~/.openclaw`,`<dst>` 默认 `~/.hermes/nurture`(或 profile 隔离时 `~/.hermes/profiles/<name>/nurture`)。

**社区侧不通过此命令迁移。** 社区数据(PG / JSON)如果是从 OpenClaw 直接复用同一个数据库,不需要迁移;如果是新部署,自然从空开始让设备重新上报。

---

## 4. 安全检查(命令实施)

```python
# plugins/nurture/migrate.py
def migrate(source: Path, dest: Path, dry_run: bool, ...):
    # 1. 校验源路径
    if not (source / "nurture-workspaces" / "data" / "recipes").exists():
        raise click.ClickException(
            f"{source} 不像 OpenClaw 目录(找不到 nurture-workspaces/data/recipes)"
        )

    # 2. 校验目标路径
    if dest.exists() and not overwrite:
        # 检查冲突项
        conflicts = find_conflicts(source, dest)
        if conflicts:
            print("冲突文件,需要 --overwrite 才会覆盖:")
            for c in conflicts:
                print(f"  {c}")
            raise click.ClickException("迁移中止")

    # 3. dry-run 模式只打印计划
    plan = build_migration_plan(source, dest, options)
    if dry_run:
        print_plan(plan)
        return

    # 4. 实际复制 + 校验
    for item in plan:
        copy_with_verify(item.src, item.dst)
        # 对于 meta.json,验证 schema 完整(所有字段都在)
        if item.dst.suffix == ".json":
            validate_schema(item.dst)
```

---

## 5. 验证不变量(自动)

迁移完成后自动跑下列检查:

| 检查项 | 实施 |
|---|---|
| meta.json 字段完整性 | 验证所有 OpenClaw 字段都已复制 |
| Recipe 文件配对 | main.py / main.meta.json 必须共存 |
| candidate / previous 配对 | 同上 |
| trace JSONL 行数对齐 | 不丢行 |
| persona / goals 编码 | UTF-8 |

任何检查失败 → 报告并要求人工干预,不自动修复。

---

## 6. OpenClaw 与 Hermes 共存

迁移完成后,OpenClaw 的 `~/.openclaw/` 不被删除,可以保留一段时间作为 backup。Hermes 与 OpenClaw 操作不同设备 / 不同 profile 即可共存,不冲突。

如果两边操作同一台 ADB 设备,需要二选一启动(避免 uiautomator2 抢占)。

---

## 7. 回滚

迁移工具不修改源 OpenClaw 数据(只读复制)。如果 Hermes 体验后想回滚到 OpenClaw,只需:

```bash
# 不需要任何操作 — OpenClaw 数据原样保留
# 直接停 Hermes,启动 OpenClaw 即可
```

如果想清空 Hermes nurture 数据:

```bash
rm -rf ~/.hermes/nurture/  # 或对应 profile 路径
```

---

## 8. 时机建议

```
推荐迁移时机:
1. OpenClaw 设备空闲(没有正在执行的任务)
2. Hermes 已完成 Phase 1 验收(单设备离线模式跑通)
3. 提前在测试设备上跑过一遍 dry-run

谨慎处理:
- 正在跑的 community-task 会丢失关联(taskId 在两边都活跃)
   → 等待 OpenClaw 完成后再迁移
- 正在 reviewer 中的 candidate recipe 会丢失候选状态
   → 不影响 main.py,但 candidate 评估需要重新积累
```
