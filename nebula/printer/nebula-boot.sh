#!/bin/sh
NEBULA_ROOT=${NEBULA_ROOT:-/user-resource/nebula}
case "$1" in
    start)
        [ -f "$NEBULA_ROOT/keep-cable" ] && [ -x "$NEBULA_ROOT/nebula-ctl.sh" ] && "$NEBULA_ROOT/nebula-ctl.sh" cable
        ;;
esac
exit 0
