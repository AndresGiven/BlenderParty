"""Check that a player's access token really came from our Supabase project.

The game server calls TokenVerifier.verify() once when a player or viewer
connects. If it returns, the token is genuine, unexpired and for our project,
and you get back who the player is.

Needs PyJWT with crypto support:  pip install "pyjwt[crypto]"
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

import jwt

# Supabase signs tokens with one of these when the project uses JWT signing keys.
ASYMMETRIC_ALGORITHMS = ["ES256", "RS256"]


class AuthError(Exception):
    """The token is missing, fake, expired or from another project."""


@dataclass(frozen=True)
class Player:
    id: str        # Supabase user ID. Stable across logins: use it for the one-connection-per-account rule.
    name: str      # display name from Google or Twitch
    provider: str  # "google" or "twitch"


class TokenVerifier:
    def __init__(self, project_url: str, publishable_key: str):
        base = project_url.rstrip("/")
        self.issuer = f"{base}/auth/v1"
        self.user_url = f"{base}/auth/v1/user"
        self.key = publishable_key
        # Downloads the project's public keys and caches them for 10 minutes.
        # If a token names a key it hasn't seen, it fetches the list again.
        self.jwks = jwt.PyJWKClient(f"{base}/auth/v1/.well-known/jwks.json", lifespan=600)

    def verify(self, token: str) -> Player:
        """Return the Player for a valid token, or raise AuthError.

        This can make a network call, so in async code run it in a thread:
            player = await asyncio.to_thread(verifier.verify, token)
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as err:
            raise AuthError("That isn't a login token.") from err

        if header.get("alg") in ASYMMETRIC_ALGORITHMS:
            claims = self._verify_locally(token)
        else:
            claims = self._ask_supabase(token)
        return _player_from(claims)

    def _verify_locally(self, token: str) -> dict:
        """Check the signature with Supabase's public key. No call to Supabase per player."""
        try:
            signing_key = self.jwks.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=ASYMMETRIC_ALGORITHMS,
                audience="authenticated",   # tokens for logged-in users
                issuer=self.issuer,         # must come from our project
                options={"require": ["exp", "sub"]},
            )
        except jwt.PyJWKClientError as err:
            raise AuthError(f"Couldn't load Supabase's signing keys: {err}") from err
        except jwt.InvalidTokenError as err:  # bad signature, expired, wrong project...
            raise AuthError(f"Token rejected: {err}") from err

    def _ask_supabase(self, token: str) -> dict:
        """Fallback for projects still on the legacy shared secret (HS256).

        Supabase recommends asking its Auth server instead of checking these
        tokens yourself. Slower, so switch the project to JWT signing keys.
        """
        request = urllib.request.Request(
            self.user_url,
            headers={"apikey": self.key, "Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                user = json.load(response)
        except urllib.error.HTTPError as err:
            raise AuthError("Token rejected by Supabase.") from err
        except urllib.error.URLError as err:
            raise AuthError(f"Couldn't reach Supabase: {err.reason}") from err
        # The /user answer is a user record, so reshape it like token claims.
        return {
            "sub": user["id"],
            "email": user.get("email"),
            "user_metadata": user.get("user_metadata") or {},
            "app_metadata": user.get("app_metadata") or {},
        }


def _player_from(claims: dict) -> Player:
    meta = claims.get("user_metadata") or {}
    app = claims.get("app_metadata") or {}
    name = (meta.get("full_name") or meta.get("name")
            or meta.get("preferred_username") or claims.get("email") or "Player")
    return Player(id=claims["sub"], name=name[:32], provider=app.get("provider", "unknown"))


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Check a saved access token, as the game server would.")
    parser.add_argument("token_file", help="file written by supabase_login.py --save-token")
    parser.add_argument("--url", required=True, help="Supabase project URL")
    parser.add_argument("--key", required=True, help="Supabase publishable key")
    args = parser.parse_args()

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    try:
        print(TokenVerifier(args.url, args.key).verify(token))
    except AuthError as err:
        raise SystemExit(f"Rejected: {err}")
