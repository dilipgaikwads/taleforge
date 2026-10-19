"""
VideoFactory - social account pages.

  /youtube              connection status, Connect / Disconnect buttons
  /youtube/connect      button: start Google's sign-in (must be done ON THE MAC)
  /youtube/callback     Google sends you back here after you approve
  /youtube/disconnect   button: remove the factory's YouTube permission

Google only sends the approval back to the Mac itself (127.0.0.1), so connecting
is done in a browser on the Mac. After that, everything works from the phone.
"""
from flask import Blueprint, flash, redirect, render_template, request, session

from .youtube import (YouTubeError, connection_status, disconnect, finish_authorization,
                      start_authorization)

bp = Blueprint("social", __name__)


def on_the_mac():
    return request.remote_addr in ("127.0.0.1", "::1") and request.host.split(":")[0] in ("127.0.0.1", "localhost")


@bp.route("/youtube")
def youtube_page():
    return render_template("youtube.html", yt=connection_status(), on_mac=on_the_mac())


@bp.post("/youtube/connect")
def youtube_connect():
    if not on_the_mac():
        flash("Connecting must be done in a browser on the Mac, at http://127.0.0.1:8000/youtube", "bad")
        return redirect("/youtube")
    try:
        url, state, verifier = start_authorization()
    except YouTubeError as exc:
        flash(str(exc), "bad")
        return redirect("/youtube")
    session["yt_state"], session["yt_verifier"] = state, verifier
    return redirect(url)


@bp.route("/youtube/callback")
def youtube_callback():
    state, verifier = session.pop("yt_state", None), session.pop("yt_verifier", None)
    if request.args.get("error"):
        flash(f"Google did not connect YouTube: {request.args.get('error')}. You can try again.", "bad")
        return redirect("/youtube")
    if not state or request.args.get("state") != state or not request.args.get("code"):
        flash("That sign-in could not be verified (it may be old). Please tap Connect again.", "bad")
        return redirect("/youtube")
    try:
        channel = finish_authorization(request.args["code"], verifier)
    except YouTubeError as exc:
        flash(f"YouTube was not connected: {exc}", "bad")
        return redirect("/youtube")
    flash(f"YouTube connected: {channel['title']}", "ok")
    return redirect("/youtube")


@bp.post("/youtube/disconnect")
def youtube_disconnect():
    disconnect()
    flash("YouTube disconnected. The factory can no longer upload until you connect again.", "ok")
    return redirect("/youtube")
