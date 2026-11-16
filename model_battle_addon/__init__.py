"""Model Battle: race your friends to model a prompt, then vote on the best one.

How the pieces fit:
  * net.Poller fetches the room state from the server on a background thread.
  * _tick() runs on Blender's main thread 4x a second (bpy.app.timers). It reads
    the latest state, exports + uploads your model when time's up, and redraws.
  * _draw_overlay() draws the prompt and timer in every 3D viewport.
  * The panel in the sidebar (N key > Model Battle tab) is the lobby UI.
"""

import math
import os
import tempfile
import threading
import webbrowser

import blf
import bpy
from bpy.props import BoolProperty, FloatProperty, PointerProperty, StringProperty

from . import net

FONT = 0  # Blender's default UI font


# ---------------------------------------------------------------------------
# Session: the room you're in. Kept outside bpy data because it owns a thread.
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, client, code, player_id):
        self.client = client
        self.code = code
        self.player_id = player_id
        self.poller = net.Poller(client, code, player_id)
        self.poller.start()
        self.submitted_round = 0          # last round we exported a model for
        self.last_round = None            # round number from the previous tick
        self.uploading = False
        self.opened_vote_page_round = 0
        self.message = ""

    def room_url(self):
        return self.client.room_url(self.code, self.player_id)


_session = None
_draw_handle = None


def _prefs():
    return bpy.context.preferences.addons[__package__].preferences


def _format_time(seconds):
    seconds = int(math.ceil(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _redraw():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _start_session(client, code, player_id):
    global _session, _draw_handle
    _stop_session()
    _session = Session(client, code, player_id)
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_overlay, (), 'WINDOW', 'POST_PIXEL')
    bpy.app.timers.register(_tick, first_interval=0.25, persistent=True)


def _stop_session():
    global _session, _draw_handle
    if _session is not None:
        _session.poller.stop()
        _session = None
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    _redraw()


# ---------------------------------------------------------------------------
# Export + upload
# ---------------------------------------------------------------------------

def _export_glb(code, round_number):
    # A mesh in Edit Mode keeps its newest changes in a separate copy until you
    # leave Edit Mode. Copy them back so the export includes them.
    for obj in bpy.data.objects:
        if obj.mode == 'EDIT':
            obj.update_from_editmode()
    path = os.path.join(tempfile.gettempdir(), f"model_battle_{code}_round{round_number}.glb")
    # Timers run without a window in the context, so give the exporter one.
    window = bpy.context.window_manager.windows[0]
    with bpy.context.temp_override(window=window):
        bpy.ops.export_scene.gltf(
            filepath=path,
            export_format='GLB',
            use_visible=True,     # hidden objects (reference images etc.) stay out
            export_apply=True,    # apply modifiers, so Subdivision etc. show up
        )
    return path


def _export_and_upload(round_number):
    s = _session
    s.submitted_round = round_number  # set first so _tick doesn't try again meanwhile
    try:
        path = _export_glb(s.code, round_number)
        with open(path, "rb") as f:
            data = f.read()
    except Exception as err:
        s.message = f"Export failed: {err}. Click Submit to retry."
        return
    s.uploading = True
    s.message = "Uploading your model..."

    def upload():  # background thread: network only, no bpy
        try:
            s.client.submit(s.code, s.player_id, data)
            s.message = "Model submitted!"
        except net.ServerError as err:
            s.message = f"Upload failed: {err}. Click Submit to retry."
        finally:
            s.uploading = False

    threading.Thread(target=upload, daemon=True).start()


# ---------------------------------------------------------------------------
# Main-thread timer
# ---------------------------------------------------------------------------

def _tick():
    s = _session
    if s is None:
        return None  # unregisters the timer
    state, _error = s.poller.snapshot()
    if state:
        phase, round_number = state["phase"], state["round"]
        you = state.get("you") or {}
        if s.last_round is None and phase == "collecting":
            s.submitted_round = round_number  # joined after time ran out: sit this round out
        elif round_number != s.last_round:
            s.message = ""                    # new round: clear last round's status text
        s.last_round = round_number
        time_up = phase == "collecting" or (phase == "modeling" and s.poller.seconds_left() == 0)
        if (time_up and s.submitted_round != round_number
                and not you.get("submitted") and not s.uploading):
            _export_and_upload(round_number)
        if phase == "voting" and s.opened_vote_page_round != round_number:
            s.opened_vote_page_round = round_number
            if _prefs().auto_open_browser:
                webbrowser.open(s.room_url())
    _redraw()
    return 0.25  # run again in 0.25 s


# ---------------------------------------------------------------------------
# Viewport overlay
# ---------------------------------------------------------------------------

WHITE = (1.0, 1.0, 1.0, 1.0)
GREY = (0.8, 0.8, 0.8, 0.9)
RED = (1.0, 0.35, 0.35, 1.0)


def _overlay_lines(s):
    state, error = s.poller.snapshot()
    if state is None:
        return [(error or "Connecting...", 16, GREY)]
    phase = state["phase"]
    left = s.poller.seconds_left()
    if phase == "lobby":
        return [(f"Room {s.code}", 22, WHITE), ("Waiting for the host to start", 15, GREY)]
    if phase == "modeling":
        return [(state["prompt"], 24, WHITE),
                (_format_time(left or 0), 36, RED if left is not None and left <= 10 else WHITE)]
    if phase == "collecting":
        return [("Time's up!", 26, RED), (s.message or "Collecting models...", 15, GREY)]
    if phase == "voting":
        return [("Vote in your browser!", 22, WHITE), (_format_time(left or 0), 18, GREY)]
    return [("Round over! Results are in your browser", 18, WHITE)]


def _draw_overlay():
    s = _session
    region = bpy.context.region
    if s is None or region is None:
        return
    scale = bpy.context.preferences.system.ui_scale
    blf.enable(FONT, blf.SHADOW)
    blf.shadow(FONT, 5, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(FONT, 1, -1)
    y = region.height - 80 * scale
    for text, size, color in _overlay_lines(s):
        blf.size(FONT, size * scale)
        width, height = blf.dimensions(FONT, text)
        blf.position(FONT, (region.width - width) / 2, y, 0)
        blf.color(FONT, *color)
        blf.draw(FONT, text)
        y -= height + 14 * scale
    blf.disable(FONT, blf.SHADOW)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class MB_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    server_url: StringProperty(name="Server", default="http://localhost:8765")
    player_name: StringProperty(name="Your Name", default="Player", maxlen=24)
    auto_open_browser: BoolProperty(name="Open voting page automatically", default=True)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "player_name")
        col.prop(self, "server_url")
        col.prop(self, "auto_open_browser")


class MB_Props(bpy.types.PropertyGroup):
    room_code: StringProperty(name="Room Code", maxlen=4)
    round_minutes: FloatProperty(name="Round Length (min)", default=5.0, min=0.5, max=60.0,
                                 step=50, precision=1)


# ---------------------------------------------------------------------------
# Operators (the buttons)
# ---------------------------------------------------------------------------

def _check_online(operator):
    if not bpy.app.online_access:
        operator.report({'ERROR'}, "Online access is off. Turn on Preferences > System > "
                                   "Network > Allow Online Access")
        return False
    return True


def _has_session(cls, context):
    return _session is not None


class MB_OT_create_room(bpy.types.Operator):
    bl_idname = "model_battle.create_room"
    bl_label = "Create Room"
    bl_description = "Host a new room. Share the code with your friends"

    def execute(self, context):
        if not _check_online(self):
            return {'CANCELLED'}
        prefs = _prefs()
        client = net.Client(prefs.server_url)
        try:
            result = client.create_room(prefs.player_name)
        except net.ServerError as err:
            self.report({'ERROR'}, str(err))
            return {'CANCELLED'}
        context.window_manager.model_battle.room_code = result["code"]
        _start_session(client, result["code"], result["player_id"])
        self.report({'INFO'}, f"Room {result['code']} created. Share the code!")
        return {'FINISHED'}


class MB_OT_join_room(bpy.types.Operator):
    bl_idname = "model_battle.join_room"
    bl_label = "Join Room"
    bl_description = "Join a friend's room with its 4-letter code"

    def execute(self, context):
        if not _check_online(self):
            return {'CANCELLED'}
        props = context.window_manager.model_battle
        code = props.room_code.strip().upper()
        if len(code) != 4 or not code.isalpha():
            self.report({'ERROR'}, "Room codes are 4 letters")
            return {'CANCELLED'}
        prefs = _prefs()
        client = net.Client(prefs.server_url)
        try:
            result = client.join_room(code, prefs.player_name)
        except net.ServerError as err:
            self.report({'ERROR'}, str(err))
            return {'CANCELLED'}
        props.room_code = result["code"]
        _start_session(client, result["code"], result["player_id"])
        return {'FINISHED'}


class MB_OT_start_round(bpy.types.Operator):
    bl_idname = "model_battle.start_round"
    bl_label = "Start Round"
    bl_description = "Pick a prompt and start the timer for everyone"
    poll = classmethod(_has_session)

    def execute(self, context):
        s = _session
        seconds = int(context.window_manager.model_battle.round_minutes * 60)
        try:
            s.client.start_round(s.code, s.player_id, seconds)
        except net.ServerError as err:
            self.report({'ERROR'}, str(err))
            return {'CANCELLED'}
        s.message = ""
        return {'FINISHED'}


class MB_OT_submit(bpy.types.Operator):
    bl_idname = "model_battle.submit"
    bl_label = "Submit Model"
    bl_description = "Upload everything visible in your scene now. You're done for this round"

    @classmethod
    def poll(cls, context):
        s = _session
        if s is None or s.uploading:
            return False
        state, _ = s.poller.snapshot()
        return (bool(state) and state["phase"] in ("modeling", "collecting")
                and not (state.get("you") or {}).get("submitted"))

    def execute(self, context):
        state, _ = _session.poller.snapshot()
        _export_and_upload(state["round"])
        return {'FINISHED'}


class MB_OT_open_page(bpy.types.Operator):
    bl_idname = "model_battle.open_page"
    bl_label = "Open Game Page"
    bl_description = "Open the room in your browser to watch, vote and see results"
    poll = classmethod(_has_session)

    def execute(self, context):
        webbrowser.open(_session.room_url())
        return {'FINISHED'}


class MB_OT_leave(bpy.types.Operator):
    bl_idname = "model_battle.leave"
    bl_label = "Leave"
    bl_description = "Leave the room"
    poll = classmethod(_has_session)

    def execute(self, context):
        _stop_session()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Sidebar panel
# ---------------------------------------------------------------------------

PHASE_NAMES = {
    "lobby": "Lobby",
    "modeling": "Modeling",
    "collecting": "Collecting models",
    "voting": "Voting",
    "results": "Results",
}


class VIEW3D_PT_model_battle(bpy.types.Panel):
    bl_label = "Model Battle"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Model Battle"

    def draw(self, context):
        if _session is None:
            self.draw_connect(context)
        else:
            self.draw_room(context, _session)

    def draw_connect(self, context):
        layout = self.layout
        prefs = _prefs()
        props = context.window_manager.model_battle
        col = layout.column()
        col.prop(prefs, "player_name")
        col.prop(prefs, "server_url")
        col.separator()
        col.operator("model_battle.create_room", icon='ADD')
        row = col.row(align=True)
        row.prop(props, "room_code", text="")
        row.operator("model_battle.join_room", text="Join")

    def draw_room(self, context, s):
        layout = self.layout
        props = context.window_manager.model_battle
        state, error = s.poller.snapshot()

        box = layout.box()
        box.label(text=f"Room {s.code}", icon='COMMUNITY')
        if error:
            layout.label(text=error, icon='ERROR')
        if state is None:
            layout.label(text="Connecting...")
            layout.operator("model_battle.leave", icon='CANCEL')
            return

        phase = state["phase"]
        you = state.get("you") or {}
        title = PHASE_NAMES.get(phase, phase)
        if state["round"]:
            title += f"  |  Round {state['round']}"
        box.label(text=title)
        if phase != "lobby":
            box.label(text=state["prompt"], icon='INFO')
        left = s.poller.seconds_left()
        if left is not None:
            box.label(text=f"Time left: {_format_time(left)}", icon='TIME')

        col = layout.column(align=True)
        col.label(text="Players  (score)")
        for player in state["players"]:
            row = col.row()
            row.enabled = player["online"]
            name = player["name"] + ("  (host)" if player["is_host"] else "")
            row.label(text=name, icon='USER')
            row.label(text=str(player["score"]))

        layout.separator()
        if phase in ("lobby", "results"):
            if you.get("is_host"):
                layout.prop(props, "round_minutes")
                layout.operator("model_battle.start_round",
                                text="Start Round" if phase == "lobby" else "Next Round", icon='PLAY')
            else:
                layout.label(text="Waiting for the host to start...")
        elif phase in ("modeling", "collecting"):
            if you.get("submitted"):
                layout.label(text="Submitted! Waiting for the others...", icon='CHECKMARK')
            else:
                text = "I'm Done: Submit Now" if phase == "modeling" else "Submit"
                layout.operator("model_battle.submit", text=text, icon='EXPORT')
        elif phase == "voting":
            layout.label(text="Vote in your browser!")
        if s.message:
            layout.label(text=s.message)

        layout.separator()
        row = layout.row(align=True)
        row.operator("model_battle.open_page", icon='URL')
        row.operator("model_battle.leave", icon='CANCEL')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    MB_Preferences,
    MB_Props,
    MB_OT_create_room,
    MB_OT_join_room,
    MB_OT_start_round,
    MB_OT_submit,
    MB_OT_open_page,
    MB_OT_leave,
    VIEW3D_PT_model_battle,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.model_battle = PointerProperty(type=MB_Props)


def unregister():
    _stop_session()
    del bpy.types.WindowManager.model_battle
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
