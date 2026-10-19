"""
VideoFactory - YouTube (official YouTube Data API v3, OAuth 2.0).

How the connection works:
  1. On the Mac, you tap "Connect YouTube". Google's own sign-in page opens.
  2. You approve. Google sends the factory a long-term permission ("refresh token").
     Your Google password is never seen or stored by the factory.
  3. The permission is kept in VideoFactory/secrets/youtube_token.json, readable
     only by your Mac user, and is never shown, logged, or put in the database.

Notes:
  - Uses PKCE and a random "state" value, the protections Google recommends.
  - While the Google Cloud app is in "Testing" mode, Google ends the permission
    after 7 days. The factory then asks you (on the dashboard) to reconnect.
  - Videos from unaudited API projects are kept PRIVATE by YouTube. Test uploads
    are therefore private on purpose.
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .db import get_db, log_event

BASE_DIR = Path(__file__).resolve().parent.parent
SECRETS_DIR = BASE_DIR / "secrets"
CLIENT_FILE = SECRETS_DIR / "youtube_client.json"
TOKEN_FILE = SECRETS_DIR / "youtube_token.json"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_URL = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
SCOPES = [
    UPLOAD_SCOPE,
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
REDIRECT_URI = "http://127.0.0.1:8000/"   # Google sends you back here, on the Mac itself
TESTING_MODE_DAYS = 7
EDUCATION_CATEGORY = "27"


class YouTubeError(Exception):
    """A plain-language YouTube problem. retryable=True means 'try again later'."""

    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class YouTubeAuthError(YouTubeError):
    """The YouTube connection is missing or has ended: the owner must reconnect."""


# ---------- small helpers ----------

def client_ready():
    return CLIENT_FILE.exists()


def _load_client():
    try:
        data = json.loads(CLIENT_FILE.read_text(encoding="utf-8"))
        info = data.get("installed") or data.get("web")
        return info["client_id"], info["client_secret"]
    except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError):
        raise YouTubeAuthError("The YouTube client file is missing or damaged. Redo Step 21, Part 3.")


def _load_token():
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_secret(path, data):
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRETS_DIR, 0o700)
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, 0o600)


def _google_error(exc):
    """Turn an HTTP error from Google into (status, reason, message)."""
    try:
        body = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
    except (json.JSONDecodeError, ValueError):
        body = {}
    error = body.get("error")
    if isinstance(error, dict):
        reasons = [e.get("reason", "") for e in error.get("errors", []) if isinstance(e, dict)]
        return exc.code, (reasons[0] if reasons else ""), error.get("message", "")
    return exc.code, str(error or ""), str(body.get("error_description", ""))


def _post_form(url, fields):
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        code, reason, message = _google_error(exc)
        if reason == "invalid_grant":
            raise YouTubeAuthError("Google ended the YouTube connection (in Testing mode this happens "
                                   "every 7 days). Reconnect YouTube on the Mac.")
        raise YouTubeError(f"Google sign-in error {code}: {reason} {message}".strip(), retryable=code >= 500)
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        raise YouTubeError("Could not reach Google. Check the Mac's internet connection.", retryable=True)


# ---------- connecting ----------

def start_authorization():
    """Return (google_sign_in_url, state, verifier). state and verifier must be kept for the callback."""
    client_id, _ = _load_client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}", state, verifier


def finish_authorization(code, verifier):
    client_id, client_secret = _load_client()
    data = _post_form(TOKEN_URL, {
        "client_id": client_id, "client_secret": client_secret, "code": code,
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": REDIRECT_URI,
    })
    granted = set((data.get("scope") or "").split())
    if UPLOAD_SCOPE not in granted:
        raise YouTubeError("The permission to upload videos was not ticked on Google's page. "
                           "Connect again and tick every box.")
    if not data.get("refresh_token"):
        raise YouTubeError("Google did not give a long-term permission. Tap Disconnect, then Connect again.")
    _save_secret(TOKEN_FILE, {
        "refresh_token": data["refresh_token"], "access_token": data.get("access_token", ""),
        "expires_at": time.time() + int(data.get("expires_in", 3600)) - 120,
        "scope": data.get("scope", ""), "connected_at": time.time(),
    })
    channel = my_channel()
    with get_db() as conn:
        conn.execute("UPDATE social_accounts SET status = 'disconnected' WHERE platform = 'youtube'")
        conn.execute(
            "INSERT INTO social_accounts (platform, account_id, account_name, status, scopes) "
            "VALUES ('youtube', ?, ?, 'connected', ?) "
            "ON CONFLICT(platform, account_id) DO UPDATE SET account_name = excluded.account_name, "
            "status = 'connected', scopes = excluded.scopes, connected_at = datetime('now'), "
            "updated_at = datetime('now')",
            (channel["id"], channel["title"], data.get("scope", "")))
    log_event("youtube_connected", f"{channel['title']} ({channel['id']})")
    return channel


def disconnect():
    token = _load_token()
    if token:
        try:
            _post_form(REVOKE_URL, {"token": token["refresh_token"]})
        except YouTubeError:
            pass  # already ended at Google's side; still remove it here
        TOKEN_FILE.unlink(missing_ok=True)
    with get_db() as conn:
        conn.execute("UPDATE social_accounts SET status = 'disconnected', updated_at = datetime('now') "
                     "WHERE platform = 'youtube'")
    log_event("youtube_disconnected", "")


def _mark_needs_reconnect():
    with get_db() as conn:
        conn.execute("UPDATE social_accounts SET status = 'needs_reconnect', updated_at = datetime('now') "
                     "WHERE platform = 'youtube' AND status = 'connected'")


def connection_status():
    """For the dashboard: {'state': 'not_set_up'|'not_connected'|'connected'|'needs_reconnect', ...}."""
    if not client_ready():
        return {"state": "not_set_up"}
    with get_db() as conn:
        row = conn.execute("SELECT * FROM social_accounts WHERE platform = 'youtube' "
                           "AND status IN ('connected', 'needs_reconnect') ORDER BY updated_at DESC LIMIT 1").fetchone()
    token = _load_token()
    if row is None or token is None:
        return {"state": "not_connected"}
    age_days = (time.time() - token.get("connected_at", time.time())) / 86400
    return {
        "state": row["status"], "name": row["account_name"], "channel_id": row["account_id"],
        "connected_at": row["connected_at"], "age_days": round(age_days, 1),
        "reconnect_soon": row["status"] == "connected" and age_days >= TESTING_MODE_DAYS - 1,
    }


# ---------- using the connection ----------

def access_token(force_refresh=False):
    token = _load_token()
    if not token:
        raise YouTubeAuthError("YouTube is not connected. Connect it on the Mac first.")
    if not force_refresh and token.get("access_token") and token.get("expires_at", 0) > time.time():
        return token["access_token"]
    client_id, client_secret = _load_client()
    try:
        data = _post_form(TOKEN_URL, {"client_id": client_id, "client_secret": client_secret,
                                      "refresh_token": token["refresh_token"], "grant_type": "refresh_token"})
    except YouTubeAuthError:
        _mark_needs_reconnect()
        raise
    token["access_token"] = data["access_token"]
    token["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 120
    _save_secret(TOKEN_FILE, token)
    return token["access_token"]


def _youtube_failure(exc, action):
    code, reason, message = _google_error(exc)
    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"):
        return YouTubeError(f"YouTube's free daily allowance is used up ({reason}). The factory will try again later.",
                            retryable=True)
    if reason == "uploadLimitExceeded":
        return YouTubeError("YouTube's daily upload limit for this channel has been reached. Try again tomorrow.",
                            retryable=True)
    if code in (401,):
        _mark_needs_reconnect()
        return YouTubeAuthError("YouTube no longer accepts the factory's permission. Reconnect YouTube on the Mac.")
    if code >= 500:
        return YouTubeError(f"YouTube is temporarily having problems ({code}).", retryable=True)
    return YouTubeError(f"YouTube refused to {action} ({code} {reason}): {message}"[:300])


def _api_get(path, params):
    for attempt in range(2):
        request = urllib.request.Request(
            f"{API_URL}{path}?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {access_token(force_refresh=attempt > 0)}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                continue
            raise _youtube_failure(exc, "answer")
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            raise YouTubeError("Could not reach YouTube. Check the Mac's internet connection.", retryable=True)


def my_channel():
    data = _api_get("/channels", {"part": "snippet", "mine": "true"})
    items = data.get("items") or []
    if not items:
        raise YouTubeError("This Google account has no YouTube channel yet. Create one on YouTube, then connect again.")
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def upload_video(path, title, description, tags, privacy="private"):
    """Upload an MP4 with YouTube's resumable upload. Returns {'id', 'privacy', 'url'}."""
    path = Path(path)
    size = path.stat().st_size
    metadata = {
        "snippet": {"title": title[:100], "description": description[:4900],
                    "tags": [t.lstrip("#") for t in tags][:15], "categoryId": EDUCATION_CATEGORY},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "embeddable": True},
    }
    location = None
    for attempt in range(2):
        request = urllib.request.Request(
            f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status",
            data=json.dumps(metadata).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {access_token(force_refresh=attempt > 0)}",
                     "Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                location = response.headers.get("Location")
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                continue
            raise _youtube_failure(exc, "start the upload")
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            raise YouTubeError("Could not reach YouTube. Check the Mac's internet connection.", retryable=True)
    if not location:
        raise YouTubeError("YouTube did not return an upload address.", retryable=True)

    request = urllib.request.Request(location, data=path.read_bytes(), method="PUT",
                                     headers={"Content-Type": "video/mp4"})
    try:
        with urllib.request.urlopen(request, timeout=1800) as response:
            video = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _youtube_failure(exc, "accept the video file")
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        raise YouTubeError("The upload was interrupted. The factory will try again.", retryable=True)

    video_id = video.get("id")
    if not video_id:
        raise YouTubeError("YouTube did not confirm the upload.", retryable=True)
    return {"id": video_id, "privacy": (video.get("status") or {}).get("privacyStatus", privacy),
            "url": f"https://www.youtube.com/shorts/{video_id}"}


def video_status(video_id):
    data = _api_get("/videos", {"part": "status", "id": video_id})
    items = data.get("items") or []
    if not items:
        return {"found": False}
    status = items[0].get("status") or {}
    return {"found": True, "upload_status": status.get("uploadStatus", ""),
            "privacy": status.get("privacyStatus", ""), "rejection": status.get("rejectionReason", "")}
