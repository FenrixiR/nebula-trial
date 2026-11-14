#!/usr/bin/env bash
set -euo pipefail

BASE=${NEBULA_BASE:-https://github.com/FenrixiR/nebula-trial/releases/latest/download}
NEBULA_CONF=${NEBULA_CONF:-/etc/nebula/update.conf}
if [ -z "${NEBULA_BASE:-}" ] && [ -r "$NEBULA_CONF" ]; then
    . "$NEBULA_CONF"
fi
SIGNER=nebula-release
SIGNER_KEY="@SIGNER_KEY@"
VERSION_FILE=${NEBULA_VERSION_FILE:-/home/nebula/host/VERSION}
STATUS_FILE=${NEBULA_STATUS_FILE:-/var/lib/nebula/update.json}
LOG_FILE=${NEBULA_UPDATE_LOG:-/var/log/nebula-update.log}
MOONRAKER=${NEBULA_MOONRAKER:-http://127.0.0.1:7125}
TMP=

say() { printf '%s\n' "$*"; }
die() { printf 'nebula-install: %s\n' "$*" >&2; exit 1; }

usage() {
    say "Installs Nebula on this machine. Safe to run again: it repairs and updates what is already there."
    say "  --yes        do not ask for confirmation"
    say "  --check      say whether a newer version is available (changes nothing)"
    say "  --update     install the newest version (refuses while a print is running)"
    say "  --uninstall  remove Nebula from this machine (same as: sudo nebula-uninstall)"
    say "  --help       this text"
}

fetch() {
    case $BASE in
        file://*) cp "${BASE#file://}/$1" "$2" ;;
        *) curl -fsSL --retry 3 -o "$2" "$BASE/$1" ;;
    esac
}

machine() {
    case $(uname -m) in
        aarch64 | arm64) echo aarch64 ;;
        x86_64 | amd64) echo x86_64 ;;
        armv7l | armv6l | i?86) die "this is a 32-bit system ($(uname -m)); Nebula needs a 64-bit one" ;;
        *) die "unsupported processor: $(uname -m)" ;;
    esac
}

verify() {
    local dir=$1 signers=${NEBULA_ALLOWED_SIGNERS:-}
    if [ -z "$signers" ]; then
        signers=$dir/allowed_signers
        printf '%s %s\n' "$SIGNER" "$SIGNER_KEY" > "$signers"
    fi
    command -v ssh-keygen > /dev/null || die "ssh-keygen is needed to check the download (install openssh-client)"
    ssh-keygen -Y verify -f "$signers" -I "$SIGNER" -n nebula -s "$dir/SHA256SUMS.sig" < "$dir/SHA256SUMS" > /dev/null 2>&1 \
        || die "the signature on the download does not match: nothing was installed"
    (cd "$dir" && sha256sum --check --quiet --ignore-missing SHA256SUMS) \
        || die "a downloaded file does not match its checksum: nothing was installed"
}

workdir() {
    TMP=$(mktemp -d)
    chmod 755 "$TMP"
}

cleanup() {
    local rc=$?
    if [ -n "${UPDATING:-}" ] && grep -q '"state": "running"' "$STATUS_FILE" 2> /dev/null; then
        status failed "the update did not finish; the previous version was kept (details: $LOG_FILE)"
    fi
    [ -n "$TMP" ] && rm -rf "${TMP:?}"
    exit "$rc"
}
trap cleanup EXIT

installed_version() { cat "$VERSION_FILE" 2> /dev/null || echo none; }

newer() {
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1)" = "$2" ]
}

latest_info() {
    fetch SHA256SUMS "$TMP/SHA256SUMS"
    fetch SHA256SUMS.sig "$TMP/SHA256SUMS.sig"
    fetch latest.json "$TMP/latest.json"
    verify "$TMP"
}

json_field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }

check() {
    command -v python3 > /dev/null || die "python3 is needed to read the version information"
    workdir
    latest_info
    python3 - "$TMP/latest.json" "$(installed_version)" << 'PY'
import json, sys
latest = json.load(open(sys.argv[1]))
installed = sys.argv[2]
def key(v):
    return [int(p) if p.isdigit() else p for p in v.replace("-", ".").split(".")]
try:
    newer = latest["version"] != installed and key(latest["version"]) > key(installed)
except TypeError:
    newer = latest["version"] != installed
print(json.dumps({"installed": installed, "available": latest["version"], "newer": newer,
                  "released": latest.get("released", ""), "notes": latest.get("notes", [])}))
PY
}

status() {
    mkdir -p "$(dirname "$STATUS_FILE")"
    printf '{"state": "%s", "from": "%s", "to": "%s", "message": "%s", "at": "%s"}\n' \
        "$1" "${FROM:-}" "${TO:-}" "$2" "$(date '+%F %T')" > "$STATUS_FILE.new"
    chmod 644 "$STATUS_FILE.new"
    mv "$STATUS_FILE.new" "$STATUS_FILE"
}

printing() {
    local state
    state=$(curl -fsS -m 3 "$MOONRAKER/printer/objects/query?print_stats" 2> /dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["status"]["print_stats"]["state"])' 2> /dev/null) || return 1
    [ "$state" = printing ] || [ "$state" = paused ]
}

package() {
    local name=nebula-linux-$(machine).tar.gz
    say "Downloading Nebula for $(machine) ..."
    fetch "$name" "$TMP/$name"
    [ -s "$TMP/latest.json" ] || fetch latest.json "$TMP/latest.json"
    verify "$TMP"
    say "Download verified: Nebula $(json_field "$TMP/latest.json" version 2> /dev/null || echo)."
    mkdir "$TMP/pkg"
    tar xzf "$TMP/$name" -C "$TMP/pkg"
}

run_setup() {
    local run=("$TMP/pkg/installer/setup.sh" "$@")
    if [ "$(id -u)" -eq 0 ]; then bash "${run[@]}"; else sudo bash "${run[@]}"; fi
}

update() {
    [ "$(id -u)" -eq 0 ] || exec sudo -n "$(readlink -f "$0")" --update --yes
    command -v python3 > /dev/null || die "python3 is needed to read the version information"
    if [ -z "${NEBULA_DETACHED:-}" ] && [ -f "$0" ] && [ -d /run/systemd/system ] && command -v systemd-run > /dev/null; then
        mkdir -p "$(dirname "$LOG_FILE")"
        systemctl reset-failed nebula-updating.service 2> /dev/null || true
        exec systemd-run --quiet --collect --unit=nebula-updating --setenv=NEBULA_DETACHED=1 \
            -p StandardOutput=append:"$LOG_FILE" -p StandardError=append:"$LOG_FILE" "$(readlink -f "$0")" --update --yes
    fi
    FROM=$(installed_version)
    UPDATING=1
    workdir
    status running "checking for a newer version"
    latest_info
    TO=$(json_field "$TMP/latest.json" version)
    if ! newer "$FROM" "$TO" && [ -z "${NEBULA_FORCE:-}" ]; then
        status done "already up to date ($FROM)"
        say "Already up to date ($FROM)."
        return 0
    fi
    if printing; then
        status failed "a print is in progress; update when it has finished"
        die "a print is in progress; update when it has finished"
    fi
    status running "downloading $TO"
    package
    status running "installing $TO"
    run_setup --yes --update
    status done "updated from $FROM to $TO"
}

install() {
    local yes=$1
    command -v systemctl > /dev/null || die "this system has no systemd"
    command -v apt-get > /dev/null || die "this installer supports Debian-based systems (Debian, Raspberry Pi OS, Armbian, Ubuntu)"
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo > /dev/null || die "run this as root, or install sudo"
    fi
    machine > /dev/null
    workdir
    fetch SHA256SUMS "$TMP/SHA256SUMS"
    fetch SHA256SUMS.sig "$TMP/SHA256SUMS.sig"
    package
    local opts=()
    [ "$yes" -eq 1 ] && opts+=(--yes)
    run_setup "${opts[@]+"${opts[@]}"}"
}

main() {
    local yes=0 arg mode=install
    for arg in "$@"; do
        case $arg in
            --yes | -y) yes=1 ;;
            --check) mode=check ;;
            --update) mode=update ;;
            --uninstall) mode=uninstall ;;
            --help | -h) usage; exit 0 ;;
            *) die "unknown option: $arg (try --help)" ;;
        esac
    done

    [ "$(uname -s)" = Linux ] || die "Nebula runs on Linux only"
    case $mode in
        uninstall)
            [ -x /usr/local/sbin/nebula-uninstall ] || die "Nebula is not installed on this machine"
            local opts=()
            [ "$yes" -eq 1 ] && opts+=(--yes)
            if [ "$(id -u)" -eq 0 ]; then exec /usr/local/sbin/nebula-uninstall "${opts[@]+"${opts[@]}"}"; fi
            exec sudo /usr/local/sbin/nebula-uninstall "${opts[@]+"${opts[@]}"}"
            ;;
        check) check ;;
        update) update ;;
        install) install "$yes" ;;
    esac
}

main "$@"
