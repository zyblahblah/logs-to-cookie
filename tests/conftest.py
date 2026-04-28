import sys
from pathlib import Path

# Make the top-level modules (processor.py, extract.py, bot.py) importable
# without installing the project as a package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
