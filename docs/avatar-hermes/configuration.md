# 配置与部署

---

## 1. 部署架构

### 最小部署(单机开发)

```
┌─ 单机 ─────────────────────────────────────────┐
│                                                │
│  Hermes 实例 (--profile community)              │
│  ├─ plugins/nurture-community 启用              │
│  ├─ FastAPI :18790                              │
│  └─ JSON 文件存储                                │
│                                                │
│  Hermes 实例 (--profile agent-A)                │
│  ├─ plugins/nurture 启用                        │
│  ├─ Python 子进程 :8600                         │
│  └─ background thread → :18790                  │
│                                                │
│  Android 设备 (USB ADB)                         │
└────────────────────────────────────────────────┘
```

### 生产部署(多设备)

```
┌─ 云端 ──────────────────────────────────────┐
│  Hermes 实例 (--profile community)           │
│  ├─ plugins/nurture-community                │
│  ├─ FastAPI :18790 (HTTPS)                   │
│  └─ PostgreSQL 17.5 (Volcengine 等)         │
└──────────────────┬──────────────────────────┘
                   │ HTTPS, Bearer token
     ┌─────────────┼─────────────┐
     ▼             ▼             ▼
┌──────────┐ ┌──────────┐ ┌──────────┐
│ Hermes A │ │ Hermes B │ │ Hermes C │   每个 hermes profile = 一个设备
│ 设备 A   │ │ 设备 B   │ │ 设备 C   │
└──────────┘ └──────────┘ └──────────┘
```

---

## 2. 设备侧配置(plugins.nurture)

`~/.hermes/config.yaml`:

```yaml
plugins:
  nurture:
    enabled: true
    pythonPath: python3
    serverHost: 127.0.0.1
    serverPort: 8600
    autoStart: true
    appEnv: prod                    # dev | prod | test

    # 设备
    deviceId: "R5CR10XXXXX"         # 留空 = 自动选首个 ADB 设备(大小写敏感!)
    workspace: ""                   # 留空 = ~/.hermes/nurture/

    # 社区(可选)
    community:
      url: "https://community.example.com"
      token: ""                     # 首次连接自动注册并获取
      pollIntervalCapsMs: 60000
      pollIntervalTaskMs: 30000

    # Agent 身份
    agentId: "agent-device-a"       # 留空自动生成

    # ★ 注意 prompt caching:
    #   配置变更需要 /new 才生效(decisions.md ADR-006 + boundary-contracts.md 不变量 7)
```

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | true | 启用插件 |
| `pythonPath` | string | "python3" | Python 二进制路径 |
| `serverHost` | string | "127.0.0.1" | Python 设备服务监听 |
| `serverPort` | int | 8600 | Python 设备服务端口 |
| `autoStart` | bool | true | session 起始自动 spawn Python 子进程 |
| `appEnv` | enum | "dev" | 环境标识 |
| `deviceId` | string | "" | 绑定 ADB 设备序列号(★ 大小写敏感) |
| `workspace` | string | "" | nurture 工作目录(默认 `~/.hermes/nurture`) |
| `community.url` | string | "" | 社区 URL(空 = 离线模式) |
| `community.token` | string | "" | Bearer token(自动管理) |
| `community.pollIntervalCapsMs` | int | 60000 | 上行间隔 |
| `community.pollIntervalTaskMs` | int | 30000 | 下行轮询间隔 |
| `agentId` | string | "" | Agent 唯一 ID |

---

## 3. 社区侧配置(plugins.nurture-community)

```yaml
plugins:
  nurture-community:
    enabled: true
    host: 127.0.0.1
    port: 18790
    databaseUrl: "postgresql://user:pass@host:5432/db?sslmode=require"
    # 留空 databaseUrl → JSON 文件后端(开发)

    # 鉴权
    jwtPrivateKey: ""               # 留空自动生成并落 ~/.hermes/nurture-community/secrets/
    jwtPublicKey: ""
    adminTokens:                    # admin scope token 列表
      - "${NURTURE_COMMUNITY_ADMIN_TOKEN}"

    # Phase 5 投递
    weeklyReport:
      enabled: false
      schedule: "0 9 * * 1"         # 周一 9 点
      target: "telegram:user_id"

# 配套配置
auxiliary:
  recipe_fusion:                    # Recipe 融合用的独立 LLM
    provider: openrouter
    model: anthropic/claude-haiku
    max_tokens: 4096
```

---

## 4. 工作目录结构

### 设备 Hermes 实例

```
~/.hermes/                          (默认 profile;profile 隔离时是 ~/.hermes/profiles/<name>/)
├── config.yaml
├── nurture/                        # ★ 本插件数据
│   ├── data/
│   │   ├── recipes/
│   │   ├── app_graphs/
│   │   ├── traces/
│   │   ├── events/
│   │   ├── checkpoints/
│   │   └── in_flight/
│   ├── agent/
│   │   ├── goals.md
│   │   ├── persona/
│   │   ├── community-tasks.json
│   │   ├── community-recipes-cache.json
│   │   ├── community-token.json    # Bearer token 缓存
│   │   ├── value-schema.json
│   │   ├── advice.json
│   │   └── directives.json
│   └── history/profiles/<account>/
└── (hermes 自身的 sessions/, logs/, skills/, ...)
```

### 社区 Hermes 实例

```
~/.hermes/profiles/community/        # 推荐用 profile 隔离
├── config.yaml
├── nurture-community/
│   ├── knowledge/                   # JSON 模式数据
│   │   ├── recipes.json
│   │   ├── graph-states.json
│   │   ├── events.json
│   │   ├── traces.json
│   │   ├── task-dispatch.json
│   │   ├── advices.json
│   │   ├── directives.json
│   │   └── value-schema.json
│   └── secrets/
│       ├── jwt-private.pem
│       └── jwt-public.pem
└── ...
```

PG 模式下,knowledge/ 仅存配置;数据全在数据库。

---

## 5. Python 设备服务环境变量

| 变量 | 说明 |
|---|---|
| `NURTURE_WORKSPACE` | 工作目录(由 hermes plugin 注入,默认 `~/.hermes/nurture`) |
| `NURTURE_ENV` | 环境标识(由 plugin 注入,从 config.appEnv 来) |
| `NURTURE_LOG_LEVEL` | 日志级别 |
| `VLM_BASE_URL` | VLM API 地址 |
| `VLM_API_KEY` | VLM API key |
| `VLM_MODEL` | VLM 模型名 |

---

## 6. 多设备 / 多 Agent 配置

每设备一个 hermes profile:

```bash
# 创建 profile
hermes profile create agent-A
hermes --profile agent-A config set plugins.nurture.deviceId "DEVICE_A_SERIAL"
hermes --profile agent-A config set plugins.nurture.community.url "https://community.example.com"

hermes profile create agent-B
hermes --profile agent-B config set plugins.nurture.deviceId "DEVICE_B_SERIAL"
hermes --profile agent-B config set plugins.nurture.community.url "https://community.example.com"

# 启动
hermes --profile agent-A gateway start &
hermes --profile agent-B gateway start &
```

每个 profile 独立的 ~/.hermes/profiles/agent-A/ 等目录,数据互不污染。

---

## 7. 设备连接

| 平台 | 工具 |
|---|---|
| Android | ADB (`adb devices`) |
| HarmonyOS | HDC (`hdc list targets`) |
| iOS | XCTest(macOS only) |

---

## 8. 启动流程

### 设备启动序列

```
1. hermes 启动(--profile <name>)
   │
2. plugins/nurture register(ctx)
   │  load_config + cfg_get
   │
3. ctx.register_tool 注册:
   │  nurture_execute / nurture_task_report / nurture_community_*
   │
4. ctx.register_hook("on_session_start", start_python_service)
   ctx.register_hook("pre_llm_call", inject_persona_goals_caps)
   │
5. background thread 启动(daemon=True)
   │  6 个周期任务(3 上行 + 3 下行)
   │
6. atexit hook 注册 stop_python_service
   │
7. session 起始 → 触发 on_session_start
   │  Python 子进程 :8600 启动
   │  community-token 加载或注册
   │
8. Pi Agent 可用,设备工具就绪
```

### 社区启动序列

```
1. hermes 启动(--profile community)
   │
2. plugins/nurture-community register(ctx)
   │
3. KnowledgeEngine 初始化
   │  ├─ databaseUrl 配置 → asyncpg pool + run migration
   │  └─ 否则 → JSON 文件后端
   │
4. AvatarRegistry 初始化(30s reconcile thread)
   │
5. FastAPI :18790 启动(uvicorn 后台线程)
   │
6. 等待 agent 上行
```

---

## 9. 健康检查

```bash
# 设备
curl http://127.0.0.1:8600/api/health

# 社区(需要 admin token)
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
     https://community.example.com/api/health
```

---

## 10. 常见问题

### Python 服务启动失败

```
检查:
1. python3 路径正确?(plugins.nurture.pythonPath)
2. 依赖已装?(pip install -r plugins/nurture/python/requirements.txt)
3. 端口被占?(lsof -i :8600)
4. ADB 可用?(adb devices)
5. NURTURE_WORKSPACE 目录存在且可写?
```

### 社区连接失败

```
检查:
1. community.url 格式(https://...)
2. community.token 有效?(过期需要重新注册:rm community-token.json)
3. 社区 FastAPI 在线?(curl https://.../api/health)
4. 网络连通?
5. JWT 时钟同步?
```

### Recipe 不生效

```
检查:
1. ~/.hermes/nurture/data/recipes/<app>/<op>/<step>/main.py 存在?
2. main.meta.json 中 disabled == false?
3. fail rate 没超 50%?
4. 查看 ~/.hermes/logs/agent.log 中的 recipe 执行记录
```

### Persona 修改不生效

正常 — Hermes prompt caching 政策禁止 mid-session 修改 system prompt(boundary-contracts.md 不变量 3)。改 persona 后用 `/new` 起新 session。
