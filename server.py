#!/usr/bin/env python3
"""
Model Battle game server.

Players race to model a prompt in Blender before the timer runs out, then
everyone votes on the best model in their browser.

Standard library only, so there's nothing to install:

    python server.py                  # listens on port 8765
    python server.py --port 9000

Open http://localhost:8765 in a browser to watch a room.
"""

import argparse
import json
import random
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROMPTS = [
    "A haunted toaster",
    "A treehouse for a dragon",
    "A coffee mug with feelings",
    "A spaceship made from kitchen tools",
    "A snail with a jetpack",
    "The world's saddest robot",
    "A castle on the back of a turtle",
    "A lighthouse in the desert",
    "A mushroom village",
    "A sword that's also a guitar",
    "A vending machine for monsters",
    "A cozy cabin inside a snow globe",
    "A pirate ship in a bathtub",
    "A cactus wearing armor",
    "A floating island with a waterfall",
    "A cat-shaped submarine",
    "A wizard's messy desk",
    "A tiny planet with one house",
    "A burger that's way too tall",
    "A mech made of furniture",
    "A tree that grows lightbulbs",
    "A train that runs on rainbows",
    "A fortress built from books",
    "A penguin's dream car",
    "An alien's souvenir shop",
    "A rubber duck battleship",
    "A clock tower that's melting",
    "A robot butler serving tea",
    "A hot air balloon shaped like a fish",
    "A gaming chair for a giant",
]

ROOM_CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O: they look like 1 and 0
OFFLINE_AFTER = 15            # seconds without a poll before a player counts as offline
ABANDONED_AFTER = 60 * 2     # delete rooms nobody has polled for an hour
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MIN_ROUND_SECONDS = 10
MAX_ROUND_SECONDS = 60 * 60
MAX_NAME_LENGTH = 24

# Phase lengths after the modeling timer; can be changed from the command line.
settings = {"collect_seconds": 20, "vote_seconds": 60}

UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"

rooms = {}                       # code -> Room
rooms_lock = threading.Lock()    # hold this whenever you read or change a Room


def now():
    return time.time()


class GameError(Exception):
    """A request the game rules don't allow. Sent back to the client as {"error": ...}."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Room:
    """One game lobby. Phases go: lobby -> modeling -> collecting -> voting -> results,
    then back to modeling when the host starts the next round."""

    def __init__(self, code):
        self.code = code
        self.players = {}          # player_id -> {"name", "score", "last_seen"}
        self.host_id = None
        self.phase = "lobby"
        self.round = 0
        self.prompt = ""
        self.phase_ends_at = None  # server timestamp, or None if the phase has no timer
        self.entries = {}          # entry_id -> {"player_id", "path", "version", "votes"}
        self.votes = {}            # voter player_id -> entry_id
        self.used_prompts = set()

    # --- players ---------------------------------------------------------

    def online_ids(self):
        t = now()
        return [pid for pid, p in self.players.items() if t - p["last_seen"] < OFFLINE_AFTER]

    def require_player(self, player_id):
        if player_id not in self.players:
            raise GameError("You're not in this room", 403)

    def join(self, name):
        online = set(self.online_ids())
        # Someone whose Blender crashed can rejoin under the same name and keep their score.
        for pid, player in self.players.items():
            if player["name"] == name and pid not in online:
                player["last_seen"] = now()
                return pid
        taken = {p["name"] for p in self.players.values()}
        unique, n = name, 2
        while unique in taken:
            unique, n = f"{name} {n}", n + 1
        pid = secrets.token_urlsafe(12)
        self.players[pid] = {"name": unique, "score": 0, "last_seen": now()}
        if self.host_id is None:
            self.host_id = pid
        return pid

    # --- rounds ----------------------------------------------------------

    def start_round(self, player_id, seconds):
        self.require_player(player_id)
        if player_id != self.host_id:
            raise GameError("Only the host can start a round", 403)
        if self.phase not in ("lobby", "results"):
            raise GameError("A round is already running", 409)
        seconds = max(MIN_ROUND_SECONDS, min(MAX_ROUND_SECONDS, int(seconds)))
        fresh = [p for p in PROMPTS if p not in self.used_prompts] or PROMPTS
        self.prompt = random.choice(fresh)
        self.used_prompts.add(self.prompt)
        self.round += 1
        self.entries = {}
        self.votes = {}
        self.phase = "modeling"
        self.phase_ends_at = now() + seconds

    def submitted_ids(self):
        return {e["player_id"] for e in self.entries.values()}

    def submit(self, player_id, data):
        self.require_player(player_id)
        if self.phase not in ("modeling", "collecting"):
            raise GameError("Submissions are closed", 409)
        entry_id = next((eid for eid, e in self.entries.items() if e["player_id"] == player_id), None)
        if entry_id is None:
            entry_id = secrets.token_hex(4)
            self.entries[entry_id] = {"player_id": player_id, "version": 0, "votes": 0}
        entry = self.entries[entry_id]
        folder = UPLOAD_DIR / self.code
        folder.mkdir(parents=True, exist_ok=True)
        entry["path"] = folder / f"round{self.round}_{entry_id}.glb"
        entry["path"].write_bytes(data)
        entry["version"] += 1

    def vote(self, player_id, entry_id):
        self.require_player(player_id)
        if self.phase != "voting":
            raise GameError("Voting isn't open", 409)
        entry = self.entries.get(entry_id)
        if entry is None:
            raise GameError("That model doesn't exist", 404)
        if entry["player_id"] == player_id:
            raise GameError("You can't vote for your own model")
        self.votes[player_id] = entry_id  # voting again changes your vote

    def eligible_voters(self):
        """Online players with at least one model that isn't theirs to vote for."""
        return [pid for pid in self.online_ids()
                if any(e["player_id"] != pid for e in self.entries.values())]

    def finish_round(self):
        for entry in self.entries.values():
            entry["votes"] = 0
        for entry_id in self.votes.values():
            self.entries[entry_id]["votes"] += 1
        for entry in self.entries.values():
            player = self.players.get(entry["player_id"])
            if player:
                player["score"] += entry["votes"]
        self.phase = "results"
        self.phase_ends_at = None

    def tick(self):
        """Called several times a second. Moves to the next phase when its timer runs
        out, or early when everyone is done."""
        t = now()
        online = set(self.online_ids())
        everyone_submitted = bool(online) and online <= self.submitted_ids()

        if self.phase == "modeling" and (t >= self.phase_ends_at or everyone_submitted):
            self.phase = "collecting"
            self.phase_ends_at = t + settings["collect_seconds"]

        if self.phase == "collecting" and (t >= self.phase_ends_at or everyone_submitted):
            if self.entries:
                self.phase = "voting"
                self.phase_ends_at = t + settings["vote_seconds"]
            else:
                self.finish_round()

        if self.phase == "voting":
            voters = self.eligible_voters()
            if t >= self.phase_ends_at or all(v in self.votes for v in voters):
                self.finish_round()

        # If the host disconnects, hand the room to the next player.
        if self.host_id not in online and online:
            self.host_id = next(pid for pid in self.players if pid in online)

    def is_abandoned(self):
        last = max((p["last_seen"] for p in self.players.values()), default=0)
        return now() - last > ABANDONED_AFTER

    # --- what clients see ------------------------------------------------

    def to_state(self, player_id):
        online = set(self.online_ids())
        submitted = self.submitted_ids()
        state = {
            "server_time": now(),
            "code": self.code,
            "phase": self.phase,
            "round": self.round,
            "prompt": self.prompt,
            "phase_ends_at": self.phase_ends_at,
            "players": [
                {
                    "name": p["name"],
                    "score": p["score"],
                    "online": pid in online,
                    "is_host": pid == self.host_id,
                    "submitted": pid in submitted,
                    "voted": pid in self.votes,
                }
                for pid, p in self.players.items()
            ],
            "you": None,
            "entries": [],
        }
        me = self.players.get(player_id)
        if me:
            state["you"] = {
                "name": me["name"],
                "is_host": player_id == self.host_id,
                "submitted": player_id in submitted,
                "voted_for": self.votes.get(player_id),
            }
        if self.phase in ("voting", "results"):
            # Entry ids are random, so sorting by id shuffles who made what.
            for entry_id in sorted(self.entries):
                entry = self.entries[entry_id]
                item = {
                    "id": entry_id,
                    "url": f"/files/{self.code}/{entry_id}.glb?v={self.round}.{entry['version']}",
                    "mine": entry["player_id"] == player_id,
                }
                if self.phase == "results":  # authors stay hidden until voting is over
                    author = self.players.get(entry["player_id"])
                    item["author"] = author["name"] if author else "?"
                    item["votes"] = entry["votes"]
                state["entries"].append(item)
        return state


def new_room_code():
    while True:
        code = "".join(random.choice(ROOM_CODE_LETTERS) for _ in range(4))
        if code not in rooms:
            return code


def get_room(code):
    room = rooms.get(code.upper())
    if room is None:
        raise GameError("Room not found. Check the code?", 404)
    return room


def clean_name(value):
    name = " ".join(str(value or "").split())[:MAX_NAME_LENGTH]
    if not name:
        raise GameError("Pick a name first")
    return name


# --- HTTP ------------------------------------------------------------------

CODE = r"(?P<code>[A-Za-z]{8})"
ROUTES = [
    ("GET", r"/", "page_home"),
    ("GET", rf"/room/{CODE}", "page_room"),
    ("GET", rf"/api/rooms/{CODE}/state", "api_state"),
    ("POST", r"/api/rooms", "api_create"),
    ("POST", rf"/api/rooms/{CODE}/join", "api_join"),
    ("POST", rf"/api/rooms/{CODE}/start", "api_start"),
    ("POST", rf"/api/rooms/{CODE}/submit", "api_submit"),
    ("POST", rf"/api/rooms/{CODE}/vote", "api_vote"),
    ("GET", rf"/files/{CODE}/(?P<entry_id>[0-9a-f]+)\.glb", "file_entry"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "ModelBattle/0.1"

    # --- plumbing ---

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        url = urlparse(self.path)
        for route_method, pattern, handler_name in ROUTES:
            match = re.fullmatch(pattern, url.path)
            if route_method == method and match:
                try:
                    getattr(self, handler_name)(parse_qs(url.query), **match.groupdict())
                except GameError as err:
                    self.send_json({"error": str(err)}, err.status)
                return
        self.send_json({"error": "Not found"}, 404)

    def log_message(self, fmt, *args):
        if "/state" not in self.path:  # skip the polling spam
            super().log_message(fmt, *args)

    def send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_bytes(json.dumps(data).encode(), "application/json", status)

    def read_body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise GameError("That upload is too big", 413)
        return self.rfile.read(length)

    def read_json(self):
        try:
            return json.loads(self.read_body(64 * 1024) or b"{}")
        except json.JSONDecodeError:
            raise GameError("Bad JSON") from None

    # --- pages ---

    def page_home(self, query):
        self.send_bytes(HOME_HTML.encode(), "text/html; charset=utf-8")

    def page_room(self, query, code):
        self.send_bytes(ROOM_HTML.encode(), "text/html; charset=utf-8")

    def file_entry(self, query, code, entry_id):
        with rooms_lock:
            entry = get_room(code).entries.get(entry_id)
            if entry is None:
                raise GameError("Model not found", 404)
            path = entry["path"]
        self.send_bytes(path.read_bytes(), "model/gltf-binary")

    # --- API ---

    def api_state(self, query, code):
        player_id = query.get("p", [""])[0]
        with rooms_lock:
            room = get_room(code)
            if player_id in room.players:
                room.players[player_id]["last_seen"] = now()
            state = room.to_state(player_id)
        self.send_json(state)

    def api_create(self, query):
        name = clean_name(self.read_json().get("name"))
        with rooms_lock:
            code = new_room_code()
            room = rooms[code] = Room(code)
            player_id = room.join(name)
        print(f"Room {code} created by {name}")
        self.send_json({"code": code, "player_id": player_id})

    def api_join(self, query, code):
        name = clean_name(self.read_json().get("name"))
        with rooms_lock:
            room = get_room(code)
            player_id = room.join(name)
        self.send_json({"code": room.code, "player_id": player_id})

    def api_start(self, query, code):
        body = self.read_json()
        with rooms_lock:
            room = get_room(code)
            room.start_round(body.get("player_id"), body.get("seconds", 300))
            print(f"Room {room.code} round {room.round}: {room.prompt}")
        self.send_json({"ok": True})

    def api_submit(self, query, code):
        player_id = self.headers.get("X-Player-Id", "")
        data = self.read_body(MAX_UPLOAD_BYTES)
        if not data.startswith(b"glTF"):
            raise GameError("That isn't a .glb file")
        with rooms_lock:
            get_room(code).submit(player_id, data)
        self.send_json({"ok": True})

    def api_vote(self, query, code):
        body = self.read_json()
        with rooms_lock:
            get_room(code).vote(body.get("player_id"), body.get("entry_id"))
        self.send_json({"ok": True})


def ticker():
    while True:
        with rooms_lock:
            for code, room in list(rooms.items()):
                room.tick()
                if room.is_abandoned():
                    del rooms[code]
        time.sleep(0.2)


# --- Browser pages -----------------------------------------------------------

HOME_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Model Battle</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:#14161a;
         color:#e8e8ea; font-family:system-ui, sans-serif; }
  form { display:flex; gap:8px; }
  input { font-size:24px; width:7ch; text-transform:uppercase; letter-spacing:.2em; padding:8px 12px;
          border-radius:8px; border:1px solid #2c3038; background:#1e2127; color:inherit; }
  button { font-size:18px; padding:8px 16px; border:0; border-radius:8px; background:#f5a524; font-weight:700; }
</style></head>
<body><main><h1>Model Battle</h1><p>Enter a room code to watch.</p>
<form onsubmit="location.href='/room/' + this.code.value.trim().toUpperCase(); return false;">
  <input name="code" maxlength="4" placeholder="ABCD" autofocus required><button>Watch</button>
</form></main></body></html>
"""

ROOM_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model Battle</title>
<script type="module" src="https://unpkg.com/@google/model-viewer/dist/model-viewer.min.js"></script>
<style>
  :root { color-scheme: dark; --bg:#14161a; --card:#1e2127; --line:#2c3038; --text:#e8e8ea;
          --muted:#9aa0aa; --accent:#f5a524; --good:#3ecf8e; --bad:#ff5d5d; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:system-ui, sans-serif; background:var(--bg); color:var(--text); }
  header { display:flex; gap:16px; align-items:center; padding:12px 20px; border-bottom:1px solid var(--line); }
  .code { font-weight:700; letter-spacing:.15em; font-size:20px; }
  .phase { color:var(--muted); text-transform:uppercase; font-size:13px; letter-spacing:.08em; }
  .timer { margin-left:auto; font-variant-numeric:tabular-nums; font-size:28px; font-weight:700; }
  .timer.urgent { color:var(--bad); }
  main { max-width:1200px; margin:0 auto; padding:20px; }
  h1 { font-size:32px; margin:8px 0 4px; }
  .hint { color:var(--muted); margin:0 0 20px; }
  .hint.error { color:var(--bad); }
  .players { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:24px; }
  .player { background:var(--card); border:1px solid var(--line); border-radius:999px; padding:6px 12px; font-size:14px; }
  .player.offline { opacity:.45; }
  .player .score { color:var(--accent); font-weight:700; margin-left:8px; }
  .player .done { color:var(--good); margin-left:6px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:16px; }
  .card { background:var(--card); border:2px solid var(--line); border-radius:12px; overflow:hidden; }
  .card.picked { border-color:var(--accent); }
  .card.winner { border-color:var(--good); }
  model-viewer { width:100%; height:280px; background:#0d0f12; }
  .card footer { padding:10px 12px; display:flex; align-items:center; gap:8px; min-height:52px; }
  .card .who { font-weight:600; }
  .card .votes { margin-left:auto; color:var(--accent); font-weight:700; }
  button { margin-left:auto; background:var(--accent); color:#111; border:0; border-radius:8px;
           padding:8px 14px; font-weight:700; cursor:pointer; }
  button:disabled { background:var(--line); color:var(--muted); cursor:default; }
</style>
</head>
<body>
<header>
  <span class="code" id="code"></span>
  <span class="phase" id="phase"></span>
  <span class="timer" id="timer"></span>
</header>
<main>
  <h1 id="title"></h1>
  <p class="hint" id="hint"></p>
  <div class="players" id="players"></div>
  <div class="grid" id="entries"></div>
</main>
<script>
const code = location.pathname.split("/").pop().toUpperCase();
const playerId = new URLSearchParams(location.search).get("p") || "";
const $ = (id) => document.getElementById(id);
let state = null;
let clockOffset = 0;   // server clock minus this computer's clock, in seconds
let gridKey = "";      // which models are on screen, so we only rebuild (and reload them) on change

const HINTS = {
  lobby: "The host starts the round from Blender.",
  modeling: "Model it in Blender before the timer runs out!",
  collecting: "Time's up! Collecting everyone's models...",
  voting: "Drag to spin a model. Vote for your favourite (not your own). You can change your vote until time runs out.",
  results: "The host can start the next round from Blender.",
};

$("code").textContent = "Room " + code;

async function poll() {
  try {
    const res = await fetch(`/api/rooms/${code}/state?p=${encodeURIComponent(playerId)}`, { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) return showError(data.error || "Something went wrong");
    clockOffset = data.server_time - Date.now() / 1000;
    state = data;
    render();
  } catch (err) {
    showError("Can't reach the server");
  }
}

function showError(message) {
  $("hint").textContent = message;
  $("hint").className = "hint error";
}

function secondsLeft() {
  if (!state || !state.phase_ends_at) return null;
  return Math.max(0, state.phase_ends_at - (Date.now() / 1000 + clockOffset));
}

function formatTime(seconds) {
  const s = Math.ceil(seconds);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function updateTimer() {
  const left = secondsLeft();
  $("timer").textContent = left === null ? "" : formatTime(left);
  $("timer").classList.toggle("urgent", left !== null && left <= 10);
}

function render() {
  const s = state;
  $("phase").textContent = s.round ? `Round ${s.round} - ${s.phase}` : s.phase;
  $("title").textContent = s.phase === "lobby" ? "Waiting for players" : s.prompt;
  let hint = HINTS[s.phase] || "";
  if (s.phase === "voting" && !s.you) hint = "Players vote from the link in their Blender add-on.";
  $("hint").textContent = hint;
  $("hint").className = "hint";
  renderPlayers();
  renderEntries();
  updateTimer();
}

function renderPlayers() {
  const s = state;
  const list = $("players");
  list.replaceChildren();
  for (const p of s.players) {
    const el = document.createElement("span");
    el.className = "player" + (p.online ? "" : " offline");
    el.textContent = p.name + (p.is_host ? " (host)" : "");
    const score = document.createElement("span");
    score.className = "score";
    score.textContent = p.score;
    el.append(score);
    const done = (s.phase === "modeling" || s.phase === "collecting") ? p.submitted
               : s.phase === "voting" ? p.voted : false;
    if (done) {
      const tick = document.createElement("span");
      tick.className = "done";
      tick.textContent = "\\u2713";
      el.append(tick);
    }
    list.append(el);
  }
}

function renderEntries() {
  const s = state;
  const grid = $("entries");
  let entries = s.entries;
  if (s.phase === "results") entries = [...entries].sort((a, b) => b.votes - a.votes);
  const key = s.phase + "|" + s.round + "|" + entries.map((e) => e.url).join(",");
  if (key !== gridKey) {
    gridKey = key;
    const top = Math.max(0, ...entries.map((e) => e.votes || 0));
    grid.replaceChildren(...entries.map((e) => makeCard(e, top)));
  }
  // Small updates that don't reload the 3D models.
  const votedFor = s.you ? s.you.voted_for : null;
  for (const card of grid.children) {
    card.classList.toggle("picked", s.phase === "voting" && card.dataset.id === votedFor);
    const button = card.querySelector("button");
    if (button) {
      button.textContent = card.dataset.id === votedFor ? "Voted" : "Vote";
      button.disabled = card.dataset.id === votedFor;
    }
  }
}

function makeCard(entry, topVotes) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.id = entry.id;
  const viewer = document.createElement("model-viewer");
  viewer.setAttribute("src", entry.url);
  viewer.setAttribute("camera-controls", "");
  viewer.setAttribute("auto-rotate", "");
  viewer.setAttribute("shadow-intensity", "1");
  const footer = document.createElement("footer");
  const who = document.createElement("span");
  who.className = "who";
  footer.append(who);
  if (state.phase === "results") {
    who.textContent = entry.author + (entry.mine ? " (you)" : "");
    const votes = document.createElement("span");
    votes.className = "votes";
    votes.textContent = entry.votes + (entry.votes === 1 ? " vote" : " votes");
    footer.append(votes);
    if (topVotes > 0 && entry.votes === topVotes) card.classList.add("winner");
  } else {
    who.textContent = entry.mine ? "Your model" : "";
    if (state.you && !entry.mine) {
      const button = document.createElement("button");
      button.textContent = "Vote";
      button.onclick = () => vote(entry.id);
      footer.append(button);
    }
  }
  card.append(viewer, footer);
  return card;
}

async function vote(entryId) {
  const res = await fetch(`/api/rooms/${code}/vote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ player_id: playerId, entry_id: entryId }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    showError(data.error || "Vote failed");
  }
  poll();
}

poll();
setInterval(poll, 1000);
setInterval(updateTimer, 250);
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Model Battle game server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="address to listen on (default: every network interface)")
    parser.add_argument("--port", type=int, default=55378)
    parser.add_argument("--collect-seconds", type=int, default=30,
                        help="how long to wait for uploads after time runs out")
    parser.add_argument("--vote-seconds", type=int, default=60)
    args = parser.parse_args()
    settings["collect_seconds"] = args.collect_seconds
    settings["vote_seconds"] = args.vote_seconds

    threading.Thread(target=ticker, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Model Battle server running on http://localhost:{args.port}")
    print(f"Friends on your network connect to http://<your-local-IP>:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
