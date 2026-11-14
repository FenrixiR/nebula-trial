#!/usr/bin/env bash
set -euo pipefail

R=${NEBULA_ROOT:-}
USER_NAME=nebula
HOME_DIR=$R/home/$USER_NAME
KEEP_DATA=0
ASSUME_YES=0
WARNINGS=()

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { WARNINGS+=("$*"); printf 'warning: %s\n' "$*" >&2; }
die() { printf 'nebula-uninstall: %s\n' "$*" >&2; exit 1; }
drop() { local p; for p in "$@"; do [ -n "$p" ] && [ "$p" != / ] && rm -rf -- "${p:?}"; done; }

usage() {
    say "Removes Nebula from this machine and gives the printer back to Cosmos."
    say "  --yes         do not ask for confirmation"
    say "  --keep-data   keep your settings, printer login, saved tuning, Kalico config and print history"
    say "                (in /home/$USER_NAME), so a later install picks up where you left off"
    say "  --help        this text"
}

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    say "This will:"
    say "  - give the printer back to Cosmos and delete everything of Nebula from the printer (if it can be reached)"
    say "  - remove the Nebula services, add-ons and system rules from this machine"
    if [ "$KEEP_DATA" -eq 1 ]; then
        say "  - keep /home/$USER_NAME/.nebula and /home/$USER_NAME/printer_data (settings, tuning, config, history)"
    else
        say "  - delete the '$USER_NAME' user with all its files: settings, tuning, Kalico config, print history"
    fi
    say "System packages that were installed for Nebula (git, python tools, dnsmasq, ...) stay; other things may use them."
    local answer
    [ -r /dev/tty ] || die "no terminal to ask on; run again with --yes"
    printf 'Continue? [y/N] ' > /dev/tty
    read -r answer < /dev/tty
    case $answer in y | Y | yes | YES) ;; *) die "cancelled, nothing was changed" ;; esac
}

printer() {
    step "The printer"
    local code=0
    if [ ! -d "$HOME_DIR/host/nebula" ]; then
        say "Nebula's program files are not here any more, so the printer cannot be cleaned from this machine."
        return
    fi
    systemctl stop nebula-panel.service 2> /dev/null || true
    (cd "$HOME_DIR/host" && runuser -u "$USER_NAME" -- python3 -m nebula.uninstall --state "$HOME_DIR/.nebula") || code=$?
    case $code in
        0) ;;
        3) warn "the printer could not be reached. It returns to Cosmos by itself within minutes of this machine going away. The files Nebula left on it do nothing; to remove them, install Nebula again and run nebula-uninstall while the printer is connected." ;;
        *) die "could not clean the printer; nothing else was removed, so you can try again" ;;
    esac
}

addons() {
    step "Add-ons"
    local state=$HOME_DIR/.nebula/addons.json id
    [ -f "$state" ] && [ -d "$HOME_DIR/host/nebula" ] || { say "none installed"; return; }
    for id in $(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))))' "$state" 2> /dev/null); do
        say "removing $id"
        (cd "$HOME_DIR/host" && python3 -m nebula.addons remove "$id" --state "$state") > /dev/null 2>&1 || warn "could not remove the add-on $id"
    done
}

services() {
    step "Services and system files"
    local u
    for u in nebula-panel nebula-moonraker nebula-kalico nebula-relay-client nebula-linkdoctor nebula-clock-save.timer nebula-clock-save centauri-usb-gadget; do
        systemctl disable --now "$u" > /dev/null 2>&1 || true
    done
    drop "$R"/etc/systemd/system/nebula-*.service "$R"/etc/systemd/system/nebula-*.timer "$R/etc/systemd/system/centauri-usb-gadget.service"
    drop "$R/etc/sudoers.d/nebula" "$R/etc/polkit-1/rules.d/50-nebula-moonraker.rules" "$R/etc/dnsmasq.d/dnsmasq-centauri-usb.conf"
    drop "$R/usr/local/sbin/centauri-usb-gadget.sh" "$R/usr/local/sbin/nebula-linkdoctor.sh" "$R/etc/modules-load.d/nebula.conf"
    drop "$R/etc/nebula" "$R/opt/nebula" "$R/var/lib/nebula" "$R/var/log/nebula-update.log"
    systemctl daemon-reload || true
    local conf
    for conf in "$R/boot/firmware/config.txt" "$R/boot/config.txt"; do
        if [ -f "$conf.nebula-backup" ]; then
            sed -i '/^dtoverlay=dwc2$/d' "$conf"
            drop "$conf.nebula-backup"
            say "USB device mode switched off again in $conf (takes effect after a reboot)"
        fi
    done
}

files() {
    step "Files"
    drop "$HOME_DIR/host" "$HOME_DIR/host.prev" "$HOME_DIR/host.new" "$HOME_DIR/kalico" "$HOME_DIR/moonraker" \
        "$HOME_DIR/klippy-env" "$HOME_DIR/moonraker-env" "$HOME_DIR/centauri-pack" "$HOME_DIR/bin"
    if [ "$KEEP_DATA" -eq 1 ]; then
        say "kept: $HOME_DIR/.nebula and $HOME_DIR/printer_data"
    elif id "$USER_NAME" > /dev/null 2>&1; then
        pkill -KILL -u "$USER_NAME" || true
        sleep 1
        if userdel -r "$USER_NAME" 2> /dev/null; then
            say "removed the '$USER_NAME' user and its files"
        else
            warn "could not remove the '$USER_NAME' user (something still runs as it); log out of it or reboot, then: sudo userdel -r $USER_NAME"
        fi
        drop "$HOME_DIR"
    fi
}

main() {
    local arg
    for arg in "$@"; do
        case $arg in
            --yes | -y) ASSUME_YES=1 ;;
            --keep-data) KEEP_DATA=1 ;;
            --help | -h) usage; exit 0 ;;
            *) die "unknown option: $arg (try --help)" ;;
        esac
    done
    [ "$(id -u)" -eq 0 ] || die "run this as root: sudo nebula-uninstall"
    confirm
    printer
    addons
    services
    files
    drop "$R/usr/local/sbin/nebula-uninstall" "$R/usr/local/sbin/nebula-update"
    printf '\nNebula has been removed.\n'
    for arg in "${WARNINGS[@]+"${WARNINGS[@]}"}"; do say "note: $arg"; done
}

main "$@"
