"""Pytest bootstrap.

Guarantees the repository root is importable so `import byconn` and
`import server` resolve regardless of the directory pytest is invoked from.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
