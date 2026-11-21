"""Log a Blender Party player in through Supabase, with Google or Twitch.

This file has no Blender imports, so you can run and test it with plain Python.
The Blender buttons and panel live in login_ui.py.

How the login works (the PKCE flow):
1. We make a random secret (the "verifier") and its SHA-256 hash (the "challenge").
2. We open the player's browser on Supabase's login page, sending only the
   challenge. The player logs in with Google or Twitch there.
3. Supabase sends the browser back to a tiny web server we run on 127.0.0.1,
   with a one-time ?code= in the address.
4. We send that code plus the original verifier to Supabase. Only the program
   that made the verifier can finish the login, so a stolen code is useless.
5. Supabase answers with an access token (short-lived, sent to our game server)
   and a refresh token (trades for a new access token without logging in again).
"""

from __future__ import annotations

import base64
import hashlib
import html
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 53682          # must match the Redirect URL you added in Supabase
CALLBACK_PATH = "/callback"
LOGIN_TIMEOUT_SECONDS = 300    # stop waiting if the player never finishes in the browser
REFRESH_MARGIN_SECONDS = 60    # refresh an access token this long before it expires
PROVIDERS = ("google", "twitch")


class LoginError(Exception):
    """Login failed. The message is written so it can be shown to the player. RAHHHH"""


class NetworkError(LoginError):
    """Supabase couldn't be reached. Unlike other errors, retrying later may work. RAHHHHHHH"""


def make_pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge). The challenge is the base64url SHA-256 of the verifier."""
    verifier = secrets.token_urlsafe(64)  # 86 characters, inside the allowed 43 to 128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class SupabaseAuth:
    """Calls Supabase Auth's web API. Keeps no login state of its own."""

    def __init__(self, project_url: str, publishable_key: str):
        self.base = project_url.rstrip("/") + "/auth/v1"
        self.key = publishable_key

    def authorize_url(self, provider: str, challenge: str, redirect_to: str) -> str:
        query = urllib.parse.urlencode({
            "provider": provider,
            "redirect_to": redirect_to,
            "code_challenge": challenge,
            "code_challenge_method": "s256",
        })
        return f"{self.base}/authorize?{query}"

    def exchange_code(self, code: str, verifier: str) -> dict:
        """Trade the one-time code for a session (access token + refresh token)."""
        return self._post_token("pkce", {"auth_code": code, "code_verifier": verifier})

    def refresh(self, refresh_token: str) -> dict:
        """Trade a refresh token for a new session. Supabase also issues a new refresh token."""
        #This doesnt seem to respect whether or not the user allows network access to addons
        return self._post_token("refresh_token", {"refresh_token": refresh_token})

    def _post_token(self, grant_type: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base}/token?grant_type={grant_type}",
            data=json.dumps(body).encode("utf-8"),
            headers={"apikey": self.key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                session = json.load(response) #Theoretically (access token, refresh token) regardless of whether I am asking for pkce or refresh token
        except urllib.error.HTTPError as err: #Must be done first because otherwise URLError will always consume the error
            raise LoginError(f"Supabase refused the login ({err.code}): {_error_text(err)}") from err
        except urllib.error.URLError as err:
            raise NetworkError(f"Couldn't reach Supabase: {err.reason}") from err
        if "expires_at" not in session:  # usually present; work it out from expires_in if not
            session["expires_at"] = int(time.time()) + int(session.get("expires_in", 3600))
        return session


def _error_text(err: urllib.error.HTTPError) -> str:
    """Looks like trying every possible way of extracting a message from an error, defaulting to just the actual returned data (payload)?"""
    try:
        payload = json.load(err)
    except (ValueError, OSError):
        return str(err.reason)
    return (payload.get("error_description") or payload.get("msg")
            or payload.get("message") or str(payload))


class _CallbackServer(http.server.HTTPServer):
    # Windows lets a second program take over a port marked "reuse address",
    # so only allow reuse on other systems.
    allow_reuse_address = os.name != "nt"
    result: tuple[str, str] | None = None   # ("code", value) or ("error", message)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Answers the one browser redirect from Supabase and records the result."""

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != CALLBACK_PATH:
            self.send_error(404)  # e.g. the browser asking for favicon.ico
            return
        params = urllib.parse.parse_qs(url.query)
        if "code" in params:
            self.server.result = ("code", params["code"][0])
            message = "Almost done! Return to blender! RAHHHHHHHHHHHH"
        else:
            error = (params.get("error_description") or params.get("error") or ["Unknown error"])[0] # [0] gets the first valid return of the conditions? always returns only 1 condition even though there are 3 maybe?
            self.server.result = ("error", error)
            message = f"Login failed: {error}. Go back to Blender and try again."
        body = (f"<!doctype html><meta charset='utf-8'><title>Blender Party</title>"
                f"<p>{html.escape(message)}</p>").encode("utf-8") #not sure why we are encoding only this part? not sure why html.escape is needed to replace &<>
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep Blender's console quiet


class LoginAttempt:
    """One try at logging in. The waiting happens on a background thread.

    Call start() once, then check .done, .session and .error. Blender checks
    them from a timer, so its interface never freezes while the browser is open.
    """

    def __init__(self, auth: SupabaseAuth, provider: str, open_browser=webbrowser.open):
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider {provider!r}")
        self.auth = auth
        self.provider = provider
        self.open_browser = open_browser
        self.done = False
        self.session: dict | None = None
        self.error: str | None = None
        self._cancelled = threading.Event()

    def start(self) -> None:
        verifier, challenge = make_pkce_pair()
        try:
            server = _CallbackServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)
        except OSError as err:
            raise LoginError(f"Port {CALLBACK_PORT} is already in use. Close any other "
                             "Blender window that's logging in, then try again.") from err
        server.timeout = 0.5  # how often the wait loop wakes up to check for cancel
        redirect_to = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
        threading.Thread(target=self._wait, args=(server, verifier), daemon=True).start()
        self.open_browser(self.auth.authorize_url(self.provider, challenge, redirect_to))

    def cancel(self) -> None:
        self._cancelled.set()

    def _wait(self, server: _CallbackServer, verifier: str) -> None:
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        try:
            while server.result is None: #while the user has not yet logged in, check to see if they cancelled or expired
                if self._cancelled.is_set():
                    raise LoginError("Login cancelled.")
                if time.monotonic() > deadline:
                    raise LoginError("Login timed out. Try again.")
                server.handle_request()  # returns after one request, or after server.timeout
            kind, value = server.result
            if kind == "error":
                raise LoginError(value)
            self.session = self.auth.exchange_code(value, verifier)
        except LoginError as err:
            self.error = str(err)
        except Exception as err:  # never let the thread die without telling the player
            self.error = f"Unexpected login error: {err}"
        finally:
            server.server_close()
            self.done = True  # set last, so .session or .error is ready when .done is


def _keep_needed(session: dict) -> dict:
    """Drop what we don't need, such as Google's or Twitch's own tokens."""
    user = session.get("user") or {}
    return {
        "access_token": session["access_token"],
        "refresh_token": session["refresh_token"],
        "expires_at": session["expires_at"],
        "user": {
            "id": user.get("id"),
            "email": user.get("email"),
            "user_metadata": user.get("user_metadata") or {},
        },
    }


class SessionStore:
    """Saves the session to a small JSON file so players stay logged in between runs."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, session: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(session), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)  # only this user can read it (ignored on Windows)
        except OSError:
            pass
        temp.replace(self.path)  # swap the whole file in, so a crash can't leave half of one

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class Account:
    """The player's login: the saved session, token refresh and logging out."""

    def __init__(self, auth: SupabaseAuth, store: SessionStore):
        self.auth = auth
        self.store = store
        self.session = store.load()

    @property
    def logged_in(self) -> bool:
        return self.session is not None

    @property
    def display_name(self) -> str:
        """RAHHHH RAHHHH Id like to add functionality to let users pick their own name this may possible if we store it in the user's session"""
        user = (self.session or {}).get("user") or {}
        meta = user.get("user_metadata") or {}
        return (meta.get("full_name") or meta.get("name")
                or meta.get("preferred_username") or user.get("email") or "Player")

    def set_session(self, session: dict) -> None:
        self.session = _keep_needed(session)
        self.store.save(self.session)

    def access_token(self) -> str:
        """Return a valid access token for the game server, refreshing it if needed.

        Refreshing is a network call, so use this right before connecting,
        not on every redraw.
        """
        if not self.session:
            raise LoginError("You're not logged in.")
        if self.session["expires_at"] - time.time() < REFRESH_MARGIN_SECONDS:
            try:
                self.set_session(self.auth.refresh(self.session["refresh_token"]))
            except NetworkError:
                raise  # offline: keep the session and let the player retry
            except LoginError as err:
                self.log_out()  # refresh token expired or revoked
                raise LoginError("Your login expired. Please log in again.") from err
        return self.session["access_token"]

    def log_out(self) -> None:
        self.session = None
        self.store.clear() #Clearing the user's saved json session info means logging out because its where we store their tokens.


def _main() -> None:
    """Try the login from a terminal, outside Blender."""
    import argparse

    parser = argparse.ArgumentParser(description="Try the Blender Party login outside Blender.")
    parser.add_argument("provider", choices=PROVIDERS)
    parser.add_argument("--url", required=True, help="Supabase project URL")
    parser.add_argument("--key", required=True, help="Supabase publishable key")
    parser.add_argument("--save-token", metavar="FILE",
                        help="write the access token here, to test the server's check")
    args = parser.parse_args()

    attempt = LoginAttempt(SupabaseAuth(args.url, args.key), args.provider)
    attempt.start()
    print("Finish logging in in your browser...")
    while not attempt.done:
        time.sleep(0.2)
    if attempt.error:
        raise SystemExit(f"Login failed: {attempt.error}")

    session = _keep_needed(attempt.session)
    minutes = int((session["expires_at"] - time.time()) / 60)
    print(f"Logged in as {session['user']['email']}. Access token expires in {minutes} min.")
    if args.save_token:
        Path(args.save_token).write_text(session["access_token"], encoding="utf-8")
        print(f"Access token saved to {args.save_token}")


if __name__ == "__main__":
    _main()
