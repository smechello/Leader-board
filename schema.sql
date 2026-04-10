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
    raw_score NUMERIC(4,2) NOT NULL CHECK (raw_score >= 0 AND raw_score <= 10),
    weighted_score NUMERIC(6,2) GENERATED ALWAYS AS (
        ROUND(
            CASE category
                WHEN 'innovation_originality'::score_category THEN raw_score * 3.00
                WHEN 'technical_implementation'::score_category THEN raw_score * 3.00
                WHEN 'business_value_impact'::score_category THEN raw_score * 2.50
                WHEN 'presentation_clarity'::score_category THEN raw_score * 1.50
            END,
            2
        )
    ) STORED,
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
CREATE INDEX idx_scores_team_id ON scores (team_id);
CREATE INDEX idx_scores_judge_id ON scores (judge_id);
CREATE INDEX idx_scores_category ON scores (category);
CREATE INDEX idx_audit_logs_actor_user_id ON audit_logs (actor_user_id);
CREATE INDEX idx_audit_logs_entity ON audit_logs (entity_type, entity_id);

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
