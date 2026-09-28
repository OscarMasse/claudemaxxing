# Make `from lib import ...` resolve however the suite is discovered: from
# inside orchestrator/ (`-s tests`) the cwd already covers it, but from the
# repo root (`-s orchestrator/tests -t orchestrator`) nothing put
# orchestrator/ on sys.path until test_gate.py happened to be imported.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The suite never sees the operator's real backlog: the recorded root lives
# under a config home that does not exist, and the repo's example data is
# allowed explicitly. Tests of the fail-loud path clear ORCH_EXAMPLE.
import os  # noqa: E402
os.environ["XDG_CONFIG_HOME"] = str(Path(__file__).resolve().parent / "no-such-config-home")
os.environ["ORCH_EXAMPLE"] = "1"
