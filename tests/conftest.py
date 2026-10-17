import os
import shutil
import stat
from pathlib import Path

import pytest
import yaml

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "project"

# Mode a file reports after os.chmod(path, stat.S_IREAD). Windows only sets the read-only attribute,
# which stat shows as r--r--r--.
READ_ONLY_MODE = 0o444 if os.name == "nt" else stat.S_IREAD


def edit_rule(root: Path, relpath: str, updates: dict) -> None:
    """Load a rule YAML under <root>/rules, dict.update it, write it back."""
    path = Path(root) / "rules" / relpath
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(updates)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def fixture_root() -> Path:
    return FIXTURE_ROOT


@pytest.fixture
def tmp_fixture(tmp_path: Path) -> Path:
    dest = tmp_path / "project"
    shutil.copytree(FIXTURE_ROOT, dest)
    return dest
