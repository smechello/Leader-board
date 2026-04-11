import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from flask_login import LoginManager
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from config import Config, validate_required_environment
from models import db
from routes.admin import admin_bp
from routes.judge import judge_bp
from routes.public import public_bp
from services.scoring_config_service import ensure_default_scoring_settings
from utils.auth import load_session_user

login_manager = LoginManager()


@login_manager.user_loader
def load_user(user_id):
	return load_session_user(user_id)


def configure_logging(app):
	log_level = getattr(logging, str(app.config["LOG_LEVEL"]).upper(), logging.INFO)
	logging.basicConfig(
		level=log_level,
		format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
	)
	app.logger.setLevel(log_level)


def register_blueprints(app):
	app.register_blueprint(public_bp)
	app.register_blueprint(admin_bp)
	app.register_blueprint(judge_bp)


def _normalize_database_url(url):
	if url and url.startswith("postgres://"):
		return url.replace("postgres://", "postgresql://", 1)
	return url


def _resolve_database_url(app):
	# Re-load .env to ensure DATABASE_URL is available for startup recovery.
	load_dotenv(override=False)
	url = (os.getenv("DATABASE_URL") or app.config.get("SQLALCHEMY_DATABASE_URI") or "").strip()
	url = _normalize_database_url(url)
	if not url:
		raise RuntimeError("DATABASE_URL is missing. Add it to Leader-board/.env.")
	return url


def _is_database_structure_error(error):
	message = str(error).lower()
	indicators = (
		"does not exist",
		"undefined table",
		"undefinedtable",
		"undefined column",
		"undefinedcolumn",
		"undefined object",
		"undefinedobject",
		"no such table",
	)
	return any(indicator in message for indicator in indicators)


def _load_schema_sql_for_recovery():
	schema_path = Path(__file__).resolve().parent / "schema.sql"
	if not schema_path.exists():
		raise RuntimeError(f"Schema file not found: {schema_path}")

	raw_sql = schema_path.read_text(encoding="utf-8")
	lines = []
	for line in raw_sql.splitlines():
		normalized = line.strip().upper()
		if normalized in {"BEGIN;", "COMMIT;"}:
			continue
		lines.append(line)

	schema_sql = "\n".join(lines)

	# Make schema execution safe to re-run for partially initialized databases.
	schema_sql = re.sub(
		r"CREATE TABLE(?!\s+IF NOT EXISTS)\s+",
		"CREATE TABLE IF NOT EXISTS ",
		schema_sql,
		flags=re.IGNORECASE,
	)

	schema_sql = re.sub(
		r"CREATE TYPE user_role AS ENUM\s*\(.*?\)\s*;",
		"""
DO $$
BEGIN
    CREATE TYPE user_role AS ENUM ('admin', 'judge');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;
""",
		schema_sql,
		count=1,
		flags=re.IGNORECASE | re.DOTALL,
	)

	schema_sql = re.sub(
		r"CREATE TYPE score_category AS ENUM\s*\(.*?\)\s*;",
		"""
DO $$
BEGIN
    CREATE TYPE score_category AS ENUM (
        'innovation_originality',
        'technical_implementation',
        'business_value_impact',
        'presentation_clarity'
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;
""",
		schema_sql,
		count=1,
		flags=re.IGNORECASE | re.DOTALL,
	)

	trigger_pattern = re.compile(
		r"CREATE TRIGGER\s+([a-zA-Z0-9_]+)\s+BEFORE UPDATE ON\s+([a-zA-Z0-9_]+)\s+FOR EACH ROW EXECUTE FUNCTION set_updated_at\(\)\s*;",
		flags=re.IGNORECASE,
	)
	schema_sql = trigger_pattern.sub(
		lambda match: (
			f"DROP TRIGGER IF EXISTS {match.group(1)} ON {match.group(2)};\n"
			f"CREATE TRIGGER {match.group(1)} BEFORE UPDATE ON {match.group(2)} "
			"FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
		),
		schema_sql,
	)

	return schema_sql


def recover_database_structure(app):
	database_url = _resolve_database_url(app)
	schema_sql = _load_schema_sql_for_recovery()

	engine = create_engine(database_url, future=True)
	try:
		with engine.connect() as connection:
			with connection.begin():
				connection.exec_driver_sql(schema_sql)
	except SQLAlchemyError as exc:
		raise RuntimeError(f"Automatic schema recovery failed: {exc}") from exc
	finally:
		engine.dispose()

	app.logger.info("Automatic schema recovery from schema.sql completed.")


def verify_database_connection(app):
	try:
		with db.engine.connect() as connection:
			connection.execute(text("SELECT 1"))
		app.logger.info("Database connection established.")
	except SQLAlchemyError as exc:
		raise RuntimeError(f"Database connection failed: {exc}") from exc


def ensure_database_compatibility(app):
	"""Apply lightweight idempotent schema updates for backward compatibility."""
	try:
		with db.engine.begin() as connection:
			connection.execute(
				text(
					"""
					ALTER TABLE teams
					ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0
					"""
				)
			)

			connection.execute(
				text(
					"""
					ALTER TABLE teams
					ADD COLUMN IF NOT EXISTS process VARCHAR(120) NOT NULL DEFAULT 'General'
					"""
				)
			)

			connection.execute(
				text(
					"""
					ALTER TABLE teams
					ADD COLUMN IF NOT EXISTS portal_login_id VARCHAR(80)
					"""
				)
			)

			connection.execute(
				text(
					"""
					ALTER TABLE teams
					ADD COLUMN IF NOT EXISTS portal_password_hash TEXT
					"""
				)
			)

			connection.execute(
				text(
					"""
					ALTER TABLE teams
					ADD COLUMN IF NOT EXISTS presentation_completed BOOLEAN NOT NULL DEFAULT FALSE
					"""
				)
			)

			connection.execute(
				text(
					"""
					ALTER TABLE teams
					ADD COLUMN IF NOT EXISTS presentation_completed_at TIMESTAMPTZ
					"""
				)
			)

			connection.execute(
				text(
					"""
					ALTER TABLE teams
					ADD COLUMN IF NOT EXISTS presentation_elapsed_seconds INTEGER
					"""
				)
			)

			connection.execute(
				text(
					"""
					UPDATE teams
					SET sort_order = id::INTEGER
					WHERE sort_order IS NULL OR sort_order = 0
					"""
				)
			)

			connection.execute(
				text("CREATE INDEX IF NOT EXISTS idx_teams_sort_order ON teams (sort_order)")
			)

			connection.execute(
				text(
					"CREATE INDEX IF NOT EXISTS idx_teams_presentation_completed ON teams (presentation_completed)"
				)
			)

			connection.execute(
				text("CREATE UNIQUE INDEX IF NOT EXISTS idx_teams_portal_login_id ON teams (portal_login_id)")
			)

			connection.execute(
				text(
					"""
					CREATE TABLE IF NOT EXISTS theme_options (
						id BIGSERIAL PRIMARY KEY,
						name VARCHAR(120) NOT NULL UNIQUE,
						created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
					)
					"""
				)
			)

			connection.execute(
				text(
					"""
					CREATE TABLE IF NOT EXISTS process_options (
						id BIGSERIAL PRIMARY KEY,
						name VARCHAR(120) NOT NULL UNIQUE,
						created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
					)
					"""
				)
			)

			connection.execute(
				text(
					"""
					CREATE TABLE IF NOT EXISTS system_settings (
						id BIGSERIAL PRIMARY KEY,
						key VARCHAR(120) NOT NULL UNIQUE,
						value TEXT NOT NULL,
						updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
					)
					"""
				)
			)

			connection.execute(
				text("CREATE INDEX IF NOT EXISTS idx_system_settings_key ON system_settings (key)")
			)

			connection.execute(
				text(
					"""
					CREATE TABLE IF NOT EXISTS judge_direct_login_links (
						id BIGSERIAL PRIMARY KEY,
						judge_id BIGINT NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
						token VARCHAR(128) NOT NULL UNIQUE,
						expires_at TIMESTAMPTZ NOT NULL,
						revoked_at TIMESTAMPTZ,
						revoke_reason VARCHAR(120),
						last_used_at TIMESTAMPTZ,
						created_by_admin VARCHAR(80),
						created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
					)
					"""
				)
			)

			connection.execute(
				text(
					"""
					CREATE TABLE IF NOT EXISTS team_direct_login_links (
						id BIGSERIAL PRIMARY KEY,
						team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
						token VARCHAR(128) NOT NULL UNIQUE,
						expires_at TIMESTAMPTZ NOT NULL,
						revoked_at TIMESTAMPTZ,
						revoke_reason VARCHAR(120),
						last_used_at TIMESTAMPTZ,
						created_by_admin VARCHAR(80),
						created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
					)
					"""
				)
			)

			connection.execute(
				text(
					"""
					CREATE TABLE IF NOT EXISTS judge_presence (
						id BIGSERIAL PRIMARY KEY,
						judge_id BIGINT NOT NULL UNIQUE REFERENCES judges(id) ON DELETE CASCADE,
						is_online BOOLEAN NOT NULL DEFAULT FALSE,
						last_seen_at TIMESTAMPTZ,
						updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
					)
					"""
				)
			)

			connection.execute(
				text(
					"""
					CREATE TABLE IF NOT EXISTS scoring_category_settings (
						id BIGSERIAL PRIMARY KEY,
						category VARCHAR(64) NOT NULL UNIQUE,
						weight_percent NUMERIC(6,2) NOT NULL,
						max_score NUMERIC(6,2) NOT NULL,
						created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
						updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
					)
					"""
				)
			)

			weighted_score_generation = connection.execute(
				text(
					"""
					SELECT is_generated
					FROM information_schema.columns
					WHERE table_schema = current_schema()
					  AND table_name = 'scores'
					  AND column_name = 'weighted_score'
					"""
				)
			).scalar()

			if str(weighted_score_generation or "").upper() == "ALWAYS":
				connection.execute(text("ALTER TABLE scores ALTER COLUMN weighted_score DROP EXPRESSION"))

			connection.execute(
				text(
					"""
					ALTER TABLE scores
					ALTER COLUMN raw_score TYPE NUMERIC(6,2)
					"""
				)
			)

			connection.execute(text("ALTER TABLE scores DROP CONSTRAINT IF EXISTS ck_scores_raw_score_range"))
			connection.execute(text("ALTER TABLE scores DROP CONSTRAINT IF EXISTS ck_scores_raw_score_non_negative"))
			connection.execute(text("ALTER TABLE scores ADD CONSTRAINT ck_scores_raw_score_non_negative CHECK (raw_score >= 0)"))

			connection.execute(
				text(
					"""
					ALTER TABLE scores
					ALTER COLUMN weighted_score TYPE NUMERIC(6,2)
					"""
				)
			)
			connection.execute(text("ALTER TABLE scores ALTER COLUMN weighted_score SET DEFAULT 0"))
			connection.execute(text("ALTER TABLE scores ALTER COLUMN weighted_score SET NOT NULL"))

			connection.execute(
				text(
					"""
					UPDATE scores
					SET weighted_score = ROUND(
						CASE
							WHEN category = 'innovation_originality'::score_category THEN raw_score * 3.00
							WHEN category = 'technical_implementation'::score_category THEN raw_score * 3.00
							WHEN category = 'business_value_impact'::score_category THEN raw_score * 2.50
							WHEN category = 'presentation_clarity'::score_category THEN raw_score * 1.50
							ELSE 0
						END,
						2
					)
					"""
				)
			)

			connection.execute(
				text("CREATE INDEX IF NOT EXISTS idx_team_direct_login_links_team_id ON team_direct_login_links (team_id)")
			)

			connection.execute(
				text("CREATE INDEX IF NOT EXISTS idx_team_direct_login_links_expires_at ON team_direct_login_links (expires_at)")
			)

			connection.execute(
				text("CREATE INDEX IF NOT EXISTS idx_judge_presence_judge_id ON judge_presence (judge_id)")
			)

			connection.execute(
				text("CREATE INDEX IF NOT EXISTS idx_scoring_category_settings_category ON scoring_category_settings (category)")
			)

			connection.execute(
				text(
					"""
					CREATE TABLE IF NOT EXISTS judge_login_requests (
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
					)
					"""
				)
			)

			connection.execute(
				text("CREATE INDEX IF NOT EXISTS idx_judge_direct_login_links_judge_id ON judge_direct_login_links (judge_id)")
			)

			connection.execute(
				text("CREATE INDEX IF NOT EXISTS idx_judge_direct_login_links_expires_at ON judge_direct_login_links (expires_at)")
			)

			connection.execute(
				text("CREATE INDEX IF NOT EXISTS idx_judge_login_requests_judge_id ON judge_login_requests (judge_id)")
			)

			connection.execute(
				text("CREATE INDEX IF NOT EXISTS idx_judge_login_requests_status ON judge_login_requests (status)")
			)

			connection.execute(
				text(
					"""
					INSERT INTO theme_options (name)
					SELECT DISTINCT theme
					FROM teams
					WHERE theme IS NOT NULL AND BTRIM(theme) <> ''
					ON CONFLICT (name) DO NOTHING
					"""
				)
			)

			connection.execute(
				text("INSERT INTO process_options (name) VALUES ('General') ON CONFLICT (name) DO NOTHING")
			)

			connection.execute(
				text(
					"""
					INSERT INTO process_options (name)
					SELECT DISTINCT process
					FROM teams
					WHERE process IS NOT NULL AND BTRIM(process) <> ''
					ON CONFLICT (name) DO NOTHING
					"""
				)
			)

			connection.execute(
				text(
					"""
					INSERT INTO system_settings (key, value)
					VALUES ('presentation_time_limit_seconds', '300')
					ON CONFLICT (key) DO NOTHING
					"""
				)
			)

			connection.execute(
				text(
					"""
					INSERT INTO system_settings (key, value)
					VALUES ('presentation_timer_state_v1', '{"running": false, "elapsed_seconds": 0, "started_at": null}')
					ON CONFLICT (key) DO NOTHING
					"""
				)
			)

		app.logger.info("Database compatibility checks completed.")
	except SQLAlchemyError as exc:
		raise RuntimeError(f"Database compatibility migration failed: {exc}") from exc


def create_app():
	validate_required_environment()

	app = Flask(__name__)
	app.config.from_object(Config)
	configure_logging(app)

	db.init_app(app)
	login_manager.init_app(app)
	login_manager.login_view = "public.login"
	login_manager.login_message_category = "warning"

	register_blueprints(app)

	@app.get("/health")
	def health():
		try:
			with db.engine.connect() as connection:
				connection.execute(text("SELECT 1"))
			return {"status": "ok", "database": "connected"}, 200
		except SQLAlchemyError as exc:
			app.logger.error("Health check database error: %s", exc)
			return {"status": "error", "database": "disconnected"}, 503

	with app.app_context():
		verify_database_connection(app)
		try:
			ensure_database_compatibility(app)
			ensure_default_scoring_settings()
		except (RuntimeError, SQLAlchemyError) as exc:
			if not _is_database_structure_error(exc):
				raise

			app.logger.warning(
				"Detected database structure error. Attempting schema recovery from schema.sql: %s",
				exc,
			)
			recover_database_structure(app)
			ensure_database_compatibility(app)
			ensure_default_scoring_settings()

	return app


app = create_app()


if __name__ == "__main__":
	app.run(debug=True)
