#!/usr/bin/env python3
"""Print a self-contained Lovelace data: module URL for hermes-live-monitor-card.js."""
from __future__ import annotations

import base64
from pathlib import Path

js_path = Path(__file__).resolve().parents[1] / 'homeassistant' / 'hermes-live-monitor-card.js'
encoded = base64.b64encode(js_path.read_bytes()).decode('ascii')
print('data:text/javascript;base64,' + encoded)
