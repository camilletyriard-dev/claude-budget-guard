import sys
from pathlib import Path

# Run without installing: python -m unittest discover tests
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
