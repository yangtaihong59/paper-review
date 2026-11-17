import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from paper_review.config import load

@pytest.fixture
def cfg():
    return load()
