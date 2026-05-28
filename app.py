from __future__ import annotations

import os
from typing import Any

from flask import Flask, request, session

from modules.app_config import CONFIG, active_login_profile, get_runtime_config
from modules.profile_store import profile_names
from modules.page_routes import register_routes
from modules.session_store import current_user


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = CONFIG.secret_key

    @app.context_processor
    def inject_template_context() -> dict[str, Any]:
        user = current_user()
        connection_profile = dict(session.get("connection_profile", {})) if user else {}
        return {
            "config": get_runtime_config(),
            "login_profile": active_login_profile(),
            "active_connection_profile": {
                "name": connection_profile.get("name", ""),
                "label": connection_profile.get("label", ""),
                "db_username": session.get("db_username", "") if user else "",
            },
            "login_profiles": profile_names(),
            "current_user": user,
            "current_role_label": user["role_label"] if user else "",
            "current_menu": user["menu"] if user else [],
            "active_slug": request.view_args.get("slug") if request.view_args else "",
        }

    @app.after_request
    def add_no_cache_headers(response):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    register_routes(app)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("MRS_WEBAPP_PORT", "5000")))
