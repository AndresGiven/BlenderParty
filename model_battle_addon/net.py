"""Talks to the Model Battle server.

Nothing in this file touches bpy, so it's safe to run on a background thread
(Blender's Python API is not thread-safe) and easy to test outside Blender.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote


class ServerError(Exception):
    """The server said no, or couldn't be reached."""


class Client:
    def __init__(self, base_url):
        self.base_url = base_url.strip().rstrip("/")

    def _request(self, method, path, json_body=None, raw_body=None, headers=None, timeout=10):
        headers = dict(headers or {})
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            data = raw_body
            headers["Content-Type"] = "application/octet-stream"
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as err:  # the server answered with an error
            try:
                message = json.loads(err.read()).get("error")
            except Exception:
                message = None
            raise ServerError(message or f"Server error {err.code}") from None
        except (urllib.error.URLError, OSError) as err:  # couldn't connect at all
            reason = getattr(err, "reason", err)
            raise ServerError(f"Can't reach {self.base_url} ({reason})") from None

    def create_room(self, name):
        return self._request("POST", "/api/rooms", {"name": name})

    def join_room(self, code, name):
        return self._request("POST", f"/api/rooms/{code}/join", {"name": name})

    def get_state(self, code, player_id):
        return self._request("GET", f"/api/rooms/{code}/state?p={quote(player_id)}", timeout=5)

    def start_round(self, code, player_id, seconds):
        return self._request("POST", f"/api/rooms/{code}/start",
                             {"player_id": player_id, "seconds": seconds})

    def submit(self, code, player_id, glb_bytes):
        return self._request("POST", f"/api/rooms/{code}/submit", raw_body=glb_bytes,
                             headers={"X-Player-Id": player_id}, timeout=60)

    def vote(self, code, player_id, entry_id):
        return self._request("POST", f"/api/rooms/{code}/vote",
                             {"player_id": player_id, "entry_id": entry_id})

    def room_url(self, code, player_id):
        return f"{self.base_url}/room/{code}?p={quote(player_id)}"


class Poller(threading.Thread):
    """Fetches the room state twice a second on a background thread.
    The main thread reads the latest copy with snapshot()."""

    def __init__(self, client, code, player_id, interval=0.5):
        super().__init__(daemon=True)
        self.client = client
        self.code = code
        self.player_id = player_id
        self.interval = interval
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._state = None
        self._error = None
        self._clock_offset = 0.0  # server clock minus our clock

    def run(self):
        while not self._stop_event.is_set():
            try:
                state = self.client.get_state(self.code, self.player_id)
                with self._lock:
                    self._state = state
                    self._error = None
                    self._clock_offset = state["server_time"] - time.time()
            except Exception as err:
                with self._lock:
                    self._error = str(err)
            self._stop_event.wait(self.interval)

    def snapshot(self):
        with self._lock:
            return self._state, self._error

    def seconds_left(self):
        """Time left in the current phase by the server's clock, or None if it has no timer."""
        with self._lock:
            state, offset = self._state, self._clock_offset
        if not state or not state.get("phase_ends_at"):
            return None
        return max(0.0, state["phase_ends_at"] - (time.time() + offset))

    def stop(self):
        self._stop_event.set()
