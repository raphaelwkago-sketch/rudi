-- ============================================================
-- Rudi — Supabase / Postgres schema
-- Run this in the Supabase SQL editor (one shot, safe to re-run).
-- ============================================================

-- ── 1. NODES ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nodes (
    project_id  TEXT        NOT NULL,
    id          TEXT        NOT NULL,
    text        TEXT,
    depends_on  JSONB       NOT NULL DEFAULT '[]',
    revises     TEXT,
    exception_to TEXT,
    status      TEXT        NOT NULL DEFAULT 'open',  -- open | superseded | folded | stub
    turn        INTEGER     NOT NULL DEFAULT 0,
    pinned      BOOLEAN     NOT NULL DEFAULT FALSE,
    hard_rules  JSONB       NOT NULL DEFAULT '[]',
    reinforcement_count   INTEGER NOT NULL DEFAULT 0,
    activation_contexts   JSONB   NOT NULL DEFAULT '[]',
    last_activated        INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (project_id, id)
);

-- ── 2. META (turn counter) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS meta (
    project_id  TEXT    NOT NULL,
    k           TEXT    NOT NULL,
    v           TEXT,
    PRIMARY KEY (project_id, k)
);

-- ── 3. FAILED FOLDS (fold-failure cache) ─────────────────────
CREATE TABLE IF NOT EXISTS failed_folds (
    project_id  TEXT    NOT NULL,
    hash        TEXT    NOT NULL,
    PRIMARY KEY (project_id, hash)
);

-- ── 4. API KEYS (one per user, issued via dashboard) ─────────
CREATE TABLE IF NOT EXISTS api_keys (
    user_id      UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    key_value    TEXT    NOT NULL UNIQUE,   -- rudi_sk_... stored in plaintext so user can retrieve it
    project_id   TEXT    NOT NULL UNIQUE,   -- sha256(key_value), used to scope all graph data
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    PRIMARY KEY (user_id)                  -- one active key per user
);

-- ── 5. INDEXES ───────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_nodes_project_status
    ON nodes (project_id, status);

CREATE INDEX IF NOT EXISTS idx_nodes_project_turn
    ON nodes (project_id, turn);

CREATE INDEX IF NOT EXISTS idx_nodes_project_pinned
    ON nodes (project_id, pinned)
    WHERE pinned = TRUE;

CREATE INDEX IF NOT EXISTS idx_api_keys_project_id
    ON api_keys (project_id);

-- ── 6. ROW LEVEL SECURITY ────────────────────────────────────
-- Graph tables: scoped by project_id set at request time (service role bypasses this).
-- api_keys table: scoped by Supabase Auth user_id.

ALTER TABLE nodes        ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta         ENABLE ROW LEVEL SECURITY;
ALTER TABLE failed_folds ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys     ENABLE ROW LEVEL SECURITY;

-- Graph tables use a session-local setting set by set_project_id().
CREATE POLICY nodes_project_isolation ON nodes
    USING      (project_id = current_setting('app.project_id', TRUE))
    WITH CHECK (project_id = current_setting('app.project_id', TRUE));

CREATE POLICY meta_project_isolation ON meta
    USING      (project_id = current_setting('app.project_id', TRUE))
    WITH CHECK (project_id = current_setting('app.project_id', TRUE));

CREATE POLICY failed_folds_project_isolation ON failed_folds
    USING      (project_id = current_setting('app.project_id', TRUE))
    WITH CHECK (project_id = current_setting('app.project_id', TRUE));

-- api_keys: users can only read/write their own row.
CREATE POLICY api_keys_owner ON api_keys
    USING      (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

-- ── 7. HELPER: set project context ───────────────────────────
-- Your API layer runs: SELECT set_project_id('proj_abc123');
-- before any graph query (only needed when NOT using the service role key).

CREATE OR REPLACE FUNCTION set_project_id(pid TEXT)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    PERFORM set_config('app.project_id', pid, TRUE);
END;
$$;
