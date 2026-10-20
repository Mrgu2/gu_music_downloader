#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Apple Music Downloader"
ICON_PATH="$ROOT_DIR/assets/macos/app-icon.icns"
DMG_NOTE_NAME="IMPORTANT - First Launch ／ 首次启动说明.txt"
HOST_ARCH="$(uname -m)"
MACOS_ARCH="${MACOS_ARCH:-$HOST_ARCH}"
DIST_DIR="${MACOS_DIST_DIR:-$ROOT_DIR/dist}"
BUILD_DIR="${MACOS_BUILD_DIR:-$ROOT_DIR/build/macos-$MACOS_ARCH}"
MACOS_BUILD_PYTHON="${MACOS_BUILD_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.10/bin/python3}"
MACOS_VENV_DIR="${MACOS_VENV_DIR:-$ROOT_DIR/.venv-macos-$MACOS_ARCH}"
APP_PATH="$DIST_DIR/$APP_NAME.app"
DMG_PATH="$DIST_DIR/$APP_NAME-$MACOS_ARCH.dmg"

case "$MACOS_ARCH" in
  arm64)
    UV_PYTHON_PLATFORM="aarch64-apple-darwin"
    USE_ARCH_WRAPPER=0
    export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
    ;;
  x86_64)
    UV_PYTHON_PLATFORM="x86_64-apple-darwin"
    USE_ARCH_WRAPPER=1
    export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
    ;;
  *)
    echo "Unsupported MACOS_ARCH: $MACOS_ARCH (expected arm64 or x86_64)"
    exit 1
    ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install uv first."
  exit 1
fi

if [[ ! -x "$MACOS_BUILD_PYTHON" ]]; then
  echo "Python interpreter not found: $MACOS_BUILD_PYTHON"
  exit 1
fi

binary_arches() {
  lipo -archs "$1" 2>/dev/null || true
}

require_binary_arch() {
  local binary="$1"
  local target_arch="$2"
  local archs
  archs="$(binary_arches "$binary")"
  if [[ -z "$archs" ]]; then
    echo "Unable to inspect Mach-O architectures for: $binary"
    exit 1
  fi
  if [[ " $archs " != *" $target_arch "* ]]; then
    echo "Binary architecture mismatch: $binary supports [$archs], expected $target_arch"
    exit 1
  fi
}

run_for_target_arch() {
  if [[ "$USE_ARCH_WRAPPER" == "1" ]]; then
    arch -x86_64 "$@"
  else
    "$@"
  fi
}

resolve_ffmpeg_source() {
  local candidates=()
  if [[ -n "${MACOS_FFMPEG_PATH:-}" ]]; then
    candidates+=("$MACOS_FFMPEG_PATH")
  fi
  candidates+=(
    "$ROOT_DIR/assets/macos/ffmpeg-$MACOS_ARCH"
  )
  if [[ "$MACOS_ARCH" == "arm64" ]]; then
    candidates+=(
      "$ROOT_DIR/assets/macos/ffmpeg"
      "/opt/homebrew/bin/ffmpeg"
    )
  else
    candidates+=(
      "/usr/local/bin/ffmpeg"
    )
  fi

  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

cd "$ROOT_DIR"
APP_VERSION="$("$MACOS_BUILD_PYTHON" -c 'from gamdl import __version__; print(__version__)')"
if [[ -e "$APP_PATH" || -e "$DMG_PATH" || -e "$DIST_DIR/$APP_NAME" ]]; then
  echo "Build output already exists. Set MACOS_DIST_DIR to a new directory to preserve existing files."
  exit 1
fi
mkdir -p "$DIST_DIR" "$BUILD_DIR"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$BUILD_DIR/pyinstaller-cache}"
mkdir -p "$PYINSTALLER_CONFIG_DIR"

if ! FFMPEG_SOURCE="$(resolve_ffmpeg_source)"; then
  echo "No ffmpeg found for $MACOS_ARCH. Set MACOS_FFMPEG_PATH or provide assets/macos/ffmpeg-$MACOS_ARCH."
  exit 1
fi
require_binary_arch "$FFMPEG_SOURCE" "$MACOS_ARCH"

UV_PROJECT_ENVIRONMENT="$MACOS_VENV_DIR" uv sync \
  --extra desktop-build \
  --locked \
  --no-editable \
  --reinstall-package gamdl \
  --python "$MACOS_BUILD_PYTHON" \
  --python-platform "$UV_PYTHON_PLATFORM"

if [[ ! -x "$MACOS_VENV_DIR/bin/python" ]]; then
  echo "Build environment python not found: $MACOS_VENV_DIR/bin/python"
  exit 1
fi

ACTUAL_BUILD_ARCH="$(run_for_target_arch "$MACOS_VENV_DIR/bin/python" -c 'import platform; print(platform.machine())')"
if [[ "$ACTUAL_BUILD_ARCH" != "$MACOS_ARCH" ]]; then
  echo "Build environment architecture mismatch: expected $MACOS_ARCH, got $ACTUAL_BUILD_ARCH"
  exit 1
fi

"$ROOT_DIR/scripts/generate-macos-icon.sh"

PYINSTALLER_ARGS=(
  --noconfirm
  --clean
  --windowed
  --name "$APP_NAME"
  --icon "$ICON_PATH"
  --collect-all webview
  --hidden-import webview.platforms.cocoa
  --hidden-import AppKit
  --hidden-import WebKit
  --target-arch "$MACOS_ARCH"
  --distpath "$DIST_DIR"
  --workpath "$BUILD_DIR/pyinstaller-work"
  --add-data "$ROOT_DIR/README.md:."
)

run_for_target_arch "$MACOS_VENV_DIR/bin/python" -m PyInstaller \
  --specpath "$BUILD_DIR" \
  "${PYINSTALLER_ARGS[@]}" \
  gamdl/desktop_app.py

require_binary_arch "$APP_PATH/Contents/MacOS/$APP_NAME" "$MACOS_ARCH"
# Set bundle metadata before bundle_ffmpeg performs the final ad hoc signing.
"$MACOS_VENV_DIR/bin/python" - "$APP_PATH" "$APP_VERSION" <<'PY'
from pathlib import Path
import plistlib
import sys

plist_path = Path(sys.argv[1]) / "Contents" / "Info.plist"
payload = plistlib.loads(plist_path.read_bytes())
payload["CFBundleShortVersionString"] = sys.argv[2]
payload["CFBundleVersion"] = sys.argv[2]
plist_path.write_bytes(plistlib.dumps(payload))
PY
"$MACOS_VENV_DIR/bin/python" "$ROOT_DIR/scripts/bundle_macos_ffmpeg.py" "$APP_PATH" "$FFMPEG_SOURCE"
require_binary_arch "$APP_PATH/Contents/Frameworks/bin/ffmpeg" "$MACOS_ARCH"

if [[ -n "${APPLE_DEVELOPER_IDENTITY:-}" ]]; then
  codesign \
    --deep \
    --force \
    --options runtime \
    --sign "$APPLE_DEVELOPER_IDENTITY" \
    "$APP_PATH"
fi

DMG_STAGE_DIR="$(mktemp -d "$BUILD_DIR/dmg-stage.XXXXXX")"
DMG_NOTE_PATH="$DMG_STAGE_DIR/$DMG_NOTE_NAME"

cleanup() {
  rm -rf "$DMG_STAGE_DIR"
}

trap cleanup EXIT

cat > "$DMG_NOTE_PATH" <<'EOF'
Apple Music Downloader
这是一个基于 Gamdl 修改的 macOS 桌面版。

为什么 macOS 可能要求你额外确认
这个构建版本目前还没有通过 Apple 的 notarization（公证）。因此即使 app 本身没有被破坏，Gatekeeper 仍然可能在第一次启动时阻止它运行。

如何打开
1. 先把 Apple Music Downloader 拖到 Applications 文件夹。
2. 打开一次应用。
3. 如果 macOS 阻止启动，先关闭弹窗，然后打开“系统设置 > 隐私与安全性”。
4. 在安全性区域找到 Apple Music Downloader，然后点击“仍要打开”。
5. 确认弹窗后，再次打开应用。

如果这个构建不是来自你信任的人，请不要打开。
项目地址：https://github.com/Mrgu2/gu_music_downloader

Apple Music Downloader
Modified from the Gamdl project for macOS desktop use.

Why macOS may ask you to confirm this app
This build has not been notarized by Apple yet. Because of that, Gatekeeper may block the first launch even though the app bundle is intact.

How to open it
1. Drag Apple Music Downloader to Applications.
2. Open the app once.
3. If macOS blocks it, close the alert and open System Settings > Privacy & Security.
4. In the Security section, click Open Anyway for Apple Music Downloader.
5. Confirm the dialog, then open the app again.

If you received this build from someone you do not trust, do not open it.
Project page: https://github.com/Mrgu2/gu_music_downloader
EOF

ditto "$APP_PATH" "$DMG_STAGE_DIR/$APP_NAME.app"
ln -s /Applications "$DMG_STAGE_DIR/Applications"

hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$DMG_STAGE_DIR" \
  -ov \
  -format UDZO \
  -imagekey zlib-level=9 \
  "$DMG_PATH" >/dev/null

if [[ -n "${APPLE_NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$DMG_PATH" --keychain-profile "$APPLE_NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP_PATH"
  xcrun stapler staple "$DMG_PATH"
fi

echo "Built app: $APP_PATH"
echo "Built dmg: $DMG_PATH"
