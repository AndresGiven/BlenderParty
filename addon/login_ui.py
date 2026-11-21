"""Blender side of the login: the buttons, a timer that watches the browser login,
and a helper that draws the account box in your panel.

Hook it into your add-on's __init__.py:

    from . import login_ui

    def register():
        login_ui.register()
        ...  # your other classes

    def unregister():
        ...  # your other classes
        login_ui.unregister()

In your panel's draw():

    login_ui.draw_account(self.layout)

When you connect to the game server, get a fresh token with:

    token = login_ui.get_account().access_token()
"""

from __future__ import annotations

from pathlib import Path

import bpy

from . import config
from .supabase_login import Account, LoginAttempt, LoginError, SessionStore, SupabaseAuth

_account: Account | None = None        # the player's login, created in register()
_attempt: LoginAttempt | None = None   # the browser login in progress, if any
_status = ""                           # short message shown in the account box


def get_account() -> Account:
    return _account


def _auth() -> SupabaseAuth:
    return SupabaseAuth(config.SUPABASE_URL, config.SUPABASE_PUBLISHABLE_KEY)


def _session_path() -> Path:
    # Blender's per-user config folder, so the login survives restarts and new files.
    folder = bpy.utils.user_resource('CONFIG', path="blender_party", create=True)
    return Path(folder) / "session.json"


def _redraw_ui() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def _check_login():
    """Timer callback on Blender's main thread. Runs every half second until login ends."""
    global _attempt, _status
    if _attempt is None:
        return None  # returning None stops the timer
    if not _attempt.done:
        return 0.5   # check again in half a second
    if _attempt.error:
        _status = _attempt.error
    else:
        _account.set_session(_attempt.session)
        _status = ""
    _attempt = None
    _redraw_ui()
    return None


class BPARTY_OT_log_in(bpy.types.Operator):
    """Log in to Blender Party in your web browser"""
    bl_idname = "bparty.log_in"
    bl_label = "Log In"

    provider: bpy.props.EnumProperty(
        name="Provider",
        items=[("google", "Google", ""), ("twitch", "Twitch", "")],
    )

    def execute(self, context):
        global _attempt, _status
        # Blender 4.2+ lets players block add-ons from going online. Respect it.
        # if not getattr(bpy.app, "online_access", True):
        #     self.report({'ERROR'}, "Turn on Allow Online Access in Preferences to log in")
        #     return {'CANCELLED'}
        if _attempt is not None:
            self.report({'INFO'}, "Already waiting for the browser")
            return {'CANCELLED'}

        attempt = LoginAttempt(_auth(), self.provider)
        try:
            attempt.start()
        except LoginError as err:
            self.report({'ERROR'}, str(err))
            return {'CANCELLED'}

        _attempt = attempt
        _status = "Finish logging in in your browser..."
        # persistent=True keeps the timer alive if the player opens another file meanwhile.
        bpy.app.timers.register(_check_login, first_interval=0.5, persistent=True)
        return {'FINISHED'}


class BPARTY_OT_cancel_log_in(bpy.types.Operator):
    """Stop waiting for the browser login"""
    bl_idname = "bparty.cancel_log_in"
    bl_label = "Cancel"

    def execute(self, context):
        if _attempt is not None:
            _attempt.cancel()  # the timer picks up the "cancelled" result
        return {'FINISHED'}


class BPARTY_OT_log_out(bpy.types.Operator):
    """Log out of Blender Party on this computer"""
    bl_idname = "bparty.log_out"
    bl_label = "Log Out"

    def execute(self, context):
        global _status
        _account.log_out()
        _status = ""
        return {'FINISHED'}


def draw_account(layout) -> None:
    """Draw the login box. Call this from your panel's draw()."""
    box = layout.box()
    if _account is not None and _account.logged_in:
        box.label(text=f"Logged in as {_account.display_name}", icon='USER')
        box.operator(BPARTY_OT_log_out.bl_idname)
    elif _attempt is not None:
        box.label(text=_status, icon='TIME')
        box.operator(BPARTY_OT_cancel_log_in.bl_idname)
    else:
        row = box.row(align=True)
        row.operator(BPARTY_OT_log_in.bl_idname, text="Log in with Google").provider = 'google'
        row.operator(BPARTY_OT_log_in.bl_idname, text="Log in with Twitch").provider = 'twitch'
        if _status:
            box.label(text=_status, icon='ERROR')


_classes = (BPARTY_OT_log_in, BPARTY_OT_cancel_log_in, BPARTY_OT_log_out)


def register() -> None:
    global _account
    for cls in _classes:
        bpy.utils.register_class(cls)
    _account = Account(_auth(), SessionStore(_session_path()))


def unregister() -> None:
    global _attempt
    if _attempt is not None:
        _attempt.cancel()
        _attempt = None
    if bpy.app.timers.is_registered(_check_login):
        bpy.app.timers.unregister(_check_login)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
