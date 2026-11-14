import argparse
import hashlib
import os
import re
import sys
import tomllib

from . import engine
from .host import SystemHost

HERE = os.path.dirname(os.path.abspath(__file__))


def pin(directory):
    for name in sorted(n for n in os.listdir(directory) if n.endswith(".toml")):
        path = os.path.join(directory, name)
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        text = open(path).read()
        for key, spec in raw.get("files", {}).items():
            with open(os.path.join(directory, spec["source"]), "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            pat = re.compile(r'(\[files\.%s\].*?sha256 = )"[^"]*"' % re.escape(key), re.S)
            text, n = pat.subn(lambda mo: mo.group(1) + '"%s"' % digest, text, count=1)
            if n != 1:
                raise engine.AddonError(f"{name}: could not find the sha256 of {key}")
        open(path, "w").write(text)
        print(f"pinned {name}")


def cameras_main(args):
    from . import cameras
    store = cameras.CameraList(os.path.expanduser("~/.nebula/cameras.json"))
    host = SystemHost()
    cmd = args[0] if args else "list"
    if cmd == "list":
        for c in store.cameras():
            where = f"{c.address}:{c.port}" + (f" {c.device}" if c.on_host else "")
            print(f"{c.id:12} {c.name}  ({where})")
    elif cmd == "add-host" and len(args) >= 4:
        opts = dict(zip(args[4::2], args[5::2]))
        store.add(cameras.Camera(args[1], args[2], "127.0.0.1", store.next_host_port(), device=args[3],
                                 resolution=opts.get("--resolution", "640x480"), max_fps=int(opts.get("--fps", 15))))
        print("added; run `cameras apply` to start it")
    elif cmd == "add" and len(args) >= 5:
        store.add(cameras.Camera(args[1], args[2], args[3], int(args[4])))
        print("added; run `cameras apply` to publish it")
    elif cmd == "remove" and len(args) == 2:
        store.remove(args[1])
        print("removed; run `cameras apply` to publish it")
    elif cmd == "apply":
        installed = engine.State(os.path.expanduser("~/.nebula/addons.json")).read()
        print("in the web interface:", ", ".join(cameras.apply(store, host, crowsnest="crowsnest" in installed)))
    else:
        print("usage: cameras list | add ID NAME ADDRESS PORT | add-host ID NAME DEVICE [--resolution WxH] [--fps N] | remove ID | apply", file=sys.stderr)
        return 2
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "cameras":
        try:
            return cameras_main(argv[1:])
        except (engine.AddonError, ValueError) as ex:
            print(f"error: {ex}", file=sys.stderr)
            return 1
    ap = argparse.ArgumentParser(prog="python3 -m nebula.addons")
    ap.add_argument("command", choices=["list", "fit", "install", "remove", "pin"])
    ap.add_argument("addon", nargs="?")
    ap.add_argument("--dir", default=HERE, help="folder with the add-on manifests and the files they ship")
    ap.add_argument("--state", default=os.path.expanduser("~/.nebula/addons.json"))
    a = ap.parse_args(argv)
    try:
        if a.command == "pin":
            pin(a.dir)
            return 0
        cat = engine.load_catalogue(a.dir)
        state = engine.State(a.state)
        host = SystemHost()
        installed = state.read()
        if a.command == "list":
            for ad in cat.addons.values():
                print(f"{ad.id:12} {'installed ' + installed[ad.id]['version'] if ad.id in installed else '-':16} {ad.title}: {ad.description}")
            return 0
        if not a.addon:
            ap.error("name an add-on")
        ad = cat.get(a.addon)
        if a.command == "fit":
            checks = engine.fit(ad, cat, host, installed)
            for c in checks:
                print(("ok   " if c.ok else "FAIL ") + f"{c.name}: {c.detail}")
            return 0 if all(c.ok for c in checks) else 1
        if a.command == "install":
            print(engine.install(ad, cat, host, state, a.dir))
        else:
            print(engine.remove(ad, host, state))
        return 0
    except engine.AddonError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1
