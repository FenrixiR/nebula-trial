import json
import os
import shutil
import tempfile
import unittest

from nebula.addons import cameras, engine
from nebula.addons.test_engine import FakeHost


class FakeMoonraker:
    def __init__(self):
        self.webcams, self.calls, self.fail = {}, [], False

    def __call__(self, method, path, data=None):
        self.calls.append((method, path))
        if self.fail:
            raise engine.AddonError("Moonraker is down")
        if method == "POST":
            self.webcams[data["name"]] = data
        else:
            self.webcams.pop(path.split("name=")[1].replace("%20", " "), None)
        return {"result": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.store = cameras.CameraList(self.d + "/cameras.json")
        self.host, self.api = FakeHost(), FakeMoonraker()

    def tearDown(self):
        shutil.rmtree(self.d)

    def apply(self):
        return cameras.apply(self.store, self.host, self.api, lambda *_: None, crowsnest=True)


class ValidationTest(unittest.TestCase):
    def bad(self, **kw):
        base = dict(id="cam", name="Cam", address="10.78.0.100", port=8080)
        with self.assertRaises(engine.AddonError):
            cameras.Camera(**{**base, **kw})

    def test_only_the_cable_and_this_host_are_reachable(self):
        for addr in ("192.168.1.1", "8.8.8.8", "10.78.1.5", "169.254.169.254", "example.org", "10.78.0.100/24", ""):
            self.bad(address=addr)

    def test_this_host_is_limited_to_the_camera_ports(self):
        for port in (80, 22, 7125, 8080, 8080 + 200, 0):
            self.bad(address="127.0.0.1", port=port, device="/dev/video0")
        self.assertEqual(cameras.Camera(id="nozzle", name="Nozzle", address="127.0.0.1", port=8081, device="/dev/video0").port, 8081)

    def test_ids_and_names_cannot_carry_nginx_syntax(self):
        for cid in ("Cam", "a b", "x;y", "../x", "a{", "-a", "a-", "", "a" * 40):
            self.bad(id=cid)
        for name in ("", "x" * 61, "line\nbreak"):
            self.bad(name=name)

    def test_port_must_be_a_number(self):
        self.bad(port="8080")
        self.bad(port=70000)


class RenderTest(unittest.TestCase):
    def test_a_camera_is_a_plain_proxy_inside_the_sites_allow_list(self):
        text = cameras.render_nginx(cameras.Camera("printer", "Printer camera", "10.78.0.100", 8080))
        self.assertIn("location /webcam/printer/ {", text)
        self.assertIn("proxy_pass http://10.78.0.100:8080/;", text)
        self.assertNotIn("allow", text)
        self.assertNotIn("auth_", text)

    def test_an_old_camera_list_that_still_has_the_removed_public_key_loads(self):
        d = tempfile.mkdtemp()
        try:
            json.dump({"cameras": [dict(cameras.PRINTER_CAMERA, public=True)], "applied": {"ids": [], "names": []}}, open(d + "/c.json", "w"))
            self.assertEqual([c.id for c in cameras.CameraList(d + "/c.json").cameras()], ["printer"])
        finally:
            shutil.rmtree(d)

    def test_only_get_and_head_are_proxied(self):
        self.assertIn("limit_except GET HEAD { deny all; }", cameras.render_nginx(cameras.Camera("p", "P", "10.78.0.100", 8080)))


class ListTest(Base):
    def test_a_fresh_list_has_the_printers_camera(self):
        self.assertEqual([c.id for c in self.store.cameras()], ["printer"])

    def test_add_remove_and_duplicates(self):
        self.store.add(cameras.Camera("nozzle", "Nozzle", "127.0.0.1", 8081, device="/dev/video0"))
        self.assertEqual([c.id for c in self.store.cameras()], ["printer", "nozzle"])
        with self.assertRaisesRegex(engine.AddonError, "share a name"):
            self.store.add(cameras.Camera("other", "Nozzle", "127.0.0.1", 8082, device="/dev/video0"))
        self.assertEqual([c.id for c in self.store.cameras()], ["printer", "nozzle"])
        with self.assertRaisesRegex(engine.AddonError, "share an id"):
            self.store.add(cameras.Camera("nozzle", "Another", "127.0.0.1", 8082, device="/dev/video0"))
        self.store.remove("nozzle")
        self.assertEqual([c.id for c in self.store.cameras()], ["printer"])

    def test_a_damaged_file_is_reported_not_overwritten(self):
        open(self.store.path, "w").write("{oops")
        with self.assertRaisesRegex(engine.AddonError, "damaged"):
            self.store.cameras()
        self.assertEqual(open(self.store.path).read(), "{oops")


class ApplyTest(Base):
    def test_apply_writes_nginx_and_tells_moonraker(self):
        self.assertEqual(self.apply(), ["printer"])
        self.assertIn("location /webcam/printer/", self.host.files["/etc/nginx/nebula-cameras/printer.conf"].decode())
        self.assertEqual(self.api.webcams["Printer camera"]["stream_url"], "/webcam/printer/stream")
        self.assertEqual(self.api.webcams["Printer camera"]["snapshot_url"], "/webcam/printer/snapshot")
        self.assertLess(self.host.calls.index("nginx -t"), self.host.calls.index("systemctl reload-or-restart nginx"))
        self.assertEqual(self.store.read()["applied"], {"ids": ["printer"], "names": ["Printer camera"]})

    def test_a_removed_camera_loses_its_file_and_its_entry_but_a_foreign_entry_stays(self):
        self.api.webcams["My own cam"] = {"name": "My own cam"}
        self.store.add(cameras.Camera("nozzle", "Nozzle", "127.0.0.1", 8081, device="/dev/video0"))
        self.apply()
        self.store.remove("nozzle")
        self.apply()
        self.assertNotIn("/etc/nginx/nebula-cameras/nozzle.conf", self.host.files)
        self.assertEqual(sorted(self.api.webcams), ["My own cam", "Printer camera"])

    def test_a_renamed_camera_replaces_its_old_entry(self):
        self.apply()
        d = self.store.read()
        d["cameras"][0]["name"] = "Bed camera"
        self.store.write(d)
        self.apply()
        self.assertEqual(sorted(self.api.webcams), ["Bed camera"])

    def test_nginx_rejecting_the_list_puts_the_old_files_back_and_does_not_touch_moonraker(self):
        self.apply()
        before = self.host.files["/etc/nginx/nebula-cameras/printer.conf"]
        self.store.add(cameras.Camera("nozzle", "Nozzle", "127.0.0.1", 8081, device="/dev/video0"))
        self.api.calls.clear()
        self.host.fail.add("nginx -t")
        with self.assertRaisesRegex(engine.AddonError, "did not accept"):
            self.apply()
        self.assertEqual(self.host.files["/etc/nginx/nebula-cameras/printer.conf"], before)
        self.assertNotIn("/etc/nginx/nebula-cameras/nozzle.conf", self.host.files)
        self.assertEqual(self.api.calls, [])

    def test_applying_twice_changes_nothing(self):
        self.apply()
        files, hooks = dict(self.host.files), dict(self.api.webcams)
        self.apply()
        self.assertEqual((self.host.files, self.api.webcams), (files, hooks))


class HostCameraTest(unittest.TestCase):
    def host_cam(self, **kw):
        base = dict(id="nozzle", name="Nozzle", address="127.0.0.1", port=8081, device="/dev/video0")
        return cameras.Camera(**{**base, **kw})

    def test_good_devices(self):
        for dev in ("/dev/video0", "/dev/video12", "/dev/v4l/by-id/usb-Cheap_Cam_1234-video-index0", "/dev/v4l/by-path/platform-3f980000.usb-usb-0:1.2:1.0-video-index0"):
            self.assertEqual(self.host_cam(device=dev).device, dev)

    def test_devices_that_are_not_cameras_are_refused(self):
        for dev in ("", "/dev/sda", "/dev/video", "/etc/passwd", "/dev/video0; reboot", "/dev/v4l/by-id/../../sda", "video0", "/dev/video0\nx"):
            with self.assertRaises(engine.AddonError, msg=dev):
                self.host_cam(device=dev)

    def test_resolution_and_fps_are_checked(self):
        for kw in ({"resolution": "640x"}, {"resolution": "0x0"}, {"resolution": "640x480 --x"}, {"max_fps": 0}, {"max_fps": 61}, {"max_fps": "15"}):
            with self.assertRaises(engine.AddonError, msg=str(kw)):
                self.host_cam(**kw)

    def test_a_camera_on_the_cable_has_no_device(self):
        with self.assertRaisesRegex(engine.AddonError, "only applies"):
            cameras.Camera("p", "P", "10.78.0.100", 8080, device="/dev/video0")

    def test_crowsnest_gets_one_section_per_host_camera_and_none_for_the_printer(self):
        cams = [cameras.Camera(**cameras.PRINTER_CAMERA), self.host_cam(resolution="1280x720", max_fps=30),
                self.host_cam(id="side", name="Side", port=8082, device="/dev/video2")]
        text = cameras.render_crowsnest(cams)
        self.assertIn("[cam nozzle]\nmode: ustreamer\nport: 8081\ndevice: /dev/video0\nresolution: 1280x720\nmax_fps: 30\n", text)
        self.assertIn("[cam side]", text)
        self.assertNotIn("printer", text)
        self.assertIn("no_proxy: false", text)

    def test_the_next_free_port_skips_the_ones_in_use(self):
        d = tempfile.mkdtemp()
        try:
            store = cameras.CameraList(d + "/c.json")
            self.assertEqual(store.next_host_port(), 8081)
            store.add(self.host_cam())
            store.add(self.host_cam(id="b", name="B", port=8083, device="/dev/video2"))
            self.assertEqual(store.next_host_port(), 8082)
        finally:
            shutil.rmtree(d)


class ApplyCrowsnestTest(Base):
    def add_nozzle(self):
        self.store.add(cameras.Camera("nozzle", "Nozzle", "127.0.0.1", 8081, device="/dev/video0"))

    def apply_with(self, crowsnest):
        return cameras.apply(self.store, self.host, self.api, lambda *_: None, crowsnest=crowsnest)

    def test_a_host_camera_without_the_add_on_is_refused_before_anything_changes(self):
        self.add_nozzle()
        with self.assertRaisesRegex(engine.AddonError, "needs the Crowsnest add-on"):
            self.apply_with(False)
        self.assertEqual((self.host.files, self.api.calls, self.host.calls), ({}, [], []))

    def test_a_host_camera_writes_the_config_and_starts_the_service_after_nginx_and_moonraker(self):
        self.add_nozzle()
        self.apply_with(True)
        conf = self.host.files["/etc/nebula/crowsnest.conf"].decode()
        self.assertIn("[cam nozzle]", conf)
        calls = self.host.calls
        self.assertLess(calls.index("systemctl reload-or-restart nginx"), calls.index("systemctl restart nebula-crowsnest.service"))
        self.assertIn("systemctl enable nebula-crowsnest.service", calls)
        self.assertIn("location /webcam/nozzle/", self.host.files["/etc/nginx/nebula-cameras/nozzle.conf"].decode())
        self.assertIn("proxy_pass http://127.0.0.1:8081/;", self.host.files["/etc/nginx/nebula-cameras/nozzle.conf"].decode())

    def test_with_no_host_camera_left_the_service_is_stopped_and_the_config_removed(self):
        self.add_nozzle()
        self.apply_with(True)
        self.store.remove("nozzle")
        self.host.calls.clear()
        self.apply_with(True)
        self.assertIn("systemctl stop nebula-crowsnest.service", self.host.calls)
        self.assertIn("systemctl disable nebula-crowsnest.service", self.host.calls)
        self.assertNotIn("/etc/nebula/crowsnest.conf", self.host.files)

    def test_with_only_the_printer_camera_crowsnest_is_never_started(self):
        self.apply_with(True)
        self.assertNotIn("systemctl restart nebula-crowsnest.service", self.host.calls)

    def test_a_service_that_will_not_start_is_reported(self):
        self.add_nozzle()
        self.host.fail.add("systemctl restart nebula-crowsnest.service")
        with self.assertRaisesRegex(engine.AddonError, "Crowsnest did not start"):
            self.apply_with(True)


if __name__ == "__main__":
    unittest.main()
