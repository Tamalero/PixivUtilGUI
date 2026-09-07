#!/usr/bin/env fish
# Launch the PixivUtil2 GUI.
#
# This project is only the front end. PixivUtil2 itself is a separate checkout;
# gui.py finds it via --pixivutil, $PIXIVUTIL_DIR, the folder remembered in
# pixivutil-gui.ini, or a sibling directory, and asks if none of those work.
#
#   ./run-gui.fish
#   ./run-gui.fish --pixivutil /path/to/PixivUtil2
#
# The GUI runs on the SYSTEM python3, because PyQt6 lives there. The downloader
# it spawns runs from the checkout's own .venv, so PyQt6 never has to be added
# to PixivUtil2's pyproject.toml.

set -l here (dirname (realpath (status --current-filename)))
cd $here

if not python3 -c "import PyQt6" 2>/dev/null
    echo "PyQt6 is missing. Install it with:"
    echo "    sudo pacman -S python-pyqt6"
    exit 1
end

set -l live (ps -eo pid,etimes,args | grep "[P]ixivUtil2.py" | grep -v gui.py)
if test -n "$live"
    echo "Warning: PixivUtil2 already looks like it is running:"
    echo "$live"
    echo "It shares db.sqlite and the log files. Let it finish first."
    echo
end

exec python3 gui.py $argv
