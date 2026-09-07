# PixivUtil2 GUI

A PyQt6 front end for [PixivUtil2](https://github.com/Nandaka/PixivUtil2).

PixivUtil2 is an interactive, menu-driven console program. This gives it a
window: pick an operation, fill in a form, press Run, and watch the log,
progress bars and a preview of the last saved file.

**This project contains no copy of PixivUtil2 and changes nothing inside it.**
It drives an existing checkout as a child process, so the downloader can be
updated, rebased or re-cloned without touching the GUI — and nothing here can
end up in a pull request against upstream.

## Install

### AppImage (recommended)

Grab `PixivUtilGUI-x86_64.AppImage` from the
[latest release](https://github.com/Tamalero/PixivUtilGUI/releases/latest),
then:

```bash
chmod +x PixivUtilGUI-x86_64.AppImage
./PixivUtilGUI-x86_64.AppImage
```

It bundles Python and Qt, so only a PixivUtil2 checkout is needed alongside it.

**Updating** is built in (AppImage Type 2 + zsync against this repo's latest
release), so only the changed blocks are downloaded:

```bash
appimageupdatetool ./PixivUtilGUI-x86_64.AppImage
```

On Arch that tool is in the AUR (`appimageupdate` or `appimageupdate-bin`); it
is not in the official repositories. Some desktops also offer "Update" in the
AppImage's own context menu once it has been integrated, which does the same
thing.

### From source

```fish
git clone https://github.com/Tamalero/PixivUtilGUI
cd PixivUtilGUI
./run-gui.fish
```

## Requirements

- Python 3.11+
- PyQt6 — on Arch/CachyOS: `sudo pacman -S python-pyqt6`
- A working PixivUtil2 checkout, with its own dependencies installed
  (`uv sync`, or a `.venv` in that folder)

## Running

```fish
./run-gui.fish
```

On first start the GUI looks for PixivUtil2 in this order:

1. `--pixivutil /path/to/PixivUtil2`
2. the `PIXIVUTIL_DIR` environment variable
3. the folder remembered in `$XDG_CONFIG_HOME/pixivutil-gui/settings.ini`
   (usually `~/.config/pixivutil-gui/settings.ini`, written on first use)
4. sibling folders — `../PixivUtilFix`, `../PixivUtil2`, `~/PixivUtil2`, …

If none of those match it asks, and remembers the answer. The prompt takes a
pasted path, a folder dragged in from your file manager, or a browse — and it
accepts either the folder or `PixivUtil2.py` inside it. Change it later with
**File → Choose PixivUtil2 folder…**.

When run as an AppImage the sibling search uses the directory the AppImage
itself sits in, and the one you launched it from — not its temporary mount
point — so keeping the AppImage next to your PixivUtil2 folder is enough.

To see what it resolved, without opening a window:

```bash
./PixivUtilGUI-x86_64.AppImage --check
```

It prints the program directory, the settings file, every directory searched,
and the checkout and interpreter it settled on. Exit status is 1 if no checkout
was found.

The GUI runs on the system Python, but launches the downloader with the
checkout's own `.venv/bin/python`, so PyQt6 never has to be installed into
PixivUtil2's environment.

## What it covers

Every operation from the console menu — Pixiv downloads (member, image, tags,
list, bookmarks, rankings, manga and novel series, groups, unlisted works),
metadata, FANBOX, Sketch, batch jobs, exports, list imports and ugoira
re-encoding.

Most run non-interactively as `PixivUtil2.py -s <op> -x [options] -- [args]`,
and the exact command is shown before you press Run. A few operations do not
support that and are driven through the console menu over stdin instead. A
**Send** box can answer any prompt by hand.

Leaving **End page** at 0 means no limit, so the form says plainly that it will
keep going until there is nothing left.

Also included:

- **Comics** — bundle each artist folder into a single **CBZ** (or CBR, if the
  `rar` command is installed) after a download, or build them on demand for any
  folder. Pages are ordered numerically, so `_p2` comes before `_p10`. Nothing
  in the download folder is moved or deleted.
- **Settings** — edits the common keys of the checkout's `config.ini`. Only the
  keys shown are rewritten, line by line, and the previous file is kept as
  `config.ini.gui-bak`. The browser to impersonate is a dropdown, filled from
  the `curl_cffi` in your own checkout, with a Custom option.
- **Lists** — edits `list.txt`, the tag list, the FANBOX list and the blacklist
  files at the paths the config resolves to.
- A warning when another PixivUtil2 is already running, since they share
  `db.sqlite` and the log files.
- **Stop** sends Ctrl-C to the downloader and escalates if it does not exit.

## Building the AppImage

```fish
./build-appimage.fish
```

Needs `pyinstaller`, `appimagetool` and `zsyncmake`. It regenerates the icons,
bundles the GUI (never PixivUtil2 itself), and bakes in the
`gh-releases-zsync|Tamalero|PixivUtilGUI|latest|…` update string. Attach both
the `.AppImage` and the `.AppImage.zsync` to the GitHub release — the zsync
file has to keep its exact name or update checks 404.

## Notes

Log lines are parsed for progress, so they follow whatever the console prints.
If a future PixivUtil2 changes its output wording, the progress bars and the
preview may need the patterns near the top of `gui.py` updated; the log itself
keeps working regardless.
