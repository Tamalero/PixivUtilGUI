"""
Entry point for the bundled (PyInstaller / AppImage) build.

gui.py is importable on its own, but PyInstaller wants a module that is not
also the thing being imported, and this is where the frozen build can do any
setup the plain checkout does not need.
"""

import sys


def main() -> int:
    import gui
    return gui.main()


if __name__ == "__main__":
    sys.exit(main())
