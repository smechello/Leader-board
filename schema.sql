BEGIN;

CREATE TYPE user_role AS ENUM ('admin', 'judge');

CREATE TYPE score_category AS ENUM (
    'innovation_originality',
    'technical_implementation',
    'business_value_impact',
    'presentation_clarity'
);

CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(80) NOT NULL UNIQUE,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role user_role NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE judges (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    display_name VARCHAR(120) NOT NULL,
    phone VARCHAR(20),
    organization VARCHAR(120),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE teams (
    id BIGSERIAL PRIMARY KEY,
    team_name VARCHAR(120) NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    portal_login_id VARCHAR(80) UNIQUE,
    portal_password_hash TEXT,
    process VARCHAR(120) NOT NULL DEFAULT 'General',
    theme VARCHAR(120) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE theme_options (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE process_options (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE judge_direct_login_links (
    id BIGSERIAL PRIMARY KEY,
    judge_id BIGINT NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
    token VARCHAR(128) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoke_reason VARCHAR(120),
    last_used_at TIMESTAMPTZ,
    created_by_admin VARCHAR(80),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE team_direct_login_links (
    id BIGSERIAL PRIMARY KEY,
    team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    token VARCHAR(128) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoke_reason VARCHAR(120),
    last_used_at TIMESTAMPTZ,
    created_by_admin VARCHAR(80),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE judge_login_requests (
    id BIGSERIAL PRIMARY KEY,
    judge_id BIGINT NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
    request_key VARCHAR(128) NOT NULL UNIQUE,
    requested_login VARCHAR(120),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at TIMESTAMPTZ,
    decided_by_admin VARCHAR(80),
    approval_expires_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ
);

CREATE TABLE judge_presence (
    id BIGSERIAL PRIMARY KEY,
    judge_id BIGINT NOT NULL UNIQUE REFERENCES judges(id) ON DELETE CASCADE,
    is_online BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE scoring_category_settings (
    id BIGSERIAL PRIMARY KEY,
    category VARCHAR(64) NOT NULL UNIQUE,
    weight_percent NUMERIC(6,2) NOT NULL,
    max_score NUMERIC(6,2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE team_members (
    id BIGSERIAL PRIMARY KEY,
    team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    full_name VARCHAR(120) NOT NULL,
    email VARCHAR(255) NOT NULL,
    phone VARCHAR(20),
    department_or_class VARCHAR(120),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (team_id, email)
);

CREATE TABLE projects (
    id BIGSERIAL PRIMARY KEY,
    team_id BIGINT NOT NULL UNIQUE REFERENCES teams(id) ON DELETE CASCADE,
    project_title VARCHAR(200) NOT NULL,
    problem_statement TEXT NOT NULL,
    project_summary TEXT NOT NULL,
    repository_url TEXT,
    demo_url TEXT,
    notes_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE scores (
    id BIGSERIAL PRIMARY KEY,
    judge_id BIGINT NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
    team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    category score_category NOT NULL,
    raw_score NUMERIC(6,2) NOT NULL CHECK (raw_score >= 0),
    weighted_score NUMERIC(6,2) NOT NULL DEFAULT 0,
    remarks TEXT,
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (judge_id, team_id, category)
);

CREATE TABLE audit_logs (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    action VARCHAR(80) NOT NULL,
    entity_type VARCHAR(80) NOT NULL,
    entity_id BIGINT,
    old_data JSONB,
    new_data JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_team_members_team_id ON team_members (team_id);
CREATE INDEX idx_teams_sort_order ON teams (sort_order);
CREATE INDEX idx_teams_portal_login_id ON teams (portal_login_id);
CREATE INDEX idx_scores_team_id ON scores (team_id);
CREATE INDEX idx_scores_judge_id ON scores (judge_id);
CREATE INDEX idx_scores_category ON scores (category);
CREATE INDEX idx_audit_logs_actor_user_id ON audit_logs (actor_user_id);
CREATE INDEX idx_audit_logs_entity ON audit_logs (entity_type, entity_id);
CREATE INDEX idx_judge_direct_login_links_judge_id ON judge_direct_login_links (judge_id);
CREATE INDEX idx_judge_direct_login_links_expires_at ON judge_direct_login_links (expires_at);
CREATE INDEX idx_team_direct_login_links_team_id ON team_direct_login_links (team_id);
CREATE INDEX idx_team_direct_login_links_expires_at ON team_direct_login_links (expires_at);
CREATE INDEX idx_judge_login_requests_judge_id ON judge_login_requests (judge_id);
CREATE INDEX idx_judge_login_requests_status ON judge_login_requests (status);
CREATE INDEX idx_judge_presence_judge_id ON judge_presence (judge_id);
CREATE INDEX idx_scoring_category_settings_category ON scoring_category_settings (category);

INSERT INTO scoring_category_settings (category, weight_percent, max_score) VALUES
('innovation_originality', 30.00, 10.00),
('technical_implementation', 30.00, 10.00),
('business_value_impact', 25.00, 10.00),
('presentation_clarity', 15.00, 10.00)
ON CONFLICT (category) DO NOTHING;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_users_updated_at
BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_judges_updated_at
BEFORE UPDATE ON judges
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_teams_updated_at
BEFORE UPDATE ON teams
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_projects_updated_at
BEFORE UPDATE ON projects
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_scores_updated_at
BEFORE UPDATE ON scores
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
