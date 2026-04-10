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
					ADD COLUMN IF NOT EXISTS process VARCHAR(120) NOT NULL DEFAULT 'General'
					"""
				)
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

	return app


app = create_app()


if __name__ == "__main__":
	app.run(debug=True)
