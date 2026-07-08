#!/usr/bin/env python3
# make-icon.py — draws the AirPlay Drop app icon (1024px PNG).
# macOS-style: rounded square, blue-violet gradient, AirPlay glyph
# (a screen with the triangle notch) in white.
# Rebuild the .icns with:  ./make-icns.sh
from PIL import Image, ImageDraw

S = 1024
img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# vertical gradient, Pacific colorway: teal -> blue
top, bottom = (0, 170, 200), (30, 90, 235)
grad = Image.new('RGBA', (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
  t = y / S
  gd.line([(0, y), (S, y)], fill=tuple(
    int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))

# macOS icon shape: rounded square with ~22% corner radius, small margin
mask = Image.new('L', (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([90, 90, S - 90, S - 90],
                                       radius=210, fill=255)
img.paste(grad, (0, 0), mask)

# AirPlay glyph: screen outline with a gap at the bottom + play triangle
d = ImageDraw.Draw(img)
white = (255, 255, 255, 255)
lw = 46  # line width

# screen: rounded rectangle, but drawn as an arc-less outline with the
# bottom edge left open in the middle (where the triangle pokes through)
sx1, sy1, sx2, sy2 = 250, 300, S - 250, 660
r = 40
d.rounded_rectangle([sx1, sy1, sx2, sy2], radius=r, outline=white, width=lw)
# cut the bottom-middle of the screen open
d.rectangle([390, sy2 - lw - 4, S - 390, sy2 + lw + 4], fill=(0, 0, 0, 0))
# re-fill the cut with gradient (so it looks open, not black)
cut = grad.crop((390, sy2 - lw - 4, S - 390, sy2 + lw + 4))
img.paste(cut, (390, sy2 - lw - 4))

# play triangle rising through the opening
d = ImageDraw.Draw(img)
d.polygon([(S // 2, 560), (330, 810), (S - 330, 810)], fill=white)

img.save('icon_1024.png')
print('icon_1024.png written')
