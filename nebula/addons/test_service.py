import hashlib
import io
import os
import shutil
import tempfile
import unittest
import zipfile

from nebula.addons import cameras, engine
from nebula.addons.host import MemoryHost
from nebula.addons.service import AddonService


def make_zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return b.getvalue()


class Moonraker:

    def __init__(self):
        self.webcams, self.calls, self.down = {}, [], False

    def __call__(self, method, path, data=None):
        self.calls.append((method, path))
        if self.down:
            raise cameras.Unreachable("connection refused")
        if method == "POST":
            self.webcams[data["name"]] = data
        else:
            self.webcams.pop(path.split("name=")[1], None)
        return {"result": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(self.d + "/cat")
        self.site = b"server {}\n"
        open(self.d + "/cat/site.conf", "wb").write(self.site)
        self.host, self.api, self.say_log = MemoryHost(), Moonraker(), []
        for aid, cat in (("web1", "web-interface"), ("web2", "web-interface"), ("crowsnest", "camera")):
            z = make_zip({"index.html": aid.encode()})
            self.host.web[f"https://example.org/{aid}.zip"] = z
            group = 'group = "web"' if cat == "web-interface" else ""
            open(f"{self.d}/cat/{aid}.toml", "w").write(f"""
[addon]
id = "{aid}"
title = "{aid.upper()}"
description = "test"
category = "{cat}"
{group}
version = "1"
[needs]
ram_mb = 10
[download.app]
url = "https://example.org/{aid}.zip"
sha256 = "{hashlib.sha256(z).hexdigest()}"
dest = "/var/www/{aid}"
[files.site]
source = "site.conf"
sha256 = "{hashlib.sha256(self.site).hexdigest()}"
dest = "/etc/nginx/sites-enabled/{aid}.conf"
[services]
reload = ["nginx"]
""")
        self.svc = AddonService(self.d + "/cat", self.d + "/addons.json", self.d + "/cameras.json", self.host, self.api,
                                self.say_log.append, background=False)

    def tearDown(self):
        shutil.rmtree(self.d)


class CatalogueTest(Base):
    def test_the_view_lists_every_add_on_with_whether_it_fits(self):
        v = self.svc.view()
        self.assertEqual(sorted(a["id"] for a in v["addons"]), ["crowsnest", "web1", "web2"])
        self.assertTrue(all(a["can_install"] and not a["installed"] for a in v["addons"]))
        self.assertEqual((v["web"], v["job"]), (None, None))

    def test_an_add_on_that_does_not_fit_says_why(self):
        self.host.ram = 3
        a = next(x for x in self.svc.view()["addons"] if x["id"] == "web1")
        self.assertFalse(a["can_install"])
        self.assertIn("3 MB free", [c for c in a["fit"] if c["name"] == "Free memory"][0]["detail"])


class JobTest(Base):
    def test_install_runs_and_the_view_shows_it(self):
        self.assertEqual(self.svc.start("install", "web1")["state"], "done")
        v = self.svc.view()
        self.assertEqual(v["web"], "web1")
        self.assertTrue(next(a for a in v["addons"] if a["id"] == "web1")["installed"])
        self.assertIn("Installing WEB1: done", self.say_log)

    def test_installing_the_other_web_interface_replaces_the_first(self):
        self.svc.start("install", "web1")
        self.svc.start("install", "web2")
        self.assertEqual(self.svc.view()["web"], "web2")

    def test_a_failure_becomes_an_error_job_not_an_exception(self):
        self.host.fail.add("apt-get")
        self.host.packages.clear()
        text = open(self.d + "/cat/web1.toml").read().replace("[needs]", '[needs]\npackages = ["nginx"]')
        open(self.d + "/cat/web1.toml", "w").write(text)
        self.svc = AddonService(self.d + "/cat", self.d + "/addons.json", self.d + "/cameras.json", self.host, self.api,
                                self.say_log.append, background=False)
        job = self.svc.start("install", "web1")
        self.assertEqual(job["state"], "error")
        self.assertIn("package nginx failed", job["message"])
        self.assertIsNone(self.svc.view()["web"])

    def test_a_second_job_while_one_runs_is_refused(self):
        self.svc.job = {"label": "Installing X", "state": "running", "message": ""}
        with self.assertRaisesRegex(engine.AddonError, "Still busy"):
            self.svc.start("install", "web1")

    def test_unknown_add_ons_and_actions_are_refused(self):
        with self.assertRaises(engine.AddonError):
            self.svc.start("install", "nope")
        with self.assertRaises(engine.AddonError):
            self.svc.start("format", "web1")

    def test_installing_a_web_interface_publishes_the_printers_camera(self):
        self.svc.start("install", "web1")
        self.assertIn("/etc/nginx/nebula-cameras/printer.conf", self.host.files)
        self.assertIn("Printer camera", self.api.webcams)

    def test_demo_pretends_without_touching_the_host(self):
        demo = AddonService(self.d + "/cat", self.d + "/demo.json", self.d + "/democ.json", self.host, self.api,
                            self.say_log.append, demo=True, background=False)
        import time
        orig, time.sleep = time.sleep, lambda s: None
        try:
            demo.start("install", "web1")
        finally:
            time.sleep = orig
        self.assertEqual(demo.view()["web"], "web1")
        self.assertEqual((self.host.files, self.host.trees, self.host.calls, self.api.calls), ({}, {}, [], []))


class CameraTest(Base):
    def test_a_host_camera_needs_crowsnest(self):
        with self.assertRaisesRegex(engine.AddonError, "Crowsnest add-on first"):
            self.svc.camera_add_host("nozzle", "Nozzle", "/dev/video0")

    def test_add_publish_and_remove_a_host_camera(self):
        self.svc.start("install", "web1")
        self.svc.start("install", "crowsnest")
        self.assertEqual(self.svc.camera_add_host("nozzle", "Nozzle", "/dev/video0", "1280x720", 30)["state"], "done")
        self.assertIn("Nozzle", self.api.webcams)
        self.assertIn("[cam nozzle]", self.host.files["/etc/nebula/crowsnest.conf"].decode())
        self.assertEqual([c["id"] for c in self.svc.cameras()["cameras"]], ["printer", "nozzle"])
        self.svc.camera_remove("nozzle")
        self.assertNotIn("Nozzle", self.api.webcams)
        self.assertNotIn("/etc/nginx/nebula-cameras/nozzle.conf", self.host.files)

    def test_removing_crowsnest_unpublishes_host_cameras_but_keeps_them_listed(self):
        self.svc.start("install", "web1")
        self.svc.start("install", "crowsnest")
        self.svc.camera_add_host("nozzle", "Nozzle", "/dev/video0")
        self.svc.start("remove", "crowsnest")
        self.assertNotIn("Nozzle", self.api.webcams)
        self.assertIn("nozzle", [c["id"] for c in self.svc.cameras()["cameras"]])
        self.assertIn("Printer camera", self.api.webcams)

    def test_the_printers_camera_cannot_be_removed(self):
        with self.assertRaisesRegex(engine.AddonError, "cannot be removed"):
            self.svc.camera_remove("printer")

    def test_without_a_web_interface_edits_are_saved_but_nothing_is_published(self):
        self.svc.start("install", "crowsnest")
        self.svc.camera_add_host("nozzle", "Nozzle", "/dev/video0")
        self.assertEqual(self.host.files.get("/etc/nginx/nebula-cameras/nozzle.conf"), None)
        self.assertEqual(self.api.webcams, {})
        self.assertIn("nozzle", [c["id"] for c in self.svc.cameras()["cameras"]])


if __name__ == "__main__":
    unittest.main()
