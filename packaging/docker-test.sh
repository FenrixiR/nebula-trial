#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

work=$(mktemp -d)
trap 'rm -rf "${work:?}"' EXIT
ssh-keygen -q -t ed25519 -N '' -f "$work/key"
mkdir -p "$work/relay"
cp centauri-link/target/release/centauri-link "$work/relay/centauri-link-x86_64"
cp "$work/relay/centauri-link-x86_64" "$work/relay/centauri-link-aarch64"
NEBULA_SIGNING_KEY="$work/key" NEBULA_DIST="$work/dist" NEBULA_RELAY_BIN_DIR="$work/relay" NEBULA_ARCHES=x86_64 \
    bash packaging/build-release.sh 0.0.1-test > /dev/null
printf 'Faster start-up\nA new Updates card\n' > "$work/notes"
for v in 0.0.2-test 0.0.3-test; do
    NEBULA_SIGNING_KEY="$work/key" NEBULA_DIST="$work/dist-$v" NEBULA_RELAY_BIN_DIR="$work/relay" NEBULA_ARCHES=x86_64 NEBULA_NOTES="$work/notes" \
        bash packaging/build-release.sh "$v" > /dev/null
done
cp packaging/container-check.sh "$work/"

chmod 755 "$work"; chmod -R a+rX "$work"
docker run --rm -e DBG=${DBG:-} --platform linux/amd64 -v "$work:/w:ro" debian:trixie-slim bash /w/container-check.sh
