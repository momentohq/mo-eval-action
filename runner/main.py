#!/usr/bin/env python3
"""Entry point retained for published Action workflows."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mo_eval_svc.runner.main import main

if __name__ == "__main__":
    raise SystemExit(main())
