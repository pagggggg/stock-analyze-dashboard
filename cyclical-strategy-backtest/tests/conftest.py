from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_on_network(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("tests must not make network requests")

    monkeypatch.setattr(urllib.request, "urlopen", fail_on_network)
