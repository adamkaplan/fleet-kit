"""The two paths every support module and bin/test-switchboard start from: the repository's bin/ directory and the repository."""
from pathlib import Path

BIN = Path(__file__).resolve().parents[3] / "bin"
REPO = BIN.parent
