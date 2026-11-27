"""Sign in with Google, and keepers saved to Vercel Blob so they follow you between devices.

Plain OAuth 'authorization code' flow: /login sends you to Google, Google sends you back
to /auth/callback, we swap the code for an ID token straight from Google's token endpoint
(so its contents can be trusted without checking the signature) and remember who you are
in Flask's signed session cookie. Only your Google account id (hashed), name and email
are kept; the keepers list lives in a private Blob store under the hashed id.
"""
import base64
import hashlib
import json
import os
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import requests
from flask import Blueprint, abort, jsonify, redirect, render_template, render_template_string, request, session

bp = Blueprint("accounts", __name__)

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
BLOB_API = "https://vercel.com/api/blob/"
BLOB_API_VERSION = "13"
MAX_KEEPERS = 3000
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

DONE_PAGE = """<!DOCTYPE html><html><head><title>My Flask App</title></head><body>
<p>{{ msg }}</p>
<p><a href="/kept">see your kept songs</a> or <a href="/" onclick="window.close(); return false;">close this tab</a> and go back to your music.</p>
<script>try { localStorage.setItem("ddLogin", String(Date.now())); } catch (e) {} setTimeout(function () { window.close(); }, 800);</script>
</body></html>"""


def setup(app):
    app.secret_key = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
    app.permanent_session_lifetime = timedelta(days=365)
    app.config.update(SESSION_COOKIE_SECURE=not app.debug and not app.testing, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax")
    app.register_blueprint(bp)


def configured():
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def _redirect_uri():
    base = os.environ.get("PUBLIC_URL") or request.host_url.rstrip("/")
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    return base + "/auth/callback"


def uid(sub):
    return hashlib.sha256(("dusty-digger:" + sub).encode()).hexdigest()[:32]


# ---- Vercel Blob (private store), straight over HTTP: there's no Python SDK.

def _blob_token():
    return os.environ.get("BLOB_READ_WRITE_TOKEN") or ""


def _store_id(token):
    parts = token.split("_")
    return parts[3] if len(parts) > 3 else ""


def blob_get(pathname):
    """Parsed JSON, None if there's no such blob yet. Raises on any other failure."""
    token = _blob_token()
    url = f"https://{_store_id(token)}.private.blob.vercel-storage.com/{pathname}?cache=0"
    r = requests.get(url, headers={"authorization": f"Bearer {token}"}, timeout=8)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def blob_put(pathname, obj):
    token = _blob_token()
    r = requests.put(BLOB_API + "?" + urlencode({"pathname": pathname}), data=json.dumps(obj).encode(), timeout=8,
                     headers={"authorization": f"Bearer {token}", "x-api-version": BLOB_API_VERSION,
                              "x-vercel-blob-store-id": _store_id(token), "x-vercel-blob-access": "private",
                              "x-add-random-suffix": "0", "x-allow-overwrite": "1",
                              "x-content-type": "application/json", "x-cache-control-max-age": "60"})
    r.raise_for_status()


def _path(user):
    return f"keepers/{user['uid']}.json"


# ---- routes

PRIVACY_PAGE = """<!DOCTYPE html><html><head><title>My Flask App</title></head><body>
<h1>Privacy</h1>
<p>Dusty Digger works without an account. Songs you keep are saved in your own browser.</p>
<p>If you sign in with Google, we get your Google account id, name and email address from Google and nothing
else. We use them only to save your keepers list so it follows you between devices. Your list is stored
in a private Vercel Blob store under a scrambled (hashed) version of your Google id, and we stay signed in
with a cookie on your device. We don't sell, share or show any of it to anyone, and there are no ads or trackers.</p>
<p>Searches go to Discogs, YouTube and Wikipedia to find the music and the information about it.</p>
<p>To stop: sign out from the Keepers list. To have your saved list deleted, email patrick.bardatcha@gmail.com.</p>
<p><a href="/">back to digging</a></p>
</body></html>"""


@bp.route("/privacy")
def privacy():
    return PRIVACY_PAGE

KEPT_VIEW = "kept.html"


@bp.route("/kept")
def kept_page():
    return render_template(KEPT_VIEW)


@bp.route("/login")
def login():
    if not configured():
        return render_template_string(DONE_PAGE, msg="Signing in isn't switched on yet.")
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    return redirect(GOOGLE_AUTH + "?" + urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": _redirect_uri(), "response_type": "code",
        "scope": "openid email profile", "state": state, "prompt": "select_account"}))


@bp.route("/auth/callback")
def callback():
    state = session.pop("oauth_state", None)
    if not state or request.args.get("state") != state or not request.args.get("code"):
        return render_template_string(DONE_PAGE, msg="That sign-in didn't go through. Try again."), 400
    try:
        r = requests.post(GOOGLE_TOKEN, timeout=10, data={
            "code": request.args["code"], "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"], "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code"})
        id_token = r.json().get("id_token") if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        id_token = None
    if not id_token:
        return render_template_string(DONE_PAGE, msg="Google didn't confirm the sign-in. Try again."), 502
    payload = id_token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if claims.get("aud") != os.environ["GOOGLE_CLIENT_ID"] or claims.get("iss") not in (
            "https://accounts.google.com", "accounts.google.com") or not claims.get("sub"):
        return render_template_string(DONE_PAGE, msg="That sign-in didn't check out. Try again."), 400
    session.permanent = True
    session["user"] = {"uid": uid(claims["sub"]), "email": claims.get("email") or "",
                       "name": claims.get("given_name") or claims.get("name") or claims.get("email") or "you"}
    return render_template_string(DONE_PAGE, msg=f"Signed in, you can close this tab. Your keepers are saved to your account ({session['user']['email']}).")


@bp.route("/logout", methods=["POST"])
def logout():
    _same_origin()
    session.pop("user", None)
    return jsonify({"ok": True})


def _same_origin():
    """Writes only from our own pages (the cookie is SameSite=Lax too)."""
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        abort(403)


def clean_keepers(items):
    out, seen = [], set()
    for k in items if isinstance(items, list) else []:
        if not isinstance(k, dict) or not YT_ID.match(str(k.get("id", ""))) or k["id"] in seen:
            continue
        seen.add(k["id"])
        out.append({"id": k["id"], "name": str(k.get("name") or "")[:300], "album": str(k.get("album") or "")[:300],
                    "link": str(k.get("link") or "")[:500],
                    "year": k["year"] if isinstance(k.get("year"), int) and 0 < k["year"] < 3000 else 0,
                    "at": k["at"] if isinstance(k.get("at"), int) and 0 < k["at"] < 10**14 else 0})
        if len(out) >= MAX_KEEPERS:
            break
    return out


@bp.route("/keepers", methods=["GET", "POST"])
def keepers():
    user = session.get("user")
    if request.method == "GET":
        if not user:
            return jsonify({"user": None, "signin": configured()})
        try:
            data = blob_get(_path(user)) or {}
        except (requests.RequestException, ValueError):
            return jsonify({"error": "storage"}), 503
        return jsonify({"user": {"name": user["name"], "email": user["email"], "uid": user["uid"]},
                        "keepers": clean_keepers(data.get("keepers"))})
    _same_origin()
    if not user:
        return jsonify({"error": "signed out"}), 401
    body = request.get_json(silent=True) or {}
    items = clean_keepers(body.get("keepers"))
    try:
        blob_put(_path(user), {"keepers": items})
    except requests.RequestException:
        return jsonify({"error": "storage"}), 503
    return jsonify({"ok": True, "count": len(items)})
