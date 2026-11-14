import threading
import time

from . import cameras, engine
from .engine import AddonError


class AddonService:
    def __init__(self, catalogue_dir, state_path, cameras_path, host, api=cameras.http_api, say=print, demo=False,
                 background=True):
        self.catalogue = engine.load_catalogue(catalogue_dir)
        self.dir = catalogue_dir
        self.state = engine.State(state_path)
        self.cams = cameras.CameraList(cameras_path)
        self.host, self.api, self.say, self.demo, self.background = host, api, say, demo, background
        self.lock = threading.Lock()
        self.job = None


    def view(self):
        installed = self.state.read()
        out = []
        for a in self.catalogue.addons.values():
            checks = engine.fit(a, self.catalogue, self.host, installed)
            blockers = [c for c in checks if not c.ok and c.name != "Alternatives"]
            rec = installed.get(a.id)
            out.append({"id": a.id, "title": a.title, "description": a.description, "category": a.category, "group": a.group,
                        "version": a.version, "recommended": a.recommended, "installed": rec is not None,
                        "outdated": rec is not None and rec.get("fingerprint") != engine.fingerprint(a),
                        "fit": [c.__dict__ for c in checks], "can_install": not blockers, "keeps": a.keeps,
                        "alternatives": [o.id for o in self.catalogue.alternatives(a)]})
        return {"addons": out, "job": self.job, "web": self.web_interface(installed)}

    def web_interface(self, installed=None):
        installed = self.state.read() if installed is None else installed
        return next((a.id for a in self.catalogue.addons.values() if a.category == "web-interface" and a.id in installed), None)


    def start(self, action, addon_id):
        if action not in ("install", "remove"):
            raise AddonError("unknown action")
        addon = self.catalogue.get(addon_id)
        label = f"{'Installing' if action == 'install' else 'Removing'} {addon.title}"
        return self._job(label, lambda: self._change(action, addon))

    def _job(self, label, work):
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise AddonError(f"Still busy: {self.job['label'].lower()}. Wait until it finishes.")
            self.job = {"label": label, "state": "running", "message": ""}
        self.say(label + "...")

        def run():
            try:
                work()
                self.job = {"label": label, "state": "done", "message": ""}
                self.say(label + ": done")
            except AddonError as ex:
                self.job = {"label": label, "state": "error", "message": str(ex)}
                self.say(f"{label}: failed: {ex}")
            except Exception as ex:
                self.job = {"label": label, "state": "error", "message": f"{type(ex).__name__}: {ex}"}
                self.say(f"{label}: failed: {ex}")
        if self.background:
            threading.Thread(target=run, daemon=True).start()
        else:
            run()
        return self.job

    def _change(self, action, addon):
        if self.demo:
            time.sleep(0.6)
            rec = self.state.read()
            if action == "install":
                for rival in self.catalogue.alternatives(addon):
                    rec.pop(rival.id, None)
                rec[addon.id] = {"version": addon.version, "fingerprint": engine.fingerprint(addon), "installed_at": int(time.time())}
            else:
                rec.pop(addon.id, None)
            self.state.write(rec)
            return
        if action == "install":
            engine.install(addon, self.catalogue, self.host, self.state, self.dir, self.say)
        else:
            engine.remove(addon, self.host, self.state, self.say)
        self._publish_cameras()

    def _publish_cameras(self):
        if self.web_interface() is None or self.demo:
            return
        cameras.apply(self.cams, self.host, self.api, self.say, crowsnest="crowsnest" in self.state.read(), skip_host=True)


    def cameras(self):
        crowsnest = "crowsnest" in self.state.read()
        return {"cameras": [dict(c.__dict__, on_host=c.on_host) for c in self.cams.cameras()], "crowsnest": crowsnest,
                "web": self.web_interface() is not None}

    def camera_add_host(self, cam_id, name, device, resolution="640x480", fps=15):
        if "crowsnest" not in self.state.read():
            raise AddonError("Install the Crowsnest add-on first: cameras on this host are streamed by it.")
        self.cams.add(cameras.Camera(cam_id, name, "127.0.0.1", self.cams.next_host_port(), device=device,
                                     resolution=resolution, max_fps=fps))
        return self._job(f"Adding camera {name}", self._publish_cameras)

    def camera_remove(self, cam_id):
        if cam_id == cameras.PRINTER_CAMERA["id"]:
            raise AddonError("The printer's own camera cannot be removed here.")
        self.cams.remove(cam_id)
        return self._job(f"Removing camera {cam_id}", self._publish_cameras)
