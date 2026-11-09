import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "cloudflare" / "src"))

from routers import app