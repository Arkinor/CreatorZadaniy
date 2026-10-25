"""Generate the application icon used by the build.

Run from the repository root:
    python tools\make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service_creator.icon import save_ico  # noqa: E402  (path setup above)


def main() -> int:
    target = ROOT / "assets" / "app.ico"
    save_ico(target, 256)
    print(f"Icon generated: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
