"""Start the AI Studio router.

Examples:
    python AiStudio/main.py
    cd AiStudio && python main.py
    python -m AiStudio
"""

import sys
from pathlib import Path

# Allow running this file directly from inside the package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AiStudio.cli import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
