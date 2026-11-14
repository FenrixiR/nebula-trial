#!/bin/sh

NEBULA_ROOT=${NEBULA_ROOT:-/user-resource/nebula}
INITDIR=${INITDIR:-/etc/init.d}
ETCDIR=${ETCDIR:-/etc}
RUNDIR=${RUNDIR:-/run}
export NEBULA_ROOT
SELF=$NEBULA_ROOT/nebula-ctl.sh
MODEFILE=$RUNDIR/nebula-mode
LOG=$NEBULA_ROOT/nebula.log
. "$NEBULA_ROOT/nebula.env" || { echo "nebula-ctl: no nebula.env"; exit 2; }

log() {
    echo "$(date "+%F %T") $*" >> "$LOG"
    echo "nebula-ctl: $*"
    tail -n 200 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
}

pidfile() { echo "$RUNDIR/nebula-$1.pid"; }
running() { [ -f "$(pidfile $1)" ] && kill -0 "$(cat "$(pidfile $1)")" 2>/dev/null; }
launch() {
    start-stop-daemon -S -b -m -p "$(pidfile $1)" -x /bin/sh -- -c "exec $2 >> $NEBULA_ROOT/$1.log 2>&1"
}
halt() {
    start-stop-daemon -K -q -p "$(pidfile $1)" 2>/dev/null
    rm -f "$(pidfile $1)"
}
listening() {
    grep -qi "^ *[0-9]*: [0-9A-F]*:$(printf %04X "$1") [0-9A-F:]* 0A " /proc/net/tcp /proc/net/tcp6 2>/dev/null
}

stop_screen() {
    [ -n "$SCREEN_CMD" ] || return 0
    halt screen
    for n in $SCREEN_KILL; do
        pids=$(pgrep -f "[$(echo "$n" | cut -c1)]$(echo "$n" | cut -c2-)") && kill $pids 2>/dev/null
    done
    sleep 1
}

cable_if() {
    for n in eth0 usb0; do [ -d "${SYSNET:-/sys/class/net}/$n" ] && { echo $n; return; }; done
    echo eth0
}

heal_cable() {
    dev=$(cable_if)
    ip link set $dev up 2>/dev/null
    udhcpc -i $dev -n -q -t 3 -T 1 -s "$NEBULA_ROOT/udhcpc-address-only.sh" >/dev/null 2>&1
}

keeper() {
    silent=0
    while :; do
        dev=$(cable_if)
        if ! ip -4 addr show $dev 2>/dev/null | grep -q "inet 10\.78\.0\."; then
            heal_cable
            silent=0
        else
            ip link show $dev 2>/dev/null | grep -q "<[^>]*[<,]UP[,>]" || {
                echo "$(date "+%F %T") keeper: $dev is down, bringing it up"
                ip link set $dev up 2>/dev/null
            }
            if ping -c 1 -W 1 "$HOST_ADDR" >/dev/null 2>&1; then silent=0; else silent=$((silent + 1)); fi
            if [ "$silent" -ge "${KEEPER_SILENT_LIMIT:-10}" ]; then
                echo "$(date "+%F %T") keeper: host silent for $silent checks, renewing the cable lease"
                heal_cable
                silent=0
            fi
        fi
        sleep 3
    done
}

BOOT_NAME=nebula-cable
BOOT_LINK=S97$BOOT_NAME

persist() {
    case "$1" in
        on)
            ln -sf "$NEBULA_ROOT/nebula-boot.sh" "$INITDIR/$BOOT_NAME"
            for r in rc3.d rc5.d; do mkdir -p "$ETCDIR/$r"; ln -sf "../init.d/$BOOT_NAME" "$ETCDIR/$r/$BOOT_LINK"; done
            touch "$NEBULA_ROOT/keep-cable"
            log "persist: on, the cable is addressed at every boot"
            ;;
        off)
            rm -f "$NEBULA_ROOT/keep-cable" "$INITDIR/$BOOT_NAME" "$ETCDIR/rc3.d/$BOOT_LINK" "$ETCDIR/rc5.d/$BOOT_LINK"
            log "persist: off, nothing of Nebula starts at boot"
            ;;
        status)
            if [ -f "$NEBULA_ROOT/keep-cable" ] && [ -L "$INITDIR/$BOOT_NAME" ] && [ -L "$ETCDIR/rc5.d/$BOOT_LINK" ]; then echo "persist: on"; else echo "persist: off"; fi
            ;;
        *) echo "usage: $0 persist on|off|status"; exit 1 ;;
    esac
}

case "$1" in
    hijack)
        if [ "$(cat $MODEFILE 2>/dev/null)" = hijacked ]; then log "already hijacked, verifying only"; exec "$SELF" verify; fi
        log "hijack: stopping stock set ($STOP_ORDER)"
        for n in $STOP_ORDER; do "$INITDIR/$n" stop || log "warning: $n stop returned $?"; done
        echo hijacked > $MODEFILE
        launch keeper "$SELF keeper"
        launch relay "$RELAY_CMD"
        [ -n "$SCREEN_CMD" ] && launch screen "$SCREEN_CMD"
        [ "${WATCHDOG_MINUTES:-0}" -gt 0 ] && launch watchdog "$SELF watchdog"
        sleep 1
        log "hijack: Nebula set started"
        ;;
    release)
        log "release: stopping Nebula set"
        for c in watchdog relay; do halt $c; done
        stop_screen
        rm -f $MODEFILE
        running keeper || launch keeper "$SELF keeper"
        for n in $START_ORDER; do "$INITDIR/$n" start || log "warning: $n start returned $?"; done
        log "release: stock set started ($START_ORDER)"
        ;;
    verify)
        bad=0
        [ "$(cat $MODEFILE 2>/dev/null)" = hijacked ] || { echo "not hijacked"; bad=1; }
        [ -n "$SCREEN_CMD" ] && { running screen || { echo "screen not running"; bad=1; }; }
        for c in relay keeper; do running $c || { echo "$c not running"; bad=1; }; done
        for p in $VERIFY_PORTS; do listening "$p" || { echo "port $p not listening"; bad=1; }; done
        [ $bad = 0 ] && echo "verify ok"
        exit $bad
        ;;
    status)
        echo "mode: $(cat $MODEFILE 2>/dev/null || echo stock)"
        [ -n "$SCREEN_CMD" ] && { running screen && echo "screen: running" || echo "screen: stopped"; }
        for c in relay keeper watchdog; do running $c && echo "$c: running" || echo "$c: stopped"; done
        ;;
    keeper) keeper ;;
    cable) running keeper || launch keeper "$SELF keeper" ;;
    persist) persist "$2" ;;
    purge)
        [ "$(cat $MODEFILE 2>/dev/null)" = hijacked ] && "$SELF" release
        for c in watchdog relay keeper; do halt $c; done
        stop_screen
        persist off
        rm -f "$RUNDIR"/nebula-*.pid "$MODEFILE"
        cd / && rm -rf "${NEBULA_ROOT:?}"
        ;;
    watchdog)
        miss=0
        while :; do
            if wget -q -T 3 -O /dev/null "http://$HOST_ADDR:${HOST_API_PORT:-7125}/server/info" 2>/dev/null; then miss=0; else miss=$((miss + 1)); fi
            if [ $((miss * 10)) -ge $((WATCHDOG_MINUTES * 60)) ]; then
                log "watchdog: host unreachable for $WATCHDOG_MINUTES min, releasing"
                rm -f "$(pidfile watchdog)"
                exec "$SELF" release
            fi
            sleep 10
        done
        ;;
    *)
        echo "usage: $0 {hijack|release|verify|status|cable|persist|purge}"
        exit 1
        ;;
esac
