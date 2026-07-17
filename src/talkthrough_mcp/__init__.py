"""talkthrough-mcp: local-first MCP server for narrated screen recordings.

Turns a screen recording (or any video/audio file) into agent-ready
structured data: timestamped transcript segments, scene-change keyframes,
OCR text, and wall-clock anchoring — with lazy retrieval tools so long
videos never flood the model context.
"""

try:
    from importlib.metadata import version as _version

    __version__ = _version("talkthrough-mcp")
except Exception:  # pragma: no cover - source checkout without install metadata
    __version__ = "0.0.0.dev0"
