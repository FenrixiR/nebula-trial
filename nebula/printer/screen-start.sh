#!/bin/sh
cd "$NEBULA_ROOT/helixscreen" || exit 1
export HELIX_DISPLAY_BACKEND=fbdev HELIX_FB_DEVICE=/dev/fb0 HELIX_TOUCH_DEVICE=/dev/input/event0
exec ./bin/helix-launcher.sh --moonraker "ws://$1:7125"
