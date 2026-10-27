#!/usr/bin/env python3
"""Lab Network launcher. Requires only Python 3.9 or newer; no packages to install."""
import os
import sys

if sys.version_info < (3, 9):
    sys.exit("Lab Network needs Python 3.9 or newer (found %d.%d)." % sys.version_info[:2])

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from labnetwork.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
