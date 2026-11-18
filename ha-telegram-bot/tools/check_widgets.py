#!/usr/bin/env python3
"""Release gate for the fixed Profile 1 artifact, without contacting Telegram."""
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from widget_transport import validate_html

source = (Path(__file__).resolve().parents[1] / "app/widgets/home.html").read_text()
raw, compressed = validate_html(source)
# This is a conservative lint of our artifact, not a complete Profile 1 parser.
for pattern in (r'<(?:input|textarea|iframe|link|meta|form|select)\b',
                r'<script[^>]+src=', r'\b(?:fetch|XMLHttpRequest|WebSocket|eval)\s*\(',
                r'@(?:import|font-face)', r'\b\d+vh\b', r'overflow\s*:\s*(?:auto|scroll)'):
    if re.search(pattern, source, re.I):
        raise SystemExit(f"Unsupported widget feature: {pattern}")
print(f"Profile 1 artifact: HTML {raw}/32768; zlib-9 {compressed}/4096 bytes")
