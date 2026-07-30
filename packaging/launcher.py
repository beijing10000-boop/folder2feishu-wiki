"""PyInstaller entry point.

Kept outside the application package so the source CLI and the packaged
executable always share exactly the same implementation.
"""

from folder2feishu.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
