import sys
from pathlib import Path

# Let test modules share helpers (test_daemon imports from test_controller).
sys.path.insert(0, str(Path(__file__).parent))
