"""Celery entrypoint must make the top-level `tenancy` package importable.

Production runs `celery -A tasks.celery_app worker` with
WorkingDirectory=<repo>/worker (both run.sh and the systemd unit), so only
worker/ lands on sys.path — not the repo root. Task modules do
`from tenancy.context import ...`; without a repo-root shim in
tasks/celery_app.py the worker crash-loops with ModuleNotFoundError.

Under pytest, tests/conftest.py inserts the repo root itself and masks the
bug — hence the fresh-interpreter subprocess below.
"""
import os
import subprocess
import sys
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1]


def test_celery_app_import_exposes_tenancy_without_pythonpath():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c", "import tasks.celery_app, tenancy.context"],
        cwd=WORKER_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
