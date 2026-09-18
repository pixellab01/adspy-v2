"""AdSpy v2 Flask application factory.

    create_app(config: dict | None = None) -> Flask

Two things worth knowing before you edit this file:

1. **Loopback only.** In LOCAL MODE (the default) there is no login, no users,
   no roles, no workspace_id (docs/00-decisions.md #4). The only thing standing
   between this app and the world is that it binds 127.0.0.1. Do not add a
   0.0.0.0 bind "for testing". SERVER MODE (``ADSPY2_SERVER_MODE=1``, or
   ``create_app({"SERVER_MODE": True, ...})``) still binds 127.0.0.1 — a reverse
   proxy does the exposing — and app/auth.py puts a login in front of every
   screen. It refuses to boot without a password and a secret key.

2. **Blueprints register themselves.** Feature modules are discovered, not
   hard-wired: every module-level ``flask.Blueprint`` found in the modules
   listed below is registered. Put the ``url_prefix`` on the Blueprint itself.

   The P0 modules in ``REQUIRED_MODULES`` must import and must yield a
   blueprint — if one does not, ``create_app`` raises. During the parallel
   build they were optional so a half-finished file could not stop the app
   booting; now that P0 is complete that leniency is a hazard, because a typo
   in ``app/jobs.py`` would degrade into "every worker call 404s" with nothing
   but an INFO log to say why. ``LATER_PHASE_MODULES`` stays optional: those
   files genuinely do not exist yet.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, Flask, render_template

from . import config as app_config
from . import db as db_module

log = logging.getLogger("adspy2")

# Import order matters only for URL-rule precedence at "/" (see _register_root).
REQUIRED_MODULES: tuple[str, ...] = (
    "app.routes.pages",       # Phase 3 — pages list + page detail (owns "/")
    "app.routes.queue",       # Phase 3 — jobs/queue screen
    "app.jobs",               # Phase 2 — 5-endpoint worker API
)

LATER_PHASE_MODULES: tuple[str, ...] = (
    "app.routes.overview",    # Phase 4 — Overview, v1's landing screen
    "app.routes.alerts",      # Phase 4 — Scaling Alerts
    "app.routes.sessions",    # Phase 4 — Sessions
    "app.routes.keyword",     # Phase 4 — Keyword Research
    "app.routes.products",    # Phase 4
    "app.routes.groups",      # Phase 4
    "app.products",           # Phase 4 — product extraction API, if it has a bp
    "app.routes.winners",     # Phase 4 — Winners + Test Queue
    "app.routes.settings",    # Phase 4 — Settings
    "app.routes.logs",        # Logs / diagnostics — issue log + snapshots
    "app.routes.dataset",     # OLD DATA / NEW DATA switch (app/dataset.py)
    "app.routes.rescan",      # product-wise re-scan (drawer action + JSON)
)


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(app_config.TEMPLATES_DIR),
        static_folder=str(app_config.STATIC_DIR),
        static_url_path="/static",
    )
    app.config.update(app_config.default_config())
    if config:
        app.config.update(config)
        # A caller that names one exact file (every test does) gets exactly
        # that file: no sidecar, no switching. A test that wants the two-file
        # mode says so with DATASET_SWITCHABLE=True and its own DATASET_PATHS.
        if "DATABASE" in config and "DATASET_SWITCHABLE" not in config:
            app.config["DATASET_SWITCHABLE"] = False
    if app.config.get("DATASET_SWITCHABLE"):
        # Boot-time value only; db.current_database_path() re-reads the sidecar
        # on every request. Kept so `config["DATABASE"]` still names a real file
        # for anything that only wants "where did we start".
        app.config["DATABASE"] = app_config.dataset_db_path(
            app_config.read_active_dataset(app.config.get("DATASET_FILE")),
            app.config.get("DATASET_PATHS"),
        )

    # Server mode (no-op in local mode). FIRST, before any blueprint: the login
    # gate must be the first before_request hook, SECRET_KEY must be the real
    # one before pages/products fall back to a random per-process key, and a
    # bad server-mode environment must stop the boot before anything is served.
    from . import auth as auth_module

    auth_module.init_app(app)

    db_module.init_app(app)

    if app.config.get("RUN_MIGRATIONS", True):
        with app.app_context():
            applied = db_module.run_migrations(db_module.get_db())
            if applied:
                log.info("migrations applied: %s", ", ".join(applied))

    # Health must always work, even if every other module is mid-rewrite.
    from .routes import health as health_module

    app.register_blueprint(health_module.bp)

    for module_path in REQUIRED_MODULES:
        _register_required(app, module_path)
    for module_path in LATER_PHASE_MODULES:
        _register_optional(app, module_path)

    _register_root(app)
    _register_error_handlers(app)
    _register_template_globals(app)
    return app


# ---------------------------------------------------------------------------
# blueprint discovery
# ---------------------------------------------------------------------------
def _register_required(app: Flask, module_path: str) -> None:
    """P0 module: import it, register its blueprints, and fail loudly if either
    step comes up empty. A silently-missing worker API is worse than no app."""
    module = importlib.import_module(module_path)          # let ImportError out
    if _register_blueprints(app, module, module_path) == 0:
        raise RuntimeError(
            f"{module_path} defines no Blueprint — the routes it owns would 404. "
            "Either give it a module-level Blueprint or move it to "
            "LATER_PHASE_MODULES."
        )


def _register_optional(app: Flask, module_path: str) -> None:
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        # Not written yet (or its own optional dependency is missing) — fine.
        log.info("skip %s: %s", module_path, exc)
        return
    except Exception as exc:                              # pragma: no cover
        log.warning("skip %s: import failed: %r", module_path, exc)
        return

    if _register_blueprints(app, module, module_path) == 0:
        log.info("no new blueprint in %s", module_path)


def _register_blueprints(app: Flask, module: Any, module_path: str) -> int:
    found = 0
    for attr in vars(module).values():
        if not isinstance(attr, Blueprint):
            continue
        if attr.name in app.blueprints:
            continue                                      # already registered
        app.register_blueprint(attr)
        found += 1
        log.info("registered blueprint %r from %s", attr.name, module_path)
    return found


def _register_root(app: Flask) -> None:
    """Give '/' a landing page only if no feature blueprint claimed it."""
    if any(rule.rule == "/" for rule in app.url_map.iter_rules()):
        return

    @app.get("/")
    def index():                                          # pragma: no cover
        return render_template(
            "base.html",
            title="AdSpy v2",
            active_nav="pages",
            placeholder=True,
        )


def _anonymous_in_server_mode(app: Flask) -> bool:
    """SERVER MODE only. The public paths (/static/*, /login, /health) can still
    404 for a stranger, and base.html carries the sidebar, the dataset chip and
    the database's absolute path. Someone who is not logged in gets a bare
    text answer instead. Local mode: always False, pages render as before."""
    if not app.config.get("SERVER_MODE"):
        return False
    from . import auth as auth_module

    try:
        return not auth_module.is_authenticated()
    except Exception:  # noqa: BLE001 - when in doubt, show nothing
        return True


def _bare(message: str, status: int):
    return message, status, {"Content-Type": "text/plain; charset=utf-8"}


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(404)
    def not_found(exc):
        from flask import request

        if request.path.startswith("/api/"):
            return {"ok": False, "error": "not found", "code": "NOT_FOUND"}, 404
        if _anonymous_in_server_mode(app):
            return _bare("Not found", 404)
        return render_template("base.html", title="Not found", not_found=True), 404

    @app.errorhandler(500)
    def server_error(exc):                                # pragma: no cover
        from flask import request

        log.exception("unhandled error on %s", request.path)
        if request.path.startswith("/api/"):
            return {"ok": False, "error": "server error", "code": "SERVER_ERROR"}, 500
        if _anonymous_in_server_mode(app):
            return _bare("Server error", 500)
        return render_template("base.html", title="Error", server_error=True), 500


def short_url(value: Any, limit: int = 60) -> str:
    """A product URL a human can read, for the line under a product name.

    Landing URLs come off the ad carrying the whole click-tracking tail —
    `?c=84823&fbclid=IwZXh0bgNhZW0CMTAAYnJpZBExeTYyWXdFMmFvdmVjbFRHdHNy...`
    — often 300+ characters. Rendered in a table cell that wraps, one product
    took three lines and pushed Domain / Active / Pages / Age / Actions off the
    right edge of the screen.

    Host + path is the part that identifies the product; the query string is
    attribution noise. The full URL stays in the anchor's href and title, so
    nothing is lost — only the display shrinks.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text[:limit]
    if not parsed.netloc:
        return text if len(text) <= limit else text[: limit - 1] + "…"
    host = parsed.netloc.removeprefix("www.")
    path = (parsed.path or "").rstrip("/")
    clean = f"{host}{path}"
    if len(clean) <= limit:
        return clean
    # Keep the host and the tail of the path — the slug is the identifying part.
    keep = max(0, limit - len(host) - 2)
    return f"{host}…{path[-keep:]}" if keep else host


def _register_template_globals(app: Flask) -> None:
    from . import dataset as dataset_module
    from .meta_links import meta_ad_url, meta_ads_library_url
    from .time_utils import age_days

    # A context processor, not a global: which dataset is live can change
    # between two requests, and every screen's header (the 404 page included)
    # shows the OLD DATA / NEW DATA chip from this.
    app.context_processor(lambda: {"dataset": dataset_module.describe()})

    app.jinja_env.globals.update(
        app_name=app.config["APP_NAME"],
        app_version=app.config["VERSION"],
        meta_ads_library_url=meta_ads_library_url,
        meta_ad_url=meta_ad_url,
        age_days=age_days,
        route_exists=_route_exists_for(app),
    )
    app.jinja_env.filters["short_url"] = short_url


def _route_exists_for(app: Flask):
    """`route_exists('/products')` — true only if some registered blueprint
    actually serves that path.

    The nav lists screens from every phase, but Phase 4 (Products, Groups) is
    not built yet. Without this the sidebar offers two links straight into the
    404 page, which reads as "the app is broken" rather than "not built yet".
    Computed once at boot: the URL map never changes after create_app returns.
    """
    from werkzeug.exceptions import HTTPException

    adapter = app.url_map.bind(app.config.get("SERVER_NAME") or "localhost")
    cache: dict[str, bool] = {}

    def route_exists(path: str) -> bool:
        if path not in cache:
            try:
                adapter.match(path, method="GET")
                cache[path] = True
            except HTTPException:
                cache[path] = False
        return cache[path]

    return route_exists
