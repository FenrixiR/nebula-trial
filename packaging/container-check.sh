#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq openssh-client curl git ca-certificates > /dev/null

printf '#!/bin/sh\necho "systemctl $*" >> /var/log/systemctl-calls\nexit 0\n' > /usr/local/bin/systemctl
chmod +x /usr/local/bin/systemctl
git config --system safe.directory '*'
git config --system uploadpack.allowAnySHA1InWant true
git config --global user.email t@t && git config --global user.name t

fake() {
    mkdir -p "$1/scripts"
    : > "$1/scripts/$2-requirements.txt"
    git -C "$1" init -q && git -C "$1" add -A && git -C "$1" commit -q -m x
    git -C "$1" rev-parse HEAD
}
export KALICO_URL=/srv/kalico MOONRAKER_URL=/srv/moonraker
KALICO_COMMIT=$(fake /srv/kalico klippy); MOONRAKER_COMMIT=$(fake /srv/moonraker moonraker)
mkdir -p /srv/screen && echo fake-screen > /srv/screen/helixscreen-fake.tar.gz
export HELIXSCREEN_URL=file:///srv/screen/helixscreen-fake.tar.gz HELIXSCREEN_SHA256=$(sha256sum /srv/screen/helixscreen-fake.tar.gz | cut -d' ' -f1)
export KALICO_COMMIT MOONRAKER_COMMIT NEBULA_TEST=1 NEBULA_BASE=file:///w/dist

fail() { echo "FAIL: $*"; exit 1; }
ok() { echo "ok: $*"; }

NEBULA_DEBUG=${DBG:-} bash /w/dist/install.sh --yes > /tmp/run1.log 2>&1 || { tail -30 /tmp/run1.log; fail "first run"; }
tail -6 /tmp/run1.log

id nebula > /dev/null && ok "user created"
H=/home/nebula
for f in host/nebula/panel/server.py host/VERSION bin/centauri-link printer_data/config/printer.cfg printer_data/config/moonraker.conf \
         printer_data/config/machine/mcu.cfg printer_data/config/macros/nozzle.cfg printer_data/config/user/00_keep.cfg \
         kalico/scripts/klippy-requirements.txt klippy-env/bin/python moonraker-env/bin/python; do
    [ -e "$H/$f" ] || fail "missing $f"
done
[ -e $H/kalico/klippy/plugins/centauri_hx711s.py ] || fail "plugin link dangling"
ok "files in place"
[ -s $H/host/nebula/screen/helixscreen-fake.tar.gz ] || fail "screen payload not fetched"
[ "$(cat $H/host/VERSION)" = 0.0.1-test ] || fail "version"
grep -q "^serial: /home/nebula/printer_data/comms/mcu-mainboard" $H/printer_data/config/machine/mcu.cfg || fail "mcu.cfg not rendered"
! grep -rq '@HOME@' $H/printer_data/config || fail "placeholder left in config"
[ "$(readlink $H/printer_data/config/nebula)" = /home/nebula/.nebula/kalico ] || fail "tuning folder not linked into the Kalico config"
ok "config rendered"
[ -z "$(find $H ! -user nebula)" ] || fail "files in the nebula home not owned by nebula: $(find $H ! -user nebula | head -3)"
for f in /etc/systemd/system/nebula-panel.service /etc/systemd/system/nebula-relay-client.service /etc/systemd/system/centauri-usb-gadget.service \
         /etc/systemd/system/nebula-linkdoctor.service /usr/local/sbin/centauri-usb-gadget.sh /etc/dnsmasq.d/dnsmasq-centauri-usb.conf \
         /etc/polkit-1/rules.d/50-nebula-moonraker.rules /etc/sudoers.d/nebula /etc/modules-load.d/nebula.conf; do
    [ -e "$f" ] || fail "missing $f"
done
visudo -cf /etc/sudoers.d/nebula > /dev/null || fail "sudoers invalid"
[ "$(stat -c %a /etc/sudoers.d/nebula)" = 440 ] || fail "sudoers mode"
ok "system files in place"
grep -q 'restart nebula-panel' /var/log/systemctl-calls || fail "panel not started"

echo "# my own change" >> $H/printer_data/config/printer.cfg
echo "# my own macro" > $H/printer_data/config/user/mine.cfg
bash /w/dist/install.sh --yes > /tmp/run2.log 2>&1 || { tail -30 /tmp/run2.log; fail "second run"; }
grep -q 'kalico: already at' /tmp/run2.log || fail "second run refetched kalico"
grep -q '# my own change' $H/printer_data/config/printer.cfg || fail "user printer.cfg overwritten"
[ -f $H/printer_data/config/user/mine.cfg ] || fail "user drop-in removed"
[ -d $H/host.prev ] || fail "no previous version kept"
ok "second run: repaired in place, user files kept, previous version kept"

printf 'wrong' > /tmp/x
cp -r /w/dist /tmp/bad && echo x >> /tmp/bad/nebula-linux-x86_64.tar.gz
NEBULA_BASE=file:///tmp/bad bash /w/dist/install.sh --yes > /tmp/run3.log 2>&1 && fail "tampered package accepted"
grep -q 'nothing was installed' /tmp/run3.log || fail "tamper message"
ok "tampered package refused"

mkdir -p /tmp/panelroot/nebula /tmp/mr/printer/objects
echo ok > /tmp/panelroot/nebula/index.html
python3 -m http.server 8080 --bind 127.0.0.1 --directory /tmp/panelroot > /dev/null 2>&1 &
PANEL_STUB=$!
sleep 1
V2=file:///w/dist-0.0.2-test
V3=file:///w/dist-0.0.3-test
status() { python3 -c 'import json;print(json.load(open("/var/lib/nebula/update.json"))["'$1'"])'; }

out=$(runuser -u nebula -- env NEBULA_BASE=$V2 /usr/local/sbin/nebula-update --check) || fail "check as the nebula user"
echo "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["newer"] and d["available"]=="0.0.2-test" and d["installed"]=="0.0.1-test" and len(d["notes"])==2, d' || fail "check output: $out"
ok "check: newer version reported with its notes"
runuser -u nebula -- sudo -n -l /usr/local/sbin/nebula-update --update --yes > /dev/null || fail "the panel user may not start the update"
! runuser -u nebula -- sudo -n -l /usr/local/sbin/nebula-update --force > /dev/null 2>&1 || fail "the panel user may run more than the update"
ok "the panel user may start exactly the update"

NEBULA_BASE=$V2 NEBULA_DETACHED=1 nebula-update --update --yes > /tmp/up1.log 2>&1 || { tail -20 /tmp/up1.log; fail "update"; }
[ "$(cat $H/host/VERSION)" = 0.0.2-test ] || fail "version after update"
[ "$(cat $H/host.prev/VERSION)" = 0.0.1-test ] || fail "previous version not kept"
[ "$(status state)" = done ] && [ "$(status to)" = 0.0.2-test ] || fail "status after update: $(cat /var/lib/nebula/update.json)"
[ -z "$(find $H ! -user nebula)" ] || fail "ownership after update"
grep -q '# my own change' $H/printer_data/config/printer.cfg || fail "user config lost in the update"
ok "update applied, previous version and config kept"

NEBULA_BASE=$V2 NEBULA_DETACHED=1 nebula-update --update --yes > /tmp/up2.log 2>&1 || fail "second update run"
grep -q 'Already up to date' /tmp/up2.log || fail "no 'already up to date'"
ok "update when current says so and changes nothing"

echo '{"result": {"status": {"print_stats": {"state": "printing"}}}}' > /tmp/mr/printer/objects/query
python3 -m http.server 7125 --bind 127.0.0.1 --directory /tmp/mr > /dev/null 2>&1 &
MR_STUB=$!
sleep 1
NEBULA_BASE=$V3 NEBULA_DETACHED=1 nebula-update --update --yes > /tmp/up3.log 2>&1 && fail "update during a print"
[ "$(cat $H/host/VERSION)" = 0.0.2-test ] || fail "version changed during a print"
[ "$(status state)" = failed ] && grep -q 'print is in progress' /var/lib/nebula/update.json || fail "status during a print: $(cat /var/lib/nebula/update.json)"
ok "update refused while a print is running"
kill $MR_STUB

kill $PANEL_STUB
NEBULA_BASE=$V3 NEBULA_DETACHED=1 nebula-update --update --yes > /tmp/up4.log 2>&1 && fail "update with a dead panel was accepted"
[ "$(cat $H/host/VERSION)" = 0.0.2-test ] || fail "no rollback: $(cat $H/host/VERSION)"
[ "$(status state)" = failed ] || fail "status after rollback"
grep -q 'previous version was restored' /tmp/up4.log || fail "rollback message"
ok "new version that does not start is rolled back"
python3 -m http.server 8080 --bind 127.0.0.1 --directory /tmp/panelroot > /dev/null 2>&1 &
PANEL_STUB=$!

gone() { for f in "$@"; do [ ! -e "$f" ] && [ ! -L "$f" ] || fail "still there: $f"; done; }
SYSTEM_FILES="/etc/systemd/system/nebula-panel.service /etc/systemd/system/nebula-relay-client.service /etc/systemd/system/centauri-usb-gadget.service
  /etc/systemd/system/nebula-linkdoctor.service /usr/local/sbin/centauri-usb-gadget.sh /usr/local/sbin/nebula-linkdoctor.sh
  /etc/dnsmasq.d/dnsmasq-centauri-usb.conf /etc/polkit-1/rules.d/50-nebula-moonraker.rules /etc/sudoers.d/nebula /etc/modules-load.d/nebula.conf"
[ -x /usr/local/sbin/nebula-uninstall ] || fail "the uninstaller was not installed"

nebula-uninstall --keep-data --yes > /tmp/un1.log 2>&1 || { tail -20 /tmp/un1.log; fail "uninstall --keep-data"; }
gone $SYSTEM_FILES $H/host $H/kalico $H/moonraker $H/klippy-env $H/moonraker-env $H/centauri-pack $H/bin /usr/local/sbin/nebula-uninstall
id nebula > /dev/null || fail "user removed although --keep-data"
grep -q '# my own change' $H/printer_data/config/printer.cfg || fail "config lost although --keep-data"
[ -d $H/.nebula ] || fail "settings lost although --keep-data"
ok "uninstall --keep-data: program and system files gone, settings and config kept"

bash /w/dist/install.sh --yes > /tmp/run4.log 2>&1 || { tail -20 /tmp/run4.log; fail "reinstall after uninstall"; }
grep -q '# my own change' $H/printer_data/config/printer.cfg || fail "kept config not picked up again"
[ -z "$(find $H ! -user nebula)" ] || fail "ownership after reinstall"
ok "reinstall on top of the kept data"

bash /w/dist/install.sh --uninstall --yes > /tmp/un2.log 2>&1 || { tail -20 /tmp/un2.log; fail "uninstall through the loader"; }
gone $SYSTEM_FILES $H /usr/local/sbin/nebula-uninstall
! id nebula > /dev/null 2>&1 || fail "user still exists"
ok "uninstall through the loader: everything gone, user removed"

bash /w/dist/install.sh --uninstall --yes > /tmp/un3.log 2>&1 && fail "uninstall without an install succeeded"

useradd --create-home --shell /bin/bash nebula
echo mine > /home/nebula/mine.txt
bash /w/dist/install.sh --yes > /tmp/run5.log 2>&1 || { tail -20 /tmp/run5.log; fail "install over an existing nebula account"; }
[ ! -f /var/lib/nebula/created-user ] || fail "the account was not made by the installer but is marked as made by it"
bash /w/dist/install.sh --uninstall --yes > /tmp/un4.log 2>&1 || { tail -20 /tmp/un4.log; fail "uninstall with a pre-existing account"; }
id nebula > /dev/null 2>&1 || fail "a pre-existing account was removed"
[ "$(cat /home/nebula/mine.txt)" = mine ] || fail "the account's own files were removed"
gone $SYSTEM_FILES $H/host $H/.nebula $H/printer_data
ok "uninstall keeps an account that was there before Nebula, and its files"
grep -q 'not installed' /tmp/un3.log || fail "message for nothing to remove"
ok "uninstall with nothing installed says so"
echo ALL-OK
