"""Jarvis's tool for the phone app: the link (and a pairing code) to open Jarvis on a phone."""
from __future__ import annotations

from jarvis_books.errors import NotFound

PHONE_DECLARATIONS = [
    {"name": "phone_link", "description": "The link that opens Jarvis as an app on the author's phone, with a "
     "one-time pairing code built in. Give the whole link; it can be added to the phone's home screen.",
     "parameters": {"type": "OBJECT", "properties": {}}},
]
PHONE_TOOLS = tuple(d["name"] for d in PHONE_DECLARATIONS)
PHONE_WORDS = ("phone", "app", "mobile", "home screen", "iphone", "android", "link")


class PhoneTools:
    """Mixed into ToolBox; `self.phone_info` (a callable returning the link details) is set by the Studio."""

    phone_info = None

    def _t_phone_link(self) -> dict:
        if self.phone_info is None:
            raise NotFound("the phone app isn't available here")
        info = self.phone_info()
        if not info.get("link"):
            raise NotFound(info.get("problem") or "the phone app's secure link isn't up yet; try again in a minute")
        return {"link": info["link"], "pairing_code": info["code"],
                "note": "Open the link on the phone, then Add to Home Screen. The code inside it works once."}
