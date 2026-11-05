"""The two paths every support module and bin/test-switchboard start from: the repository's bin/ directory and the repository."""
from pathlib import Path

BIN = Path(__file__).resolve().parents[3] / "bin"
REPO = BIN.parent


def load_matrix(path=None):
    """The impact matrix through its ONE loader (the JSON plus docs/review-laws/impact-entries/, ask #2206 part 3): the tests that read the matrix call this, never json.loads on the file."""
    import sys
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    import impact_floor
    return impact_floor.load_matrix(path)
