"""Tenant-aware storage layout: <root>/<slug>/...

Roots stay as the callers' module globals (worker tests monkeypatch
``AUDIO_PATH``/``RESULTS_PATH``), so every helper takes the root explicitly.
"""
from __future__ import annotations

from pathlib import Path


def tenant_audio_sessions_dir(root: str | Path, slug: str, session_id) -> Path:
    return Path(root) / slug / "sessions" / str(session_id)


def tenant_audio_amocrm_dir(root: str | Path, slug: str, note_id) -> Path:
    return Path(root) / slug / "amocrm" / str(note_id)


def tenant_results_dir(root: str | Path, slug: str, session_id) -> Path:
    return Path(root) / slug / str(session_id)
