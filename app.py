import logging

from flask import Flask
from flask_login import LoginManager
from sqlalchemy import text
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
		ensure_database_compatibility(app)
		ensure_default_scoring_settings()

	return app


app = create_app()


if __name__ == "__main__":
	app.run(debug=True)
