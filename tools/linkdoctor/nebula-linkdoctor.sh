#!/bin/sh
SYSFS=${SYSFS:-/sys}
CONFIGFS=${CONFIGFS:-$SYSFS/kernel/config/usb_gadget}
POLL=${POLL:-0.02}
WINDOW=${WINDOW:-300}
MAX_PER_WINDOW=${MAX_PER_WINDOW:-5}

log() { echo "$(date "+%F %T") linkdoctor: $*"; }

UDC=${UDC:-$(ls "$SYSFS/class/udc" 2>/dev/null | head -n 1)}
[ -n "$UDC" ] || { log "no UDC found"; exit 1; }
GADGET=${GADGET:-$(ls "$CONFIGFS" 2>/dev/null | head -n 1)}
[ -n "$GADGET" ] || { log "no gadget in $CONFIGFS (start the gadget service first)"; exit 1; }
STATE=$SYSFS/class/udc/$UDC/state
BIND=$CONFIGFS/$GADGET/UDC

log "watching $UDC, gadget $GADGET"
prev=$(cat "$STATE" 2>/dev/null)
window_start=$(date +%s)
count=0
while :; do
    s=$(cat "$STATE" 2>/dev/null)
    if [ "$s" != "$prev" ]; then
        log "state: $prev -> $s"
        if [ "$s" = "not attached" ]; then
            now=$(date +%s)
            if [ $((now - window_start)) -ge "$WINDOW" ]; then window_start=$now; count=0; fi
            if [ "$count" -ge "$MAX_PER_WINDOW" ]; then
                log "rate limit reached ($MAX_PER_WINDOW in ${WINDOW}s), not rebinding"
            else
                count=$((count + 1))
                echo "" > "$BIND"
                echo "$UDC" > "$BIND"
                log "rebound $UDC ($count in this window)"
                [ -n "$ONCE_FOR_TEST" ] && exit 0
            fi
        fi
        prev=$s
    fi
    sleep "$POLL"
done
