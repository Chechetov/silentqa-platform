"""Backend test fixtures: import paths for app/ and the tenancy package."""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))          # import app.*
sys.path.insert(0, str(BACKEND.parent))   # import tenancy.*
