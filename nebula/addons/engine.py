import hashlib
import io
import json
import os
import posixpath
import tempfile
import time
import tomllib
import zipfile
from dataclasses import dataclass, field

SYSTEMD_DIR = "/etc/systemd/system/"
ASIDE_DIR = "/var/lib/nebula-addons/displaced"


def aside(path):
    return ASIDE_DIR + "/" + path.strip("/").replace("/", "%")


PROBE_TRIES, PROBE_WAIT = 12, 0.5
MAX_UNPACKED = 200 * 1024 * 1024


class AddonError(Exception):
    pass


@dataclass
class Addon:
    id: str
    title: str
    description: str
    category: str
    group: str
    version: str
    recommended: bool
    needs: dict
    downloads: dict
    files: dict
    units: list
    managed: list
    reload: list
    check: dict
    keeps: list
    displace: list
    raw: dict = field(default_factory=dict)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""



def load_addon(path):
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    try:
        a = raw["addon"]
        addon = Addon(
            id=a["id"], title=a["title"], description=a["description"], category=a["category"],
            group=a.get("group", ""), version=a["version"], recommended=bool(a.get("recommended", False)), needs=raw.get("needs", {}),
            downloads=raw.get("download", {}), files=raw.get("files", {}),
            units=raw.get("services", {}).get("units", []), managed=raw.get("services", {}).get("managed", []), reload=raw.get("services", {}).get("reload", []), check=raw.get("check", {}),
            keeps=raw.get("remove", {}).get("keeps", []),
            displace=raw.get("displace", {}).get("paths", []), raw=raw)
    except KeyError as e:
        raise AddonError(f"add-on {path}: missing {e}")
    if not addon.id.replace("-", "").isalnum() or not addon.id.islower():
        raise AddonError(f"add-on {path}: id must be lower-case letters, digits and dashes")
    for name, d in addon.downloads.items():
        for key in ("url", "sha256", "dest"):
            if not d.get(key):
                raise AddonError(f"{addon.id}: download {name} has no {key}")
        if not d["url"].startswith("https://"):
            raise AddonError(f"{addon.id}: download {name} must be https")
        _absolute(addon.id, d["dest"])
        if type(d.get("strip", 0)) is not int or not 0 <= d.get("strip", 0) <= 3:
            raise AddonError(f"{addon.id}: download {name}: strip must be a number from 0 to 3")
    for name, spec in addon.files.items():
        for key in ("source", "sha256", "dest"):
            if not spec.get(key):
                raise AddonError(f"{addon.id}: file {name} has no {key}")
        _absolute(addon.id, spec["dest"])
    return addon


def _absolute(addon_id, dest):
    if not dest.startswith("/") or ".." in dest.split("/"):
        raise AddonError(f"{addon_id}: destination {dest} must be an absolute path without ..")


class Catalogue:
    def __init__(self, addons):
        self.addons = {a.id: a for a in addons}
        if len(self.addons) != len(addons):
            raise AddonError("two add-ons share an id")

    def get(self, addon_id):
        if addon_id not in self.addons:
            raise AddonError(f"unknown add-on {addon_id}")
        return self.addons[addon_id]

    def alternatives(self, addon):
        return [a for a in self.addons.values() if addon.group and a.group == addon.group and a.id != addon.id]


def load_catalogue(directory):
    names = sorted(n for n in os.listdir(directory) if n.endswith(".toml"))
    return Catalogue([load_addon(os.path.join(directory, n)) for n in names])



class State:

    def __init__(self, path):
        self.path = path

    def read(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except ValueError:
            raise AddonError(f"{self.path} is damaged; move it aside to start from a clean list")

    def write(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)



def fit(addon, catalogue, host, installed):
    f = host.facts()
    n = addon.needs
    checks = []
    if "ram_mb" in n:
        checks.append(Check("Free memory", f["ram_free_mb"] >= n["ram_mb"],
                            f"needs about {n['ram_mb']} MB, {f['ram_free_mb']} MB free"))
    if "disk_mb" in n:
        checks.append(Check("Disk space", f["disk_free_mb"] >= n["disk_mb"],
                            f"needs about {n['disk_mb']} MB, {f['disk_free_mb']} MB free"))
    if n.get("models"):
        ok = any(m in f["model"] for m in n["models"])
        checks.append(Check("Device model", ok, f"this device is a {f['model'] or 'unknown model'}; "
                            f"this add-on needs one of: {', '.join(n['models'])}"))
    for key, text in n.get("advice", {}).items():
        if key in f["model"]:
            checks.append(Check("Note", True, text))
    for other in n.get("addons", []):
        checks.append(Check(f"Needs {other}", other in installed, "install it first" if other not in installed else "installed"))
    rivals = [a for a in catalogue.alternatives(addon) if a.id in installed]
    if rivals:
        checks.append(Check("Alternatives", False, f"{rivals[0].title} is installed; installing this replaces it"))
    return checks



def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def unzip(data, strip=0):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise AddonError("the download is not a zip file")
    out, total = {}, 0
    for info in z.infolist():
        if info.is_dir():
            continue
        name = posixpath.normpath(info.filename)
        if name.startswith("/") or name == ".." or name.startswith("../"):
            raise AddonError(f"the download has an unsafe path: {info.filename}")
        total += info.file_size
        if total > MAX_UNPACKED:
            raise AddonError("the download unpacks to far more than expected")
        parts = name.split("/")
        if len(parts) <= strip:
            continue
        out["/".join(parts[strip:])] = z.read(info)
    return out


def fingerprint(addon):
    parts = [addon.version] + [d["sha256"] + d["dest"] for _, d in sorted(addon.downloads.items())] \
        + [f["sha256"] + f["dest"] for _, f in sorted(addon.files.items())]
    return sha256_bytes("\n".join(parts).encode())


def install(addon, catalogue, host, state, source_dir, log=print):
    installed = state.read()
    if addon.id in installed and installed[addon.id].get("fingerprint") == fingerprint(addon):
        return "already"
    for rival in catalogue.alternatives(addon):
        if rival.id in installed:
            log(f"{addon.title}: replacing {rival.title}")
            remove(rival, host, state, log)
    installed = state.read()
    problems = [c for c in fit(addon, catalogue, host, installed) if not c.ok]
    if problems:
        raise AddonError(f"{addon.title} cannot be installed: " + "; ".join(f"{c.name}: {c.detail}" for c in problems))
    if addon.id in installed:
        log(f"{addon.title}: changed since the last install ({installed[addon.id]['version']} -> {addon.version}), upgrading")

    undo = []
    systemd = any(f["dest"].startswith(SYSTEMD_DIR) for f in addon.files.values())

    def unwind():
        if not undo:
            return
        for step in reversed(undo):
            try:
                step()
            except Exception as ex:
                log(f"{addon.title}: undo failed: {ex}")
        if systemd:
            host.run(["systemctl", "daemon-reload"])
        for unit in addon.reload:
            host.run(["systemctl", "reload-or-restart", unit])

    try:
        trees, blobs = {}, {}
        for name, d in addon.downloads.items():
            log(f"{addon.title}: downloading {name}")
            data = host.fetch(d["url"])
            if sha256_bytes(data) != d["sha256"]:
                raise AddonError(f"download {name} does not match its pinned sha256; nothing was installed")
            trees[name] = unzip(data, d.get("strip", 0))
        for name, spec in addon.files.items():
            with open(os.path.join(source_dir, spec["source"]), "rb") as fh:
                data = fh.read()
            if sha256_bytes(data) != spec["sha256"]:
                raise AddonError(f"file {name} does not match its pinned sha256; nothing was installed")
            blobs[name] = data

        for pkg in addon.needs.get("packages", []):
            if host.run(["dpkg", "-s", pkg])[0] != 0:
                log(f"{addon.title}: installing package {pkg}")
                rc, out = host.run(["apt-get", "install", "-y", "--no-install-recommends", pkg])
                if rc != 0:
                    raise AddonError(f"package {pkg} failed to install: {out.strip()[-200:]}")

        for name, d in addon.downloads.items():
            dest, new, old = d["dest"], d["dest"] + ".new", d["dest"] + ".old"
            host.remove(new)
            host.remove(old)
            host.put_tree(trees[name], new)
            had = host.exists(dest)
            if had:
                host.rename(dest, old)
            host.rename(new, dest)

            def back(dest=dest, old=old, had=had):
                host.remove(dest)
                if had:
                    host.rename(old, dest)
            undo.append(back)

        for path in addon.displace:
            if host.exists(path):
                host.rename(path, aside(path))
                undo.append(lambda path=path: host.rename(aside(path), path))

        for name, spec in addon.files.items():
            dest = spec["dest"]
            before = host.read(dest)
            host.put(dest, blobs[name], int(spec.get("mode", "0644"), 8))
            undo.append(lambda dest=dest, before=before: host.put(dest, before, 0o644) if before is not None else host.remove(dest))

        if systemd:
            host.run(["systemctl", "daemon-reload"])
        test = addon.check.get("test")
        if test:
            rc, out = host.run(test)
            if rc != 0:
                raise AddonError(f"the configuration test failed: {out.strip()[-200:]}")

        if addon.units:
            host.run(["systemctl", "daemon-reload"])
        for unit in addon.units:
            was = host.run(["systemctl", "is-enabled", unit])[0] == 0
            rc, out = host.run(["systemctl", "enable", unit])
            if rc != 0:
                raise AddonError(f"could not enable {unit}: {out.strip()[-200:]}")
            undo.append(lambda unit=unit, was=was: None if was else host.run(["systemctl", "disable", unit]))
            undo.append(lambda unit=unit: host.run(["systemctl", "stop", unit]))
            rc, out = host.run(["systemctl", "restart", unit])
            if rc != 0:
                raise AddonError(f"{unit} did not start: {out.strip()[-200:]}")
        for unit in addon.reload:
            rc, out = host.run(["systemctl", "reload-or-restart", unit])
            if rc != 0:
                raise AddonError(f"{unit} did not accept the new configuration: {out.strip()[-200:]}")

        url = addon.check.get("url")
        if url:
            status = host.probe(url)
            for _ in range(PROBE_TRIES):
                if status != 0:
                    break
                time.sleep(PROBE_WAIT)
                status = host.probe(url)
            if status != addon.check.get("status", 200):
                raise AddonError(f"{url} answered {status}, expected {addon.check.get('status', 200)}")

        installed = state.read()
        installed[addon.id] = {"version": addon.version, "fingerprint": fingerprint(addon), "installed_at": int(time.time())}
        state.write(installed)
        for d in addon.downloads.values():
            host.remove(d["dest"] + ".old")
    except AddonError:
        unwind()
        raise
    except Exception as ex:
        unwind()
        raise AddonError(f"{addon.title}: {ex}")
    log(f"{addon.title}: installed")
    return "installed"


def remove(addon, host, state, log=print):
    installed = state.read()
    for unit in addon.units + addon.managed:
        host.run(["systemctl", "stop", unit])
        host.run(["systemctl", "disable", unit])
    for spec in addon.files.values():
        host.remove(spec["dest"])
    for path in addon.displace:
        if host.exists(aside(path)) and not host.exists(path):
            host.rename(aside(path), path)
    for d in addon.downloads.values():
        for path in (d["dest"], d["dest"] + ".old", d["dest"] + ".new"):
            host.remove(path)
    if addon.units or addon.managed or any(f["dest"].startswith(SYSTEMD_DIR) for f in addon.files.values()):
        host.run(["systemctl", "daemon-reload"])
    for unit in addon.reload:
        host.run(["systemctl", "reload-or-restart", unit])
    present = addon.id in installed
    installed.pop(addon.id, None)
    state.write(installed)
    log(f"{addon.title}: removed")
    return "removed" if present else "absent"
