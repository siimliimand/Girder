"""``python -m girder.cli`` support (parity with the former single-file module)."""

import sys

from girder.cli import main

if __name__ == "__main__":
    sys.exit(main())
