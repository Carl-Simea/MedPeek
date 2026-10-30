import os
import sys
import tempfile
from pathlib import Path

# Tests always run with fake Gemma answers and a throwaway reference library.
os.environ["MOCK_GEMMA"] = "1"
os.environ["MOCK_DELAY_SECONDS"] = "0"
os.environ["ADMIN_TOKEN"] = "test-token"
os.environ["REFERENCE_DIR"] = tempfile.mkdtemp(prefix="xray-refs-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
