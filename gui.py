#!/usr/bin/env python3
"""
PixivUtil2 — PyQt6 frontend.

A standalone front end for Nandaka's PixivUtil2. It lives in its own repo and
deliberately keeps *nothing* inside the PixivUtil2 checkout, so it can never be
swept into an upstream pull request. PixivUtil2 is a runtime dependency, found
at startup (see locate_pixivutil below) and driven as a child process; this
file is only UI wiring.

Two ways of driving it, picked per mode in MODES below:

  * non-interactive — ``PixivUtil2.py -s <op> -x [options] -- [args]``.
    Preferred: every value is passed on the command line, nothing to guess.
  * interactive — the menu is fed over stdin.  Needed for the handful of
    operations that either are not in the CLI's ``__valid_options`` (``f6``,
    ``u``, ``i``) or ignore their arguments and prompt anyway (``14``, ``15``,
    and ``16``/``17``/``18``, where the non-interactive path also drops the
    ranking date and mishandles the mode).

A "Send" box is always available so any unexpected prompt can be answered by
hand instead of hanging the run.
"""

from __future__ import annotations

import codecs
import os
import re
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QAction, QColor, QDesktopServices, QFont, QPixmap, QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
    QStackedWidget, QStatusBar, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

VERSION = "1.0.2"

# Frozen (PyInstaller/AppImage) builds have no __file__ on disk to speak of, and
# the bundle is mounted read-only, so nothing may be written next to it.
FROZEN = getattr(sys, "frozen", False)
GUI_DIR = (Path(sys.executable).resolve().parent if FROZEN
           else Path(__file__).resolve().parent)

# Config always goes to XDG: inside an AppImage the program directory is a
# read-only squashfs mount that changes path on every run and every update.
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
SETTINGS_FILE = CONFIG_HOME / "pixivutil-gui" / "settings.ini"

SCRIPT = "PixivUtil2.py"
CONFIG_NAME = "config.ini"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}

# Where PixivUtil2 is checked out. Resolved once at startup by locate_pixivutil()
# and re-pointed by "File -> Choose PixivUtil2 folder...".
PIXIVUTIL_DIR: Path = GUI_DIR

# Sibling layouts to try when nothing has been configured yet.
CANDIDATE_DIRS = (
    "../PixivUtilFix",
    "../PixivUtil2",
    "../../PixivUtilFix",
    "./PixivUtilFix",
    "./PixivUtil2",
    "~/PixivUtil2",
    "~/PixivUtilFix",
)


def search_bases() -> list[Path]:
    """
    Directories the CANDIDATE_DIRS are resolved against.

    Inside an AppImage, GUI_DIR is a throwaway /tmp mount, so a sibling search
    from there finds nothing. $APPIMAGE is the path of the .AppImage file itself
    and $OWD is the directory the user launched it from — those are the ones
    that actually sit next to a checkout.
    """
    bases = []
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        bases.append(Path(appimage).resolve().parent)
    original_cwd = os.environ.get("OWD")
    if original_cwd:
        bases.append(Path(original_cwd))
    bases.append(GUI_DIR)
    bases.append(Path.cwd())

    unique = []
    for base in bases:
        if base not in unique:
            unique.append(base)
    return unique


def is_pixivutil_dir(path) -> bool:
    """A checkout is usable when the entry point and its handlers are there."""
    path = Path(path).expanduser()
    return (path / SCRIPT).is_file() and (path / "handler").is_dir()


def open_externally(path) -> bool:
    """
    Hand a file or folder to the desktop.

    Same trap as the file dialogs: under the Plasma platform theme this goes
    through KIO, and the bundle has no KIO workers, so it can fail silently.
    Fall back to xdg-open, which is a plain executable on the host.
    """
    if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
        return True
    try:
        subprocess.Popen(["xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def pick_path(parent, caption: str, directory: str, *, folder: bool = False,
              filters: list | None = None, preselect: str = "") -> str:
    """
    Open a file/folder chooser and return the chosen path, or "".

    Always Qt's own dialog, never the desktop's. A PyInstaller bundle drags in
    libKF6KIO* as a dependency of the Plasma platform theme but none of the KIO
    *workers* that actually enumerate a directory, so the native dialog comes up
    looking perfect and lists nothing at all — no files and no subdirectories,
    whatever the name filter says. Qt's built-in dialog uses QFileSystemModel
    and needs nothing outside the bundle.
    """
    dialog = QFileDialog(parent, caption, directory)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    if folder:
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    else:
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setNameFilters(filters or ["All files (*)"])
    if preselect:
        dialog.selectFile(preselect)

    # Somewhere to jump to besides $HOME, since the interesting paths are
    # usually on another mount entirely.
    sidebar = [QUrl.fromLocalFile(str(Path.home()))]
    for base in search_bases():
        if base.is_dir() and not (FROZEN and (base == GUI_DIR or GUI_DIR in base.parents)):
            sidebar.append(QUrl.fromLocalFile(str(base)))
    seen, unique = set(), []
    for url in sidebar:
        if url.toLocalFile() not in seen:
            seen.add(url.toLocalFile())
            unique.append(url)
    dialog.setSidebarUrls(unique)

    if dialog.exec() != QDialog.DialogCode.Accepted:
        return ""
    chosen = dialog.selectedFiles()
    return chosen[0] if chosen else ""


def normalise_checkout(text: str) -> Path | None:
    """
    Turn whatever the user pasted into a checkout directory, or None.

    Accepts the folder, the path of PixivUtil2.py inside it, a quoted path, and
    a file:// URL — Dolphin's location bar copies the last of those, and that is
    what someone reaches for when a file dialog will not cooperate.
    """
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        return None
    if text.startswith("file://"):
        text = QUrl(text).toLocalFile()      # also undoes %20 and friends
    candidate = Path(text).expanduser()

    if candidate.is_file():                  # they picked PixivUtil2.py itself
        candidate = candidate.parent
    if is_pixivutil_dir(candidate):
        return candidate.resolve()
    # Pointed just inside the checkout, e.g. at handler/ — accept the parent.
    if is_pixivutil_dir(candidate.parent):
        return candidate.parent.resolve()
    return None


def browse_start_dir(current: str = "") -> str:
    """
    A sensible directory for the file dialog to open in.

    Never the program directory: in an AppImage that is a /tmp mount holding
    nothing but bin/ and share/, which is what made the first-run dialog look
    empty and unnavigable.
    """
    existing = Path((current or "").strip()).expanduser()
    if existing.is_file():
        existing = existing.parent
    if current and existing.is_dir():
        return str(existing)

    for base in search_bases():
        if FROZEN and (base == GUI_DIR or GUI_DIR in base.parents):
            continue                          # inside the read-only mount
        if base.is_dir():
            return str(base)
    return str(Path.home())


def remembered_dir() -> Path | None:
    """The folder saved by a previous run, if it is still valid."""
    try:
        text = SETTINGS_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "pixivutil_dir":
            candidate = Path(value.strip()).expanduser()
            if is_pixivutil_dir(candidate):
                return candidate.resolve()
    return None


def remember_dir(path: Path) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            "# Written by PixivUtilGUI — where the PixivUtil2 checkout lives.\n"
            f"pixivutil_dir = {path}\n", encoding="utf-8")
    except OSError:
        pass                                # not fatal, just re-ask next time


def locate_pixivutil(explicit: str = "") -> Path | None:
    """
    Find the PixivUtil2 checkout, most explicit source first:
    --pixivutil, $PIXIVUTIL_DIR, the remembered folder, then known siblings.
    """
    for source in (explicit, os.environ.get("PIXIVUTIL_DIR", "")):
        if source:
            candidate = Path(source).expanduser()
            return candidate.resolve() if is_pixivutil_dir(candidate) else None

    saved = remembered_dir()
    if saved is not None:
        return saved

    for base in search_bases():
        for relative in CANDIDATE_DIRS:
            candidate = (base / relative).expanduser()
            if is_pixivutil_dir(candidate):
                return candidate.resolve()
    return None


# ── console output plumbing ───────────────────────────────────────────────────

# colorama strips SGR codes on its own when stdout is a pipe, but the console
# title is written straight to the stream, so strip both here regardless.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1b\](.*?)(?:\x07|\x1b\\)", re.DOTALL)

# PixivHelper.print_progress() draws a 40-cell bar; the fill ratio is the only
# reliable percentage, since the sizes beside it are pre-formatted strings.
BAR_RE = re.compile(r"^\[([━╹╸█. ]{40})\]\s")
OVERALL_RE = re.compile(r"\[(\d+) of (\d+)\]")
DONE_RE = re.compile(r"Download done ==> (.+?)\s*$")
EXISTS_RE = re.compile(r"Local file exists: (.+?)\s*$")

ERROR_WORDS = ("error", "failed", "cannot", "invalid", "traceback", "exception")
WARN_WORDS = ("warn", "skipped", "skipping", "retrying", "aborted", "missing")


def classify(line: str) -> str:
    """Guess a log level for a console line (the CLI prints no level prefix)."""
    low = line.lower()
    if any(w in low for w in ERROR_WORDS):
        return "error"
    if any(w in low for w in WARN_WORDS):
        return "warn"
    if "download done" in low:
        return "ok"
    return "info"


def find_python() -> str:
    """The project venv if it is there, otherwise whatever is running us."""
    venv = PIXIVUTIL_DIR / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def running_sessions() -> list[str]:
    """Other PixivUtil2.py processes — they share db.sqlite and the log file."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,etimes,args"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    me = os.getpid()
    hits = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3 or SCRIPT not in parts[2] or "grep" in parts[2]:
            continue
        if int(parts[0]) == me:
            continue
        hits.append(f"pid {parts[0]}, running {int(parts[1]) // 60} min")
    return hits


# ── mode description ──────────────────────────────────────────────────────────

@dataclass
class F:
    """One input in a mode's form."""
    key: str
    label: str
    kind: str = "text"      # text | ids | int | bool | choice | file | dir | date
    default: object = ""
    choices: tuple = ()     # ((value, human label), ...)
    tip: str = ""


@dataclass
class Job:
    """A resolved run: what to put on the command line and into stdin."""
    op: str
    opts: list = field(default_factory=list)
    args: list = field(default_factory=list)
    stdin: list = field(default_factory=list)
    interactive: bool = False


@dataclass
class Mode:
    op: str
    label: str
    group: str
    fields: list
    build: object           # (values, ctx) -> Job
    note: str = ""


PAGES = [
    F("start_page", "Start page", "int", 1),
    F("end_page", "End page", "int", 0, tip="0 = no limit"),
]
DATES = [
    F("start_date", "Start date", "date", tip="YYYY-MM-DD, leave blank for none"),
    F("end_date", "End date", "date", tip="YYYY-MM-DD, leave blank for none"),
]
PRIVACY = (("n", "Public bookmarks only"),
           ("y", "Public and private"),
           ("o", "Private bookmarks only"))
TAG_SORT = (("date_d", "Newest first"),
            ("date", "Oldest first"),
            ("popular_d", "Most popular (premium only)"),
            ("popular_male_d", "Popular with male users (premium only)"),
            ("popular_female_d", "Popular with female users (premium only)"))
EXPORT_DB = (("n", "Exclude"), ("y", "Include"), ("o", "Only this one"))


def split_ids(text: str) -> list[str]:
    return [t for t in re.split(r"[,\s]+", (text or "").strip()) if t]


def opt_pages(v) -> list[str]:
    out = []
    if str(v.get("start_page", "")).strip() != "":
        out += ["--sp", str(v["start_page"])]
    if str(v.get("end_page", "")).strip() != "":
        out += ["--ep", str(v["end_page"])]
    return out


def opt_dates(v) -> list[str]:
    out = []
    if (v.get("start_date") or "").strip():
        out += ["--start_date", v["start_date"].strip()]
    if (v.get("end_date") or "").strip():
        out += ["--end_date", v["end_date"].strip()]
    return out


def opt_bcl(v) -> list[str]:
    n = str(v.get("bookmark_count", "")).strip()
    return ["--bcl", n] if n not in ("", "-1", "0") else []


def opt_list_file(v) -> list[str]:
    f = (v.get("list_file") or "").strip()
    return ["-f", f] if f else []


def opt_export_name(v) -> list[str]:
    f = (v.get("export_filename") or "").strip()
    return ["--ef", f] if f else []


# ── the operations, in menu order ─────────────────────────────────────────────

MODES: list[Mode] = [
    Mode("1", "Download by member ID", "Pixiv",
         [F("ids", "Member IDs", "ids", tip="One or more, separated by commas or spaces"),
          F("include_sketch", "Also fetch their Pixiv Sketch", "bool", False)] + PAGES,
         lambda v, c: Job("1",
                          opt_pages(v) + (["--is"] if v["include_sketch"] else []),
                          split_ids(v["ids"]))),

    Mode("2", "Download by image ID", "Pixiv",
         [F("ids", "Image IDs", "ids")],
         lambda v, c: Job("2", [], split_ids(v["ids"]))),

    Mode("3", "Download by tags", "Pixiv",
         [F("tags", "Tags", "text", tip="Space-separated tags are ANDed by Pixiv"),
          F("wildcard", "Partial match (s_tag)", "bool", False),
          F("sort_order", "Sort order", "choice", "date_d", TAG_SORT),
          F("bookmark_count", "Minimum bookmarks", "int", 0, tip="0 = no minimum")]
         + DATES + PAGES,
         lambda v, c: Job("3",
                          opt_pages(v) + opt_dates(v) + opt_bcl(v)
                          + ["--tag_sort_order", v["sort_order"]]
                          + (["--wt"] if v["wildcard"] else []),
                          v["tags"].split())),

    Mode("4", "Download from list file", "Pixiv",
         [F("list_file", "List file", "file", tip="Blank = list.txt in downloadListDirectory"),
          F("tag", "Only this tag", "text", tip="Optional filter, blank = everything"),
          F("include_sketch", "Also fetch Pixiv Sketch", "bool", False)],
         lambda v, c: Job("4",
                          opt_list_file(v) + (["--is"] if v["include_sketch"] else []),
                          [v["tag"].strip()] if v["tag"].strip() else [])),

    Mode("5", "Download from followed artists", "Pixiv",
         [F("privacy", "Bookmarks to use", "choice", "n", PRIVACY),
          F("bookmark_count", "Minimum bookmarks", "int", 0, tip="0 = no minimum")] + PAGES,
         # -p is what unlocks the page range in this menu, so always send it.
         lambda v, c: Job("5", ["-p", v["privacy"]] + opt_pages(v) + opt_bcl(v))),

    Mode("6", "Download own bookmarked images", "Pixiv",
         [F("privacy", "Bookmarks to use", "choice", "n", PRIVACY),
          F("tag", "Only this tag", "text", tip="Optional, blank = every bookmark"),
          F("use_image_tag", "Match against image tags, not bookmark tags", "bool", False)]
         + PAGES,
         lambda v, c: Job("6",
                          ["-p", v["privacy"]] + opt_pages(v)
                          + (["--uit"] if v["use_image_tag"] else []),
                          [v["tag"].strip()] if v["tag"].strip() else [])),

    Mode("7", "Download from tags list file", "Pixiv",
         [F("list_file", "Tags list file", "file", tip="Blank = ./tags.txt"),
          F("wildcard", "Partial match (s_tag)", "bool", False),
          F("sort_order", "Sort order", "choice", "date_d", TAG_SORT),
          F("bookmark_count", "Minimum bookmarks", "int", 0, tip="0 = no minimum")]
         + DATES + PAGES,
         lambda v, c: Job("7",
                          opt_list_file(v) + opt_pages(v) + opt_dates(v) + opt_bcl(v)
                          + ["--tag_sort_order", v["sort_order"]]
                          + (["--wt"] if v["wildcard"] else []))),

    Mode("8", "Download new illusts from bookmarked members", "Pixiv",
         [F("bookmark_count", "Minimum bookmarks", "int", 0, tip="0 = no minimum")] + PAGES,
         lambda v, c: Job("8", opt_pages(v) + opt_bcl(v))),

    Mode("9", "Download by title / caption", "Pixiv",
         [F("text", "Title or caption text", "text")] + DATES + PAGES,
         lambda v, c: Job("9", opt_pages(v) + opt_dates(v), v["text"].split())),

    Mode("10", "Download by tag and member ID", "Pixiv",
         [F("member_id", "Member ID", "text"),
          F("tags", "Tags", "text")] + PAGES,
         lambda v, c: Job("10", opt_pages(v),
                          [v["member_id"].strip()] + v["tags"].split())),

    Mode("11", "Download another member's bookmarks", "Pixiv",
         [F("ids", "Member IDs", "ids")],
         lambda v, c: Job("11", [], split_ids(v["ids"])),
         note="For your own bookmarks use \"Download own bookmarked images\" instead."),

    Mode("12", "Download by group ID", "Pixiv",
         [F("group_id", "Group ID", "text"),
          F("limit", "Limit", "int", 100),
          F("process_external", "Include external images", "bool", False)],
         lambda v, c: Job("12", [], [v["group_id"].strip(), str(v["limit"]),
                                     "y" if v["process_external"] else "n"])),

    Mode("13", "Download by manga series ID", "Pixiv",
         [F("ids", "Manga series IDs", "ids")] + PAGES,
         lambda v, c: Job("13", opt_pages(v), split_ids(v["ids"]))),

    # 14 and 15 accept -s but ignore argv entirely, so answer their prompts.
    Mode("14", "Download by novel ID", "Pixiv",
         [F("ids", "Novel IDs", "ids")],
         lambda v, c: Job("14", stdin=[",".join(split_ids(v["ids"]))])),

    Mode("15", "Download by novel series ID", "Pixiv",
         [F("ids", "Novel series IDs", "ids")] + PAGES,
         lambda v, c: Job("15", stdin=[",".join(split_ids(v["ids"])),
                                       str(v["start_page"]), str(v["end_page"])])),

    # 16-18 are driven through the menu: the -s path drops the ranking date and
    # passes the wrong mode/type for new illusts.
    Mode("16", "Download by rank", "Pixiv",
         [F("rank_mode", "Ranking", "choice", "daily",
            tuple((m, m.capitalize()) for m in
                  ("daily", "weekly", "monthly", "rookie", "original", "male", "female"))),
          F("rank_content", "Content type", "choice", "all",
            (("all", "All"), ("illust", "Illustrations"), ("ugoira", "Ugoira"), ("manga", "Manga"))),
          F("date", "Date", "text", tip="YYYYMMDD, blank = today")] + PAGES,
         lambda v, c: Job("16", interactive=True,
                          stdin=[v["rank_mode"], v["rank_content"], v["date"].strip(),
                                 str(v["start_page"]), str(v["end_page"])])),

    Mode("17", "Download by rank (R-18)", "Pixiv",
         [F("rank_mode", "Ranking", "choice", "daily_r18",
            (("daily_r18", "Daily R-18"), ("weekly_r18", "Weekly R-18"),
             ("male_r18", "Male R-18"), ("female_r18", "Female R-18"))),
          F("rank_content", "Content type", "choice", "all",
            (("all", "All"), ("illust", "Illustrations"), ("ugoira", "Ugoira"), ("manga", "Manga"))),
          F("date", "Date", "text", tip="YYYYMMDD, blank = today")] + PAGES,
         lambda v, c: Job("17", interactive=True,
                          stdin=[v["rank_mode"], v["rank_content"], v["date"].strip(),
                                 str(v["start_page"]), str(v["end_page"])])),

    Mode("18", "Download new illusts", "Pixiv",
         [F("rank_mode", "Type", "choice", "illust",
            (("illust", "Illustrations"), ("manga", "Manga"))),
          F("max_page", "Max pages", "int", 1, tip="0 = no limit")],
         lambda v, c: Job("18", interactive=True,
                          stdin=[v["rank_mode"], str(v["max_page"])])),

    Mode("19", "Download by unlisted image ID", "Pixiv",
         [F("ids", "Unlisted IDs", "ids", tip="The alphanumeric IDs from /artworks/unlisted/…")],
         lambda v, c: Job("19", [], split_ids(v["ids"]))),

    Mode("m1", "Metadata by member ID", "Metadata",
         [F("ids", "Member IDs", "ids")],
         lambda v, c: Job("m1", [], split_ids(v["ids"]))),

    Mode("m2", "Metadata by image ID", "Metadata",
         [F("ids", "Image IDs", "ids")],
         lambda v, c: Job("m2", [], split_ids(v["ids"]))),

    Mode("m3", "Metadata by manga series ID", "Metadata",
         [F("ids", "Manga series IDs", "ids")],
         lambda v, c: Job("m3", [], split_ids(v["ids"]))),

    Mode("m4", "Metadata by tag", "Metadata",
         [F("tags", "Tags", "ids"),
          F("filter", "Filter", "choice", "none",
            (("none", "No filter"), ("pixpedia", "Has pixpedia entry"),
             ("translation", "Has translation"),
             ("pixpedia_or_translation", "Has either")))],
         lambda v, c: Job("m4", ["--tmf", v["filter"]], split_ids(v["tags"]))),

    Mode("f1", "Download from supporting list", "FANBOX",
         PAGES,
         lambda v, c: Job("f1", opt_pages(v))),

    Mode("f2", "Download by creator ID", "FANBOX",
         [F("ids", "Creator IDs", "ids")] + PAGES,
         lambda v, c: Job("f2", opt_pages(v), split_ids(v["ids"]))),

    Mode("f3", "Download by post ID", "FANBOX",
         [F("ids", "Post IDs", "ids")],
         lambda v, c: Job("f3", [], split_ids(v["ids"]))),

    Mode("f4", "Download from following list", "FANBOX",
         PAGES,
         lambda v, c: Job("f4", opt_pages(v))),

    Mode("f5", "Download from custom list file", "FANBOX",
         [F("list_file", "List file", "file", tip="Blank = listPathFanbox from config.ini")] + PAGES,
         lambda v, c: Job("f5", opt_list_file(v) + opt_pages(v))),

    # f6 is missing from the CLI's __valid_options, so -s would be rejected.
    Mode("f6", "Download their Pixiv works by FANBOX creator ID", "FANBOX",
         [F("ids", "Creator IDs", "ids")] + PAGES,
         lambda v, c: Job("f6", interactive=True,
                          stdin=[",".join(split_ids(v["ids"])),
                                 str(v["start_page"]), str(v["end_page"])])),

    Mode("f7", "Download latest supporting posts", "FANBOX",
         [F("pages", "Number of pages", "int", 1)],
         lambda v, c: Job("f7", [], [str(v["pages"])])),

    Mode("s1", "Download by creator ID", "Sketch",
         [F("ids", "Creator IDs", "ids")],
         lambda v, c: Job("s1", [], split_ids(v["ids"]))),

    Mode("s2", "Download by post ID", "Sketch",
         [F("ids", "Post IDs", "ids")],
         lambda v, c: Job("s2", [], split_ids(v["ids"]))),

    Mode("b", "Run batch job", "Other",
         [F("batch_file", "batch_job.json", "file")],
         lambda v, c: Job("b", (["--bf", v["batch_file"].strip()]
                                if v["batch_file"].strip() else []))),

    Mode("l", "Export local database", "Other",
         [F("export_filename", "Save to", "text", "export-database.txt"),
          F("use_pixiv", "Pixiv table", "choice", "y", EXPORT_DB),
          F("use_fanbox", "FANBOX table", "choice", "n", EXPORT_DB),
          F("use_sketch", "Sketch table", "choice", "n", EXPORT_DB)],
         lambda v, c: Job("l", opt_export_name(v)
                          + ["--up", v["use_pixiv"], "--uf", v["use_fanbox"],
                             "--us", v["use_sketch"]])),

    Mode("e", "Export followed artists", "Other",
         [F("export_filename", "Save to", "text", "export.txt"),
          F("privacy", "Bookmarks to use", "choice", "n", PRIVACY)],
         lambda v, c: Job("e", opt_export_name(v) + ["-p", v["privacy"]])),

    Mode("m", "Export another member's followed artists", "Other",
         [F("member_id", "Member ID", "text"),
          F("export_filename", "Save to", "text", tip="Blank = export-user-<id>.txt")],
         lambda v, c: Job("m", opt_export_name(v), [v["member_id"].strip()])),

    Mode("p", "Export bookmarked images", "Other",
         [F("export_filename", "Save to", "text", "Exported_images.txt"),
          F("privacy", "Bookmarks to use", "choice", "n", PRIVACY),
          F("tag", "Only this tag", "text"),
          F("use_image_tag", "Match against image tags", "bool", False)] + PAGES,
         lambda v, c: Job("p", opt_export_name(v) + ["-p", v["privacy"]] + opt_pages(v)
                          + (["--uit"] if v["use_image_tag"] else []),
                          [v["tag"].strip()] if v["tag"].strip() else [])),

    # u and i are also missing from __valid_options.
    Mode("u", "Re-encode stored ugoira", "Other", [],
         # The second "y" only gets asked when overwrite is on; a stray extra
         # line would be eaten by the menu, so send it only when it is needed.
         lambda v, c: Job("u", interactive=True,
                          stdin=["y", "y"] if c.get("overwrite") else ["y"]),
         note="Overwrites every stored ugoira and the files derived from it. "
              "This cannot be undone."),

    Mode("i", "Import a list file into the database", "Other",
         [F("list_file", "List file", "file", tip="Blank = list.txt")],
         lambda v, c: Job("i", interactive=True, stdin=[v["list_file"].strip()])),

    Mode("c", "Print the active config", "Other", [],
         lambda v, c: Job("c")),
]

MODE_BY_OP = {m.op: m for m in MODES}


# ── config.ini ────────────────────────────────────────────────────────────────

# The keys the Settings tab exposes, as (section, key, label, kind, choices).
# Everything else in config.ini is left untouched.
SETTINGS_SCHEMA = [
    ("Paths", [
        ("Settings", "rootDirectory", "Download root", "dir", ()),
        ("Settings", "downloadListDirectory", "List file directory", "dir", ()),
        ("Settings", "dbPath", "Database path", "text", ()),
    ]),
    ("Authentication", [
        ("Authentication", "cookie", "Pixiv PHPSESSID cookie", "text", ()),
        ("Authentication", "cookieFanbox", "FANBOX session cookie", "text", ()),
        ("Authentication", "userAgentImpersonation", "Impersonate", "text", ()),
    ]),
    ("Filenames", [
        ("Filename", "filenameFormat", "Illustration", "text", ()),
        ("Filename", "filenameMangaFormat", "Manga", "text", ()),
        ("Filename", "filenameFormatSketch", "Sketch", "text", ()),
        ("Filename", "filenameFormatNovel", "Novel", "text", ()),
        ("Filename", "createMangaDir", "Give manga its own directory", "bool", ()),
        ("Filename", "useTagsAsDir", "Use tags as directory", "bool", ()),
        ("Filename", "useTranslatedTag", "Use translated tags", "bool", ()),
    ]),
    ("Download control", [
        ("Pixiv", "numberOfPage", "Default page limit", "int", ()),
        ("Pixiv", "r18mode", "R-18 mode", "bool", ()),
        ("DownloadControl", "overwrite", "Overwrite existing files", "bool", ()),
        ("DownloadControl", "checkLastModified", "Check last-modified", "bool", ()),
        ("DownloadControl", "dateDiff", "Only images newer than N days", "int", ()),
        ("DownloadControl", "dayLastUpdated", "Only members updated within N days", "int", ()),
        ("DownloadControl", "backupOldFile", "Back up replaced files", "bool", ()),
        ("Settings", "downloadAvatar", "Download member avatars", "bool", ()),
        ("Settings", "writeImageInfo", "Write .txt info files", "bool", ()),
        ("Settings", "writeImageJSON", "Write .json info files", "bool", ()),
    ]),
    ("Network", [
        ("Network", "downloadDelay", "Delay between downloads (s)", "int", ()),
        ("Network", "timeout", "Timeout (s)", "int", ()),
        ("Network", "retry", "Retries", "int", ()),
        ("Network", "retryWait", "Wait between retries (s)", "int", ()),
        ("Network", "useProxy", "Use proxy", "bool", ()),
        ("Network", "proxyAddress", "Proxy address", "text", ()),
        ("Network", "checkNewVersion", "Check for new versions", "bool", ()),
    ]),
    ("Ugoira (animations)", [
        ("Ugoira", "createUgoira", "Keep the .ugoira archive", "bool", ()),
        ("Ugoira", "createWebm", "Convert to webm", "bool", ()),
        ("Ugoira", "createWebp", "Convert to webp", "bool", ()),
        ("Ugoira", "createGif", "Convert to gif", "bool", ()),
        ("Ugoira", "createApng", "Convert to apng", "bool", ()),
        ("Ugoira", "createAvif", "Convert to avif", "bool", ()),
        ("Ugoira", "createMkv", "Convert to mkv", "bool", ()),
        ("Ugoira", "deleteZipFile", "Delete the source zip", "bool", ()),
        ("FFmpeg", "ffmpeg", "ffmpeg binary", "text", ()),
        ("FFmpeg", "ffmpegCodec", "Codec", "text", ()),
        ("FFmpeg", "ffmpegExt", "Extension", "text", ()),
        ("FFmpeg", "ffmpegParam", "Parameters", "text", ()),
    ]),
    ("Logging", [
        ("Debug", "logLevel", "Log level", "choice",
         (("DEBUG", "DEBUG"), ("INFO", "INFO"), ("WARNING", "WARNING"), ("ERROR", "ERROR"))),
        ("Debug", "enableDump", "Dump pages on parse failure", "bool", ()),
        ("Debug", "disableLog", "Disable logging", "bool", ()),
    ]),
]


def read_ini(path: Path) -> dict:
    """(section, key) -> value, without configparser's %-interpolation."""
    import configparser
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    return {(s, k): v for s in parser.sections() for k, v in parser.items(s)}


def patch_ini(path: Path, changes: dict) -> None:
    """
    Rewrite only the changed keys, line by line.

    A full configparser round-trip would reformat the whole file, and a
    malformed config.ini makes PixivUtil2 crash before its logger exists — so
    touch as little as possible and keep a backup.
    """
    if not changes:
        return
    original = path.read_text(encoding="utf-8").splitlines(keepends=True)
    (path.parent / (path.name + ".gui-bak")).write_text("".join(original), encoding="utf-8")

    pending = dict(changes)
    section = None
    out = []
    for line in original:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            out.append(line)
            continue
        if section is not None and "=" in stripped and not stripped.startswith(("#", ";")):
            key = stripped.split("=", 1)[0].strip()
            if (section, key) in pending:
                out.append(f"{key} = {pending.pop((section, key))}\n")
                continue
        out.append(line)

    if pending:  # keys that were not in the file at all
        by_section = {}
        for (sec, key), val in pending.items():
            by_section.setdefault(sec, []).append(f"{key} = {val}\n")
        text = "".join(out)
        for sec, lines in by_section.items():
            marker = f"[{sec}]\n"
            idx = text.find(marker)
            if idx < 0:
                text += f"\n[{sec}]\n" + "".join(lines)
            else:
                at = idx + len(marker)
                text = text[:at] + "".join(lines) + text[at:]
        out = [text]

    path.write_text("".join(out), encoding="utf-8")


def as_bool(value: str, default: bool = False) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on") if value else default


# ── the child process ─────────────────────────────────────────────────────────

def command_line(job: Job, config_path: str = "") -> list[str]:
    argv = [find_python(), "-u", SCRIPT]
    if config_path:
        argv += ["-c", config_path]
    argv += ["-x"]                     # never wait for "press enter to exit"
    if not job.interactive:
        argv += ["-s", job.op]
    argv += job.opts
    if job.args:
        argv += ["--", *job.args]      # so a tag starting with "-" survives
    return argv


def initial_stdin(job: Job) -> list[str]:
    """The answers to feed in before anything is read back."""
    return [job.op, *job.stdin] if job.interactive else list(job.stdin)


class RunWorker(QThread):
    line = pyqtSignal(str, str, bool)    # (text, level, transient)
    title = pyqtSignal(str)              # console title, used as a status line
    overall = pyqtSignal(int, int)       # (current, total)
    file_progress = pyqtSignal(int, str)  # (percent or -1 for busy, caption)
    preview = pyqtSignal(str)            # path of a just-saved file
    counts = pyqtSignal(int, int, int)   # (downloaded, existing, errors)
    done = pyqtSignal(int, str)          # (exit code, message)

    def __init__(self, job: Job, config_path: str = ""):
        super().__init__()
        self.job = job
        self.config_path = config_path
        self.proc: subprocess.Popen | None = None
        self._cur = ""
        self._last_transient = None
        self._pending_transient = None
        self._last_transient_at = 0.0
        self._downloaded = 0
        self._existing = 0
        self._errors = 0
        self._stopping = False
        self._stop_at = 0.0
        self._terminated = False
        self._killed = False

    # -- command line ---------------------------------------------------------

    def argv(self) -> list[str]:
        return command_line(self.job, self.config_path)

    def initial_stdin(self) -> list[str]:
        return initial_stdin(self.job)

    # -- lifecycle ------------------------------------------------------------

    def run(self):
        argv = self.argv()
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        try:
            self.proc = subprocess.Popen(
                argv, cwd=str(PIXIVUTIL_DIR), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, start_new_session=True, env=env,
            )
        except OSError as ex:
            self.done.emit(-1, f"Could not start {argv[0]}: {ex}")
            return

        for answer in self.initial_stdin():
            self.send(answer)

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = self.proc.stdout.fileno()
        try:
            while True:
                # Waiting with a timeout separates "more output is coming" from
                # "it is sitting at a prompt", so partial lines can be rate
                # limited while streaming yet still shown once things go quiet.
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    self._flush_transient()
                    self._escalate()
                    if self.proc.poll() is not None:
                        break
                    continue
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                self._feed(decoder.decode(chunk))
                if self._stopping:
                    self._escalate()
        except (OSError, ValueError):
            pass
        self._flush_transient()
        if self._cur:
            self._dispatch(ANSI_RE.sub("", OSC_RE.sub("", self._cur)).rstrip(), True)
            self._cur = ""

        code = self.proc.wait()
        if self._stopping:
            self.done.emit(code, "Stopped.")
        elif code == 0:
            self.done.emit(code, "Finished.")
        else:
            self.done.emit(code, f"Finished with exit code {code}.")

    def send(self, text: str) -> bool:
        """Answer a prompt. Returns False once the child's stdin is gone."""
        if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
            return False
        try:
            self.proc.stdin.write((text + "\n").encode("utf-8", "replace"))
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def cancel(self):
        """
        Ask the downloader to stop. Returns at once — this is called from the
        UI thread, so the escalation to SIGTERM/SIGKILL happens in _escalate(),
        which the reader loop calls as it polls.
        """
        if self._stopping:
            return
        self._stopping = True
        self._stop_at = time.monotonic()
        self._signal(signal.SIGINT)   # the CLI unwinds a Ctrl-C cleanly
        self.send("x")

    def _signal(self, sig) -> bool:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _escalate(self):
        if not self._stopping or self.proc is None or self.proc.poll() is not None:
            return
        waited = time.monotonic() - self._stop_at
        if waited > 13 and not self._killed:
            self._killed = True
            self._signal(signal.SIGKILL)
        elif waited > 8 and not self._terminated:
            self._terminated = True
            self._signal(signal.SIGTERM)

    # -- output parsing -------------------------------------------------------

    def _feed(self, text: str):
        buf = self._cur + text
        while True:
            nl = buf.find("\n")
            if nl < 0:
                break
            segment, buf = buf[:nl], buf[nl + 1:]
            pieces = segment.split("\r")
            for piece in pieces[:-1]:
                if piece:
                    self._emit(piece, False)
            self._emit(pieces[-1], True)

        pieces = buf.split("\r")
        for piece in pieces[:-1]:
            if piece:
                self._emit(piece, False)
        self._cur = pieces[-1]
        if self._cur:
            self._emit(self._cur, False)

    def _emit(self, raw: str, ended: bool):
        for match in OSC_RE.finditer(raw):
            body = match.group(1)
            if body[:2] in ("0;", "2;"):
                self.title.emit(ANSI_RE.sub("", body[2:]).strip())
        text = ANSI_RE.sub("", OSC_RE.sub("", raw)).rstrip()

        if ended:
            self._last_transient = None
            self._pending_transient = None
            self._dispatch(text, True)
            return

        # The unterminated tail is re-read on every chunk, and safePrint() emits
        # one write per word, so drop repeats and cap the rate.
        if not text or text == self._last_transient:
            return
        self._pending_transient = text
        if time.monotonic() - self._last_transient_at < 0.04:
            return
        self._flush_transient()

    def _flush_transient(self):
        text = self._pending_transient
        if text is None:
            return
        self._pending_transient = None
        self._last_transient = text
        self._last_transient_at = time.monotonic()
        self._dispatch(text, False)

    def _dispatch(self, text: str, ended: bool):
        if not text and not ended:
            return

        bar = BAR_RE.match(text)
        if bar:
            cells = bar.group(1)
            caption = text[bar.end():].strip()
            if "█" in cells:                      # indeterminate spinner
                self.file_progress.emit(-1, caption)
            else:
                filled = sum(1 for ch in cells if ch in "━╸╹")
                self.file_progress.emit(round(filled * 100 / len(cells)), caption)
            return

        match = OVERALL_RE.search(text)
        if match:
            self.overall.emit(int(match.group(1)), int(match.group(2)))

        changed = False
        match = DONE_RE.search(text)
        if match:
            self._downloaded += 1
            changed = True
            self.preview.emit(match.group(1).strip())
            self.file_progress.emit(100, "")
        elif EXISTS_RE.search(text):
            self._existing += 1
            changed = True

        level = classify(text)
        if level == "error":
            self._errors += 1
            changed = True
        if changed:
            self.counts.emit(self._downloaded, self._existing, self._errors)

        self.line.emit(text, level, not ended)


# ── forms ─────────────────────────────────────────────────────────────────────

# Inputs that must not be blank, per mode.
REQUIRED = {
    "1": ("ids",), "2": ("ids",), "3": ("tags",), "9": ("text",),
    "10": ("member_id", "tags"), "11": ("ids",), "12": ("group_id",),
    "13": ("ids",), "14": ("ids",), "15": ("ids",), "19": ("ids",),
    "m1": ("ids",), "m2": ("ids",), "m3": ("ids",), "m4": ("tags",),
    "f2": ("ids",), "f3": ("ids",), "f6": ("ids",),
    "s1": ("ids",), "s2": ("ids",), "m": ("member_id",),
}


class ModeForm(QWidget):
    """The inputs for one mode, built from its field list."""

    def __init__(self, mode: Mode, on_change):
        super().__init__()
        self.mode = mode
        self.getters = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if mode.note:
            note = QLabel(mode.note)
            note.setWordWrap(True)
            note.setStyleSheet("color: palette(mid);")
            layout.addWidget(note)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(form)

        for f in mode.fields:
            widget, getter = self._make(f, on_change)
            self.getters[f.key] = getter
            if f.tip:
                widget.setToolTip(f.tip)
            if f.kind == "bool":
                form.addRow("", widget)
            else:
                form.addRow(f.label + ":", widget)

        if not mode.fields and not mode.note:
            hint = QLabel("No options — press Run.")
            hint.setStyleSheet("color: palette(mid);")
            layout.addWidget(hint)
        layout.addStretch(1)

    def _make(self, f: F, on_change):
        if f.kind == "bool":
            box = QCheckBox(f.label)
            box.setChecked(bool(f.default))
            box.stateChanged.connect(on_change)
            return box, box.isChecked

        if f.kind == "choice":
            combo = QComboBox()
            for value, label in f.choices:
                combo.addItem(label, value)
            index = combo.findData(f.default)
            combo.setCurrentIndex(max(index, 0))
            combo.currentIndexChanged.connect(on_change)
            return combo, lambda: combo.currentData()

        if f.kind == "int":
            spin = QSpinBox()
            spin.setRange(0, 1_000_000)
            spin.setValue(int(f.default or 0))
            spin.valueChanged.connect(on_change)
            return spin, spin.value

        edit = QLineEdit(str(f.default or ""))
        edit.textChanged.connect(on_change)
        if f.tip:
            edit.setPlaceholderText(f.tip)

        if f.kind in ("file", "dir"):
            row = QWidget()
            box = QHBoxLayout(row)
            box.setContentsMargins(0, 0, 0, 0)
            box.addWidget(edit, 1)
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda: self._browse(edit, f.kind))
            box.addWidget(browse)
            return row, edit.text

        return edit, edit.text

    def _browse(self, edit: QLineEdit, kind: str):
        start = edit.text().strip() or str(PIXIVUTIL_DIR)
        picked = pick_path(self, "Choose a directory" if kind == "dir" else "Choose a file",
                           start, folder=(kind == "dir"))
        if picked:
            edit.setText(picked)

    def values(self) -> dict:
        return {key: getter() for key, getter in self.getters.items()}

    def missing(self) -> list[str]:
        values = self.values()
        labels = {f.key: f.label for f in self.mode.fields}
        return [labels.get(key, key) for key in REQUIRED.get(self.mode.op, ())
                if not str(values.get(key, "")).strip()]


# ── main window ───────────────────────────────────────────────────────────────

LEVEL_COLORS = {
    "error": "#e05561",
    "warn": "#d19a66",
    "ok": "#68b672",
    "info": None,      # leave at the palette's text colour
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PixivUtil2")
        self.resize(1180, 780)

        self.config_path = PIXIVUTIL_DIR / CONFIG_NAME
        self.config_values = read_ini(self.config_path)
        self.worker: RunWorker | None = None
        self._running = False
        self._setting_widgets = {}

        self._build_menu()
        self._build_ui()
        self._select_mode(0)
        self._append(f"Driving {SCRIPT} in {PIXIVUTIL_DIR}", "ok")
        if not (PIXIVUTIL_DIR / ".venv" / "bin" / "python").exists():
            self._append(
                f"No .venv in {PIXIVUTIL_DIR} — falling back to {find_python()}, "
                "which may not have PixivUtil2's dependencies. Run 'uv sync' there.",
                "warn")
        self._warn_about_other_sessions()

    # -- construction ---------------------------------------------------------

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("&File")

        act = QAction("Open download folder", self)
        act.triggered.connect(self._open_download_folder)
        file_menu.addAction(act)

        act = QAction("Open config.ini", self)
        act.triggered.connect(lambda: open_externally(self.config_path))
        file_menu.addAction(act)

        act = QAction("Use a different config.ini…", self)
        act.triggered.connect(self._pick_config)
        file_menu.addAction(act)

        file_menu.addSeparator()
        act = QAction("Choose PixivUtil2 folder…", self)
        act.triggered.connect(self._pick_pixivutil)
        file_menu.addAction(act)

        file_menu.addSeparator()
        act = QAction("Reload config from disk", self)
        act.triggered.connect(self._reload_config)
        file_menu.addAction(act)

        file_menu.addSeparator()
        act = QAction("Quit", self)
        act.triggered.connect(self.close)
        file_menu.addAction(act)

        help_menu = self.menuBar().addMenu("&Help")
        act = QAction("About", self)
        act.triggered.connect(self._about)
        help_menu.addAction(act)

    def _build_ui(self):
        left = self._build_left()
        left.setMinimumWidth(400)
        right = self._build_right()
        right.setMinimumWidth(420)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setSizes([470, 710])
        self.setCentralWidget(splitter)

        self.setStatusBar(QStatusBar())
        self.status_label = QLabel("Idle")
        self.counts_label = QLabel("")
        self.source_label = QLabel("")
        self.source_label.setStyleSheet("color: palette(mid);")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.source_label)
        self.statusBar().addPermanentWidget(self.counts_label)
        self._update_source_label()

    def _build_left(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_download_tab(), "Download")
        tabs.addTab(self._build_settings_tab(), "Settings")
        tabs.addTab(self._build_lists_tab(), "Lists")
        return tabs

    def _build_download_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        self.mode_combo = QComboBox()
        self.mode_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.mode_combo.setMinimumContentsLength(28)
        model = self.mode_combo.model()
        seen_groups = set()
        for mode in MODES:
            if mode.group not in seen_groups:
                seen_groups.add(mode.group)
                if self.mode_combo.count():
                    self.mode_combo.insertSeparator(self.mode_combo.count())
                self.mode_combo.addItem(f"— {mode.group} —", None)
                item = model.item(self.mode_combo.count() - 1)
                item.setEnabled(False)
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.mode_combo.addItem(f"    {mode.label}", mode.op)
        self.mode_combo.currentIndexChanged.connect(self._select_mode)

        chooser = QGroupBox("Operation")
        chooser_box = QVBoxLayout(chooser)
        chooser_box.addWidget(self.mode_combo)
        outer.addWidget(chooser)

        self.forms = QStackedWidget()
        self.form_index = {}
        for mode in MODES:
            form = ModeForm(mode, self._refresh_command)
            self.form_index[mode.op] = self.forms.count()
            self.forms.addWidget(form)

        options = QGroupBox("Options")
        options_box = QVBoxLayout(options)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.forms)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        options_box.addWidget(scroll)
        outer.addWidget(options, 1)

        self.command_view = QLineEdit()
        self.command_view.setReadOnly(True)
        self.command_view.setFont(QFont("monospace"))
        self.command_view.setToolTip("The command this will run")
        self.command_view.setMinimumWidth(200)
        command_box = QGroupBox("Command")
        command_layout = QVBoxLayout(command_box)
        command_layout.addWidget(self.command_view)
        outer.addWidget(command_box)

        buttons = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self._run)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        outer.addLayout(buttons)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        inner = QWidget()
        inner_box = QVBoxLayout(inner)
        for title, entries in SETTINGS_SCHEMA:
            group = QGroupBox(title)
            form = QFormLayout(group)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for section, key, label, kind, choices in entries:
                current = self.config_values.get((section, key), "")
                widget, getter, setter = self._make_setting(kind, current, choices)
                self._setting_widgets[(section, key)] = (getter, setter)
                if kind == "bool":
                    widget.setText(label)
                    form.addRow("", widget)
                else:
                    form.addRow(label + ":", widget)
            inner_box.addWidget(group)
        inner_box.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        note = QLabel("Only the keys shown here are rewritten; a backup is kept "
                      "as config.ini.gui-bak. A running download keeps the "
                      "config it started with.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        outer.addWidget(note)

        buttons = QHBoxLayout()
        save = QPushButton("Save to config.ini")
        save.clicked.connect(self._save_settings)
        revert = QPushButton("Reload")
        revert.clicked.connect(self._reload_config)
        buttons.addWidget(save)
        buttons.addWidget(revert)
        buttons.addStretch(1)
        outer.addLayout(buttons)
        return page

    def _make_setting(self, kind, current, choices):
        """Returns (widget, read value as a string, write value from a string)."""
        if kind == "bool":
            box = QCheckBox()
            box.setChecked(as_bool(current))
            return (box,
                    lambda: "True" if box.isChecked() else "False",
                    lambda v: box.setChecked(as_bool(v)))
        if kind == "choice":
            combo = QComboBox()
            for value, label in choices:
                combo.addItem(label, value)
            combo.setCurrentIndex(max(combo.findData(current), 0))
            return (combo,
                    lambda: combo.currentData(),
                    lambda v: combo.setCurrentIndex(max(combo.findData(v), 0)))
        if kind == "int":
            spin = QSpinBox()
            spin.setRange(0, 1_000_000)

            def set_int(value):
                try:
                    spin.setValue(int(value or 0))
                except ValueError:
                    spin.setValue(0)

            set_int(current)
            return spin, lambda: str(spin.value()), set_int

        edit = QLineEdit(current)
        if kind == "dir":
            row = QWidget()
            box = QHBoxLayout(row)
            box.setContentsMargins(0, 0, 0, 0)
            box.addWidget(edit, 1)
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda: self._browse_into(edit))
            box.addWidget(browse)
            return row, edit.text, edit.setText
        return edit, edit.text, edit.setText

    def _browse_into(self, edit: QLineEdit):
        picked = pick_path(self, "Choose a directory",
                           edit.text().strip() or str(PIXIVUTIL_DIR), folder=True)
        if picked:
            edit.setText(picked)

    def _build_lists_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        self.list_combo = QComboBox()
        self.list_combo.currentIndexChanged.connect(self._load_list_file)
        outer.addWidget(self.list_combo)

        self.list_edit = QPlainTextEdit()
        self.list_edit.setFont(QFont("monospace"))
        outer.addWidget(self.list_edit, 1)

        self.list_status = QLabel("")
        self.list_status.setStyleSheet("color: palette(mid);")
        outer.addWidget(self.list_status)

        buttons = QHBoxLayout()
        save = QPushButton("Save")
        save.clicked.connect(self._save_list_file)
        reload_button = QPushButton("Reload")
        reload_button.clicked.connect(self._load_list_file)
        buttons.addWidget(save)
        buttons.addWidget(reload_button)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        self._populate_list_files()
        return page

    def _build_right(self) -> QWidget:
        panel = QWidget()
        outer = QVBoxLayout(panel)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("monospace"))
        self.log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.log.document().setMaximumBlockCount(8000)

        self.preview = QLabel("No preview yet")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(180)
        self.preview.setStyleSheet("color: palette(mid);")
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.preview_path = ""

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(self.log)
        vertical.addWidget(self.preview)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 1)
        vertical.setSizes([540, 220])
        outer.addWidget(vertical, 1)

        self.file_bar = QProgressBar()
        self.file_bar.setFormat("%p%  —  current file")
        self.overall_bar = QProgressBar()
        self.overall_bar.setFormat("%v of %m")
        outer.addWidget(self.file_bar)
        outer.addWidget(self.overall_bar)

        send_row = QHBoxLayout()
        self.send_edit = QLineEdit()
        self.send_edit.setPlaceholderText("Answer a prompt from the downloader…")
        self.send_edit.setEnabled(False)
        self.send_edit.returnPressed.connect(self._send_input)
        self.send_button = QPushButton("Send")
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self._send_input)
        clear = QPushButton("Clear log")
        clear.clicked.connect(self._clear_log)
        send_row.addWidget(self.send_edit, 1)
        send_row.addWidget(self.send_button)
        send_row.addWidget(clear)
        outer.addLayout(send_row)
        return panel

    # -- mode selection -------------------------------------------------------

    def current_mode(self) -> Mode:
        return MODE_BY_OP[self.mode_combo.currentData()]

    def _select_mode(self, index: int):
        op = self.mode_combo.itemData(index)
        if op is None:                      # a group header — skip to the next real one
            for i in range(index + 1, self.mode_combo.count()):
                if self.mode_combo.itemData(i) is not None:
                    self.mode_combo.setCurrentIndex(i)
                    return
            return
        self.forms.setCurrentIndex(self.form_index[op])
        self._refresh_command()

    def _config_arg(self) -> str:
        default = PIXIVUTIL_DIR / CONFIG_NAME
        return "" if self.config_path == default else str(self.config_path)

    def _build_job(self) -> Job:
        ctx = {"overwrite": as_bool(
            self.config_values.get(("DownloadControl", "overwrite"), "False"))}
        return self.current_mode().build(self.forms.currentWidget().values(), ctx)

    def _refresh_command(self, *_):
        import shlex
        try:
            job = self._build_job()
            argv = command_line(job, self._config_arg())
        except Exception as ex:             # a half-typed value, nothing to report yet
            self.command_view.setText(f"({ex})")
            return
        text = shlex.join(argv)
        if job.stdin or job.interactive:
            answers = ([job.op] + job.stdin) if job.interactive else job.stdin
            text += "   « " + " / ".join(a if a else "<blank>" for a in answers)
        self.command_view.setText(text)
        self.command_view.setCursorPosition(0)

    # -- running --------------------------------------------------------------

    def _run(self):
        if self._running:
            return
        form = self.forms.currentWidget()
        missing = form.missing()
        if missing:
            QMessageBox.warning(self, "Missing input",
                                "Please fill in: " + ", ".join(missing))
            return

        mode = self.current_mode()
        if mode.op == "u":
            confirm = QMessageBox.question(
                self, "Re-encode every stored ugoira?",
                "This re-encodes and overwrites all stored ugoira and the files "
                "derived from them. It cannot be undone.\n\nProceed?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if confirm != QMessageBox.StandardButton.Yes:
                return

        others = running_sessions()
        if others:
            confirm = QMessageBox.question(
                self, "Another PixivUtil2 is running",
                "Another PixivUtil2 process is already running (" +
                "; ".join(others) + ").\n\nThey share db.sqlite and the log "
                "files, so running two at once can corrupt both.\n\nStart anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if confirm != QMessageBox.StandardButton.Yes:
                return

        if self.worker is not None and self.worker.isRunning():
            self.worker.wait(3000)

        job = self._build_job()
        self.overall_bar.setRange(0, 1)
        self.overall_bar.setValue(0)
        self.file_bar.setRange(0, 100)
        self.file_bar.setValue(0)
        self.counts_label.setText("")
        self._append(f"$ {self.command_view.text()}", "ok")

        self.worker = RunWorker(job, self._config_arg())
        self.worker.line.connect(self._on_line)
        self.worker.title.connect(self._on_title)
        self.worker.overall.connect(self._on_overall)
        self.worker.file_progress.connect(self._on_file_progress)
        self.worker.preview.connect(self._on_preview)
        self.worker.counts.connect(self._on_counts)
        self.worker.done.connect(self._on_done)
        self.worker.start()
        self._running = True

        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.send_edit.setEnabled(True)
        self.send_button.setEnabled(True)
        self.mode_combo.setEnabled(False)
        self.status_label.setText("Starting…")

    def _stop(self):
        if not self._running or self.worker is None:
            return
        self.stop_button.setEnabled(False)
        self.status_label.setText("Stopping…")
        self._append("Stopping — sending Ctrl-C to the downloader…", "warn")
        self.worker.cancel()

    def _send_input(self):
        if not self._running or self.worker is None:
            return
        text = self.send_edit.text()
        if self.worker.send(text):
            self._append(f"> {text}", "ok")
            self.send_edit.clear()
        else:
            self._append("Could not send — the downloader is not reading input.", "warn")

    def _clear_log(self):
        self.log.clear()

    # -- worker signals -------------------------------------------------------

    def _on_line(self, text: str, level: str, transient: bool):
        if transient:
            self.status_label.setText(text[:300])
        else:
            self._append(text, level)

    def _append(self, text: str, level: str = "info"):
        bar = self.log.verticalScrollBar()
        hbar = self.log.horizontalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        was_at = bar.value()
        color = LEVEL_COLORS.get(level)
        self.log.setTextColor(QColor(color) if color else self.palette().text().color())
        self.log.append(text)

        # append() leaves the cursor at the end of the new line; with wrapping
        # off, a long path then drags the view sideways and hides the start of
        # every line. Park the cursor at column 0 instead — setting it is what
        # drives Qt's own "keep the cursor visible" scroll, so resetting the
        # scrollbar afterwards would be undone.
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        self.log.setTextCursor(cursor)
        hbar.setValue(hbar.minimum())
        bar.setValue(bar.maximum() if at_bottom else was_at)

    def _on_title(self, title: str):
        self.setWindowTitle(f"PixivUtil2 — {title}" if title else "PixivUtil2")

    def _on_overall(self, current: int, total: int):
        self.overall_bar.setRange(0, max(total, 1))
        self.overall_bar.setValue(current)

    def _on_file_progress(self, percent: int, caption: str):
        caption = caption.replace("%", "")
        if percent < 0:
            self.file_bar.setRange(0, 0)
        else:
            self.file_bar.setRange(0, 100)
            self.file_bar.setValue(percent)
        self.file_bar.setFormat(f"%p%  —  {caption}" if caption else "%p%")

    def _on_counts(self, downloaded: int, existing: int, errors: int):
        self.counts_label.setText(
            f"downloaded {downloaded}   ·   already had {existing}   ·   errors {errors}")

    def _on_preview(self, path: str):
        target = Path(path)
        if not target.is_absolute():
            target = PIXIVUTIL_DIR / target
        self.preview_path = str(target)
        self._preview_pixmap = None
        if target.suffix.lower() in IMAGE_SUFFIXES and target.exists():
            pixmap = QPixmap(str(target))
            if not pixmap.isNull():
                self._preview_pixmap = pixmap
        self._show_preview()

    def _show_preview(self):
        if getattr(self, "_preview_pixmap", None) is None:
            self.preview.setText(Path(self.preview_path).name if self.preview_path
                                 else "No preview yet")
            return
        self.preview.setPixmap(self._preview_pixmap.scaled(
            self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._show_preview()

    def _on_done(self, code: int, message: str):
        level = "ok" if code == 0 else "error"
        self._append(message, level)
        self.status_label.setText(message)
        self.file_bar.setRange(0, 100)
        # The worker object stays referenced until the next run replaces it —
        # dropping a QThread from inside its own signal handler is a crash.
        self._running = False
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.send_edit.setEnabled(False)
        self.send_button.setEnabled(False)
        self.mode_combo.setEnabled(True)
        self.setWindowTitle("PixivUtil2")

    # -- config ---------------------------------------------------------------

    def _save_settings(self):
        changes = {}
        for location, (getter, _setter) in self._setting_widgets.items():
            value = getter()
            if value != self.config_values.get(location, ""):
                changes[location] = value
        if not changes:
            QMessageBox.information(self, "Settings", "Nothing changed.")
            return
        try:
            patch_ini(self.config_path, changes)
        except OSError as ex:
            QMessageBox.critical(self, "Settings", f"Could not write config.ini:\n{ex}")
            return
        self.config_values = read_ini(self.config_path)
        self._populate_list_files()
        self._append(f"Saved {len(changes)} setting(s) to {self.config_path.name}.", "ok")
        QMessageBox.information(
            self, "Settings",
            f"Saved {len(changes)} setting(s). A backup of the previous file is "
            f"in {self.config_path.name}.gui-bak.")

    def _reload_config(self):
        """Re-read config.ini and push it back into the Settings tab."""
        self.config_values = read_ini(self.config_path)
        for location, (_getter, setter) in self._setting_widgets.items():
            setter(self.config_values.get(location, ""))
        self._populate_list_files()
        self._append(f"Reloaded {self.config_path}.", "ok")

    def _pick_config(self):
        picked = pick_path(self, "Choose a config.ini", str(self.config_path.parent),
                           filters=["INI files (*.ini)", "All files (*)"],
                           preselect=self.config_path.name)
        if not picked:
            return
        self.config_path = Path(picked)
        self._reload_config()
        self._refresh_command()
        self._append(f"Using config {self.config_path}.", "ok")

    def _pick_pixivutil(self):
        """Point the GUI at a different PixivUtil2 checkout, without a restart."""
        global PIXIVUTIL_DIR
        if self._running:
            QMessageBox.warning(self, "Still downloading",
                                "Stop the current download first.")
            return
        picked = ask_for_pixivutil(self, str(PIXIVUTIL_DIR))
        if picked is None or picked == PIXIVUTIL_DIR:
            return
        PIXIVUTIL_DIR = picked
        remember_dir(picked)
        self.config_path = picked / CONFIG_NAME
        self._reload_config()
        self._refresh_command()
        self._update_source_label()
        self._append(f"Now driving {SCRIPT} in {picked}.", "ok")

    def _update_source_label(self):
        self.source_label.setText(f"PixivUtil2: {PIXIVUTIL_DIR.name}")
        self.source_label.setToolTip(
            f"{PIXIVUTIL_DIR}\nInterpreter: {find_python()}")

    def _open_download_folder(self):
        root = self.config_values.get(("Settings", "rootDirectory"), "") or str(PIXIVUTIL_DIR)
        path = Path(root)
        if not path.is_absolute():
            path = PIXIVUTIL_DIR / path
        if not open_externally(path):
            QMessageBox.warning(self, "Open folder",
                                f"Could not open {path} in a file manager.")

    # -- list files -----------------------------------------------------------

    def _resolve(self, raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else PIXIVUTIL_DIR / path

    def _populate_list_files(self):
        download_list_dir = self.config_values.get(
            ("Settings", "downloadListDirectory"), "") or "."
        fanbox_list = self.config_values.get(
            ("FANBOX", "listPathFanbox"), "") or "listfanbox.txt"
        entries = [
            ("Pixiv member list (list.txt)", self._resolve(download_list_dir) / "list.txt"),
            ("Tag list (tags.txt)", self._resolve("tags.txt")),
            ("FANBOX list", self._resolve(fanbox_list)),
            ("Blacklisted tags", self._resolve("blacklist_tags.txt")),
            ("Blacklisted members", self._resolve("blacklist_members.txt")),
            ("Blacklisted titles", self._resolve("blacklist_titles.txt")),
            ("Suppressed tags", self._resolve("suppress_tags.txt")),
        ]
        current = self.list_combo.currentData()
        self.list_combo.blockSignals(True)
        self.list_combo.clear()
        for label, path in entries:
            self.list_combo.addItem(f"{label} — {path}", str(path))
        index = self.list_combo.findData(current)
        self.list_combo.setCurrentIndex(max(index, 0))
        self.list_combo.blockSignals(False)
        self._load_list_file()

    def _load_list_file(self, *_):
        raw = self.list_combo.currentData()
        if not raw:
            return
        path = Path(raw)
        try:
            self.list_edit.setPlainText(path.read_text(encoding="utf-8"))
            self.list_status.setText(f"{path} — {path.stat().st_size} bytes")
        except FileNotFoundError:
            self.list_edit.setPlainText("")
            self.list_status.setText(f"{path} — does not exist yet, saving will create it")
        except (OSError, UnicodeDecodeError) as ex:
            self.list_edit.setPlainText("")
            self.list_status.setText(f"{path} — cannot read: {ex}")

    def _save_list_file(self):
        raw = self.list_combo.currentData()
        if not raw:
            return
        path = Path(raw)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.list_edit.toPlainText(), encoding="utf-8")
        except OSError as ex:
            QMessageBox.critical(self, "Lists", f"Could not write {path}:\n{ex}")
            return
        self.list_status.setText(f"{path} — saved")

    # -- misc -----------------------------------------------------------------

    def _warn_about_other_sessions(self):
        others = running_sessions()
        if not others:
            return
        QMessageBox.warning(
            self, "PixivUtil2 is already running",
            "Another PixivUtil2 process is running (" + "; ".join(others) + ").\n\n"
            "It shares db.sqlite and the log files with anything started here, "
            "so let it finish before starting a download.")

    def _about(self):
        QMessageBox.about(
            self, "About",
            "<b>PixivUtil2 GUI</b><br><br>"
            "A standalone PyQt6 front end. PixivUtil2 itself is a separate "
            "checkout, driven as a child process.<br><br>"
            f"Version {VERSION}<br>"
            f"GUI: {GUI_DIR}<br>"
            f"PixivUtil2: {PIXIVUTIL_DIR}<br>"
            f"Interpreter: {find_python()}<br>"
            f"Config: {self.config_path}")

    def closeEvent(self, event):
        if self._running and self.worker is not None:
            confirm = QMessageBox.question(
                self, "Still downloading",
                "A download is still running. Stop it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if confirm != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(16000)   # cancel() escalates to SIGKILL by ~13 s
        event.accept()


class LocateDialog(QDialog):
    """
    Ask for the PixivUtil2 checkout.

    A bare QFileDialog was not good enough: in an AppImage it opens on a
    read-only /tmp mount, and a directory-only picker gives no way to type or
    paste a path without knowing the Ctrl+L trick. This offers all three —
    paste, drag from a file manager, or browse — and says whether what it has
    is actually a checkout before the OK button does anything.
    """

    def __init__(self, parent=None, start: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Locate PixivUtil2")
        self.setMinimumWidth(620)
        self.setAcceptDrops(True)
        self._resolved: Path | None = None

        layout = QVBoxLayout(self)
        blurb = QLabel(
            f"This is only the front end — it needs a <b>{SCRIPT}</b> checkout "
            "to drive.<p>Paste the path below, drag the folder in from your "
            f"file manager, or browse for it. Either the folder or {SCRIPT} "
            "itself will do.")
        blurb.setWordWrap(True)
        blurb.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(blurb)

        row = QHBoxLayout()
        self.edit = QLineEdit(start)
        self.edit.setPlaceholderText(f"/path/to/PixivUtil2   (or …/{SCRIPT})")
        self.edit.textChanged.connect(self._revalidate)
        browse_file = QPushButton(f"Browse for {SCRIPT}…")
        browse_file.clicked.connect(self._browse_file)
        browse_dir = QPushButton("Browse for folder…")
        browse_dir.clicked.connect(self._browse_dir)
        row.addWidget(self.edit, 1)
        row.addWidget(browse_file)
        row.addWidget(browse_dir)
        layout.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._revalidate()
        self.edit.setFocus()

    # -- validation -----------------------------------------------------------

    def _revalidate(self, *_):
        self._resolved = normalise_checkout(self.edit.text())
        typed = self.edit.text().strip()
        if self._resolved is not None:
            self.status.setText(f"✓ Found {SCRIPT} in {self._resolved}")
            self.status.setStyleSheet("color: #2e7d32;")
        elif not typed:
            self.status.setText("Waiting for a path…")
            self.status.setStyleSheet("color: palette(mid);")
        else:
            self.status.setText(
                f"✗ No {SCRIPT} with a handler/ directory there.")
            self.status.setStyleSheet("color: #c62828;")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self._resolved is not None)

    # -- input ----------------------------------------------------------------

    def _browse_file(self):
        start = browse_start_dir(self.edit.text())
        picked = pick_path(self, f"Select {SCRIPT}", start,
                           filters=["Python scripts (*.py)", "All files (*)"],
                           preselect=SCRIPT)
        if picked:
            self.edit.setText(picked)

    def _browse_dir(self):
        picked = pick_path(self, "Select the PixivUtil2 folder",
                           browse_start_dir(self.edit.text()), folder=True)
        if picked:
            self.edit.setText(picked)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.edit.setText(urls[0].toLocalFile())
            event.acceptProposedAction()

    def value(self) -> Path | None:
        return self._resolved


def ask_for_pixivutil(parent=None, start: str = "") -> Path | None:
    dialog = LocateDialog(parent, start)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return dialog.value()
    return None


def report_paths(explicit: str = "") -> int:
    """--check: print what the program resolved, without opening a window."""
    global PIXIVUTIL_DIR
    found = locate_pixivutil(explicit)
    print(f"PixivUtilGUI {VERSION}")
    print(f"  frozen        : {FROZEN}")
    print(f"  program dir   : {GUI_DIR}")
    print(f"  settings file : {SETTINGS_FILE}"
          f"{'' if SETTINGS_FILE.exists() else '  (not written yet)'}")
    for index, base in enumerate(search_bases()):
        print(f"  {'search bases  :' if index == 0 else ' ' * 17} {base}")
    if found is None:
        print("  PixivUtil2    : NOT FOUND")
        return 1
    PIXIVUTIL_DIR = found
    print(f"  PixivUtil2    : {found}")
    print(f"  interpreter   : {find_python()}")
    print(f"  config.ini    : {found / CONFIG_NAME}"
          f"{'' if (found / CONFIG_NAME).exists() else '  (missing)'}")
    return 0


def main():
    argv = list(sys.argv[1:])
    explicit = ""
    if "--pixivutil" in argv:
        index = argv.index("--pixivutil")
        explicit = argv[index + 1] if index + 1 < len(argv) else ""
        if not explicit or not is_pixivutil_dir(explicit):
            print(f"--pixivutil: {explicit or '(missing)'} is not a "
                  f"PixivUtil2 checkout", file=sys.stderr)
            return 2

    if "--check" in argv:
        return report_paths(explicit)
    if "--version" in argv:
        print(f"PixivUtilGUI {VERSION}")
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName("PixivUtilGUI")
    app.setApplicationDisplayName("PixivUtil2")

    global PIXIVUTIL_DIR
    found = locate_pixivutil(explicit)
    if found is None:
        found = ask_for_pixivutil()
        if found is None:
            return 1
        remember_dir(found)
    PIXIVUTIL_DIR = found

    icon_path = PIXIVUTIL_DIR / "icon2.ico"
    if icon_path.exists():
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
