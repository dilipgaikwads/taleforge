"""
VideoFactory - dashboard login (one owner, one password).

- Every page requires login, except the login page itself.
- If no password has been set yet, the whole dashboard stays locked.
- After 5 wrong attempts from the same device, that device must wait 5 minutes.
- Phones stay logged in for 30 days (until you tap "Log out" or change the password).
"""
import time
from datetime import timedelta

from flask import redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash

from .config import DASHBOARD_PASSWORD_HASH, FLASK_SECRET_KEY
from .db import log_event

MAX_FAILURES = 5
LOCKOUT_SECONDS = 300
_failures = {}  # device address -> (wrong attempts, time of last wrong attempt)

STYLE = """
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>
  :root { --bg:#f4f5f7; --card:#ffffff; --text:#16181d; --muted:#5d6470;
          --bad:#c62828; --accent:#2f5bea; --field:#f0f2f6; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1115; --card:#1a1d24; --text:#eceef2; --muted:#9aa1ad;
            --bad:#ff6b6b; --accent:#6f8cff; --field:#262a33; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
         background:var(--bg); color:var(--text);
         padding: max(16px, env(safe-area-inset-top)) 16px max(24px, env(safe-area-inset-bottom)); }
  main { max-width: 420px; margin: 12vh auto 0; background: var(--card);
         border-radius: 16px; padding: 24px 20px; }
  h1 { font-size: 1.5rem; margin: 0 0 6px; }
  p { color: var(--muted); margin: 0 0 18px; }
  label { display:block; font-weight:600; margin-bottom:8px; }
  input { width:100%; padding:14px; border-radius:12px; border:1px solid transparent;
          background:var(--field); color:var(--text); font:inherit; font-size:1rem; }
  input:focus { outline: 2px solid var(--accent); }
  button { width:100%; margin-top:16px; padding:14px; border:0; border-radius:12px;
           background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer; }
  .error { color: var(--bad); font-weight:600; margin: 14px 0 0; }
  code { background: var(--field); padding: 2px 6px; border-radius: 6px; word-break: break-all; }
</style>
"""

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>VideoFactory - Log in</title>""" + STYLE + """</head>
<body><main>
  <h1>VideoFactory</h1>
  <p>Enter your dashboard password.</p>
  <form method="post" action="/login">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
    <button type="submit">Log in</button>
  </form>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
</main></body></html>"""

SETUP_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>VideoFactory - Locked</title>""" + STYLE + """</head>
<body><main>
  <h1>Dashboard locked</h1>
  <p>No dashboard password has been set yet, so the dashboard stays locked for safety.</p>
  <p>On the Mac, in Terminal, inside the VideoFactory folder, run:</p>
  <p><code>venv/bin/python -m app.set_password</code></p>
  <p>Then restart the app.</p>
</main></body></html>"""


def _is_locked(ip):
    count, last = _failures.get(ip, (0, 0.0))
    if count < MAX_FAILURES:
        return False
    if time.time() - last < LOCKOUT_SECONDS:
        return True
    _failures.pop(ip, None)  # lockout period is over
    return False


def _record_failure(ip):
    count, _ = _failures.get(ip, (0, 0.0))
    _failures[ip] = (count + 1, time.time())


def init_auth(app):
    """Switch on login protection for the whole app."""
    app.secret_key = FLASK_SECRET_KEY or None
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )

    @app.before_request
    def require_login():
        if not DASHBOARD_PASSWORD_HASH or not FLASK_SECRET_KEY:
            return render_template_string(SETUP_PAGE), 503
        if request.path == "/login":
            return None
        if session.get("logged_in"):
            return None
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = ""
        ip = request.remote_addr or "unknown"
        if request.method == "POST":
            if _is_locked(ip):
                error = "Too many wrong attempts. Please wait 5 minutes and try again."
            elif check_password_hash(DASHBOARD_PASSWORD_HASH, request.form.get("password", "")):
                _failures.pop(ip, None)
                session.clear()
                session["logged_in"] = True
                session.permanent = True
                log_event("login_success", ip)
                return redirect("/")
            else:
                _record_failure(ip)
                log_event("login_failed", ip)
                error = "Wrong password."
        return render_template_string(LOGIN_PAGE, error=error)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))
