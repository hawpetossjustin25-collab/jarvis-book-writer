"""Jarvis's desktop tools (a ToolBox mixin): programs, websites, system facts, clipboard, screen, keyboard, mouse."""
from __future__ import annotations

from jarvis_books.errors import NotFound


def _decl(name: str, description: str, props: dict | None = None, required: list | None = None) -> dict:
    params: dict = {"type": "OBJECT", "properties": props or {}}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


S = {"type": "STRING"}
DESKTOP_DECLARATIONS = [
    _decl("open_item", "Open something on the author's computer: a program by name (spotify, chrome, notepad, "
          "word, discord, steam, calculator, settings...), a website (youtube.com or a full link), a file or a "
          "folder (opens in its usual program), or a link like mailto:someone@example.com.",
          {"target": S}, ["target"]),
    _decl("close_program", "Close an open program by name (asks it to close, so it can offer to save; force "
          "'yes' ends it straight away and unsaved work is lost).",
          {"name": S, "force": {"type": "STRING", "enum": ["yes", "no"]}}, ["name"]),
    _decl("list_programs", "The programs open on the author's computer right now (name, window title, memory)."),
    _decl("system_info", "Facts about the author's computer: battery and charging, free space on each drive, "
          "memory, processor and load, Windows version, how long it has been on."),
    _decl("read_clipboard", "What the author has copied (the clipboard text)."),
    _decl("copy_to_clipboard", "Put text on the clipboard so the author can paste it anywhere.", {"text": S}, ["text"]),
    _decl("look_at_screen", "Take a screenshot of the author's screen and look at it: describe what is on it, "
          "read text from it, or find where something is (answers give x,y pixel positions for click_screen).",
          {"question": {"type": "STRING", "description": "what to look for or answer about the screen"}}),
    _decl("type_text", "Type text into whatever window has focus, as if on the keyboard (open_item or "
          "click_screen first to pick the window).", {"text": S}, ["text"]),
    _decl("press_keys", "Press a key or a shortcut in the window that has focus: e.g. enter, ctrl+s, alt+tab, "
          "alt+f4, win+d, ctrl+shift+esc, f5.", {"keys": S}, ["keys"]),
    _decl("click_screen", "Click the mouse at a spot on the screen (x, y in pixels from the top-left, as "
          "look_at_screen reports them).",
          {"x": {"type": "INTEGER"}, "y": {"type": "INTEGER"},
           "button": {"type": "STRING", "enum": ["left", "right"]},
           "clicks": {"type": "INTEGER", "description": "1, or 2 for a double click"}}, ["x", "y"]),
    _decl("media_control", "Volume and music on the computer: volume_up, volume_down, mute, play_pause, "
          "next_track, previous_track. times repeats it (each volume step is 2%).",
          {"action": {"type": "STRING", "enum": ["volume_up", "volume_down", "mute", "play_pause", "next_track",
                                                 "previous_track"]},
           "times": {"type": "INTEGER"}}, ["action"]),
]
DESKTOP_TOOLS = tuple(d["name"] for d in DESKTOP_DECLARATIONS)
DESKTOP_WORDS = ("open", "launch", "start", "close", "quit", "exit", "program", "app", "window", "screen",
                 "click", "type", "press", "key", "shortcut", "clipboard", "copy", "paste", "battery", "volume",
                 "louder", "quieter", "mute", "music", "song", "pause", "skip", "spotify", "chrome", "browser",
                 "youtube", "notepad", "word", "excel", "discord", "steam", "settings", "memory", "ram", "disk",
                 "drive", "space", "cpu", "computer", "pc", "laptop", "running", "screenshot", "see", "look")
VISION_PROMPT = ("You are looking at a screenshot of the author's Windows screen ({w}x{h} pixels). Answer the "
                 "question about it precisely. When asked where something is, give its centre as x,y pixel "
                 "coordinates from the top-left of the image.")


class DesktopTools:
    desktop = None  # set by the Studio
    vision = None  # set by the Studio: fn(system, parts) -> text, using Gemini (LFM3 can't see)

    def _desktop(self):
        if self.desktop is None:
            raise NotFound("desktop control is not available here")
        return self.desktop

    def _t_open_item(self, target: str) -> dict:
        return self._desktop().open(target)

    def _t_close_program(self, name: str, force: str = "no") -> dict:
        return self._desktop().close(name, force == "yes")

    def _t_list_programs(self) -> dict:
        return self._desktop().programs()

    def _t_system_info(self) -> dict:
        return self._desktop().info()

    def _t_read_clipboard(self) -> dict:
        return self._desktop().get_clipboard()

    def _t_copy_to_clipboard(self, text: str) -> dict:
        return self._desktop().set_clipboard(text)

    def _t_look_at_screen(self, question: str = "") -> dict:
        shot = self._desktop().screenshot()
        if self.vision is None:
            return {"screenshot": shot["path"], "error": "looking at images needs Gemini, which isn't set up"}
        ask = (question or "").strip() or "Describe what is on the screen: the open windows and anything important."
        answer = self.vision(VISION_PROMPT.format(w=shot["width"], h=shot["height"]),
                             [self.desktop.image_part(shot["path"]), {"text": ask}])
        return {"screen": f"{shot['width']}x{shot['height']}", "answer": answer, "screenshot": shot["path"]}

    def _t_type_text(self, text: str) -> dict:
        return self._desktop().type_text(text)

    def _t_press_keys(self, keys: str) -> dict:
        return self._desktop().press(keys)

    def _t_click_screen(self, x: int, y: int, button: str = "left", clicks: int = 1) -> dict:
        return self._desktop().click(x, y, button or "left", clicks or 1)

    def _t_media_control(self, action: str, times: int = 1) -> dict:
        return self._desktop().media(action, times or 1)
