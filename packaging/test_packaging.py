import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


class ReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        key = cls.d + "/key"
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key])
        cls.relay = cls.d + "/relay"
        os.makedirs(cls.relay)
        for arch in ("aarch64", "x86_64"):
            with open(f"{cls.relay}/centauri-link-{arch}", "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(f"{cls.relay}/centauri-link-{arch}", 0o755)
        cls.dist = cls.d + "/dist"
        env = dict(os.environ, NEBULA_SIGNING_KEY=key, NEBULA_DIST=cls.dist, NEBULA_RELAY_BIN_DIR=cls.relay)
        cls.build = run(["bash", HERE + "/build-release.sh", "9.9.9"], cwd=REPO, env=env)
        cls.key = key

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d)

    def install(self, dist=None, extra=(), env=None):
        e = dict(os.environ, NEBULA_BASE="file://" + (dist or self.dist), **(env or {}))
        return run(["bash", self.dist + "/install.sh", *extra], env=e)

    def test_the_release_builds_with_both_processor_types_signed(self):
        self.assertEqual(self.build.returncode, 0, self.build.stderr)
        names = sorted(os.listdir(self.dist))
        self.assertEqual(names, ["SHA256SUMS", "SHA256SUMS.sig", "install.sh", "latest.json", "nebula-linux-aarch64.tar.gz", "nebula-linux-x86_64.tar.gz"])

    def test_the_package_has_the_runtime_and_no_tests_or_dev_files(self):
        listing = run(["tar", "tzf", self.dist + "/nebula-linux-x86_64.tar.gz"]).stdout.splitlines()
        joined = "\n".join(listing)
        for needle in ("installer/setup.sh", "installer/loader.sh", "installer/uninstall.sh", "installer/pins.env", "bin/centauri-link-x86_64", "VERSION",
                       "nebula/panel/server.py", "nebula/printer/nebula-ctl.sh", "nebula/relay/centauri-link", "host/systemd/nebula-relay-client.service",
                       "host/config/machine/mcu.cfg", "host/sudoers/nebula", "host/polkit/50-nebula-moonraker.rules"):
            self.assertIn(needle, joined)
        for bad in ("test_", "__pycache__", "live-snapshots", "proposals", "centauri-link-aarch64", "build_payload", "nebula/README", "helixscreen-cc1"):
            self.assertNotIn(bad, joined)

    def dwc2(self, text, fn="dwc2_state"):
        with tempfile.TemporaryDirectory() as d:
            funcs = os.path.join(d, "f.sh")
            with open(os.path.join(REPO, "packaging/installer/setup.sh")) as f:
                src = f.read()
            with open(funcs, "w") as f:
                f.write(re.search(r"^dwc2_state\(\).*?^}\n\ndwc2_host_off\(\).*?^}\n", src, re.S | re.M).group(0))
            cfg = os.path.join(d, "config.txt")
            with open(cfg, "w") as f:
                f.write(text)
            r = run(["bash", "-c", f'. "{funcs}"; {fn} "{cfg}"'])
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(cfg) as f:
                return r.stdout.strip(), f.read()

    def test_the_stock_pi_config_does_not_count_as_usb_device_mode(self):
        stock = "otg_mode=1\n[all]\n[pi4]\narm_boost=1\n[cm5]\ndtoverlay=dwc2,dr_mode=host\n[all]\n"
        self.assertEqual(self.dwc2(stock)[0], "none")
        self.assertEqual(self.dwc2(stock + "dtoverlay=dwc2\n")[0], "peripheral")
        self.assertEqual(self.dwc2("dtoverlay=dwc2,dr_mode=otg\n")[0], "peripheral")

    def test_an_active_host_mode_overlay_is_found_and_commented_out(self):
        text = "[all]\ndtoverlay=dwc2,dr_mode=host\n[cm5]\ndtoverlay=dwc2,dr_mode=host\n"
        self.assertEqual(self.dwc2(text)[0], "host")
        state, fixed = self.dwc2(text, "dwc2_host_off")
        self.assertEqual(fixed, "[all]\n#dtoverlay=dwc2,dr_mode=host\n[cm5]\ndtoverlay=dwc2,dr_mode=host\n")

    def test_the_screen_download_pin_matches_every_manifest(self):
        import tomllib
        with open(os.path.join(REPO, "packaging/installer/pins.env")) as f:
            pinned = re.search(r"HELIXSCREEN_SHA256:=([0-9a-f]{64})", f.read()).group(1)
        mdir = os.path.join(REPO, "nebula/manifests")
        for name in sorted(os.listdir(mdir)):
            with open(os.path.join(mdir, name), "rb") as f:
                files = tomllib.load(f)["files"]
            self.assertEqual(files["helixscreen-cc1.tgz"]["sha256"], pinned, name)

    def test_a_tampered_package_is_refused_before_anything_runs(self):
        bad = self.d + "/tampered"
        shutil.copytree(self.dist, bad)
        for arch in ("aarch64", "x86_64"):
            with open(f"{bad}/nebula-linux-{arch}.tar.gz", "ab") as f:
                f.write(b"x")
        r = self.install(bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing was installed", r.stderr)

    def test_a_list_signed_by_someone_else_is_refused(self):
        other = self.d + "/other"
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", other])
        bad = self.d + "/resigned"
        shutil.copytree(self.dist, bad)
        os.remove(bad + "/SHA256SUMS.sig")
        run(["ssh-keygen", "-Y", "sign", "-q", "-f", other, "-n", "nebula", bad + "/SHA256SUMS"])
        r = self.install(bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("signature", r.stderr)

    def test_the_loader_has_the_release_key_filled_in(self):
        with open(self.dist + "/install.sh") as f:
            text = f.read()
        self.assertNotIn("@SIGNER_KEY@", text)
        self.assertIn("ssh-ed25519 ", text)

    def version_file(self, text):
        path = self.d + "/installed-version"
        with open(path, "w") as f:
            f.write(text)
        return path

    def check(self, installed, dist=None):
        env = dict(os.environ, NEBULA_BASE="file://" + (dist or self.dist), NEBULA_VERSION_FILE=self.version_file(installed))
        return run(["bash", self.dist + "/install.sh", "--check"], env=env)

    def test_check_reports_a_newer_version_and_nothing_else(self):
        import json
        r = self.check("9.9.8")
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout)
        self.assertEqual((info["installed"], info["available"], info["newer"]), ("9.9.8", "9.9.9", True))
        self.assertIn("released", info)

    def test_check_knows_when_there_is_nothing_newer(self):
        import json
        self.assertFalse(json.loads(self.check("9.9.9").stdout)["newer"])
        self.assertFalse(json.loads(self.check("10.0.0").stdout)["newer"])
        self.assertTrue(json.loads(self.check("9.9.10").stdout)["newer"] is False)
        self.assertTrue(json.loads(self.check("none").stdout)["newer"])

    def test_the_download_location_can_be_set_in_a_root_owned_file(self):
        import json
        conf = self.d + "/update.conf"
        with open(conf, "w") as f:
            f.write("BASE=file://" + self.dist + "\n")
        env = dict(os.environ, NEBULA_CONF=conf, NEBULA_VERSION_FILE=self.version_file("9.9.8"))
        env.pop("NEBULA_BASE", None)
        r = run(["bash", self.dist + "/install.sh", "--check"], env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["available"], "9.9.9")

    def test_check_refuses_version_information_that_was_changed(self):
        bad = self.d + "/badlatest"
        shutil.copytree(self.dist, bad)
        with open(bad + "/latest.json", "a") as f:
            f.write(" ")
        r = self.check("9.9.8", bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing was installed", r.stderr)

    def test_the_package_carries_its_own_loader_with_the_key_for_later_updates(self):
        out = run(["tar", "xzOf", self.dist + "/nebula-linux-x86_64.tar.gz", "./installer/loader.sh"]).stdout
        self.assertNotIn("@SIGNER_KEY@", out)
        self.assertIn("ssh-ed25519 ", out)

    def test_unknown_options_and_help(self):
        r = run(["bash", self.dist + "/install.sh", "--nope"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown option", r.stderr)
        r = run(["bash", self.dist + "/install.sh", "--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("--yes", r.stdout)

    def test_a_32_bit_system_is_refused_with_a_plain_message(self):
        d = self.d + "/fakebin"
        os.makedirs(d, exist_ok=True)
        with open(d + "/uname", "w") as f:
            f.write('#!/bin/sh\n[ "$1" = -m ] && echo armv7l || echo Linux\n')
        os.chmod(d + "/uname", 0o755)
        r = run(["bash", self.dist + "/install.sh"], env=dict(os.environ, PATH=d + ":" + os.environ["PATH"]))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("32-bit", r.stderr)

    def test_the_loader_and_setup_are_valid_shell_and_end_by_calling_main(self):
        for p in (self.dist + "/install.sh", HERE + "/installer/setup.sh", HERE + "/build-release.sh"):
            self.assertEqual(run(["bash", "-n", p]).returncode, 0, p)
        for p in (self.dist + "/install.sh", HERE + "/installer/setup.sh"):
            with open(p) as f:
                last = [l for l in f.read().splitlines() if l.strip()][-1]
            self.assertTrue(last.startswith("main"), p)


if __name__ == "__main__":
    unittest.main()
