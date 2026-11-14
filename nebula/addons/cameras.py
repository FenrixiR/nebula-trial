import ipaddress
import json
import os
import re
import tempfile
from urllib.parse import quote
from dataclasses import asdict, dataclass

from .engine import AddonError

CROWSNEST_CONF = "/etc/nebula/crowsnest.conf"
CROWSNEST_UNIT = "nebula-crowsnest.service"
CAMERA_DIR = "/etc/nginx/nebula-cameras"
CABLE_NET = ipaddress.ip_network("10.78.0.0/24")
HOST_PORTS = range(8081, 8200)
PRINTER_CAMERA = {"id": "printer", "name": "Printer camera", "address": "10.78.0.100", "port": 8080}
MOONRAKER = "http://127.0.0.1:7125"


class Unreachable(AddonError):
    pass


@dataclass
class Camera:
    id: str
    name: str
    address: str
    port: int
    device: str = ""
    resolution: str = "640x480"
    max_fps: int = 15

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?", self.id):
            raise AddonError(f"camera id {self.id!r}: lower-case letters, digits and dashes only")
        if not self.name.strip() or len(self.name) > 60 or any(ord(c) < 32 for c in self.name):
            raise AddonError(f"camera {self.id}: the name must be 1 to 60 printable characters")
        if type(self.port) is not int:
            raise AddonError(f"camera {self.id}: the port must be a number")
        try:
            ip = ipaddress.ip_address(self.address)
        except ValueError:
            raise AddonError(f"camera {self.id}: {self.address!r} is not an IP address")
        if ip in CABLE_NET:
            ok_port = 1 <= self.port <= 65535
        elif ip.is_loopback:
            ok_port = self.port in HOST_PORTS
        else:
            raise AddonError(f"camera {self.id}: cameras must be on the printer cable ({CABLE_NET}) or on this host")
        if not ok_port:
            raise AddonError(f"camera {self.id}: port {self.port} is not allowed for {self.address}")
        if ip.is_loopback:
            if not re.fullmatch(r"/dev/(video\d{1,3}|v4l/by-(id|path)/[A-Za-z0-9_.:-]{1,120})", self.device):
                raise AddonError(f"camera {self.id}: device {self.device!r} must be /dev/videoN or /dev/v4l/by-id/NAME")
            if not re.fullmatch(r"[1-9]\d{2,3}x[1-9]\d{2,3}", self.resolution):
                raise AddonError(f"camera {self.id}: resolution {self.resolution!r} must look like 640x480")
            if type(self.max_fps) is not int or not 1 <= self.max_fps <= 60:
                raise AddonError(f"camera {self.id}: frames per second must be a number from 1 to 60")
        elif self.device:
            raise AddonError(f"camera {self.id}: a device only applies to cameras on this host")

    @property
    def on_host(self):
        return ipaddress.ip_address(self.address).is_loopback


def render_nginx(cam):
    return f'''# Nebula camera "{cam.id}" (generated; edit the camera list, not this file)
location /webcam/{cam.id}/ {{
    limit_except GET HEAD {{ deny all; }}
    proxy_pass http://{cam.address}:{cam.port}/;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 3600;
}}
'''


def render_crowsnest(cams):
    out = ["# Nebula (generated from the camera list; edit the list, not this file)\n",
           "[crowsnest]\nlog_level: quiet\nrollover_on_start: false\nno_proxy: false\n"]
    for c in cams:
        if c.on_host:
            out.append(f"[cam {c.id}]\nmode: ustreamer\nport: {c.port}\ndevice: {c.device}\n"
                       f"resolution: {c.resolution}\nmax_fps: {c.max_fps}\n")
    return "\n".join(out)


def webcam_entry(cam):
    return {"name": cam.name, "service": "mjpegstreamer", "enabled": True, "location": "printer",
            "stream_url": f"/webcam/{cam.id}/stream", "snapshot_url": f"/webcam/{cam.id}/snapshot"}


class CameraList:

    def __init__(self, path):
        self.path = path

    def read(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except FileNotFoundError:
            return {"cameras": [PRINTER_CAMERA], "applied": {"ids": [], "names": []}}
        except ValueError:
            raise AddonError(f"{self.path} is damaged; move it aside to start from the default list")
        d.setdefault("applied", {"ids": [], "names": []})
        return d

    def cameras(self):
        return self.check([Camera(**{k: v for k, v in c.items() if k in Camera.__dataclass_fields__}) for c in self.read()["cameras"]])

    @staticmethod
    def check(cams):
        if len({c.id for c in cams}) != len(cams):
            raise AddonError("two cameras share an id")
        if len({c.name for c in cams}) != len(cams):
            raise AddonError("two cameras share a name")
        return cams

    def write(self, d):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def next_host_port(self):
        used = {c.port for c in self.cameras() if c.on_host}
        for port in HOST_PORTS:
            if port not in used:
                return port
        raise AddonError("no free camera port left")

    def add(self, cam):
        d = self.read()
        self.check([Camera(**{k: v for k, v in c.items() if k in Camera.__dataclass_fields__}) for c in d["cameras"]] + [cam])
        d["cameras"].append(asdict(cam))
        self.write(d)

    def remove(self, cam_id):
        d = self.read()
        d["cameras"] = [c for c in d["cameras"] if c["id"] != cam_id]
        self.write(d)


def http_api(method, path, data=None):
    import urllib.error
    import urllib.request
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(MOONRAKER + path, data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as ex:
        raise AddonError(f"Moonraker answered {ex.code} to {method} {path}")
    except (urllib.error.URLError, OSError) as ex:
        raise Unreachable(f"Moonraker is not reachable: {ex}")
    except ValueError as ex:
        raise AddonError(f"Moonraker sent something unreadable for {method} {path}: {ex}")


def apply(store, host, api=http_api, log=print, crowsnest=False, skip_host=False):
    cams = store.cameras()
    if skip_host and not crowsnest:
        cams = [c for c in cams if not c.on_host]
    on_host = [c for c in cams if c.on_host]
    if on_host and not crowsnest:
        raise AddonError("camera " + ", ".join(c.id for c in on_host) + " is on this host and needs the Crowsnest add-on")
    state = store.read()
    old_ids = state["applied"]["ids"]
    previous = {cid: host.read(f"{CAMERA_DIR}/{cid}.conf") for cid in set(old_ids) | {c.id for c in cams}}
    for cam in cams:
        host.put(f"{CAMERA_DIR}/{cam.id}.conf", render_nginx(cam).encode(), 0o644)
    for cid in old_ids:
        if cid not in {c.id for c in cams}:
            host.remove(f"{CAMERA_DIR}/{cid}.conf")
    rc, out = host.run(["nginx", "-t"])
    if rc == 0:
        rc, out = host.run(["systemctl", "reload-or-restart", "nginx"])
    if rc != 0:
        for cid, text in previous.items():
            if text is None:
                host.remove(f"{CAMERA_DIR}/{cid}.conf")
            else:
                host.put(f"{CAMERA_DIR}/{cid}.conf", text, 0o644)
        host.run(["systemctl", "reload-or-restart", "nginx"])
        raise AddonError(f"nginx did not accept the camera list: {out.strip()[-200:]}")
    names = []
    for cam in cams:
        api("POST", "/server/webcams/item", webcam_entry(cam))
        names.append(cam.name)
    for name in state["applied"]["names"]:
        if name not in names:
            api("DELETE", "/server/webcams/item?name=" + quote(name))
    state["applied"] = {"ids": [c.id for c in cams], "names": names}
    store.write(state)
    if crowsnest:
        if on_host:
            host.put(CROWSNEST_CONF, render_crowsnest(cams).encode(), 0o644)
            host.run(["systemctl", "enable", CROWSNEST_UNIT])
            rc, out = host.run(["systemctl", "restart", CROWSNEST_UNIT])
            if rc != 0:
                raise AddonError(f"Crowsnest did not start: {out.strip()[-200:]}")
        else:
            host.run(["systemctl", "stop", CROWSNEST_UNIT])
            host.run(["systemctl", "disable", CROWSNEST_UNIT])
            host.remove(CROWSNEST_CONF)
    log(f"cameras: {len(cams)} in the web interface")
    return [c.id for c in cams]
