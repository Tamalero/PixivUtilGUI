# PixivUtil2 GUI — Claude Context

PyQt6 front end for [PixivUtil2](https://github.com/Nandaka/PixivUtil2).

**This is its own git repo.** It lives at `…/Personal/MediaWebsites/PixivUtilGUI/`,
a sibling of the other downloaders. It was split out of the `PixivUtilFix/`
working tree on **2026-09-07**, where it had been living as an untracked
`gui.py`. See `../../CLAUDE.md` and the workspace root `CLAUDE.md` for the
isolation and shell-dialect rules.

- **Platform:** Arch/CachyOS, x86_64, system Python 3 + `python-pyqt6` via pacman
- **Remote:** `origin` → **https://github.com/Tamalero/PixivUtilGUI** (public)
- **Released:** AppImage (Type 2) with `gh-releases-zsync` autoupdate — see Packaging
- **Git author:** pinned in this repo's local config. This is a `Personal/`
  project, so it takes the **personal** address, not the Acorn one — see the
  git-author hard rule in the workspace root `CLAUDE.md`. Do not spell the
  address out in tracked files: this repo is public.
- **Launcher:** `./run-gui.fish` (fish — everything Cesar runs on the desktop is fish)

---

## ⚠️ The whole point: never touch the PixivUtil2 checkout

`PixivUtilFix/` is a **fork of PixivUtil2 whose branches get PR'd upstream**.
Anything added inside it risks ending up in a public pull request. So:

> **Never add, move or modify files inside the PixivUtil2 checkout from this
> project.** The GUI only *reads* `config.ini` and the list files, *writes*
> `config.ini` when the user presses Save in the Settings tab (keeping a
> `.gui-bak`), and spawns `PixivUtil2.py` as a child process. Nothing else.

That is also why PyQt6 is **not** added to PixivUtil2's `pyproject.toml`: the
GUI runs on the system Python and launches the downloader with the checkout's
own `.venv/bin/python`.

## Finding the checkout

`locate_pixivutil()` resolves it once at startup, most explicit first:
`--pixivutil <path>` → `$PIXIVUTIL_DIR` → the path remembered in
`pixivutil-gui.ini` → the `CANDIDATE_DIRS` siblings → ask the user.
`pixivutil-gui.ini` is gitignored — it is a per-machine path, not shared state.
`PIXIVUTIL_DIR` is a module global, re-pointed at runtime by
**File → Choose PixivUtil2 folder…**, so read it at call time, never cache it.

A folder counts as a checkout only if it has both `PixivUtil2.py` and a
`handler/` directory (`is_pixivutil_dir()`).

`gui.py --check` prints the whole resolution without starting Qt, and is the
only way to inspect this logic inside a built AppImage — use it after any
change to the discovery code, from at least: the AppImage's own directory, an
unrelated directory (must report NOT FOUND), and a directory next to a checkout
(exercises `$OWD`).

Inside an AppImage `GUI_DIR` is a `/tmp/.mount_*` path that changes every run,
so **nothing may be written next to the program** — that is why the settings
file is XDG, not in-tree. It is also why `browse_start_dir()` refuses to open a
file dialog there: the mount holds only `bin/` and `share/`, which looks like an
empty, unnavigable filesystem to whoever is clicking.

### First-run locator — two bugs fixed in 1.0.1

1. **`QT_PLUGIN_PATH` pointed at a directory that does not exist.** PyInstaller
   6 puts the bundle under `_internal/`, so the plugins are at
   `usr/bin/_internal/PyQt6/Qt6/plugins`; AppRun (adapted from Poipiku, built
   with an older PyInstaller) exported the pre-`_internal` path. Qt then found
   no `platformthemes` and fell back to its own plain dialogs even though
   `KDEPlasmaPlatformTheme6.so` **is** bundled. AppRun now probes for
   `_internal` and only exports the variable if the directory really exists —
   an unset `QT_PLUGIN_PATH` is better than a wrong one. **Re-check this after
   any PyInstaller major upgrade.**
2. **The dialog opened on the read-only mount** and was directory-only, with a
   title naming a `.py` file. Replaced by `LocateDialog`, which takes a pasted
   path, a drag from a file manager, or either kind of browse, and validates
   live. `normalise_checkout()` accepts the folder, `PixivUtil2.py` itself, a
   quoted path, a `file://` URL (what Dolphin's location bar copies) or a
   subdirectory of the checkout.

Never make the first run depend on a working native file dialog: an AppImage
lands on desktops whose Qt integration is not bundled.

## How the CLI is driven

Modes live in one declarative `MODES` table. Most run as
`-s <op> -x [options] -- [args]`; `--` matters so a tag starting with `-`
survives optparse.

Seven modes **cannot** use `-s` and are fed through the console menu on stdin
(`Job.interactive`):

| Mode | Why |
|---|---|
| `f6`, `u`, `i` | missing from the CLI's `__valid_options`, so `-s` is rejected outright |
| `14`, `15` | accept `-s` but ignore argv and prompt anyway |
| `16`, `17`, `18` | the `-s` path drops the ranking date; `18` also passes `type_mode="illusts"` (not a valid mode) and `max_page` as a string |

Those are upstream quirks, verified against the source — do not "simplify" them
back to `-s` without re-checking `PixivUtil2.py`.

`-x` (exit when done) is always passed, otherwise the CLI blocks on
`input('press enter to exit.')` and the run never finishes.

## Reading the output

Run the child with `-u`. colorama already strips SGR codes when stdout is a
pipe, but Python would otherwise block-buffer and the log would arrive in lumps.

Parsed from the console text: the OSC console title (`\33]0;…\a` → window
title), `[n of m]` → overall bar, `print_progress()`'s **40-cell** bar → file
bar (the fill ratio is the only reliable percentage; the sizes beside it are
pre-formatted strings), and `Download done ==> <path>` → preview + counter.

The reader uses `select()` with a 50 ms timeout so it can tell "more output is
coming" from "sitting at a prompt". Partial lines are throttled to ~25/s while
streaming and flushed when it goes quiet — needed because `PixivHelper.safePrint`
emits **one write per word**, and because a prompt never ends in a newline.

## Stopping

`cancel()` is called from the UI thread and must not block: it sends SIGINT to
the child's **process group** (`start_new_session=True` at spawn) and returns.
Escalation to SIGTERM at 8 s and SIGKILL at 13 s happens in `_escalate()`,
called from the reader loop. Verified against a child that ignores both signals.

Do not drop the `RunWorker` reference inside its own `done` handler — destroying
a still-running QThread crashes. `_running` tracks state; the object is only
replaced on the next run, after `wait()`.

## Testing

No display needed: `QT_QPA_PLATFORM=offscreen` plus `widget.grab().save(...)`
renders the window to PNG. `compile()` the file rather than `ast.parse()` —
only `compile()` catches misplaced `global` declarations.

Check for a live session before any test run that hits the network:
`ps -eo pid,etimes,cmd | grep "[P]ixivUtil2.py"`. Two processes share
`db.sqlite` and the rotating log.
