"""Shared fixtures for worker tests."""
import sys
from pathlib import Path

# Make `tasks` package importable during tests
sys.path.insert(0, str(Path(__file__).parent.parent))
