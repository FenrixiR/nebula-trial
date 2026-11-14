#!/bin/sh
set -e
BIN=centauri-link/target/armv7-unknown-linux-musleabihf/release/centauri-link
M=${1:-nebula/manifests/26.09.0.toml}
[ -f $BIN ] || { echo "build it first: cd centauri-link && cargo build --release --target armv7-unknown-linux-musleabihf"; exit 1; }
mkdir -p nebula/relay nebula/screen
HX=nebula/screen/helixscreen-cc1-v1.0.6.tar.gz
[ -f $HX ] || curl -sSL -o $HX https://github.com/prestonbrown/helixscreen/releases/download/v1.0.6/helixscreen-cc1-v1.0.6.tar.gz
cp $BIN nebula/relay/centauri-link
python3 -m nebula.hijack pin --manifest $M --payload nebula
python3 -m nebula.hijack plan --manifest $M --payload nebula
