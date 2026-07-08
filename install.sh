#!/bin/sh
# install.sh - one-time setup for AirPlay Drop.
# Installs the two Python packages it needs, pre-downloads the bundled
# ffmpeg, and builds "AirPlay Drop.app" pointing at this folder.
set -e
cd "$(dirname "$0")"
PY="$(command -v python3)"

echo "==> Installing Python packages (pywebview, static-ffmpeg)..."
"$PY" -m pip install --quiet pywebview static-ffmpeg

echo "==> Downloading the bundled ffmpeg (one time, ~80 MB)..."
"$PY" - <<'EOF'
from static_ffmpeg import run
run.get_or_fetch_platform_executables_else_raise()
print('ffmpeg ready')
EOF

echo "==> Building AirPlay Drop.app..."
cat > /tmp/airplay-drop-droplet.applescript <<EOF
property py : "$PY"
property gui : "$(pwd)/airplay-app.py"
on run
  do shell script py & " " & quoted form of gui & " > /dev/null 2>&1 &"
end run
on open theFiles
  set videoPath to POSIX path of item 1 of theFiles
  do shell script py & " " & quoted form of gui & " " & quoted form of videoPath & " > /dev/null 2>&1 &"
end open
EOF
osacompile -o "AirPlay Drop.app" /tmp/airplay-drop-droplet.applescript
cp icon/applet.icns "AirPlay Drop.app/Contents/Resources/applet.icns"
touch "AirPlay Drop.app"

echo ""
echo "Done. Drag 'AirPlay Drop.app' to your Dock, or double-click it."
echo "First launch: macOS will ask permission for the app to control Terminal-free launching; click Allow."
