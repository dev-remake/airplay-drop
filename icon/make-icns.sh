#!/bin/sh
# make-icns.sh - turns icon_1024.png into applet.icns (run from icon/)
set -e
rm -rf AppIcon.iconset
mkdir AppIcon.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s icon_1024.png --out "AppIcon.iconset/icon_${s}x${s}.png" > /dev/null
  d=$((s * 2))
  sips -z $d $d icon_1024.png --out "AppIcon.iconset/icon_${s}x${s}@2x.png" > /dev/null
done
iconutil -c icns AppIcon.iconset -o applet.icns
rm -rf AppIcon.iconset
echo "applet.icns written"
