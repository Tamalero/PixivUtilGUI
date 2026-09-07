#!/usr/bin/env fish
#
# Build a self-contained AppImage (Type 2) with GitHub autoupdate wired in.
#
# Fish, because this only ever runs on the author's machine. The AppRun inside
# the image is bash — that one runs on the user's.
#
# Needs: pyinstaller, appimagetool, zsyncmake.
# Output: PixivUtilGUI-x86_64.AppImage (+ .zsync, next to this script)

set -l here (dirname (realpath (status --current-filename)))
cd $here; or exit 1

set -l APP     PixivUtilGUI
set -l VERSION (python3 -c "import gui; print(gui.VERSION)")
set -l ARCH    x86_64
set -l GH_USER Tamalero
set -l GH_REPO PixivUtilGUI

# gh-releases-zsync makes the built image check this repo's *latest* release on
# every update, so 1.0.0 already knows how to find 1.0.1. The filename here must
# match the asset name uploaded to the release, or the check 404s.
set -l UPDATE_INFO "gh-releases-zsync|$GH_USER|$GH_REPO|latest|$APP-$ARCH.AppImage.zsync"

echo "── Building $APP $VERSION ($ARCH)"

for tool in pyinstaller appimagetool zsyncmake
    if not command -q $tool
        echo "  missing: $tool" >&2
        exit 1
    end
end

# Never rm -rf on this machine — move the old build aside instead.
for stale in build dist $APP.AppDir
    if test -e $stale
        set -l aside "$stale.old-"(date +%s)
        mv $stale $aside
        echo "  moved previous $stale to $aside"
    end
end
mkdir -p $APP.AppDir/usr/bin

echo "── Icons"
QT_QPA_PLATFORM=offscreen python3 packaging/create_icon.py; or exit 1

echo "── PyInstaller"
# PixivUtil2 is NOT bundled: it stays an external checkout that the GUI drives
# with that checkout's own interpreter, so none of its dependencies belong here.
pyinstaller \
    --noconfirm --clean --log-level WARN \
    --name pixivutilgui \
    --windowed \
    --paths . \
    --hidden-import gui \
    --exclude-module PyQt6.QtWebEngineWidgets \
    --exclude-module PyQt6.QtWebEngineCore \
    --exclude-module PyQt6.QtWebEngineQuick \
    --exclude-module tkinter \
    --exclude-module PIL \
    --exclude-module numpy \
    packaging/entry.py; or exit 1

echo "── AppDir"
cp -a dist/pixivutilgui/. $APP.AppDir/usr/bin/
cp packaging/AppRun $APP.AppDir/AppRun
chmod +x $APP.AppDir/AppRun
cp packaging/pixivutilgui.desktop $APP.AppDir/
cp packaging/pixivutilgui.png $APP.AppDir/
cp packaging/pixivutilgui.png $APP.AppDir/.DirIcon

# appimagetool wants the icon discoverable the freedesktop way as well, at
# every size shipped — desktops pick whichever fits the slot they are filling.
mkdir -p $APP.AppDir/usr/share/icons/hicolor/512x512/apps
cp packaging/pixivutilgui.png $APP.AppDir/usr/share/icons/hicolor/512x512/apps/
mkdir -p $APP.AppDir/usr/share/icons/hicolor/256x256/apps
cp packaging/pixivutilgui-256.png \
   $APP.AppDir/usr/share/icons/hicolor/256x256/apps/pixivutilgui.png
mkdir -p $APP.AppDir/usr/share/applications
cp packaging/pixivutilgui.desktop $APP.AppDir/usr/share/applications/

echo "── appimagetool (update info: $UPDATE_INFO)"
for stale in $APP-$ARCH.AppImage $APP-$ARCH.AppImage.zsync
    if test -e $stale
        mv $stale "$stale.old-"(date +%s)
    end
end
set -x ARCH $ARCH
set -x VERSION $VERSION
appimagetool --no-appstream -u "$UPDATE_INFO" $APP.AppDir $APP-$ARCH.AppImage; or exit 1

if not test -f $APP-$ARCH.AppImage.zsync
    echo "  .zsync was not produced — autoupdate would not work" >&2
    exit 1
end

echo
echo "── Done"
ls -lh $APP-$ARCH.AppImage $APP-$ARCH.AppImage.zsync
echo
echo "Release both files as assets on the '$VERSION' tag of $GH_USER/$GH_REPO."
echo "The .zsync must keep the name $APP-$ARCH.AppImage.zsync or updates 404."
