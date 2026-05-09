-- Nurture Community: JSON → PostgreSQL migration
-- All statements are idempotent (IF NOT EXISTS)

-- 1. Events
CREATE TABLE IF NOT EXISTS nurture_events (
  event_id     TEXT PRIMARY KEY,
  agent_id     TEXT NOT NULL,
  device_id    TEXT NOT NULL DEFAULT '',
  event_type   TEXT NOT NULL,
  timestamp    TIMESTAMPTZ NOT NULL,
  operation    TEXT NOT NULL DEFAULT '',
  step         TEXT NOT NULL DEFAULT '',
  task_id      TEXT,
  payload      JSONB NOT NULL DEFAULT '{}',
  ingested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_events_agent ON nurture_events(agent_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON nurture_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_operation ON nurture_events(operation);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON nurture_events(timestamp);

-- 2. Traces
CREATE TABLE IF NOT EXISTS nurture_traces (
  trace_id    TEXT PRIMARY KEY,
  agent_id    TEXT NOT NULL,
  device_id   TEXT NOT NULL DEFAULT '',
  app         TEXT NOT NULL,
  operation   TEXT NOT NULL,
  step        TEXT NOT NULL DEFAULT '',
  timestamp   TIMESTAMPTZ NOT NULL,
  actions     JSONB NOT NULL DEFAULT '[]',
  success     BOOLEAN NOT NULL DEFAULT FALSE,
  duration_ms INTEGER,
  task_id     TEXT,
  extra       JSONB,
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_traces_app ON nurture_traces(app);
CREATE INDEX IF NOT EXISTS idx_traces_agent ON nurture_traces(agent_id);
CREATE INDEX IF NOT EXISTS idx_traces_operation ON nurture_traces(app, operation);
CREATE INDEX IF NOT EXISTS idx_traces_timestamp ON nurture_traces(timestamp);
CREATE INDEX IF NOT EXISTS idx_traces_task ON nurture_traces(task_id) WHERE task_id IS NOT NULL;

-- 3. Recipes
CREATE TABLE IF NOT EXISTS nurture_recipes (
  id                  SERIAL PRIMARY KEY,
  app                 TEXT NOT NULL,
  operation           TEXT NOT NULL,
  step                TEXT NOT NULL,
  version             TEXT NOT NULL,
  code                TEXT NOT NULL DEFAULT '',
  origin_node_id      TEXT NOT NULL,
  origin_device_model TEXT NOT NULL DEFAULT '',
  created_at          BIGINT NOT NULL,
  node_stats          JSONB NOT NULL DEFAULT '{}',
  global_success_rate REAL NOT NULL DEFAULT 0,
  global_sample_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(app, operation, step, version)
);
CREATE INDEX IF NOT EXISTS idx_recipes_key ON nurture_recipes(app, operation, step);

-- 4. Graph states
CREATE TABLE IF NOT EXISTS nurture_graph_states (
  id                 SERIAL PRIMARY KEY,
  state_id           TEXT NOT NULL,
  app                TEXT NOT NULL,
  name               TEXT NOT NULL,
  description        TEXT NOT NULL DEFAULT '',
  indicators         JSONB NOT NULL DEFAULT '[]',
  discovery_path     JSONB NOT NULL DEFAULT '[]',
  is_optional        BOOLEAN NOT NULL DEFAULT FALSE,
  source             TEXT NOT NULL DEFAULT 'discovered',
  discovered_by      TEXT NOT NULL,
  discovered_at      BIGINT NOT NULL,
  confirmed_by_nodes JSONB NOT NULL DEFAULT '[]',
  consensus          BOOLEAN NOT NULL DEFAULT FALSE,
  transitions        JSONB NOT NULL DEFAULT '{}',
  UNIQUE(app, state_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_app ON nurture_graph_states(app);
CREATE INDEX IF NOT EXISTS idx_graph_consensus ON nurture_graph_states(app, consensus);

-- 5. Tasks
CREATE TABLE IF NOT EXISTS nurture_tasks (
  task_id      TEXT PRIMARY KEY,
  requirements JSONB NOT NULL,
  app          TEXT NOT NULL,
  description  TEXT NOT NULL,
  priority     TEXT NOT NULL DEFAULT 'normal',
  created_at   BIGINT NOT NULL,
  expires_at   TIMESTAMPTZ NOT NULL,
  status       TEXT NOT NULL DEFAULT 'active',
  completed_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON nurture_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_app ON nurture_tasks(app);

-- 6. Advice
CREATE TABLE IF NOT EXISTS nurture_advices (
  advice_id       TEXT PRIMARY KEY,
  app             TEXT NOT NULL,
  type            TEXT NOT NULL,
  summary         TEXT NOT NULL,
  rules           JSONB NOT NULL DEFAULT '[]',
  confidence      REAL NOT NULL DEFAULT 0,
  based_on_traces INTEGER NOT NULL DEFAULT 0,
  created_at      BIGINT NOT NULL,
  expires_at      TIMESTAMPTZ,
  delivered_to    JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_advices_app ON nurture_advices(app);

-- 7. Directives
CREATE TABLE IF NOT EXISTS nurture_directives (
  directive_id TEXT PRIMARY KEY,
  app          TEXT NOT NULL,
  type         TEXT NOT NULL,
  summary      TEXT NOT NULL,
  instructions JSONB NOT NULL DEFAULT '[]',
  priority     INTEGER NOT NULL DEFAULT 5,
  created_at   BIGINT NOT NULL,
  expires_at   TIMESTAMPTZ,
  delivered_to JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_directives_app ON nurture_directives(app);
