import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import zipfile

from nebula.addons import engine
from nebula.addons.host import MemoryHost as FakeHost
from nebula.addons.host import SystemHost

HERE = os.path.dirname(os.path.abspath(__file__))


def make_zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return b.getvalue()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = self.d + "/src"
        os.makedirs(self.src)
        self.host = FakeHost()
        self.state = engine.State(self.d + "/state/addons.json")
        self.site = b"server { listen 80; }\n"
        with open(self.src + "/site.conf", "wb") as f:
            f.write(self.site)
        self.zip_a = make_zip({"index.html": b"A", "assets/app.js": b"js"})
        self.zip_b = make_zip({"index.html": b"B"})
        self.host.web["https://example.org/a.zip"] = self.zip_a
        self.host.web["https://example.org/b.zip"] = self.zip_b
        self.write_manifest("a", "web", self.zip_a, "/var/www/a")
        self.write_manifest("b", "web", self.zip_b, "/var/www/b")
        self.cat = engine.load_catalogue(self.d + "/manifests")
        self.log = []

    def tearDown(self):
        shutil.rmtree(self.d)

    def write_manifest(self, addon_id, group, zdata, dest, extra=""):
        self.host.web[f"https://example.org/{addon_id}.zip"] = zdata
        os.makedirs(self.d + "/manifests", exist_ok=True)
        with open(f"{self.d}/manifests/{addon_id}.toml", "w") as f:
            f.write(f'''
[addon]
id = "{addon_id}"
title = "Addon {addon_id.upper()}"
description = "test"
category = "web-interface"
group = "{group}"
version = "1.0"
[needs]
ram_mb = 10
disk_mb = 50
packages = ["nginx"]
models = ["Raspberry Pi"]
[download.app]
url = "https://example.org/{addon_id}.zip"
sha256 = "{sha(zdata)}"
dest = "{dest}"
[files.site]
source = "site.conf"
sha256 = "{sha(self.site)}"
dest = "/etc/nginx/sites-enabled/nebula-{addon_id}.conf"
mode = "0644"
[services]
reload = ["nginx"]
[check]
test = ["nginx", "-t"]
url = "http://127.0.0.1/"
{extra}''')

    def install(self, addon_id):
        return engine.install(self.cat.get(addon_id), self.cat, self.host, self.state, self.src, self.log.append)

    def remove(self, addon_id):
        return engine.remove(self.cat.get(addon_id), self.host, self.state, self.log.append)


class InstallTest(Base):
    def test_a_good_install_puts_everything_in_place_and_records_it(self):
        self.assertEqual(self.install("a"), "installed")
        self.assertEqual(self.host.trees["/var/www/a"]["assets/app.js"], b"js")
        self.assertEqual(self.host.files["/etc/nginx/sites-enabled/nebula-a.conf"], self.site)
        self.assertIn("nginx", self.host.packages)
        self.assertIn("systemctl reload-or-restart nginx", self.host.calls)
        self.assertEqual(self.state.read()["a"]["version"], "1.0")

    def test_installing_twice_does_nothing_the_second_time(self):
        self.install("a")
        self.host.calls.clear()
        self.assertEqual(self.install("a"), "already")
        self.assertEqual(self.host.calls, [])

    def test_an_edited_pinned_file_makes_the_next_install_an_upgrade(self):
        self.install("a")
        new = b"server { listen 80; # changed\n}\n"
        open(self.src + "/site.conf", "wb").write(new)
        text = open(self.d + "/manifests/a.toml").read().replace(sha(self.site), sha(new))
        open(self.d + "/manifests/a.toml", "w").write(text)
        self.cat = engine.load_catalogue(self.d + "/manifests")
        self.assertEqual(self.install("a"), "installed")
        self.assertEqual(self.host.files["/etc/nginx/sites-enabled/nebula-a.conf"], new)
        self.assertEqual(self.install("a"), "already")

    def test_an_upgrade_that_fails_leaves_the_previous_version_installed(self):
        self.install("a")
        old_fp = self.state.read()["a"]["fingerprint"]
        new = b"server { listen 80; # v2\n}\n"
        open(self.src + "/site.conf", "wb").write(new)
        text = open(self.d + "/manifests/a.toml").read().replace(sha(self.site), sha(new))
        open(self.d + "/manifests/a.toml", "w").write(text)
        self.cat = engine.load_catalogue(self.d + "/manifests")
        self.host.fail.add("nginx -t")
        with self.assertRaises(engine.AddonError):
            self.install("a")
        self.assertEqual(self.host.files["/etc/nginx/sites-enabled/nebula-a.conf"], self.site)
        self.assertEqual(self.host.trees["/var/www/a"]["index.html"], b"A")
        self.assertEqual(self.state.read()["a"]["fingerprint"], old_fp)

    def test_a_successful_install_leaves_no_backup_directory_behind(self):
        self.host.trees["/var/www/a"] = {"index.html": b"previous"}
        self.install("a")
        self.assertNotIn("/var/www/a.old", self.host.trees)

    def test_a_server_that_opens_its_port_a_moment_late_is_waited_for(self):
        answers = [0, 0, 0, 200]
        self.host.probe = lambda url: answers.pop(0) if len(answers) > 1 else answers[0]
        old, engine.PROBE_WAIT = engine.PROBE_WAIT, 0
        try:
            self.assertEqual(self.install("a"), "installed")
        finally:
            engine.PROBE_WAIT = old

    def test_a_server_that_never_answers_is_an_error_after_the_retries(self):
        self.host.probe = lambda url: 0
        old, engine.PROBE_WAIT = engine.PROBE_WAIT, 0
        try:
            with self.assertRaisesRegex(engine.AddonError, "answered 0"):
                self.install("a")
        finally:
            engine.PROBE_WAIT = old
        self.assertEqual(self.state.read(), {})

    def test_a_wrong_hash_stops_before_anything_changes(self):
        self.host.web["https://example.org/a.zip"] = make_zip({"index.html": b"tampered"})
        with self.assertRaisesRegex(engine.AddonError, "pinned sha256"):
            self.install("a")
        self.assertEqual((self.host.trees, self.host.files, self.state.read()), ({}, {}, {}))
        self.assertFalse([c for c in self.host.calls if c.startswith(("put", "apt-get", "systemctl"))])

    def test_a_pinned_file_that_changed_in_the_repo_is_refused(self):
        with open(self.src + "/site.conf", "ab") as f:
            f.write(b"# edited\n")
        with self.assertRaisesRegex(engine.AddonError, "pinned sha256"):
            self.install("a")
        self.assertEqual(self.host.trees, {})

    def test_a_failing_config_test_unwinds_everything(self):
        self.host.files["/etc/nginx/sites-enabled/nebula-a.conf"] = b"old site"
        self.host.fail.add("nginx -t")
        with self.assertRaisesRegex(engine.AddonError, "configuration test failed"):
            self.install("a")
        self.assertEqual(self.host.files["/etc/nginx/sites-enabled/nebula-a.conf"], b"old site")
        self.assertNotIn("/var/www/a", self.host.trees)
        self.assertEqual(self.state.read(), {})
        self.assertEqual(self.host.calls[-1], "systemctl reload-or-restart nginx")

    def test_a_service_that_does_not_answer_unwinds_everything(self):
        self.host.status = 502
        with self.assertRaisesRegex(engine.AddonError, "answered 502"):
            self.install("a")
        self.assertEqual((self.host.trees, self.host.files, self.state.read()), ({}, {}, {}))

    def test_an_existing_install_directory_comes_back_when_the_new_one_fails(self):
        self.host.trees["/var/www/a"] = {"index.html": b"previous"}
        self.host.status = 500
        with self.assertRaises(engine.AddonError):
            self.install("a")
        self.assertEqual(self.host.trees["/var/www/a"], {"index.html": b"previous"})

    def test_a_package_that_cannot_be_installed_stops_the_install(self):
        self.host.fail.add("apt-get")
        with self.assertRaisesRegex(engine.AddonError, "package nginx failed"):
            self.install("a")
        self.assertEqual(self.host.trees, {})


class FitTest(Base):
    def test_too_little_memory_is_refused_with_the_numbers(self):
        self.host.ram = 4
        with self.assertRaisesRegex(engine.AddonError, "needs about 10 MB, 4 MB free"):
            self.install("a")

    def test_too_little_disk_is_refused(self):
        self.host.disk = 3
        with self.assertRaisesRegex(engine.AddonError, "Disk space"):
            self.install("a")

    def test_the_wrong_host_model_is_refused(self):
        self.host.model = "Some Other Board"
        with self.assertRaisesRegex(engine.AddonError, "Device model"):
            self.install("a")

    def test_an_add_on_that_needs_another_waits_for_it(self):
        self.write_manifest("c", "", self.zip_a, "/var/www/c", extra="")
        text = open(self.d + "/manifests/c.toml").read().replace('models = ["Raspberry Pi"]', 'addons = ["a"]')
        open(self.d + "/manifests/c.toml", "w").write(text)
        self.cat = engine.load_catalogue(self.d + "/manifests")
        with self.assertRaisesRegex(engine.AddonError, "Needs a"):
            self.install("c")
        self.install("a")
        self.assertEqual(self.install("c"), "installed")


class SwitchTest(Base):
    def test_installing_an_alternative_replaces_the_installed_one(self):
        self.install("a")
        self.install("b")
        self.assertNotIn("/var/www/a", self.host.trees)
        self.assertNotIn("/etc/nginx/sites-enabled/nebula-a.conf", self.host.files)
        self.assertIn("/var/www/b", self.host.trees)
        self.assertEqual(sorted(self.state.read()), ["b"])

    def test_alternatives_are_reported_by_the_fit_check(self):
        self.install("a")
        checks = engine.fit(self.cat.get("b"), self.cat, self.host, self.state.read())
        self.assertIn("Addon A is installed", [c.detail for c in checks if c.name == "Alternatives"][0])


class DisplaceTest(Base):
    def setUp(self):
        super().setUp()
        text = open(self.d + "/manifests/a.toml").read() + '\n[displace]\npaths = ["/etc/nginx/sites-enabled/default"]\n'
        open(self.d + "/manifests/a.toml", "w").write(text)
        self.cat = engine.load_catalogue(self.d + "/manifests")
        self.host.files["/etc/nginx/sites-enabled/default"] = b"distro default"

    def test_the_distros_file_is_moved_aside_and_put_back_on_remove(self):
        self.install("a")
        self.assertNotIn("/etc/nginx/sites-enabled/default", self.host.files)
        self.assertEqual(self.host.files[engine.aside("/etc/nginx/sites-enabled/default")], b"distro default")
        self.assertNotIn("/etc/nginx/sites-enabled", engine.aside("/etc/nginx/sites-enabled/default"))
        self.remove("a")
        self.assertEqual(self.host.files["/etc/nginx/sites-enabled/default"], b"distro default")
        self.assertNotIn(engine.aside("/etc/nginx/sites-enabled/default"), self.host.files)

    def test_a_failed_install_puts_it_back_too(self):
        self.host.fail.add("nginx -t")
        with self.assertRaises(engine.AddonError):
            self.install("a")
        self.assertEqual(self.host.files["/etc/nginx/sites-enabled/default"], b"distro default")


class RemoveTest(Base):
    def test_remove_takes_everything_out_and_is_safe_twice(self):
        self.install("a")
        self.assertEqual(self.remove("a"), "removed")
        self.assertEqual((self.host.trees, self.host.files, self.state.read()), ({}, {}, {}))
        self.assertEqual(self.remove("a"), "absent")

    def test_remove_asks_the_shared_service_to_reload(self):
        self.install("a")
        self.host.calls.clear()
        self.remove("a")
        self.assertIn("systemctl reload-or-restart nginx", self.host.calls)
        self.assertNotIn("systemctl stop nginx", self.host.calls)


class OwnUnitsTest(Base):
    def setUp(self):
        super().setUp()
        self.write_manifest("d", "", self.zip_a, "/var/www/d")
        text = open(self.d + "/manifests/d.toml").read().replace('reload = ["nginx"]', 'units = ["nebula-d.service"]')
        open(self.d + "/manifests/d.toml", "w").write(text)
        self.cat = engine.load_catalogue(self.d + "/manifests")

    def test_own_units_are_enabled_started_stopped_and_disabled(self):
        self.install("d")
        self.assertIn("systemctl enable nebula-d.service", self.host.calls)
        self.assertIn("systemctl restart nebula-d.service", self.host.calls)
        self.remove("d")
        self.assertIn("systemctl stop nebula-d.service", self.host.calls)
        self.assertIn("systemctl disable nebula-d.service", self.host.calls)

    def test_a_unit_that_will_not_start_is_stopped_and_not_left_enabled(self):
        self.host.fail.add("systemctl restart nebula-d.service")
        with self.assertRaisesRegex(engine.AddonError, "did not start"):
            self.install("d")
        self.assertIn("systemctl disable nebula-d.service", self.host.calls)
        self.assertEqual(self.state.read(), {})


class ManifestTest(Base):
    def bad(self, old, new):
        text = open(self.d + "/manifests/a.toml").read().replace(old, new)
        open(self.d + "/manifests/a.toml", "w").write(text)
        with self.assertRaises(engine.AddonError):
            engine.load_catalogue(self.d + "/manifests")

    def test_downloads_must_be_https(self):
        self.bad("https://example.org/a.zip", "http://example.org/a.zip")

    def test_destinations_must_be_absolute_and_stay_put(self):
        self.bad('dest = "/var/www/a"', 'dest = "/var/www/../../etc"')

    def test_a_download_without_a_pin_is_refused(self):
        self.bad(f'sha256 = "{sha(self.zip_a)}"\n', "")

    def test_two_add_ons_cannot_share_an_id(self):
        shutil.copy(self.d + "/manifests/a.toml", self.d + "/manifests/a2.toml")
        with self.assertRaisesRegex(engine.AddonError, "share an id"):
            engine.load_catalogue(self.d + "/manifests")

    def test_the_shipped_add_ons_load_and_their_repo_files_match_their_pins(self):
        cat = engine.load_catalogue(HERE)
        for a in cat.addons.values():
            for name, spec in a.files.items():
                with open(os.path.join(HERE, spec["source"]), "rb") as f:
                    self.assertEqual(sha(f.read()), spec["sha256"], f"{a.id}: {name}")


class ShippedSiteTest(unittest.TestCase):

    def test_every_proxied_location_overwrites_the_client_address_headers(self):
        for name in sorted(n for n in os.listdir(HERE) if n.endswith(".conf")):
            text = open(os.path.join(HERE, name)).read()
            blocks = re.findall(r"location [^{]*\{[^}]*proxy_pass[^}]*\}", text)
            self.assertTrue(blocks, name)
            for b in blocks:
                if "internal;" in b:
                    continue
                self.assertIn("proxy_set_header X-Real-IP $remote_addr;", b, name)
                self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", b, name)
                self.assertNotIn("$proxy_add_x_forwarded_for", b, name)


class ShippedWebInterfacesTest(unittest.TestCase):
    def test_every_web_interface_is_an_alternative_to_the_others_and_listens_on_80_only_once(self):
        cat = engine.load_catalogue(HERE)
        web = [a for a in cat.addons.values() if a.category == "web-interface"]
        self.assertGreaterEqual(len(web), 2)
        for a in web:
            self.assertEqual({o.id for o in cat.alternatives(a)}, {o.id for o in web} - {a.id})
            self.assertEqual(a.displace, ["/etc/nginx/sites-enabled/default"])
            self.assertEqual(len(a.files), 1)
            self.assertTrue(list(a.files.values())[0]["dest"].startswith("/etc/nginx/sites-enabled/"))


class SystemdFilesTest(Base):
    def setUp(self):
        super().setUp()
        text = open(self.d + "/manifests/a.toml").read().replace("/etc/nginx/sites-enabled/nebula-a.conf", "/etc/systemd/system/nebula-a.service")
        text = text.replace('reload = ["nginx"]', 'managed = ["nebula-a.service"]')
        open(self.d + "/manifests/a.toml", "w").write(text)
        self.cat = engine.load_catalogue(self.d + "/manifests")

    def test_a_unit_file_is_loaded_by_systemd_but_not_started_by_the_install(self):
        self.install("a")
        self.assertIn("systemctl daemon-reload", self.host.calls)
        self.assertNotIn("systemctl restart nebula-a.service", self.host.calls)
        self.assertNotIn("systemctl enable nebula-a.service", self.host.calls)

    def test_remove_stops_a_managed_unit_and_forgets_the_file(self):
        self.install("a")
        self.host.calls.clear()
        self.remove("a")
        self.assertIn("systemctl stop nebula-a.service", self.host.calls)
        self.assertIn("systemctl disable nebula-a.service", self.host.calls)
        self.assertIn("systemctl daemon-reload", self.host.calls)
        self.assertNotIn("/etc/systemd/system/nebula-a.service", self.host.files)

    def test_a_failed_install_reloads_systemd_after_putting_things_back(self):
        self.host.status = 500
        with self.assertRaises(engine.AddonError):
            self.install("a")
        self.assertEqual(self.host.calls[-1], "systemctl daemon-reload")
        self.assertNotIn("/etc/systemd/system/nebula-a.service", self.host.files)


class AdviceTest(Base):
    def test_advice_for_this_host_model_is_shown_and_does_not_block(self):
        text = open(self.d + "/manifests/a.toml").read().replace(
            "[needs]", '[needs]\n[needs.advice]\n"Zero" = "no USB camera port here"')
        open(self.d + "/manifests/a.toml", "w").write(text)
        self.cat = engine.load_catalogue(self.d + "/manifests")
        checks = engine.fit(self.cat.get("a"), self.cat, self.host, {})
        self.assertEqual([c.detail for c in checks if c.name == "Note"], ["no USB camera port here"])
        self.assertTrue(all(c.ok for c in checks))
        self.host.model = "Raspberry Pi 4 Model B"
        self.assertEqual([c for c in engine.fit(self.cat.get("a"), self.cat, self.host, {}) if c.name == "Note"], [])


class UnzipStripTest(unittest.TestCase):
    def test_a_wrapping_folder_can_be_stripped(self):
        z = make_zip({"wrap/": b"", "wrap/a": b"1", "wrap/d/b": b"2", "stray": b"3"})
        self.assertEqual(engine.unzip(z, strip=1), {"a": b"1", "d/b": b"2"})

    def test_the_strip_count_is_checked_in_the_manifest(self):
        d = tempfile.mkdtemp()
        try:
            for bad in ("strip = 9", 'strip = "1"'):
                open(d + "/x.toml", "w").write("""[addon]
id = "x"
title = "X"
description = "x"
category = "x"
version = "1"
[download.z]
url = "https://example.org/z.zip"
sha256 = "00"
dest = "/opt/nebula/x"
""" + bad + "\n")
                with self.assertRaisesRegex(engine.AddonError, "strip"):
                    engine.load_addon(d + "/x.toml")
        finally:
            shutil.rmtree(d)


class ShippedCrowsnestTest(unittest.TestCase):
    def test_the_unit_is_sandboxed_and_the_add_on_leaves_starting_to_the_camera_list(self):
        a = engine.load_catalogue(HERE).get("crowsnest")
        self.assertEqual(a.units, [])
        self.assertEqual(a.managed, ["nebula-crowsnest.service"])
        unit = open(os.path.join(HERE, "nebula-crowsnest.service")).read()
        for line in ("DynamicUser=yes", "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "SupplementaryGroups=video"):
            self.assertIn(line, unit)
        self.assertNotIn("User=root", unit)


class ShippedRecommendedTest(unittest.TestCase):
    def test_exactly_one_web_interface_is_the_recommended_one_and_it_is_mainsail(self):
        cat = engine.load_catalogue(HERE)
        rec = [a.id for a in cat.addons.values() if a.category == "web-interface" and a.recommended]
        self.assertEqual(rec, ["mainsail"])


class ShippedInternetBlockTest(unittest.TestCase):

    PRIVATE = ("127.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "::1", "fc00::/7", "fe80::/10")

    def test_the_sites_answer_private_addresses_only(self):
        for name in ("nebula-mainsail.conf", "nebula-fluidd.conf"):
            text = open(os.path.join(HERE, name)).read()
            server = text[text.index("server {"):]
            for net in self.PRIVATE:
                self.assertIn(f"allow {net};", server, name)
            allows = re.findall(r"allow ([^;]+);", server)
            self.assertEqual(sorted(allows), sorted(self.PRIVATE), name)
            self.assertLess(server.rindex("allow "), server.index("deny all;"), name)
            self.assertLess(server.index("deny all;"), server.index("location /"), name)
            self.assertNotIn("auth_basic", text)
            self.assertNotIn("listen 443", text)

    def test_the_manifests_check_plain_http(self):
        for aid in ("mainsail", "fluidd"):
            a = engine.load_catalogue(HERE).get(aid)
            self.assertEqual((a.check["url"], a.check["status"]), ("http://127.0.0.1/", 200))

    def test_the_firewall_refuses_everything_but_private_addresses_and_lets_replies_through(self):
        text = open(os.path.join(HERE, "nebula-block.nft")).read()
        body = text[text.index("table inet nebula_block {"):]
        v4 = re.search(r"set lan4 \{.*?elements = \{([^}]*)\}", body, re.S).group(1)
        v6 = re.search(r"set lan6 \{.*?elements = \{([^}]*)\}", body, re.S).group(1)
        self.assertEqual(sorted(x.strip() for x in v4.split(",")),
                         sorted(["0.0.0.0/32", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16"]))
        self.assertEqual(sorted(x.strip() for x in v6.split(",")), sorted(["::/128", "::1/128", "fc00::/7", "fe80::/10"]))
        rules = [l.strip() for l in body[body.index("chain input"):].splitlines()[1:] if l.strip() and not l.strip().startswith("}")]
        self.assertEqual(rules[1:], ["ct state established,related accept", "ip saddr @lan4 accept", "ip6 saddr @lan6 accept",
                                     'counter drop comment "from the internet"'])
        self.assertIn("policy accept", rules[0])
        self.assertIn("delete table inet nebula_block", text)

    def test_the_firewall_add_on_checks_the_rules_before_starting_and_is_a_unit_of_its_own(self):
        a = engine.load_catalogue(HERE).get("internet-block")
        self.assertEqual(a.check["test"], ["/usr/sbin/nft", "-c", "-f", "/etc/nebula/nebula-block.nft"])
        self.assertEqual(a.units, ["nebula-block.service"])
        self.assertTrue(a.recommended)
        unit = open(os.path.join(HERE, "nebula-block.service")).read()
        self.assertIn("ExecStop=/usr/sbin/nft delete table inet nebula_block", unit)
        self.assertIn("Before=network-pre.target", unit)

    def test_installing_it_checks_then_starts_and_removing_it_takes_the_rules_out(self):
        d = tempfile.mkdtemp()
        try:
            host, state = FakeHost(), engine.State(d + "/s.json")
            cat = engine.load_catalogue(HERE)
            a = cat.get("internet-block")
            engine.install(a, cat, host, state, HERE, lambda *_: None)
            self.assertLess(host.calls.index("/usr/sbin/nft -c -f /etc/nebula/nebula-block.nft"),
                            host.calls.index("systemctl restart nebula-block.service"))
            self.assertIn("/etc/nebula/nebula-block.nft", host.files)
            engine.remove(a, host, state, lambda *_: None)
            self.assertIn("systemctl stop nebula-block.service", host.calls)
            self.assertNotIn("/etc/nebula/nebula-block.nft", host.files)
        finally:
            shutil.rmtree(d)

    def test_a_rule_file_that_nft_rejects_stops_the_install_before_the_unit_starts(self):
        d = tempfile.mkdtemp()
        try:
            host, state = FakeHost(), engine.State(d + "/s.json")
            host.fail.add("/usr/sbin/nft -c")
            cat = engine.load_catalogue(HERE)
            with self.assertRaisesRegex(engine.AddonError, "configuration test failed"):
                engine.install(cat.get("internet-block"), cat, host, state, HERE, lambda *_: None)
            self.assertNotIn("systemctl restart nebula-block.service", host.calls)
            self.assertEqual(host.files, {})
        finally:
            shutil.rmtree(d)


class UnzipTest(unittest.TestCase):
    def test_paths_that_climb_out_are_refused(self):
        with self.assertRaisesRegex(engine.AddonError, "unsafe path"):
            engine.unzip(make_zip({"../evil": b"x"}))
        with self.assertRaisesRegex(engine.AddonError, "unsafe path"):
            engine.unzip(make_zip({"/etc/evil": b"x"}))

    def test_a_non_zip_is_refused(self):
        with self.assertRaisesRegex(engine.AddonError, "not a zip"):
            engine.unzip(b"<html>404</html>")

    def test_directories_are_skipped_and_files_kept(self):
        self.assertEqual(engine.unzip(make_zip({"d/": b"", "d/x": b"1"})), {"d/x": b"1"})


class StateTest(unittest.TestCase):
    def test_a_damaged_state_file_is_reported_not_overwritten(self):
        d = tempfile.mkdtemp()
        try:
            p = d + "/addons.json"
            open(p, "w").write("{broken")
            with self.assertRaisesRegex(engine.AddonError, "damaged"):
                engine.State(p).read()
            self.assertEqual(open(p).read(), "{broken")
        finally:
            shutil.rmtree(d)


class SystemHostTest(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.h = SystemHost(sudo=())

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_put_tree_replaces_a_directory_and_put_sets_the_mode(self):
        dest = self.d + "/www/site"
        self.h.put_tree({"index.html": b"1", "a/b.js": b"2"}, dest)
        self.h.put_tree({"index.html": b"3"}, dest)
        self.assertEqual(sorted(os.listdir(dest)), ["index.html"])
        self.h.put(self.d + "/etc/x.conf", b"c", 0o640)
        self.assertEqual(oct(os.stat(self.d + "/etc/x.conf").st_mode & 0o777), "0o640")
        self.assertEqual(self.h.read(self.d + "/etc/x.conf"), b"c")
        self.assertIsNone(self.h.read(self.d + "/etc/none"))

    def test_remove_refuses_system_directories(self):
        for p in ("/", "/etc", "/var/www", "/usr/local/bin", "/etc/../etc/nginx", "relative/path/x", "/a/b"):
            with self.assertRaisesRegex(engine.AddonError, "refusing"):
                self.h.remove(p)

    def test_rename_creates_the_target_directory(self):
        self.h.put(self.d + "/a/b/file", b"x")
        self.h.rename(self.d + "/a/b/file", self.d + "/new/dir/file")
        self.assertEqual(self.h.read(self.d + "/new/dir/file"), b"x")

    def test_http_downloads_are_refused(self):
        with self.assertRaises(engine.AddonError):
            self.h.fetch("http://example.org/x")

    def test_facts_are_numbers(self):
        f = self.h.facts()
        self.assertGreater(f["ram_free_mb"], 0)
        self.assertGreater(f["disk_free_mb"], 0)


if __name__ == "__main__":
    unittest.main()
