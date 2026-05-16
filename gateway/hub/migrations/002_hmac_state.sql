-- Phase v4a — HMAC replay-protection nonce store
-- Insert each (nonce, source_slug) pair with creation timestamp.
-- Replay-check: SELECT 1 FROM hub_nonces WHERE nonce=? — if hit, reject.
-- Cleanup: prune nonces older than HMAC max_age_seconds*2.

CREATE TABLE IF NOT EXISTS hub_nonces (
  nonce TEXT PRIMARY KEY,
  source_slug TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hub_nonces_created_at ON hub_nonces(created_at);
CREATE INDEX IF NOT EXISTS idx_hub_nonces_source ON hub_nonces(source_slug);
