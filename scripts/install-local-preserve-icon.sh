#!/bin/bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Apple Music Downloader.app"
INSTALLED="/Applications/$APP_NAME"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$ROOT_DIR/local-backups/$STAMP"
DIST_DIR="$ROOT_DIR/dist-local-$STAMP"
mkdir -p "$BACKUP_DIR"
if [[ ! -d "$INSTALLED" ]]; then
  echo "Existing app not found: $INSTALLED" >&2
  exit 1
fi
# Copy the full existing app, including its custom icon and metadata.
/usr/bin/ditto "$INSTALLED" "$BACKUP_DIR/$APP_NAME"
ICON_NAME=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIconFile' "$INSTALLED/Contents/Info.plist")
[[ "$ICON_NAME" == *.icns ]] || ICON_NAME="$ICON_NAME.icns"
OLD_ICON="$BACKUP_DIR/$APP_NAME/Contents/Resources/$ICON_NAME"
test -f "$OLD_ICON"
if [[ -n "${MACOS_BUILD_PYTHON:-}" ]]; then
  BUILD_PYTHON="$MACOS_BUILD_PYTHON"
elif [[ -x /opt/homebrew/bin/python3.11 ]]; then
  BUILD_PYTHON=/opt/homebrew/bin/python3.11
else
  BUILD_PYTHON="$(command -v python3)"
fi
MACOS_FFMPEG_PATH="$BACKUP_DIR/$APP_NAME/Contents/Frameworks/bin/ffmpeg" \
MACOS_BUILD_PYTHON="$BUILD_PYTHON" MACOS_DIST_DIR="$DIST_DIR" \
  bash "$ROOT_DIR/scripts/build-macos-app.sh"
NEW_APP="$DIST_DIR/$APP_NAME"
NEW_ICON=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIconFile' "$NEW_APP/Contents/Info.plist")
[[ "$NEW_ICON" == *.icns ]] || NEW_ICON="$NEW_ICON.icns"
cp -p "$OLD_ICON" "$NEW_APP/Contents/Resources/$NEW_ICON"
# Preserve a Finder custom icon too, if one exists.
FINDER_ICON=$'Icon\r'
if [[ -f "$BACKUP_DIR/$APP_NAME/$FINDER_ICON" ]]; then
  /usr/bin/ditto "$BACKUP_DIR/$APP_NAME/$FINDER_ICON" "$NEW_APP/$FINDER_ICON"
  if /usr/bin/xattr -px com.apple.FinderInfo "$BACKUP_DIR/$APP_NAME" > "$BACKUP_DIR/finder-info.hex" 2>/dev/null; then
    /usr/bin/xattr -wx com.apple.FinderInfo "$(cat "$BACKUP_DIR/finder-info.hex")" "$NEW_APP"
  fi
fi
/usr/bin/codesign --force --deep --sign - "$NEW_APP"
/usr/bin/codesign --verify --deep --strict "$NEW_APP"
cmp "$OLD_ICON" "$NEW_APP/Contents/Resources/$NEW_ICON"
if /usr/bin/pgrep -f '^/Applications/Apple Music Downloader.app/Contents/MacOS/' >/dev/null; then
  echo "Please quit Apple Music Downloader, then press Return to install."
  read -r
fi
if /usr/bin/pgrep -f '^/Applications/Apple Music Downloader.app/Contents/MacOS/' >/dev/null; then
  echo "App still running; installation stopped. New build: $NEW_APP" >&2
  exit 1
fi
STAGED="/Applications/Apple Music Downloader-new-$STAMP.app"
/usr/bin/ditto "$NEW_APP" "$STAGED"
/usr/bin/codesign --verify --deep --strict "$STAGED"
mv "$INSTALLED" "/Applications/Apple Music Downloader-backup-$STAMP.app"
mv "$STAGED" "$INSTALLED"
echo "Installed with your original icon. Backup: $BACKUP_DIR/$APP_NAME"
echo "Launch from: $INSTALLED"
