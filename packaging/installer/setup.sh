#!/usr/bin/env bash
set -euo pipefail
[ -z "${NEBULA_DEBUG:-}" ] || set -x

PKG=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
R=${NEBULA_ROOT:-}
USER_NAME=nebula
HOME_DIR=$R/home/$USER_NAME
VERSION=$(cat "$PKG/VERSION")
source "$PKG/installer/pins.env"

ASSUME_YES=0
UPDATING=0
NEED_REBOOT=0
PANEL_URL=${NEBULA_PANEL_URL:-http://127.0.0.1:8080/nebula/}
WARNINGS=()

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { WARNINGS+=("$*"); printf 'warning: %s\n' "$*" >&2; }
die() { printf 'nebula-setup: %s\n' "$*" >&2; exit 1; }
as_user() { runuser -u "$USER_NAME" -- "$@"; }
quiet() {
    local what=$1 log pid i=0 t0=$SECONDS frames='|/-\'
    shift
    log=$(mktemp)
    "$@" > "$log" 2>&1 &
    pid=$!
    if [ -t 1 ]; then
        while kill -0 "$pid" 2> /dev/null; do
            printf '\r  %s %s (%ds)' "${frames:i++%4:1}" "$what" "$((SECONDS - t0))"
            sleep 0.25
        done
        printf '\r\033[K'
    fi
    if wait "$pid"; then
        drop "$log"
    else
        tail -n 25 "$log" >&2
        die "$what failed (full output: $log)"
    fi
}
drop() { local p; for p in "$@"; do [ -n "$p" ] && [ "$p" != / ] && rm -rf -- "${p:?}"; done; }

arch() {
    case $(uname -m) in
        aarch64 | arm64) echo aarch64 ;;
        x86_64 | amd64) echo x86_64 ;;
        *) die "unsupported processor: $(uname -m)" ;;
    esac
}

is_raspberry_pi() { grep -qi 'raspberry pi' "$R/proc/device-tree/model" 2> /dev/null || grep -qi 'raspberry pi' "$R/sys/firmware/devicetree/base/model" 2> /dev/null; }

preflight() {
    step "Checking this machine"
    [ "$(id -u)" -eq 0 ] || die "must run as root"
    local arch mem disk
    arch=$(arch)
    say "system: $( ( . /etc/os-release 2> /dev/null && echo "${PRETTY_NAME:-unknown}" ) || echo unknown) ($arch)"
    command -v apt-get > /dev/null || die "this installer supports Debian-based systems"
    [ -d /run/systemd/system ] || [ -n "${NEBULA_TEST:-}" ] || die "systemd is not running"
    mem=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
    say "memory: ${mem} MB"
    [ "$mem" -ge 350 ] || die "at least 350 MB of memory is needed (this machine has ${mem} MB)"
    disk=$(df -Pm "${R:-/}" | awk 'NR==2 {print $4}')
    say "free disk: ${disk} MB"
    [ "$disk" -ge 1500 ] || die "at least 1.5 GB of free disk space is needed (${disk} MB free)"
    if ! ls /sys/class/udc 2> /dev/null | grep -q . && ! is_raspberry_pi; then
        warn "no USB device port (UDC) was found. The cable link to the printer needs one. On some boards it must be switched on in the device tree; the rest of Nebula is installed anyway."
    fi
}

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    say
    say "Nebula $VERSION will be installed on this machine:"
    say "  - a user '$USER_NAME' and the files in /home/$USER_NAME"
    say "  - system packages: $(tr '\n' ' ' < "$PKG/installer/packages.txt")"
    say "  - Kalico and Moonraker, downloaded at fixed versions"
    say "  - services for the panel, the USB cable link and a link watchdog"
    say "  - a small sudo rule and a polkit rule for the '$USER_NAME' user"
    say "Cable power: if this machine has its own power supply, the USB cable to the printer must have its 5 V (VBUS) wire cut."
    say "  Only a low-power board such as the Pi Zero 2 W can run from the printer's USB port with an uncut cable."
    say "Nothing is changed on the printer until you press Take over in the panel."
    local answer
    [ -r /dev/tty ] || die "no terminal to ask on; run again with --yes"
    printf 'Continue? [y/N] ' > /dev/tty
    read -r answer < /dev/tty
    case $answer in y | Y | yes | YES) ;; *) die "cancelled, nothing was changed" ;; esac
}

packages() {
    step "Installing system packages"
    export DEBIAN_FRONTEND=noninteractive
    quiet "apt-get update" apt-get update
    quiet "installing packages" apt-get install -y $(tr '\n' ' ' < "$PKG/installer/packages.txt")
}

make_user() {
    step "Setting up the '$USER_NAME' user"
    if ! id "$USER_NAME" > /dev/null 2>&1; then
        useradd --create-home --shell /bin/bash --groups dialout,plugdev "$USER_NAME"
    fi
    install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/printer_data"
    install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/bin" "$HOME_DIR/host" \
        "$HOME_DIR/printer_data/comms" "$HOME_DIR/printer_data/config" "$HOME_DIR/printer_data/logs" \
        "$HOME_DIR/printer_data/gcodes" "$HOME_DIR/printer_data/database"
}

checkout() {
    local dir=$HOME_DIR/$1
    if [ -d "$dir/.git" ] && [ "$(as_user git -C "$dir" rev-parse HEAD 2> /dev/null)" = "$3" ]; then
        say "$1: already at ${3:0:10}"
        return
    fi
    say "$1: fetching ${3:0:10}"
    drop "$dir"
    quiet "git init" as_user git init -q "$dir"
    as_user git -C "$dir" remote add origin "$2"
    quiet "downloading $1" as_user git -C "$dir" fetch -q --depth 1 origin "$3"
    as_user git -C "$dir" checkout -q FETCH_HEAD
    [ "$(as_user git -C "$dir" rev-parse HEAD)" = "$3" ] || die "$1: fetched version is not the pinned one"
}

venv() {
    local env=$HOME_DIR/$1-env
    [ -x "$env/bin/python" ] || quiet "creating the Python environment for $1" as_user python3 -m venv "$env"
    quiet "installing Python packages for $1" as_user "$env/bin/pip" install -q --disable-pip-version-check -r "$2"
}

sources() {
    step "Kalico and Moonraker"
    checkout kalico "$KALICO_URL" "$KALICO_COMMIT"
    checkout moonraker "$MOONRAKER_URL" "$MOONRAKER_COMMIT"
    venv klippy "$HOME_DIR/kalico/scripts/klippy-requirements.txt"
    venv moonraker "$HOME_DIR/moonraker/scripts/moonraker-requirements.txt"
    drop "$HOME_DIR/centauri-pack"
    cp -a "$PKG/host/pack" "$HOME_DIR/centauri-pack"
    chown -R "$USER_NAME:$USER_NAME" "$HOME_DIR/centauri-pack"
    as_user mkdir -p "$HOME_DIR/kalico/klippy/plugins"
    quiet "linking the Kalico plugins" as_user "$HOME_DIR/centauri-pack/install.sh" "$HOME_DIR/kalico"
}

screen_payload() {
    local file=${HELIXSCREEN_URL##*/} cache=$HOME_DIR/.nebula/cache
    install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.nebula" "$cache"
    if [ "$(sha256sum "$cache/$file" 2> /dev/null | cut -d' ' -f1)" != "$HELIXSCREEN_SHA256" ]; then
        say "HelixScreen: downloading $file"
        drop "$cache/$file.part"
        quiet "downloading HelixScreen" as_user curl -fsSL --retry 3 -o "$cache/$file.part" "$HELIXSCREEN_URL"
        [ "$(sha256sum "$cache/$file.part" | cut -d' ' -f1)" = "$HELIXSCREEN_SHA256" ] || { drop "$cache/$file.part"; die "HelixScreen: the download is not the pinned file"; }
        mv "$cache/$file.part" "$cache/$file"
    else
        say "HelixScreen: already downloaded"
    fi
    install -d -o "$USER_NAME" -g "$USER_NAME" "$1"
    install -m 644 -o "$USER_NAME" -g "$USER_NAME" "$cache/$file" "$1/$file"
}

nebula_code() {
    step "Nebula $VERSION"
    local arch
    arch=$(arch)
    drop "$HOME_DIR/host.new"
    install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/host.new"
    cp -a "$PKG/nebula" "$HOME_DIR/host.new/nebula"
    screen_payload "$HOME_DIR/host.new/nebula/screen"
    chown -R "$USER_NAME:$USER_NAME" "$HOME_DIR/host.new"
    if [ -d "$HOME_DIR/host/nebula" ]; then
        drop "$HOME_DIR/host.prev"
        mv "$HOME_DIR/host" "$HOME_DIR/host.prev"
    else
        drop "$HOME_DIR/host"
    fi
    mv "$HOME_DIR/host.new" "$HOME_DIR/host"
    [ -f "$HOME_DIR/bin/centauri-link" ] && cp -p "$HOME_DIR/bin/centauri-link" "$HOME_DIR/bin/centauri-link.prev"
    install -m 755 -o "$USER_NAME" -g "$USER_NAME" "$PKG/bin/centauri-link-$arch" "$HOME_DIR/bin/centauri-link"
    printf '%s\n' "$VERSION" > "$HOME_DIR/host/VERSION"
}

render() { sed "s#@HOME@#/home/$USER_NAME#g" "$1" > "$2"; }

config() {
    step "Kalico configuration"
    local cfg=$HOME_DIR/printer_data/config d f tmp
    for d in machine macros; do
        drop "$cfg/$d.new"
        install -d "$cfg/$d.new"
        for f in "$PKG/host/config/$d"/*.cfg; do render "$f" "$cfg/$d.new/$(basename "$f")"; done
        drop "$cfg/$d.prev"
        [ -d "$cfg/$d" ] && mv "$cfg/$d" "$cfg/$d.prev"
        mv "$cfg/$d.new" "$cfg/$d"
    done
    install -d "$cfg/user"
    [ -f "$cfg/user/00_keep.cfg" ] || cp "$PKG/host/config/user/00_keep.cfg" "$cfg/user/"
    [ -f "$cfg/printer.cfg" ] || cp "$PKG/host/config/printer.cfg" "$cfg/printer.cfg"
    if [ ! -f "$cfg/moonraker.conf" ]; then
        tmp=$(mktemp)
        render "$PKG/host/moonraker.conf" "$tmp"
        install -m 644 "$tmp" "$cfg/moonraker.conf"
        drop "$tmp"
    fi
    install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.nebula" "$HOME_DIR/.nebula/kalico"
    ln -sfn "/home/$USER_NAME/.nebula/kalico" "$cfg/nebula"
    chown -R "$USER_NAME:$USER_NAME" "$cfg"
}

dwc2_state() {
    awk '
        /^[ \t]*\[.*\]/ { sec = $0; gsub(/\[|\]|[ \t]/, "", sec); next }
        (sec == "" || sec == "all" || sec == "pi02") && /^dtoverlay=dwc2([, \t]|$)/ { st = ($0 ~ /dr_mode=host/) ? "host" : "peripheral" }
        END { print (st == "" ? "none" : st) }
    ' "$1"
}

dwc2_host_off() {
    awk '
        /^[ \t]*\[.*\]/ { sec = $0; gsub(/\[|\]|[ \t]/, "", sec) }
        (sec == "" || sec == "all" || sec == "pi02") && /^dtoverlay=dwc2([, \t]|$)/ && /dr_mode=host/ { print "#" $0; next }
        { print }
    ' "$1" > "$1.nebula-new" && cat "$1.nebula-new" > "$1" && rm -f "$1.nebula-new"
}

boot_settings() {
    step "USB cable link"
    install -d "$R/etc/modules-load.d"
    echo libcomposite > "$R/etc/modules-load.d/nebula.conf"
    local conf= c
    for c in "$R/boot/firmware/config.txt" "$R/boot/config.txt"; do [ -f "$c" ] && { conf=$c; break; }; done
    if is_raspberry_pi && [ -n "$conf" ]; then
        local dwc2
        dwc2=$(dwc2_state "$conf")
        if [ "$dwc2" != peripheral ]; then
            cp "$conf" "$conf.nebula-backup"
            [ "$dwc2" = host ] && dwc2_host_off "$conf"
            if grep -q '^\[all\]' "$conf"; then
                sed -i '0,/^\[all\]/s//[all]\ndtoverlay=dwc2/' "$conf"
            else
                printf '\n[all]\ndtoverlay=dwc2\n' >> "$conf"
            fi
            NEED_REBOOT=1
            say "Turned on the Pi's USB device mode (backup: $conf.nebula-backup)"
        else
            say "USB device mode is already on"
        fi
    elif ls /sys/class/udc 2> /dev/null | grep -q .; then
        say "USB device port found: $(ls /sys/class/udc | head -n1)"
    fi
}

system_files() {
    step "System services"
    local s=$PKG/host u
    install -d "$R/usr/local/sbin" "$R/etc/systemd/system" "$R/etc/sudoers.d" "$R/etc/polkit-1/rules.d" "$R/etc/dnsmasq.d"
    install -m 755 "$s/services/centauri-usb-gadget.sh" "$R/usr/local/sbin/centauri-usb-gadget.sh"
    install -m 755 "$s/services/nebula-linkdoctor.sh" "$R/usr/local/sbin/nebula-linkdoctor.sh"
    install -m 755 "$PKG/installer/uninstall.sh" "$R/usr/local/sbin/nebula-uninstall"
    install -m 755 "$PKG/installer/loader.sh" "$R/usr/local/sbin/nebula-update"
    install -d -m 755 "$R/var/lib/nebula"
    for u in "$s"/systemd/*; do install -m 644 "$u" "$R/etc/systemd/system/$(basename "$u")"; done
    install -m 644 "$s/services/dnsmasq-centauri-usb.conf" "$R/etc/dnsmasq.d/dnsmasq-centauri-usb.conf"
    install -m 644 "$s/polkit/50-nebula-moonraker.rules" "$R/etc/polkit-1/rules.d/50-nebula-moonraker.rules"
    install -m 440 "$s/sudoers/nebula" "$R/etc/sudoers.d/nebula.new"
    if command -v visudo > /dev/null; then
        visudo -cf "$R/etc/sudoers.d/nebula.new" > /dev/null || { drop "$R/etc/sudoers.d/nebula.new"; die "the sudo rule did not validate"; }
    fi
    mv "$R/etc/sudoers.d/nebula.new" "$R/etc/sudoers.d/nebula"
}

start_services() {
    step "Starting"
    chown -R "$USER_NAME:$USER_NAME" "$HOME_DIR"
    systemctl daemon-reload
    systemctl enable centauri-usb-gadget.service nebula-linkdoctor.service nebula-panel.service dnsmasq.service > /dev/null 2>&1 || true
    if [ "$NEED_REBOOT" -eq 0 ]; then
        if systemctl start centauri-usb-gadget.service; then
            systemctl restart dnsmasq.service || warn "dnsmasq did not start (see: journalctl -u dnsmasq)"
            systemctl start nebula-linkdoctor.service || true
        else
            warn "the USB cable link did not start, so the printer cannot be reached over the cable yet (see: journalctl -u centauri-usb-gadget)"
        fi
    fi
    systemctl restart nebula-panel.service
}

panel_healthy() { curl -fsS -o /dev/null "$PANEL_URL" 2> /dev/null; }

wait_for_panel() {
    local i
    for i in $(seq 1 30); do
        panel_healthy && return 0
        sleep 1
    done
    return 1
}

rollback() {
    step "The new version did not start: going back to the previous one"
    local cfg=$HOME_DIR/printer_data/config d
    if [ -d "$HOME_DIR/host.prev" ]; then
        drop "$HOME_DIR/host.failed"
        mv "$HOME_DIR/host" "$HOME_DIR/host.failed"
        mv "$HOME_DIR/host.prev" "$HOME_DIR/host"
    fi
    for d in machine macros; do
        if [ -d "$cfg/$d.prev" ]; then
            drop "$cfg/$d"
            mv "$cfg/$d.prev" "$cfg/$d"
        fi
    done
    [ -f "$HOME_DIR/bin/centauri-link.prev" ] && mv "$HOME_DIR/bin/centauri-link.prev" "$HOME_DIR/bin/centauri-link"
    systemctl restart nebula-panel.service || true
    wait_for_panel || true
    die "the update did not start, so the previous version was restored (details: journalctl -u nebula-panel)"
}

finish() {
    local ip w
    ip=$(hostname -I 2> /dev/null | awk '{print $1}')
    printf '\n'
    local healthy=1
    if [ -z "${NEBULA_TEST:-}" ] || [ "$UPDATING" -eq 1 ]; then
        wait_for_panel || healthy=0
    fi
    if [ "$healthy" -eq 0 ] && [ "$UPDATING" -eq 1 ]; then
        rollback
    fi
    if [ "$healthy" -eq 1 ]; then
        say "Nebula $VERSION is installed."
    else
        warn "the panel did not answer within 30 seconds; look at: journalctl -u nebula-panel"
    fi
    for w in "${WARNINGS[@]+"${WARNINGS[@]}"}"; do say "note: $w"; done
    if [ "$UPDATING" -eq 1 ]; then
        say "Open the panel:  http://${ip:-this-machine}:8080/nebula/"
    else
        local n=1
        say
        say "Next:"
        if [ "$NEED_REBOOT" -eq 1 ]; then
            say "  $n. Restart this machine, to switch on the USB cable port:  sudo reboot"
            n=$((n + 1))
        fi
        say "  $n. Connect the USB cable between this machine and the printer, then switch the printer off and on once."
        say "     The printer only sets up its end of the cable when it starts."
        n=$((n + 1))
        say "  $n. Open the panel:  http://${ip:-this-machine}:8080/nebula/"
    fi
    say
    say "To remove Nebula again:  sudo nebula-uninstall"
}

main() {
    local arg
    for arg in "$@"; do
        case $arg in
            --yes | -y) ASSUME_YES=1 ;;
            --update) UPDATING=1 ;;
            *) die "unknown option: $arg" ;;
        esac
    done
    preflight
    confirm
    packages
    make_user
    sources
    nebula_code
    config
    boot_settings
    system_files
    start_services
    finish
}

main "$@"
