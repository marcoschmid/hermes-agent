PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS hub_events_log (
  event_id TEXT PRIMARY KEY,
  source_slug TEXT NOT NULL,
  topic_slug TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('received','delivered_inbox','delivered_telegram','failed')),
  payload TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hub_events_log_created ON hub_events_log(created_at);
