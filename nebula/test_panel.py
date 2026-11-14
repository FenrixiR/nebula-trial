import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from . import backend
from .addons.test_service import Base as ServiceBase
from .hijack.test_engine import CALM, RELAY_OK, Sandbox, free_port, make_manifest, make_payload
from .panel import server
import subprocess
from .hijack import engine
import os
import shutil
import time


class Client:
    def __init__(self, port):
        self.port, self.cookie, self.set_cookie = port, None, ""

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Content-Type": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        c.request(method, "/nebula/api/" + path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        sc = r.getheader("Set-Cookie")
        if sc:
            self.cookie, self.set_cookie = sc.split(";")[0], sc
        return r.status, json.loads(r.read() or b"{}")


def serve(be):
    st = server.State(tempfile.mkdtemp(prefix="nebula-state-"), be)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(st))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return st, srv, Client(srv.server_address[1])


class PanelTest(unittest.TestCase):
    def test_auth_flow_and_lan_only_helper(self):
        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        self.assertEqual(c.call("GET", "state")[1]["auth"], "setup")
        self.assertEqual(c.call("GET", "checks")[0], 401)
        self.assertEqual(c.call("POST", "setup", {"password": "short"})[0], 400)
        self.assertEqual(c.call("POST", "setup", {"password": "longenough1"})[0], 200)
        self.assertEqual(c.call("GET", "state")[1]["auth"], "ok")
        self.assertEqual(c.call("POST", "setup", {"password": "other-pass1"})[0], 409)
        c2 = Client(srv.server_address[1])
        self.assertEqual(c2.call("GET", "state")[1]["auth"], "login")
        self.assertEqual(c2.call("POST", "login", {"password": "wrong-pass"})[0], 403)
        self.assertTrue(server.is_private("192.168.1.5") and server.is_private("10.78.0.1") and server.is_private("::1"))
        self.assertFalse(server.is_private("8.8.8.8") or server.is_private("2001:4860::1"))
        self.assertTrue(server.is_private("100.101.102.103"))
        self.assertTrue(server.is_private("::ffff:192.168.1.5") and server.is_private("fd7a:115c:a1e0::1") and server.is_private("fe80::1%wlan0"))
        for documentation_or_reserved in ("203.0.113.50", "198.51.100.7", "192.0.2.1", "198.18.0.1", "240.0.0.1", "0.0.0.0", "100.128.0.1",
                                          "172.32.0.1", "2001:db8::1", "::ffff:8.8.8.8", "not an address", ""):
            self.assertFalse(server.is_private(documentation_or_reserved), documentation_or_reserved)
        self.assertEqual(oct(os.stat(st.auth_file).st_mode & 0o777), "0o600")
        self.assertNotIn("longenough1", open(st.auth_file).read())

    def test_a_sign_in_survives_a_panel_restart_and_a_password_change_signs_others_out(self):
        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        self.assertEqual(c.call("GET", "update")[0], 200)
        stored = open(st.sessions_file).read()
        self.assertNotIn(c.cookie.split("=", 1)[1], stored)
        self.assertEqual(oct(os.stat(st.sessions_file).st_mode & 0o777), "0o600")
        again = server.State(st.dir, backend.DemoBackend())
        srv2 = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(again))
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        self.addCleanup(srv2.shutdown)
        c2 = Client(srv2.server_address[1])
        c2.cookie = c.cookie
        self.assertEqual(c2.call("GET", "update")[0], 200)
        other = Client(srv2.server_address[1])
        self.assertEqual(other.call("POST", "login", {"password": "longenough1"})[0], 200)
        self.assertEqual(other.call("GET", "update")[0], 200)
        self.assertEqual(c2.call("POST", "password", {"current": "longenough1", "new": "another-pass1"})[0], 200)
        self.assertEqual(c2.call("GET", "update")[0], 200)
        self.assertEqual(other.call("GET", "update")[0], 401)

    def test_an_expired_sign_in_is_refused(self):
        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        for h in st.sessions:
            st.sessions[h] = time.time() - 1
        self.assertEqual(c.call("GET", "update")[0], 401)

    def test_boot_hook_is_off_until_asked_and_can_be_turned_off_again(self):
        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        self.assertEqual(c.call("GET", "cable-boot")[1], {"on": False})
        code, body = c.call("POST", "cable-boot", {"on": True})
        self.assertEqual((code, body["cable_boot"]), (200, True))
        self.assertEqual(c.call("GET", "cable-boot")[1], {"on": True})
        self.assertEqual(c.call("POST", "cable-boot", {"on": False})[1]["cable_boot"], False)
        self.assertIn("start-up script off", " ".join(x["text"] for x in c.call("GET", "state")[1]["log"]))

    def test_demo_scenarios_gate_the_hijack(self):
        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        c.call("POST", "connect", {"path": "B"})
        c.call("POST", "scenario", {"name": "busy"})
        code, body = c.call("POST", "hijack", {"calibration": "recalibrate"})
        self.assertEqual(code, 409)
        c.call("POST", "scenario", {"name": "uncalibrated"})
        code, body = c.call("POST", "hijack", {"calibration": "existing"})
        self.assertEqual(code, 409)
        self.assertIn("cannot be kept", body["error"])
        code, body = c.call("POST", "hijack", {"calibration": "recalibrate"})
        self.assertEqual((code, body["phase"]), (200, "HIJACKED"))
        self.assertEqual(c.call("POST", "release", {})[1]["phase"], "CONFIGURED")

    def test_setup_steps_are_locked_while_hijacked_and_show_what_was_decided(self):
        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        c.call("POST", "connect", {"path": "B"})
        c.call("POST", "scenario", {"name": "ready"})
        before = c.call("GET", "checks")[1]
        self.assertNotIn("frozen", before)
        c.call("POST", "hijack", {"calibration": "existing"})
        code, body = c.call("POST", "connect", {"path": "A", "address": "1.2.3.4"})
        self.assertEqual(code, 409)
        self.assertIn("Pass control back to Cosmos first", body["error"])
        self.assertEqual(c.call("POST", "pair", {"password": "x"})[0], 409)
        self.assertEqual(st.path, "B")
        checks = c.call("GET", "checks")[1]
        self.assertTrue(checks["frozen"])
        self.assertEqual(checks["checks"], before["checks"])
        cal = c.call("GET", "calibration")[1]
        self.assertTrue(cal["frozen"])
        self.assertEqual(cal["choice"], "existing")
        c.call("POST", "release", {})
        self.assertNotIn("frozen", c.call("GET", "checks")[1])
        self.assertEqual(c.call("POST", "connect", {"path": "A", "address": "1.2.3.4"})[0], 200)

    def test_releasing_by_hand_turns_auto_hijack_off(self):
        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        c.call("POST", "connect", {"path": "B"})
        c.call("POST", "scenario", {"name": "ready"})
        c.call("POST", "persist", {"on": True})
        c.call("POST", "hijack", {"calibration": "recalibrate"})
        self.assertTrue(c.call("GET", "state")[1]["persist"])
        body = c.call("POST", "release", {})[1]
        self.assertFalse(body["persist"])
        self.assertTrue(any("automatic take-over turned off" in l["text"] for l in body["log"]))
        with open(st.settings_file) as f:
            self.assertFalse(json.load(f)["persist"])

    def test_existing_calibration_is_exported_for_kalico(self):
        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        c.call("POST", "connect", {"path": "B"})
        c.call("POST", "scenario", {"name": "ready"})
        code, body = c.call("POST", "hijack", {"calibration": "existing"})
        self.assertEqual((code, body["phase"]), (200, "HIJACKED"))
        text = open(os.path.join(st.dir, "kalico", "calibration.cfg")).read()
        self.assertIn("[bed_mesh default]", text)

    def test_recalibrate_removes_an_older_import(self):
        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        c.call("POST", "connect", {"path": "B"})
        c.call("POST", "scenario", {"name": "ready"})
        c.call("POST", "hijack", {"calibration": "existing"})
        f = os.path.join(st.dir, "kalico", "calibration.cfg")
        self.assertTrue(os.path.exists(f))
        c.call("POST", "release", {})
        c.call("POST", "hijack", {"calibration": "recalibrate"})
        self.assertFalse(os.path.exists(f))

    def test_real_backend_runs_the_engine_in_the_sandbox(self):
        sb = Sandbox()
        payload = make_payload(RELAY_OK)
        ports = [free_port() for _ in range(3)]
        m, mpath = make_manifest(payload, ports)
        be = backend.RealBackend(runner_factory=lambda addr: sb)
        be.reachable = lambda addr: (True, "sandbox")
        self.addCleanup(sb.cleanup)
        self.addCleanup(shutil.rmtree, payload, True)
        old = (server.MANIFEST, server.PAYLOAD)
        server.MANIFEST, server.PAYLOAD = mpath, payload
        self.addCleanup(lambda: setattr(server, "MANIFEST", old[0]) or setattr(server, "PAYLOAD", old[1]))
        st2, srv2, c2 = serve(be)
        self.addCleanup(srv2.shutdown)
        c2.call("POST", "setup", {"password": "longenough1"})
        c2.call("POST", "connect", {"path": "B"})
        code, body = c2.call("GET", "checks")
        self.assertTrue(body["all_ok"], body)
        cal = c2.call("GET", "calibration")[1]
        self.assertFalse(cal["use_existing_allowed"])
        code, body = c2.call("POST", "hijack", {"calibration": "recalibrate"})
        self.assertEqual((code, body["phase"]), (200, "HIJACKED"), body)
        self.assertEqual(sb.log(), ["gui-switcher stop", "moonraker stop", "klipper stop"])
        self.assertEqual(c2.call("POST", "release", {})[1]["phase"], "CONFIGURED")
        self.assertEqual(sb.log()[-3:], ["klipper start", "moonraker start", "gui-switcher start"])


class HijackRaceTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.payload = make_payload(RELAY_OK)
        self.m, self.mpath = make_manifest(self.payload, [free_port() for _ in range(3)])
        self.addCleanup(self.sb.cleanup)
        self.addCleanup(shutil.rmtree, self.payload, True)
        old = (server.MANIFEST, server.PAYLOAD)
        server.MANIFEST, server.PAYLOAD = self.mpath, self.payload
        self.addCleanup(lambda: (setattr(server, "MANIFEST", old[0]), setattr(server, "PAYLOAD", old[1])))
        be = backend.RealBackend(runner_factory=lambda a: self.sb)
        self.st, srv, self.c = serve(be)
        self.addCleanup(srv.shutdown)
        self.c.call("POST", "setup", {"password": "longenough1"})
        self.c.call("POST", "connect", {"path": "B"})

    def test_a_printer_the_watcher_already_hijacked_is_adopted_not_an_error(self):
        engine.apply(self.sb, self.m, self.payload, log=lambda s: None)
        code, body = self.c.call("POST", "hijack", {"calibration": "recalibrate"})
        self.assertEqual((code, body["phase"]), (200, "HIJACKED"), body)
        self.assertEqual(self.sb.log().count("klipper stop"), 1)

    def test_hijack_waits_while_another_hijack_is_running(self):
        import threading
        out = []
        self.st.work.acquire()
        t = threading.Thread(target=lambda: out.append(self.c.call("POST", "hijack", {"calibration": "recalibrate"})))
        t.start()
        t.join(0.5)
        self.assertTrue(t.is_alive(), "the request must wait for the running hijack")
        self.st.work.release()
        t.join(20)
        self.assertEqual(out[0][0], 200)


class RestoreTest(unittest.TestCase):
    def test_settings_survive_a_restart_and_auto_hijack_rearms(self):
        sb = Sandbox()
        payload = make_payload(RELAY_OK)
        m, mpath = make_manifest(payload, [free_port() for _ in range(3)])
        self.addCleanup(sb.cleanup)
        self.addCleanup(shutil.rmtree, payload, True)
        old = (server.MANIFEST, server.PAYLOAD, server.WATCH_DELAY)
        server.MANIFEST, server.PAYLOAD, server.WATCH_DELAY = mpath, payload, 0.2
        self.addCleanup(lambda: (setattr(server, "MANIFEST", old[0]), setattr(server, "PAYLOAD", old[1]), setattr(server, "WATCH_DELAY", old[2])))
        be = backend.RealBackend(runner_factory=lambda a: sb)
        be.password = "x"
        st, srv, c = serve(be)
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        c.call("POST", "connect", {"path": "B"})
        st.first_done = True
        self.assertEqual(c.call("POST", "persist", {"on": True})[0], 200)
        first = st._watch[1]
        st.stop_watcher()
        first.join(20)
        self.assertFalse(first.is_alive())
        self.assertEqual(oct(os.stat(st.settings_file).st_mode & 0o777), "0o600")
        be2 = backend.RealBackend(runner_factory=lambda a: sb)
        be2.password = "x"
        st2 = server.State(st.dir, be2)
        self.addCleanup(st2.stop_watcher)
        st2.restore(m)
        self.assertEqual((st2.path, st2.address, st2.persist), ("B", "10.78.0.100", True))
        for _ in range(60):
            if st2.phase == "HIJACKED":
                break
            time.sleep(0.2)
        self.assertEqual(st2.phase, "HIJACKED")


class EnsureConfigTest(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        os.makedirs(self.d + "/.nebula/kalico")
        os.makedirs(self.d + "/printer_data/config")
        self.cfg = self.d + "/printer_data/config/printer.cfg"
        with open(self.cfg, "w") as f:
            f.write("[include machine/*.cfg]\n")
        self.st = server.State(self.d + "/.nebula", backend.RealBackend(runner_factory=lambda a: None))

    def test_the_factory_block_is_added_once_when_nothing_was_imported(self):
        self.st.ensure_config()
        self.st.ensure_config()
        with open(self.cfg) as f:
            text = f.read()
        self.assertEqual(text.count("#*# <---------------------- SAVE_CONFIG"), 1)
        self.assertIn("position_endstop", text)

    def test_imported_tuning_means_no_factory_block(self):
        with open(self.d + "/.nebula/kalico/calibration.cfg", "w") as f:
            f.write("[stepper_z]\nposition_endstop: 5.6\n")
        self.st.ensure_config()
        with open(self.cfg) as f:
            self.assertNotIn("#*# <---------------------- SAVE_CONFIG", f.read())


class AddonsPanelTest(ServiceBase):

    def setUp(self):
        super().setUp()
        self.st, srv, self.c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        self.st.addons = self.svc
        self.srv = srv

    def signed_in(self):
        self.assertEqual(self.c.call("POST", "setup", {"password": "longenough1"})[0], 200)

    def wait_for(self, what, limit=4):
        end = time.time() + limit
        while time.time() < end:
            if what():
                return True
            time.sleep(0.05)
        return False

    def test_an_unreachable_printer_on_the_cable_says_to_restart_it_once(self):
        from nebula import backend

        class Runner:
            def run(self, cmd, timeout=120):
                return 255, "ssh: connect to host 10.78.0.100 port 22: No route to host"

        be = backend.RealBackend(runner_factory=lambda addr: Runner())
        ok, detail = be.reachable("10.78.0.100")
        self.assertFalse(ok)
        self.assertIn("restart the printer once", detail)
        ok, detail = be.reachable("192.168.1.50")
        self.assertNotIn("restart the printer", detail)

    def test_updates_check_and_start_through_the_updater(self):
        from nebula import updates

        class Fake:
            def __init__(self):
                self.started = 0
                self.state = None

            def version(self):
                return "1.0.0"

            def check(self):
                return {"installed": "1.0.0", "available": "1.1.0", "newer": True, "released": "2026-11-12", "notes": ["a"]}

            def start(self):
                self.started += 1
                self.state = {"state": "running"}

            def status(self):
                return self.state

        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        st.updater = Fake()
        code, d = c.call("GET", "update")
        self.assertEqual((code, d["version"], d["status"]), (200, "1.0.0", None))
        code, d = c.call("POST", "update/check", {})
        self.assertEqual((code, d["check"]["available"], d["check"]["newer"]), (200, "1.1.0", True))
        self.assertEqual(c.call("POST", "update/apply", {}), (200, {"started": True}))
        self.assertEqual(st.updater.started, 1)
        self.assertEqual(c.call("POST", "update/apply", {})[0], 409)
        self.assertEqual(st.updater.started, 1)
        st.updater.state = None
        st.phase = "HIJACKING"
        self.assertEqual(c.call("POST", "update/apply", {})[0], 409)

        class Broken(Fake):
            def check(self):
                raise updates.UpdateError("no internet")

        st.updater = Broken()
        code, d = c.call("POST", "update/check", {})
        self.assertEqual((code, d["error"]), (502, "no internet"))

    def test_the_update_endpoints_need_a_login(self):
        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        self.assertEqual(c.call("GET", "update")[0], 401)
        self.assertEqual(c.call("POST", "update/check", {})[0], 401)
        self.assertEqual(c.call("POST", "update/apply", {})[0], 401)

    def test_the_demo_updater_walks_through_an_update_without_installing_anything(self):
        st, srv, c = serve(backend.DemoBackend())
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        st.updater.seconds = 0.05
        self.assertTrue(c.call("POST", "update/check", {})[1]["check"]["newer"])
        c.call("POST", "update/apply", {})
        for _ in range(100):
            d = c.call("GET", "update")[1]
            if d["status"] and d["status"]["state"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual((d["version"], d["status"]["state"]), ("0.3.0", "done"))
        self.assertFalse(c.call("POST", "update/check", {})[1]["check"]["newer"])

    def test_nothing_is_reachable_without_signing_in(self):
        for method, path in (("GET", "addons"), ("GET", "cameras"), ("POST", "addons/install"), ("POST", "cameras/add-host"),
                             ("POST", "password")):
            self.assertEqual(self.c.call(method, path, {} if method == "POST" else None)[0], 401, path)
        self.assertEqual(self.host.calls, [])

    def test_install_through_the_panel_and_see_it(self):
        self.signed_in()
        code, body = self.c.call("POST", "addons/install", {"id": "web1"})
        self.assertEqual((code, body["job"]["state"]), (202, "done"))
        view = self.c.call("GET", "addons")[1]
        self.assertEqual(view["web"], "web1")
        self.assertEqual(self.c.call("GET", "state")[1]["job"]["state"], "done")

    def test_errors_come_back_as_a_readable_message(self):
        self.signed_in()
        code, body = self.c.call("POST", "addons/install", {"id": "nope"})
        self.assertEqual(code, 409)
        self.assertIn("unknown add-on", body["error"])
        code, body = self.c.call("POST", "cameras/add-host", {"id": "n", "name": "N", "device": "/dev/video0"})
        self.assertEqual(code, 409)
        self.assertIn("Crowsnest", body["error"])
        self.assertEqual(self.c.call("POST", "cameras/add-host", {"id": "n", "name": "N", "device": "x", "fps": "abc"})[0], 409)

    def test_cameras_through_the_panel(self):
        self.signed_in()
        self.c.call("POST", "addons/install", {"id": "web1"})
        self.c.call("POST", "addons/install", {"id": "crowsnest"})
        code, _ = self.c.call("POST", "cameras/add-host", {"id": "nozzle", "name": "Nozzle", "device": "/dev/video0"})
        self.assertEqual(code, 202)
        listing = self.c.call("GET", "cameras")[1]
        self.assertEqual([x["id"] for x in listing["cameras"]], ["printer", "nozzle"])
        self.assertTrue(listing["crowsnest"] and listing["web"])
        self.assertEqual(self.c.call("POST", "cameras/public", {"id": "printer", "public": True})[0], 404)
        self.c.call("POST", "cameras/remove", {"id": "nozzle"})
        self.assertEqual([x["id"] for x in self.c.call("GET", "cameras")[1]["cameras"]], ["printer"])

    def test_the_panel_password_can_be_changed(self):
        self.signed_in()
        self.assertEqual(self.c.call("POST", "password", {"current": "wrong-pass-1", "new": "another-pass1"})[0], 403)
        self.assertEqual(self.c.call("POST", "password", {"current": "longenough1", "new": "short"})[0], 400)
        self.assertEqual(self.c.call("POST", "password", {"current": "longenough1", "new": "another-pass1"})[0], 200)
        c2 = Client(self.srv.server_address[1])
        self.assertEqual(c2.call("POST", "login", {"password": "longenough1"})[0], 403)
        self.assertEqual(c2.call("POST", "login", {"password": "another-pass1"})[0], 200)

    def test_by_default_the_panel_does_not_talk_to_moonraker_at_all(self):
        self.signed_in()
        time.sleep(0.3)
        self.assertEqual(self.api.calls, [])

class WatcherWiringTest(unittest.TestCase):
    def test_toggle_hijacks_an_idle_printer_and_stops(self):
        sb = Sandbox()
        payload = make_payload(RELAY_OK)
        m, mpath = make_manifest(payload, [free_port() for _ in range(3)])
        self.addCleanup(sb.cleanup)
        self.addCleanup(shutil.rmtree, payload, True)
        old = (server.MANIFEST, server.PAYLOAD, server.WATCH_DELAY)
        server.MANIFEST, server.PAYLOAD, server.WATCH_DELAY = mpath, payload, 0.2
        self.addCleanup(lambda: (setattr(server, "MANIFEST", old[0]), setattr(server, "PAYLOAD", old[1]), setattr(server, "WATCH_DELAY", old[2])))
        be = backend.RealBackend(runner_factory=lambda a: sb)
        st, srv, c = serve(be)
        self.addCleanup(srv.shutdown)
        c.call("POST", "setup", {"password": "longenough1"})
        c.call("POST", "connect", {"path": "B"})
        code, body = c.call("POST", "persist", {"on": True})
        self.assertEqual(code, 409)
        be.password = "pw"
        self.assertEqual(c.call("POST", "persist", {"on": True})[0], 200)
        time.sleep(1.5)
        self.assertEqual(sb.log().count("klipper stop"), 0)
        self.assertFalse(c.call("GET", "state")[1]["first_done"])
        self.assertEqual(c.call("POST", "persist", {"on": False})[0], 200)
        st.first_done = True
        self.assertEqual(c.call("POST", "persist", {"on": True})[0], 200)
        for _ in range(60):
            if c.call("GET", "state")[1]["phase"] == "HIJACKED":
                break
            time.sleep(0.2)
        self.assertEqual(c.call("GET", "state")[1]["phase"], "HIJACKED")
        self.assertEqual(sb.log().count("klipper stop"), 1)
        self.assertEqual(c.call("POST", "persist", {"on": False})[0], 200)
        self.assertFalse(c.call("GET", "state")[1]["persist"])


class SshRunnerTest(unittest.TestCase):
    def test_password_only_travels_in_the_environment(self):
        r = engine.SshRunner("10.78.0.100", password="s3cret-pw")
        self.addCleanup(shutil.rmtree, r.dir, True)
        self.assertNotIn("s3cret-pw", " ".join(r.argv("echo hi")))
        self.assertEqual(r.env()["NEBULA_PW"], "s3cret-pw")
        self.assertEqual(r.env()["SSH_ASKPASS_REQUIRE"], "force")
        out = subprocess.run([r.askpass], env=dict(r.env()), capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "s3cret-pw")
        self.assertNotIn("s3cret-pw", open(r.askpass).read())
        self.assertEqual(oct(os.stat(r.dir).st_mode & 0o777), "0o700")

    def test_key_mode_is_batch_and_has_no_password_env(self):
        r = engine.SshRunner("h", key="/k")
        self.addCleanup(shutil.rmtree, r.dir, True)
        self.assertIn("BatchMode=yes", r.argv("x"))
        self.assertNotIn("NEBULA_PW", r.env())

    def test_printer_login_is_saved_as_a_setting(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        f = os.path.join(d, "pw")
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        be = backend.RealBackend(runner_factory=lambda a: sb, remember_file=f)
        self.assertTrue(be.login("x", "pw-1")[0])
        self.assertEqual(open(f).read(), "pw-1")
        self.assertEqual(oct(os.stat(f).st_mode & 0o777), "0o600")
        be2 = backend.RealBackend(runner_factory=lambda a: sb, remember_file=f)
        be2.load_remembered()
        self.assertEqual(be2.password, "pw-1")


if __name__ == "__main__":
    unittest.main()
