-- Phase 6b — Local registry mirror (O1). Full-table pull from MC, read-backend
-- swap target. NO hub_secret here (separate registry_secrets.json, Step 6).
CREATE TABLE IF NOT EXISTS registry_audiences (
  id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS registry_sources (
  id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT, service_type TEXT, owner TEXT,
  default_topic_id TEXT, default_severity TEXT, severity_max TEXT, max_severity TEXT,
  rate_limit_per_min INTEGER, enabled INTEGER NOT NULL DEFAULT 1,
  allowed_topics TEXT, allowed_audiences TEXT, synced_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_topics (
  id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT, description TEXT,
  default_severity TEXT, default_urgency TEXT, default_actionability TEXT,
  default_audience_id TEXT, default_audience_slug TEXT, retention_days INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1, synced_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_rules (
  id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, priority INTEGER NOT NULL, topic_glob TEXT,
  source_id TEXT, audience_id TEXT NOT NULL, severity_min TEXT NOT NULL, urgency_min TEXT NOT NULL,
  actionability_min TEXT NOT NULL, quiet_class TEXT, channel_set_id TEXT NOT NULL,
  escalation_policy_id TEXT, enabled INTEGER NOT NULL DEFAULT 1, synced_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_registry_rules_audience ON registry_rules(audience_id);
CREATE INDEX IF NOT EXISTS idx_registry_rules_priority ON registry_rules(priority);
CREATE TABLE IF NOT EXISTS registry_channels (
  id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, type TEXT NOT NULL, audience_id TEXT,
  target_ref TEXT, target_thread_id TEXT, target_subtype TEXT, display_name TEXT,
  bot_token_env_ref TEXT, rate_limit_per_min INTEGER, supports_ack INTEGER NOT NULL DEFAULT 0,
  supports_buttons INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, synced_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_channel_sets (
  id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT, audience_id TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, synced_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_channel_set_members (
  channel_set_id TEXT NOT NULL, channel_id TEXT NOT NULL, position INTEGER NOT NULL,
  fallback_after_seconds INTEGER, required INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (channel_set_id, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_registry_csm_set ON registry_channel_set_members(channel_set_id);
CREATE TABLE IF NOT EXISTS registry_sync_meta (
  key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER NOT NULL
);
