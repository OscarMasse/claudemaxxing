# Make `from lib import ...` resolve however the suite is discovered: from
# inside orchestrator/ (`-s tests`) the cwd already covers it, but from the
# repo root (`-s orchestrator/tests -t orchestrator`) nothing put
# orchestrator/ on sys.path until test_gate.py happened to be imported.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
