#!/usr/bin/env python3
"""Launch the OB return-link web control page."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ob.webapp import serve
from ob import config

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else config.WEB_PORT
    serve(port)
