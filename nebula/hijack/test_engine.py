import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from . import engine as e

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REAL_MANIFEST = os.path.join(REPO, "nebula", "manifests", "26.09.0.toml")
PORTS = []


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


RELAY_OK = """#!/usr/bin/env python3
import re, socket, sys, time
socks = []
for a in sys.argv:
    m = re.search(r"@(\\d+)$", a)
    if m:
        s = socket.socket(); s.setsockopt(1, 2, 1); s.bind(("127.0.0.1", int(m.group(1)))); s.listen(); socks.append(s)
time.sleep(600)
"""
RELAY_BAD = "#!/bin/sh\nexit 3\n"

CALM = {"result": {"status": {"print_stats": {"state": "standby"}, "extruder": {"target": 0.0}, "heater_bed": {"target": 0.0}}}}


class Sandbox:

    def __init__(self, moonraker=CALM):
        self.d = tempfile.mkdtemp(prefix="nebula-sbx-")
        for sub in ("etc/init.d", "lib/firmware", "run", "user-resource"):
            os.makedirs(os.path.join(self.d, sub))
        w = lambda p, t: open(os.path.join(self.d, p), "w").write(t)
        w("etc/issue", "OpenCentauri Cosmos 26.09.0 \\n \\l\n")
        for n in ("bed", "toolhead"):
            v = "4c37a204e5c2ed31b979baba7fb4d32a587aa590-r11"
            w(f"etc/{n}.ver", v + "\n")
            w(f"lib/firmware/klipper-{n}.bin.ver", v + "\n")
        for n in ("klipper", "moonraker", "gui-switcher", "klipper-firmware-bed"):
            p = os.path.join(self.d, "etc/init.d", n)
            w(f"etc/init.d/{n}", f'#!/bin/sh\necho "{n} $1" >> {self.d}/initlog\n')
            os.chmod(p, 0o755)
        self.moonraker = moonraker

    def map(self, text):
        return re.sub(r"(?<![\w.-])/(etc/|lib/firmware|run/|user-resource)", lambda m: self.d + "/" + m.group(1), text)

    def run(self, cmd, timeout=60):
        if cmd.startswith("wget -q -T 3 -O - 'http://127.0.0.1"):
            return (0, json.dumps(self.moonraker)) if self.moonraker else (1, "")
        env = dict(os.environ, INITDIR=self.d + "/etc/init.d", RUNDIR=self.d + "/run")
        p = subprocess.run(["sh", "-c", self.map(cmd)], capture_output=True, text=True, env=env, timeout=timeout)
        return p.returncode, p.stdout + p.stderr

    def put(self, data, remote, mode):
        path = self.map(remote)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, int(mode, 8))

    def log(self):
        try:
            return open(self.d + "/initlog").read().split("\n")[:-1]
        except FileNotFoundError:
            return []

    def cleanup(self):
        env = dict(os.environ, INITDIR=self.d + "/etc/init.d", RUNDIR=self.d + "/run", NEBULA_ROOT=self.d + "/user-resource/nebula")
        if os.path.exists(env["NEBULA_ROOT"] + "/nebula-ctl.sh"):
            subprocess.run(["sh", env["NEBULA_ROOT"] + "/nebula-ctl.sh", "release"], env=env, capture_output=True)
        shutil.rmtree(self.d, ignore_errors=True)


def make_payload(relay_text):
    d = tempfile.mkdtemp(prefix="nebula-payload-")
    os.makedirs(d + "/printer")
    os.makedirs(d + "/relay")
    for n in ("nebula-ctl.sh", "nebula-boot.sh", "udhcpc-address-only.sh", "screen-start.sh"):
        shutil.copy(os.path.join(REPO, "nebula/printer", n), d + "/printer/" + n)
    open(d + "/relay/centauri-link", "w").write(relay_text)
    os.makedirs(d + "/screen")
    os.makedirs(d + "/fake/helixscreen/bin")
    open(d + "/fake/helixscreen/bin/helix-launcher.sh", "w").write("#!/bin/sh\nwhile :; do sleep 1; done\n")
    os.chmod(d + "/fake/helixscreen/bin/helix-launcher.sh", 0o755)
    subprocess.run(["tar", "czf", d + "/screen/helixscreen-cc1-v1.0.6.tar.gz", "-C", d + "/fake", "helixscreen"], check=True)
    shutil.copy(os.path.join(REPO, "nebula/screen/settings.json"), d + "/screen/settings.json")
    return d


def make_manifest(payload, ports):
    text = open(REAL_MANIFEST).read()
    text = text.replace("ports = [7001, 7002, 7003]", f"ports = {ports}")
    text = text.replace("mainboard=/dev/ttyRPMSG0@7001", f"mainboard=/dev/null@{ports[0]}")
    text = text.replace("bed=/dev/ttyS4:250000@7002", f"bed=/dev/null@{ports[1]}")
    text = text.replace("toolhead=/dev/serial/by-path/platform-4101400.usb-usb-0:1:1.0@7003", f"toolhead=/dev/null@{ports[2]}")
    text = text.replace("watchdog_minutes = 10", "watchdog_minutes = 0")
    path = payload + "/manifest.toml"
    open(path, "w").write(text)
    e.pin_manifest(path, payload)
    return e.load_manifest(path), path


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.payload = make_payload(RELAY_OK)
        self.ports = [free_port() for _ in range(3)]
        self.m, self.mpath = make_manifest(self.payload, self.ports)

    def tearDown(self):
        self.sb.cleanup()
        shutil.rmtree(self.payload, ignore_errors=True)

    def wait_ports(self):
        for _ in range(50):
            if all(socket.socket().connect_ex(("127.0.0.1", p)) == 0 for p in self.ports):
                return True
            time.sleep(0.1)
        return False

    def test_real_manifest_loads_and_protects_firmware_scripts(self):
        m = e.load_manifest(REAL_MANIFEST)
        self.assertEqual(m.stop, ["gui-switcher", "moonraker", "klipper"])
        bad = open(REAL_MANIFEST).read().replace('order = ["gui-switcher", "moonraker", "klipper"]',
                                                  'order = ["gui-switcher", "klipper-firmware-bed"]')
        p = tempfile.mktemp(suffix=".toml")
        open(p, "w").write(bad)
        with self.assertRaises(e.NebulaError):
            e.load_manifest(p)

    def test_purge_runs_the_printer_side_purge_and_reports_what_is_left(self):
        class Recorder:
            def __init__(self, left):
                self.cmds, self.left = [], left

            def run(self, cmd, timeout=120):
                self.cmds.append(cmd)
                return 0, self.left if cmd.startswith("ls -d") else ""

        m = e.load_manifest(REAL_MANIFEST)
        r = Recorder("")
        self.assertEqual(e.purge(r, m), [])
        self.assertIn("nebula-ctl.sh purge", r.cmds[0])
        self.assertIn("[ -f", r.cmds[0])
        self.assertEqual(e.purge(Recorder("/user-resource/nebula\n"), m), ["/user-resource/nebula"])

    def test_purge_failure_is_reported(self):
        class Failing:
            def run(self, cmd, timeout=120):
                return 1, "boom"

        with self.assertRaisesRegex(e.NebulaError, "removing Nebula from the printer failed"):
            e.purge(Failing(), e.load_manifest(REAL_MANIFEST))

    def test_boot_hook_pushes_only_the_small_scripts_and_reports_its_state(self):
        class Recorder:
            def __init__(self):
                self.cmds, self.puts, self.on = [], [], False

            def run(self, cmd, timeout=120):
                self.cmds.append(cmd)
                if cmd.startswith("sha256sum "):
                    name = cmd.split()[-1].strip("'").rsplit("/", 1)[-1]
                    return 0, e.sha256_file(os.path.join(REPO, "nebula", "printer", name)) + "  " + name
                if cmd.endswith("persist on"):
                    self.on = True
                elif cmd.endswith("persist off"):
                    self.on = False
                elif cmd.endswith("persist status"):
                    return 0, "persist: on" if self.on else "persist: off"
                return 0, ""

            def put(self, data, remote, mode):
                self.puts.append(remote.rsplit("/", 1)[-1])

        payload = make_payload("x")
        self.addCleanup(shutil.rmtree, payload, True)
        m = e.load_manifest(REAL_MANIFEST)
        r = Recorder()
        self.assertTrue(e.cable_boot(r, m, payload, "on"))
        self.assertEqual(sorted(r.puts), sorted(["nebula-ctl.sh", "nebula-boot.sh", "udhcpc-address-only.sh", "nebula.env"]))
        self.assertFalse(e.cable_boot(r, m, payload, "off"))
        self.assertEqual(len(r.puts), 4)

    def test_shipped_scripts_match_pins(self):
        m = e.load_manifest(REAL_MANIFEST)
        for name in ("nebula-ctl.sh", "nebula-boot.sh", "udhcpc-address-only.sh", "screen-start.sh"):
            self.assertEqual(m.files[name]["sha256"], e.sha256_file(os.path.join(REPO, "nebula", m.files[name]["source"])),
                             f"{name} changed: run `python3 -m nebula.hijack pin`")

    def test_unpinned_manifest_is_refused(self):
        m = e.load_manifest(REAL_MANIFEST)
        with self.assertRaises(e.NebulaError):
            e.apply(self.sb, m, self.payload, log=lambda s: None)

    def test_apply_then_release(self):
        e.apply(self.sb, self.m, self.payload, log=lambda s: None)
        self.assertTrue(self.wait_ports())
        self.assertEqual(self.sb.log(), ["gui-switcher stop", "moonraker stop", "klipper stop"])
        root = self.sb.d + "/user-resource/nebula"
        self.assertEqual(oct(os.stat(root + "/nebula-ctl.sh").st_mode & 0o777), "0o755")
        self.assertIn("STOP_ORDER='gui-switcher moonraker klipper'", open(root + "/nebula.env").read())
        self.assertNotIn("klipper-firmware", "".join(self.sb.log()))
        rc, out = e.ctl(self.sb, self.m, "verify")
        self.assertEqual(rc, 0, out)
        e.release(self.sb, self.m)
        self.assertEqual(self.sb.log()[3:], ["klipper start", "moonraker start", "gui-switcher start"])
        self.assertFalse(os.path.exists(self.sb.d + "/run/nebula-mode"))
        time.sleep(0.3)
        self.assertNotEqual(socket.socket().connect_ex(("127.0.0.1", self.ports[0])), 0)

    def test_screen_app_is_unpacked_once_started_and_stopped(self):
        e.apply(self.sb, self.m, self.payload, log=lambda s: None)
        root = self.sb.d + "/user-resource/nebula"
        self.assertTrue(os.path.exists(root + "/helixscreen/bin/helix-launcher.sh"))
        self.assertFalse(os.path.exists(root + "/helixscreen-cc1.tgz"))
        self.assertTrue(os.path.exists(root + "/.unpacked-helixscreen-cc1.tgz.sha256"))
        self.assertIn('"wizard_completed": true', open(root + "/helixscreen/config/settings.json").read())
        rc, out = e.ctl(self.sb, self.m, "status")
        self.assertIn("screen: running", out)
        self.assertEqual(e.ctl(self.sb, self.m, "verify")[0], 0)
        e.release(self.sb, self.m)
        time.sleep(0.5)
        rc, out = subprocess.run(["pgrep", "-f", "[h]elix-launcher.sh --moonraker"], capture_output=True, text=True).returncode, ""
        self.assertEqual(rc, 1, "the screen app must be gone after release")
        os.remove(root + "/helixscreen/bin/helix-launcher.sh")
        e.push_all(self.sb, self.m, self.payload)
        self.assertFalse(os.path.exists(root + "/helixscreen/bin/helix-launcher.sh"))

    def test_watchdog_probes_the_hosts_moonraker_port(self):
        text = open(os.path.join(REPO, "nebula/printer/nebula-ctl.sh")).read()
        self.assertIn('"http://$HOST_ADDR:${HOST_API_PORT:-7125}/server/info"', text)

    def test_second_apply_only_verifies(self):
        e.apply(self.sb, self.m, self.payload, log=lambda s: None)
        with self.assertRaises(e.NebulaError):
            e.apply(self.sb, self.m, self.payload, log=lambda s: None)
        self.assertEqual(self.sb.log().count("klipper stop"), 1)

    def test_busy_printer_is_refused_before_anything_is_touched(self):
        busy = {"result": {"status": {"print_stats": {"state": "printing"}, "extruder": {"target": 210.0}, "heater_bed": {"target": 60.0}}}}
        self.sb.moonraker = busy
        with self.assertRaises(e.NebulaError) as cm:
            e.apply(self.sb, self.m, self.payload, log=lambda s: None)
        self.assertIn("printer idle", str(cm.exception))
        self.assertEqual(self.sb.log(), [])
        self.assertFalse(os.path.exists(self.sb.d + "/user-resource/nebula"))

    def test_moonraker_silent_is_not_idle(self):
        self.sb.moonraker = None
        with self.assertRaises(e.NebulaError):
            e.apply(self.sb, self.m, self.payload, log=lambda s: None)
        self.assertEqual(self.sb.log(), [])

    def test_firmware_mismatch_is_refused(self):
        open(self.sb.d + "/etc/bed.ver", "w").write("other-r9\n")
        with self.assertRaises(e.NebulaError) as cm:
            e.apply(self.sb, self.m, self.payload, log=lambda s: None)
        self.assertIn("bed MCU firmware", str(cm.exception))
        self.assertEqual(self.sb.log(), [])

    def test_unknown_cosmos_version_is_refused(self):
        open(self.sb.d + "/etc/issue", "w").write("OpenCentauri Cosmos 27.01.0\n")
        with self.assertRaises(e.NebulaError):
            e.apply(self.sb, self.m, self.payload, log=lambda s: None)

    def test_failed_launch_releases_automatically(self):
        open(self.payload + "/relay/centauri-link", "w").write(RELAY_BAD)
        e.pin_manifest(self.mpath, self.payload)
        m = e.load_manifest(self.mpath)
        with self.assertRaises(e.NebulaError) as cm:
            e.apply(self.sb, m, self.payload, log=lambda s: None)
        self.assertIn("released to stock", str(cm.exception))
        self.assertEqual(self.sb.log(), ["gui-switcher stop", "moonraker stop", "klipper stop",
                                         "klipper start", "moonraker start", "gui-switcher start"])

    def test_corrupt_push_is_detected(self):
        orig = self.sb.put
        def bad_put(data, remote, mode):
            orig(data[:-1] if remote.endswith("centauri-link") else data, remote, mode)
        self.sb.put = bad_put
        with self.assertRaises(e.NebulaError) as cm:
            e.apply(self.sb, self.m, self.payload, log=lambda s: None)
        self.assertIn("hash mismatch", str(cm.exception))
        self.assertEqual(self.sb.log(), [])

    def test_plan_lists_everything_and_touches_nothing(self):
        text = "\n".join(e.plan(self.m, self.payload))
        for needle in ("Stop the printer's own software: gui-switcher, moonraker, klipper", "settings file", "pass control back to Cosmos"):
            self.assertIn(needle, text)
        self.assertNotIn("klipper-firmware", text)


if __name__ == "__main__":
    unittest.main()
