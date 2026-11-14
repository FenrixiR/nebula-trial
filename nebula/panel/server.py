import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import autosave, backend as backends, calibration, host_stack, kalico_export
from ..addons import engine as addon_engine, host as addon_host, service as addon_service
from ..watcher import Watcher
from ..hijack import engine
from .. import updates

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
MANIFEST = os.path.join(REPO, "nebula", "manifests")
SESSION_SECONDS = 30 * 24 * 3600
WATCH_DELAY = 10.0
PAYLOAD = os.path.join(REPO, "nebula")
ADDONS = os.path.join(REPO, "nebula", "addons")


class State:
    def __init__(self, directory, backend):
        self.dir = directory
        self.backend = backend
        os.makedirs(directory, exist_ok=True)
        self.lock = threading.Lock()
        self.work = threading.Lock()
        self.demo = backend.demo
        self.paired = backend.demo
        self.sessions_file = os.path.join(directory, "sessions.json")
        self.sessions = self.load_sessions()
        self.path = None
        self.address = ""
        self.phase = "NEW"
        self.log = []
        self.step = 0
        self.persist = False
        self.updater = updates.DemoUpdater() if backend.demo else updates.Updater(os.path.join(REPO, "VERSION"))
        self.cable_boot = None
        self.snapshot = {}
        self.auth_file = os.path.join(directory, "auth.json")
        self.addons = None
        self.settings_file = os.path.join(directory, "settings.json")

    def save(self):
        fd = os.open(self.settings_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"path": self.path, "address": self.address, "persist": self.persist}, f)

    def restore(self, manifest):
        try:
            with open(self.settings_file) as f:
                s = json.load(f)
        except (OSError, ValueError):
            return
        self.path, self.address = s.get("path"), s.get("address", "")
        if not self.path:
            return
        self.phase = "CONFIGURED"
        if not self.demo:
            self.paired = self.backend.password is not None
        self.say(f"settings restored: {'cable only' if self.path == 'B' else 'home network'}, printer {self.address}")
        if s.get("persist") and not self.demo and self.start_watcher(manifest):
            self.persist = True

    def locked(self):
        return self.phase in ("HIJACKING", "HIJACKED", "RELEASING")

    def start_watcher(self, manifest):
        self.stop_watcher()
        if self.demo:
            self.say("automatic take-over on (demo: nothing is watched)")
            return True
        if self.backend.password is None:
            return False
        stop = threading.Event()
        w = Watcher(manifest, PAYLOAD, self.backend._runner, self.address, log=self.say,
                    base_delay=WATCH_DELAY, sleep=stop.wait, lock=self.work,
                    stack=getattr(self.backend, "stack", None))

        def on_result(result):
            if result in ("hijacked", "already"):
                self.phase = "HIJACKED"
            elif result == "failed":
                self.phase = "CONFIGURED"
            elif result in ("unreachable", "busy") and self.phase == "HIJACKED":
                self.phase = "CONFIGURED"

        w.on_start = lambda: (setattr(self, "phase", "HIJACKING"), self.say("automatic take-over: taking over the printer"))
        t = threading.Thread(target=w.run, kwargs={"stop": stop.is_set, "on_result": on_result}, daemon=True)
        self._watch = (stop, t)
        t.start()
        self.say("automatic take-over on: watching for the printer")
        return True

    def stop_watcher(self):
        w = getattr(self, "_watch", None)
        if w:
            w[0].set()
            self._watch = None
            self.say("automatic take-over off")

    def addon_service(self):
        if self.addons is None:
            host = addon_host.MemoryHost() if self.demo else addon_host.SystemHost()
            self.addons = addon_service.AddonService(ADDONS, os.path.join(self.dir, "addons.json"),
                                                     os.path.join(self.dir, "cameras.json"), host, say=self.say, demo=self.demo)
        return self.addons

    def has_password(self):
        return os.path.exists(self.auth_file)

    def load_sessions(self):
        try:
            with open(self.sessions_file) as f:
                now = time.time()
                return {h: t for h, t in json.load(f).items() if t > now}
        except (OSError, ValueError, AttributeError):
            return {}

    def save_sessions(self):
        fd = os.open(self.sessions_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.sessions, f)

    @staticmethod
    def token_hash(tok):
        return hashlib.sha256(tok.encode()).hexdigest()

    def add_session(self, tok):
        now = time.time()
        self.sessions = {h: t for h, t in self.sessions.items() if t > now}
        self.sessions[self.token_hash(tok)] = now + SESSION_SECONDS
        self.save_sessions()

    def session_ok(self, tok):
        exp = self.sessions.get(self.token_hash(tok))
        return exp is not None and exp > time.time()

    def drop_other_sessions(self, keep_tok):
        keep = self.token_hash(keep_tok) if keep_tok else None
        self.sessions = {h: t for h, t in self.sessions.items() if h == keep}
        self.save_sessions()

    def set_password(self, pw):
        salt = secrets.token_bytes(16)
        h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
        with open(self.auth_file, "w") as f:
            json.dump({"salt": salt.hex(), "hash": h.hex()}, f)
        os.chmod(self.auth_file, 0o600)

    def check_password(self, pw):
        try:
            d = json.load(open(self.auth_file))
        except (OSError, ValueError):
            return False
        h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(d["salt"]), n=2**14, r=8, p=1)
        return hmac.compare_digest(h.hex(), d["hash"])

    def say(self, text):
        self.log.append({"t": time.strftime("%H:%M:%S"), "text": text})
        self.log = self.log[-200:]


HOME_NETWORKS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10")]


def is_private(addr):
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in n for n in HOME_NETWORKS if n.version == ip.version)


def make_handler(state):
    manifest = engine.load_manifests(MANIFEST)

    class H(BaseHTTPRequestHandler):

        def chosen(self, issue=None):
            if issue is None:
                try:
                    issue = state.backend.facts(state.address)["issue"]
                except Exception:
                    issue = ""
            return engine.pick(manifest, issue)
        server_version = "Nebula"

        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json", headers=()):
            data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype != "application/json" else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def authed(self):
            if state.demo and os.environ.get("NEBULA_DEMO_NOAUTH"):
                return True
            return state.session_ok(self.session_token())

        def session_token(self):
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "nebula_session":
                    return value
            return ""

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def guard(self):
            if not is_private(self.client_address[0]):
                self.send(403, {"error": "Nebula answers only on the local network."})
                return False
            return True

        def do_GET(self):
            if not self.guard():
                return
            p = self.path.split("?")[0].rstrip("/")
            if p in ("", "/nebula"):
                self.send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html")
            elif p == "/nebula/api/state":
                self.send(200, self.public_state())
            elif not self.authed():
                self.send(401, {"error": "login required"})
            elif p == "/nebula/api/cable-boot":
                self.send(200, {"on": self.cable_boot("status")})
            elif p == "/nebula/api/update":
                self.send(200, {"version": state.updater.version(), "status": state.updater.status()})
            elif p == "/nebula/api/addons":
                self.send(200, state.addon_service().view())
            elif p == "/nebula/api/cameras":
                self.send(200, state.addon_service().cameras())
            elif p == "/nebula/api/checks":
                self.send(200, self.frozen("checks") if state.locked() else self.checks())
            elif p == "/nebula/api/calibration":
                self.send(200, self.frozen("calibration") if state.locked() else self.calibration())
            elif p == "/nebula/api/plan":
                self.send(200, {"steps": engine.plan(self.chosen(), PAYLOAD)})
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.guard():
                return
            p = self.path.split("?")[0].rstrip("/")
            b = self.body()
            with state.lock:
                if p == "/nebula/api/setup":
                    if state.has_password():
                        return self.send(409, {"error": "already set up"})
                    if len(b.get("password", "")) < 8:
                        return self.send(400, {"error": "Use at least 8 characters."})
                    state.set_password(b["password"])
                    return self.login_ok()
                if p == "/nebula/api/login":
                    if not state.check_password(b.get("password", "")):
                        time.sleep(1)
                        return self.send(403, {"error": "Wrong password."})
                    return self.login_ok()
                if not self.authed():
                    return self.send(401, {"error": "login required"})
                if p.startswith(("/nebula/api/addons/", "/nebula/api/cameras/")):
                    return self.addon_call(p, b)
                if p == "/nebula/api/password":
                    if not state.check_password(b.get("current", "")):
                        time.sleep(1)
                        return self.send(403, {"error": "The current password is wrong."})
                    if len(b.get("new", "")) < 8:
                        return self.send(400, {"error": "Use at least 8 characters."})
                    state.set_password(b["new"])
                    state.drop_other_sessions(self.session_token())
                    return self.send(200, {"ok": True})
                if p in ("/nebula/api/connect", "/nebula/api/pair") and state.locked():
                    return self.send(409, {"error": "Pass control back to Cosmos first. These settings are locked while Nebula is running the printer."})
                if p == "/nebula/api/connect":
                    if b.get("path") not in ("A", "B"):
                        return self.send(400, {"error": "Choose how the printer is connected."})
                    if b["path"] == "A" and not b.get("address", "").strip():
                        return self.send(400, {"error": "Enter the printer's address."})
                    state.path, state.address = b["path"], b.get("address", "").strip() if b["path"] == "A" else "10.78.0.100"
                    state.phase = "CONFIGURED"
                    state.say(f"connection: {'cable only' if state.path == 'B' else 'home network'}, printer {state.address}")
                    state.save()
                    return self.send(200, self.public_state())
                if p == "/nebula/api/scenario" and state.demo:
                    if b.get("name") in state.backend.names:
                        state.backend.scenario = b["name"]
                    return self.send(200, self.public_state())
                if p == "/nebula/api/pair" and not state.demo:
                    return self.pair(b)
                if p == "/nebula/api/persist":
                    on = bool(b.get("on"))
                    if on and not state.start_watcher(manifest):
                        return self.send(409, {"error": "Save the printer login first (step 1)."})
                    if not on:
                        state.stop_watcher()
                    state.persist = on
                    state.save()
                    return self.send(200, self.public_state())
                if p == "/nebula/api/cable-boot":
                    return self.cable_boot_set(bool(b.get("on")))
                if p == "/nebula/api/update/check":
                    return self.update_check()
                if p == "/nebula/api/update/apply":
                    return self.update_apply()
                if p == "/nebula/api/hijack":
                    return self.hijack(b)
                if p == "/nebula/api/release":
                    return self.release()
            self.send(404, {"error": "not found"})

        def update_check(self):
            try:
                info = state.updater.check()
            except updates.UpdateError as ex:
                return self.send(502, {"error": str(ex)})
            state.say(f"update check: {info['available']} is the newest version" + (" (newer than yours)" if info["newer"] else " (you have it)"))
            return self.send(200, {"version": state.updater.version(), "check": info, "status": state.updater.status()})

        def update_apply(self):
            if state.phase in ("HIJACKING", "RELEASING"):
                return self.send(409, {"error": "Wait until the take-over or hand-back has finished, then update."})
            current = state.updater.status()
            if current and current.get("state") == "running":
                return self.send(409, {"error": "An update is already running."})
            try:
                state.updater.start()
            except updates.UpdateError as ex:
                return self.send(502, {"error": str(ex)})
            state.say("update started: the panel restarts for a moment; the printer is not touched")
            return self.send(200, {"started": True})

        def cable_boot(self, action):
            try:
                with state.work:
                    state.cable_boot = bool(state.backend.cable_boot(self.chosen(), PAYLOAD, state.address, action))
            except (engine.NebulaError, OSError, ValueError):
                state.cable_boot = False
            return state.cable_boot

        def cable_boot_set(self, on):
            try:
                with state.work:
                    state.cable_boot = bool(state.backend.cable_boot(self.chosen(), PAYLOAD, state.address, "on" if on else "off"))
            except (engine.NebulaError, OSError, ValueError) as ex:
                return self.send(409, {"error": f"Could not change it on the printer: {ex}"})
            state.say("printer start-up script " + ("on: the cable is set up at every printer start" if state.cable_boot
                                              else "off: nothing of Nebula starts with the printer"))
            self.send(200, self.public_state())

        def addon_call(self, p, b):
            svc = state.addon_service()
            try:
                if p == "/nebula/api/addons/install":
                    job = svc.start("install", b.get("id", ""))
                elif p == "/nebula/api/addons/remove":
                    job = svc.start("remove", b.get("id", ""))
                elif p == "/nebula/api/cameras/add-host":
                    job = svc.camera_add_host(b.get("id", ""), b.get("name", ""), b.get("device", ""),
                                              b.get("resolution", "640x480"), int(b.get("fps", 15)))
                elif p == "/nebula/api/cameras/remove":
                    job = svc.camera_remove(b.get("id", ""))
                else:
                    return self.send(404, {"error": "not found"})
            except (addon_engine.AddonError, ValueError, TypeError) as ex:
                return self.send(409, {"error": str(ex)})
            return self.send(202, {"job": job})

        def login_ok(self):
            tok = secrets.token_urlsafe(24)
            state.add_session(tok)
            self.send(200, {"ok": True}, headers=[("Set-Cookie", f"nebula_session={tok}; HttpOnly; SameSite=Strict; Path=/nebula; Max-Age={SESSION_SECONDS}")])

        def public_state(self):
            return {"auth": "ok" if self.authed() else ("login" if state.has_password() else "setup"),
                    "demo": state.demo, "scenario": getattr(state.backend, "scenario", None), "scenarios": state.backend.names,
                    "paired": state.paired,
                    "path": state.path, "address": state.address, "phase": state.phase,
                    "printer_password": (getattr(state.backend, "password", None) or "") if self.authed() and not state.demo else "",
                    "persist": state.persist, "cable_boot": state.cable_boot, "log": state.log[-40:], "host": host_facts(state.demo),
                    "job": state.addons.job if state.addons else None}

        def frozen(self, what):
            snap = state.snapshot.get(what)
            if what == "checks":
                return dict(snap or {"checks": [], "all_ok": True}, frozen=True)
            return dict(snap or {"parts": [], "use_existing_allowed": False, "missing": [], "message": ""},
                        frozen=True, choice=state.snapshot.get("choice"))

        def checks(self):
            ok, detail = state.backend.reachable(state.address)
            cs = [Check("printer reachable", ok, detail)]
            if ok:
                f = state.backend.facts(state.address)
                cs += engine.evaluate(self.chosen(f["issue"]), f)
            return {"checks": [c.__dict__ for c in cs], "all_ok": all(c.ok for c in cs)}

        def calibration(self):
            try:
                v = calibration.verdict(state.backend.config_files(state.address))
            except Exception as ex:
                return {"parts": [], "use_existing_allowed": False, "missing": ["all"],
                        "message": f"Cannot read the printer's calibration ({ex}). Full calibration required."}
            return {"parts": [p.__dict__ for p in v.parts], "use_existing_allowed": v.use_existing_allowed,
                    "missing": v.missing, "message": v.message}

        def hijack(self, b):
            with state.work:
                return self._hijack(b)

        def _hijack(self, b):
            if not state.demo:
                try:
                    already = state.backend.facts(state.address).get("mode") == "hijacked"
                except Exception:
                    already = False
                if already:
                    state.phase = "HIJACKED"
                    state.say("automatic take-over had just taken over the printer; nothing more to do")
                    return self.send(200, self.public_state())
            ch = self.checks()
            state.snapshot["checks"] = ch
            if not ch["all_ok"]:
                return self.send(409, {"error": "Checks failed, nothing was changed.", "checks": ch})
            cal = self.calibration()
            choice = b.get("calibration", "recalibrate")
            state.snapshot["calibration"], state.snapshot["choice"] = cal, choice
            if choice == "existing" and not cal["use_existing_allowed"]:
                return self.send(409, {"error": "The saved tuning cannot be kept: " + cal["message"]})
            exported = os.path.join(state.dir, "kalico", "calibration.cfg")
            printer_cfg = os.path.join(os.path.dirname(os.path.abspath(state.dir)), "printer_data", "config", "printer.cfg")
            if choice != "existing" and os.path.exists(exported):
                os.remove(exported)
                state.say("earlier imported tuning removed")
            if choice != "existing" and autosave.ensure_defaults(printer_cfg):
                state.say("factory calibration values added to printer.cfg; calibrate on the printer to replace them")
            if choice == "existing":
                try:
                    text = kalico_export.export(state.backend.config_files(state.address))
                except Exception as ex:
                    return self.send(409, {"error": f"Could not read the printer's saved tuning: {ex}"})
                if autosave.drop_defaults(printer_cfg):
                    state.say("factory calibration values removed from printer.cfg: the printer's own are used")
                os.makedirs(os.path.join(state.dir, "kalico"), exist_ok=True)
                with open(os.path.join(state.dir, "kalico", "calibration.cfg"), "w") as f:
                    f.write(text)
                state.say("the printer's saved tuning was copied to kalico/calibration.cfg")
            state.phase = "HIJACKING"
            state.say("take-over started" + (" (demo: simulated, the printer is not touched)" if state.demo else ""))
            try:
                state.backend.apply(self.chosen(), PAYLOAD, state.address, state.say)
            except engine.NebulaError as ex:
                state.phase = "CONFIGURED"
                state.say(f"take-over failed: {ex}")
                return self.send(409, {"error": str(ex)})
            state.phase = "HIJACKED"
            state.say("printer taken over; tuning: " + ("kept" if choice == "existing" else "tuned again"))
            self.send(200, self.public_state())

        def release(self):
            with state.work:
                state.backend.release(self.chosen(), state.address, state.say)
                state.phase = "CONFIGURED"
                if state.persist:
                    state.stop_watcher()
                    state.persist = False
                    state.save()
                    state.say("automatic take-over turned off: you passed control back to Cosmos on purpose")
            self.send(200, self.public_state())

        def pair(self, b):
            pw = b.get("password", "")
            if not pw:
                return self.send(400, {"error": "Enter the printer's login password."})
            ok, detail = state.backend.login(state.address, pw)
            state.paired = ok
            state.say("printer login saved" if ok
                      else "printer login failed: " + detail)
            return self.send(200 if ok else 409, self.public_state() if ok else {"error": "Could not log in to the printer: " + detail})

    return H


Check = engine.Check


def host_facts(demo):
    return {"name": "demo-host" if demo else os.uname().nodename, "link": "USB cable 10.78.0.1",
            "doctor": "watching the USB state", "version": "26.09.0"}


def main():
    global MANIFEST
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--state", default=os.path.expanduser("~/.nebula"))
    ap.add_argument("--real", action="store_true", help="drive a real printer (default: demo, printer not touched)")
    ap.add_argument("--manifest", default=None, help="one manifest file, instead of every version in nebula/manifests")
    ap.add_argument("--no-host-stack", action="store_true", help="real mode without starting/stopping Kalico and Moonraker")
    a = ap.parse_args()
    if a.manifest:
        MANIFEST = a.manifest
    stack = host_stack.HostStack() if a.real and not a.no_host_stack else None
    be = backends.RealBackend(remember_file=os.path.join(a.state, "printer-password"), stack=stack) if a.real else backends.DemoBackend()
    if a.real:
        be.load_remembered()
    st = State(a.state, be)
    st.restore(engine.load_manifests(MANIFEST))
    srv = ThreadingHTTPServer((a.bind, a.port), make_handler(st))
    print(f"nebula panel on {a.bind}:{a.port} /nebula (demo={st.demo})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
