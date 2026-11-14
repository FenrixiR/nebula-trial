#!/usr/bin/env bash
set -euo pipefail

VERSION=${1:?usage: build-release.sh VERSION}
OUT=${NEBULA_DIST:-dist}
KEY=${NEBULA_SIGNING_KEY:-}
PUBFILE=${KEY:+$KEY.pub}
PUBFILE=${PUBFILE:-${NEBULA_SIGNING_PUBKEY:-}}
[ -n "$PUBFILE" ] && [ -r "$PUBFILE" ] || { echo "set NEBULA_SIGNING_KEY or NEBULA_SIGNING_PUBKEY" >&2; exit 1; }
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
cd "$REPO"

declare -A TRIPLE=([aarch64]=aarch64-unknown-linux-musl [x86_64]=x86_64-unknown-linux-musl)
ARCHES=${NEBULA_ARCHES:-"aarch64 x86_64"}

stage() {
    local arch=$1 dir=$2 bin
    bin=${NEBULA_RELAY_BIN_DIR:-centauri-link/target}/${TRIPLE[$arch]}/release/centauri-link
    [ -x "$bin" ] || bin=${NEBULA_RELAY_BIN_DIR:-centauri-link/target}/centauri-link-$arch
    [ -x "$bin" ] || { echo "missing relay binary for $arch: $bin" >&2; exit 1; }
    mkdir -p "$dir/bin" "$dir/installer" "$dir/host"
    tar cf - --exclude='__pycache__' --exclude='test_*.py' --exclude='*.pyc' --exclude=nebula/README.md --exclude=nebula/build_payload.sh --exclude='nebula/screen/*.tar.gz' nebula | tar xf - -C "$dir"
    cp "$bin" "$dir/bin/centauri-link-$arch"
    cp "$LOADER" "$dir/installer/loader.sh"
    cp "$HERE/installer/setup.sh" "$HERE/installer/uninstall.sh" "$HERE/installer/pins.env" "$HERE/installer/packages.txt" "$dir/installer/"
    tar cf - --exclude=__pycache__ --exclude=live-snapshots --exclude=proposals -C host config | tar xf - -C "$dir/host"
    mkdir -p "$dir/host/services" "$dir/host/systemd" "$dir/host/polkit" "$dir/host/sudoers"
    cp host/services/centauri-usb-gadget.sh host/services/dnsmasq-centauri-usb.conf "$dir/host/services/"
    cp tools/linkdoctor/nebula-linkdoctor.sh "$dir/host/services/"
    cp nebula/host/systemd/nebula-*.service nebula/host/systemd/nebula-*.timer "$dir/host/systemd/"
    cp host/services/centauri-usb-gadget.service tools/linkdoctor/nebula-linkdoctor.service "$HERE/installer/nebula-relay-client.service" "$dir/host/systemd/"
    cp nebula/host/50-nebula-moonraker.rules "$dir/host/polkit/50-nebula-moonraker.rules"
    cp nebula/host/sudoers-nebula "$dir/host/sudoers/nebula"
    cp nebula/host/moonraker.conf "$dir/host/moonraker.conf"
    tar cf - --exclude=__pycache__ -C host pack | tar xf - -C "$dir/host"
    printf '%s\n' "$VERSION" > "$dir/VERSION"
}

write_latest() {
    NOTES_FILE=${NEBULA_NOTES:-} python3 - "$VERSION" << 'PY'
import datetime, json, os, sys
notes = []
if os.environ.get("NOTES_FILE"):
    with open(os.environ["NOTES_FILE"]) as f:
        notes = [l.strip() for l in f if l.strip()]
print(json.dumps({"version": sys.argv[1], "released": datetime.date.today().isoformat(), "notes": notes}, indent=1))
PY
}

main() {
    local arch work pub
    rm -rf "${OUT:?}"
    mkdir -p "$OUT"
    work=$(mktemp -d)
    pub=$(cut -d' ' -f1,2 "$PUBFILE")
    LOADER=$work/loader.sh
    sed "s#@SIGNER_KEY@#$pub#" "$HERE/install.sh" > "$LOADER"
    for arch in $ARCHES; do
        stage "$arch" "$work/$arch"
        tar czf "$OUT/nebula-linux-$arch.tar.gz" --owner=0 --group=0 -C "$work/$arch" .
    done
    cp "$LOADER" "$OUT/install.sh"
    write_latest > "$OUT/latest.json"
    (cd "$OUT" && sha256sum nebula-linux-*.tar.gz install.sh latest.json > SHA256SUMS)
    if [ -n "$KEY" ]; then
        ssh-keygen -Y sign -q -f "$KEY" -n nebula "$OUT/SHA256SUMS"
    else
        echo "SHA256SUMS is not signed yet: ssh-keygen -Y sign -f KEY -n nebula $OUT/SHA256SUMS"
    fi
    rm -rf "${work:?}"
    ls -l "$OUT"
}

main
