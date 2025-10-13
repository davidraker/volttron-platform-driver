import os
from pathlib import Path
import pytest


@pytest.fixture(scope="session")
def get_pyproject_toml() -> Path:
    """Locate nearest pyproject.toml for volttron-testing fixtures.

    Provides a minimal implementation so volttrontesting's volttron_instance
    fixture can find the project's pyproject.toml during test collection.
    """
    # Search upward from current working directory
    for parent in Path(os.getcwd()).parents:
        candidate = parent / "pyproject.toml"
        if candidate.exists():
            return candidate

    # Fallback: search relative to this file's location
    for parent in Path(__file__).parents:
        candidate = parent / "pyproject.toml"
        if candidate.exists():
            return candidate

    raise ValueError("Could not find pyproject.toml file tree.")
