-- PostgreSQL / Supabase schema for the TIIS check-in bot (from the db-architect review).
-- Applied once as a Supabase migration; tests apply it to a throwaway schema.
-- The app connects as the postgres role (bypasses RLS). RLS is ON with no policies so the public
-- Supabase API (anon/authenticated keys) can read nothing.

CREATE TYPE progress_t    AS ENUM ('done', 'doing', 'not_started', 'stuck');
CREATE TYPE blocker_t     AS ENUM ('dont_know_start', 'no_time', 'tool_hard', 'not_confident', 'other');
CREATE TYPE help_cat_t    AS ENUM ('content', 'tool', 'confidence', 'other');
CREATE TYPE help_status_t AS ENUM ('waiting', 'accepted', 'replied', 'closed');
CREATE TYPE followup_t    AS ENUM ('used', 'not_yet', 'stuck');

CREATE FUNCTION update_updated_at() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END $$;

-- Learners. Hard delete (declining consent removes everything); no soft-delete column on purpose.
CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  line_user_id  TEXT NOT NULL UNIQUE,
  nickname      TEXT CHECK (char_length(nickname) BETWEEN 1 AND 30),
  goal          TEXT CHECK (goal IN ('resume', 'skill', 'interview', 'unsure')),
  display_name  TEXT,                                   -- LINE name, so the team can find the learner in OA Manager
  consented_at  TIMESTAMPTZ,                            -- evidence of consent
  flow_state    TEXT NOT NULL DEFAULT 'consent' CHECK (flow_state IN (
                  'consent', 'nickname', 'goal', 'idle', 'ci_progress', 'ci_conf', 'ci_blocker',
                  'ci_blocker_text', 'ci_contact', 'ci_edit', 'help_cat', 'help_detail', 'resume', 'fu')),
  flow_data     JSONB NOT NULL DEFAULT '{}',            -- serialised conversation state (intentional 1NF exception)
  joined_on     DATE NOT NULL,
  blocked_at    TIMESTAMPTZ,                            -- set when the learner blocks/unfollows the bot
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Weekly topics, set by the team (one list for every goal).
CREATE TABLE weekly_tasks (
  week         SMALLINT PRIMARY KEY CHECK (week BETWEEN 1 AND 4),
  title        TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
  description  TEXT NOT NULL DEFAULT '' CHECK (char_length(description) <= 500),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- team-set values: today only the project start date
CREATE TABLE project_settings (
  key         TEXT PRIMARY KEY CHECK (char_length(key) BETWEEN 1 AND 64),
  value       TEXT NOT NULL CHECK (char_length(value) <= 200),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE checkins (
  user_id        UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  week           SMALLINT NOT NULL REFERENCES weekly_tasks (week),
  progress       progress_t NOT NULL,
  confidence     SMALLINT NOT NULL CHECK (confidence BETWEEN 1 AND 5),
  blocker        blocker_t,
  blocker_note   TEXT CHECK (char_length(blocker_note) <= 300),
  wants_contact  BOOLEAN NOT NULL DEFAULT false,
  task_title     TEXT,                                  -- title the learner answered (denormalised on purpose)
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, week),
  CHECK (blocker_note IS NULL OR blocker = 'other')
);
CREATE INDEX idx_checkins_week ON checkins (week);

CREATE TABLE help_requests (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- the #n learners see
  user_id     UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  category    help_cat_t NOT NULL,
  status      help_status_t NOT NULL DEFAULT 'waiting',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- one still-open request per learner and category, enforced by the database (race-safe)
CREATE UNIQUE INDEX uq_help_one_open ON help_requests (user_id, category)
  WHERE status IN ('waiting', 'accepted');
CREATE INDEX idx_help_user_recent ON help_requests (user_id, id DESC);
CREATE INDEX idx_help_status ON help_requests (status, id);

CREATE TABLE help_notes (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  help_id     BIGINT NOT NULL REFERENCES help_requests (id) ON DELETE CASCADE,
  author      TEXT NOT NULL CHECK (author IN ('learner', 'team')),
  body        TEXT NOT NULL CHECK (char_length(body) BETWEEN 1 AND 2000),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_help_notes_help ON help_notes (help_id, id);

CREATE TABLE help_status_log (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  help_id      BIGINT NOT NULL REFERENCES help_requests (id) ON DELETE CASCADE,
  from_status  help_status_t,
  to_status    help_status_t NOT NULL,
  changed_by   TEXT,
  changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_help_log_help ON help_status_log (help_id, id);

CREATE TABLE followups (
  user_id     UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK (kind IN ('fu_mid', 'fu_final')),
  outcome     followup_t NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, kind)
);

CREATE TABLE pushes_sent (
  user_id  UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  kind     TEXT NOT NULL,
  key      TEXT NOT NULL,
  PRIMARY KEY (user_id, kind, key)
);

-- LINE webhook event ids already handled (de-duplication of redeliveries)
CREATE TABLE webhook_events (
  event_id     TEXT PRIMARY KEY,
  received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_webhook_events_received ON webhook_events (received_at);

-- messages the system sends on its own (status updates, reminders)
CREATE TABLE outbox (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id       UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  messages      JSONB NOT NULL,
  attempts      SMALLINT NOT NULL DEFAULT 0,
  last_error    TEXT,
  delivered_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_outbox_pending ON outbox (user_id, id) WHERE delivered_at IS NULL;

CREATE TRIGGER trg_users_updated BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_tasks_updated BEFORE UPDATE ON weekly_tasks FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_checkins_updated BEFORE UPDATE ON checkins FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_help_updated BEFORE UPDATE ON help_requests FOR EACH ROW EXECUTE FUNCTION update_updated_at();

ALTER TABLE project_settings   ENABLE ROW LEVEL SECURITY;
ALTER TABLE users            ENABLE ROW LEVEL SECURITY;
ALTER TABLE weekly_tasks     ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkins         ENABLE ROW LEVEL SECURITY;
ALTER TABLE help_requests    ENABLE ROW LEVEL SECURITY;
ALTER TABLE help_notes       ENABLE ROW LEVEL SECURITY;
ALTER TABLE help_status_log  ENABLE ROW LEVEL SECURITY;
ALTER TABLE followups        ENABLE ROW LEVEL SECURITY;
ALTER TABLE pushes_sent      ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events   ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox           ENABLE ROW LEVEL SECURITY;
