import shutil
from pathlib import Path

import pytest
import yaml

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "project"


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
