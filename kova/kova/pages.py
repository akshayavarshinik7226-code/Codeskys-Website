from flask import Blueprint, current_app, redirect, render_template, url_for

from .auth import current_user

bp = Blueprint("pages", __name__)


def _ctx():
    return {"platform_name": current_app.config["PLATFORM_NAME"]}


@bp.get("/")
def home():
    if current_user() is not None:
        return render_template("app.html", **_ctx())
    return render_template("landing.html", **_ctx())


@bp.get("/login")
def login_page():
    if current_user() is not None:
        return redirect(url_for("pages.home"))
    return render_template("login.html", **_ctx())
