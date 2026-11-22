"""Permission levels for tools that self-upgrades add.

permissions.py is protected from upgrades, so an upgrade that adds a tool names its level here
("read", "safe", "change", "outside" or "system"). These entries never override a built-in tool's level,
and an unknown level counts as "system" (always asks).
"""
ADDED_TOOL_LEVELS: dict[str, str] = {}
