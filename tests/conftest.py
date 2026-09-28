from pathlib import Path
import pytest
import yaml
from hr_platform import fixtures as f
from hr_platform.store import Store


@pytest.fixture
def config():
    return yaml.safe_load(Path("configs/research.yaml").read_text())


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path, clock=lambda: f.at(2, 2))
    yield s
    s.close()


@pytest.fixture
def collected(store):
    store.ingest(f.event("a", 2), f.archive())
    store.clock = lambda: f.at(4, 20)
    return store
