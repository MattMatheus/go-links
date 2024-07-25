# Modified by Matt Matheus on 25 July 2024
# Description of changes: Adjusted error handling and line lengths

import datetime
import json
import logging
import os
import traceback

# import jinja2
from flask import (
    Flask,
    send_from_directory,
    redirect,
    render_template,
    request,
    jsonify,
    session,
    make_response,
)
from flask_login import LoginManager, current_user, logout_user
from flask_migrate import Migrate, upgrade as upgrade_db
from flask_wtf.csrf import generate_csrf
import sentry_sdk
from werkzeug.routing import BaseConverter

from shared_helpers import config, feature_flags
from db import db
from sqlalchemy.exc import SQLAlchemyError

sentry_config = config.get_config_by_key_path(["monitoring", "sentry"])
if sentry_config:
    from sentry_sdk.integrations.flask import FlaskIntegration

    sentry_sdk.init(
        dsn=sentry_config["dsn"],
        integrations=[FlaskIntegration()],
        traces_sample_rate=sentry_config.get("traces_sample_rate", 0.1),
    )


SIGNIN_DURATION_IN_DAYS = 30


def init_app_without_routes(disable_csrf=False):
    app = Flask(__name__)

    app.secret_key = config.get_config()["sessions_secret"]

    app.config["SQLALCHEMY_DATABASE_URI"] = config.get_config()["postgres"]["url"]
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
        # SQLAlchemy deprecated the `postgres` dialect, but it's still used by Heroku Postgres:
        # https://help.heroku.com/ZKNTJQSK/why-is-sqlalchemy-1-4-x-not-connecting-to-heroku-postgres
        app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace(
            "postgres://", "postgresql://", 1
        )

    if config.get_config()["postgres"].get("commercial_url"):
        app.config["SQLALCHEMY_BINDS"] = {
            "commercial": config.get_config()["postgres"]["commercial_url"]
        }

    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SESSION_COOKIE_SECURE"] = True

    from modules.base import authentication

    if os.getenv("ENVIRONMENT") == "test_env":
        app.before_request(authentication.login_test_user)

    @app.errorhandler(403)
    def handle_403(error):
        return jsonify({"error_type": "error_bar", "error": error.description or ""}), 403

    login_manager = LoginManager()
    login_manager.init_app(app)

    global csrf_protect

    if not disable_csrf:
        app.before_request(authentication.check_csrf)

    @login_manager.user_loader
    def load_user(user_id):
        from modules.users.helpers import get_user_by_id

        sentry_sdk.set_user({"id": user_id})

        return get_user_by_id(user_id)

    app.before_request(authentication.validate_user_authentication)

    # ensure signins last for SIGNIN_DURATION_IN_DAYS even with browser restarts
    app.permanent_session_lifetime = datetime.timedelta(days=SIGNIN_DURATION_IN_DAYS)

    @app.before_request
    def manage_durable_session():
        session.permanent = True

        if current_user.is_authenticated:
            try:
                if not session.get("last_signin") or (
                    datetime.datetime.utcnow() - session["last_signin"].replace(tzinfo=None)
                ) > datetime.timedelta(days=SIGNIN_DURATION_IN_DAYS):
                    logout_user()
            except Exception as e:
                logging.error(e)
                logout_user()

    @app.after_request
    def apply_csp(response):
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self' data:; "
                "style-src 'self' fonts.googleapis.com 'unsafe-inline'; "
                "font-src fonts.gstatic.com; "
                "base-uri 'self'"
            )

        return response

    @app.route("/_/health_check")
    def health_check():
        return "OK"

    db.init_app(app)

    return app


app = init_app_without_routes()


with app.app_context():
    try:
        global migrate

        migrate = Migrate(app, db)

        if os.getenv("POSTGRES_UPGRADE_ON_START", "").lower() == "true":
            upgrade_db(directory=os.path.join(os.path.dirname(__file__), "migrations"))
    except SQLAlchemyError as e:
        logging.warning(
            "SQLAlchemy error during Flask-Migrate/Alembic operation. This may be expected "
            "if you've deployed a new version of the app and an older version hasn't "
            "finished shutting down, or you've rolled back versions. "
            "Error: %s\nTraceback: %s",
            str(e),
            traceback.format_exc(),
        )
    except Exception as e:
        logging.error(
            "Unexpected error during Flask-Migrate/Alembic operation. " "Error: %s\nTraceback: %s",
            str(e),
            traceback.format_exc(),
        )


class RegexConverter(BaseConverter):

    def __init__(self, map, *items):
        super(RegexConverter, self).__init__(map)
        self.regex = items[0] if items else ""


app.url_map.converters["regex"] = RegexConverter


def add_routes():
    from modules.base.handlers import routes as base_routes
    from modules.links.handlers import routes as link_routes
    from modules.routing.handlers import routes as follow_routes
    from modules.users.handlers import routes as user_routes

    COMMERCIAL_BLUEPRINTS = []
    COMMERCIAL_MIDDLEWARE = []

    app.register_blueprint(base_routes)
    app.register_blueprint(link_routes)
    app.register_blueprint(user_routes)
    for blueprint in COMMERCIAL_BLUEPRINTS:
        app.register_blueprint(blueprint)
    app.register_blueprint(follow_routes)  # must be registered last since it matches any URL

    for middleware_handler in COMMERCIAL_MIDDLEWARE:
        app.before_request(middleware_handler)


@app.route("/")
def home():
    if not current_user.is_authenticated:
        return redirect("https://www.trot.to" if request.host == "trot.to" else "/_/auth/login")

    from modules.organizations.helpers import get_org_settings

    namespaces = config.get_organization_config(current_user.organization).get("namespaces", [])
    admin_links = get_org_settings(current_user.organization).get("admin_links", [])

    if feature_flags.provider.get("new_frontend", current_user):
        return render_template("_next_static/index.html")

    nonce = os.urandom(16).hex()

    response = render_template(
        "index.html",
        csrf_token=generate_csrf(),
        default_namespace=config.get_default_namespace(current_user.organization),
        namespaces=json.dumps(namespaces),
        admin_links=json.dumps(admin_links),
        nonce=nonce,
    )
    response = make_response(response)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' maxcdn.bootstrapcdn.com ajax.googleapis.com 'nonce-{nonce}'; "
        "style-src 'self' fonts.googleapis.com maxcdn.bootstrapcdn.com 'unsafe-inline'; "
        "font-src fonts.gstatic.com maxcdn.bootstrapcdn.com; "
        "base-uri 'self';"
    )

    return response


@app.route("/_csrf_token")
def get_csrf_token():
    """Return csrf token for API call"""
    if not current_user.is_authenticated:
        return redirect("https://www.trot.to" if request.host == "trot.to" else "/_/auth/login")

    return {"csrfToken": generate_csrf()}


@app.route("/_admin_links")
def admin_links():
    """Returns admin links for API call"""
    if not current_user.is_authenticated:
        return redirect("https://www.trot.to" if request.host == "trot.to" else "/_/auth/login")

    from modules.organizations.helpers import get_org_settings

    admin_links = get_org_settings(current_user.organization).get("admin_links", [])
    return json.dumps(admin_links)


@app.route("/_scripts/config.js")
def layout_config():
    _config = (
        config.get_organization_config(current_user.organization)
        if current_user.is_authenticated
        else config.get_config()
    )

    return (
        f"window._trotto = window._trotto || {{}}; "
        f"window._trotto.layout = {json.dumps(_config.get('layout', {}))!s};"
    )


def _is_safe_path(base_path, user_input):
    # Resolve the absolute path
    base_path = os.path.abspath(base_path)
    user_path = os.path.abspath(os.path.join(base_path, user_input))

    # Ensure the base path is still the prefix of the resolved absolute path
    return user_path.startswith(base_path)


@app.route("/_styles/<path:path>")
def static_style_files(path):
    """Old frontend styles"""
    if not _is_safe_path("/_styles", path):
        return "Invalid path", 400

    return send_from_directory("static/_styles", path)


@app.route("/_scripts/<path:path>")
def static_script_files(path):
    """Old frontend scripts"""
    if not _is_safe_path("/_scripts", path):
        return "Invalid path", 400

    return send_from_directory("static/_scripts", path)


@app.route("/_images/<path:path>")
def static_image_files(path):
    """Images route, used by various clients"""
    if not _is_safe_path("/_images", path):
        return "Invalid path", 400

    return send_from_directory("static/_images", path)


@app.route("/_next_static/<path:path>")
def static_next_files(path: str):
    """Handle next.js assets separately"""
    if not _is_safe_path("/_next_static", path):
        return "Invalid path", 400
    return send_from_directory("static/templates/_next_static", path)


if __name__ == "__main__":
    add_routes()
    app.run()
