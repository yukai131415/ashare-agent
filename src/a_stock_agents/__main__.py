from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    package_dir = Path(__file__).resolve().parent
    src_dir = str(package_dir.parent)
    sys.path = [item for item in sys.path if Path(item or ".").resolve() != package_dir]
    sys.path.insert(0, src_dir)
    from a_stock_agents.cli import main
else:
    from .cli import main

raise SystemExit(main())
