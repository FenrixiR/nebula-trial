import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field

IDLE_STATES = ("standby", "complete", "cancelled")


class NebulaError(Exception):
    pass



@dataclass
class Manifest:
    version: str
    firmware: dict
    stop: list
    start: list
    keep: list
    root: str
    files: dict
    launch: dict
    ports: list
    raw: dict = field(default_factory=dict)

    @property
    def screen(self):
        return self.raw.get("screen", {})

    @property
    def host_addr(self):
        return self.launch["host_addr"]


class ManifestSet:

    def __init__(self, manifests):
        key = lambda m: tuple(int(x) for x in m.version.split("."))
        self.all = sorted(manifests, key=key)
        if not self.all:
            raise NebulaError("no manifests found")

    def pick(self, issue):
        for m in reversed(self.all):
            if m.version in issue:
                return m
        return self.all[-1]


def load_manifests(path):
    if os.path.isdir(path):
        names = sorted(n for n in os.listdir(path) if n.endswith(".toml"))
        return ManifestSet([load_manifest(os.path.join(path, n)) for n in names])
    return ManifestSet([load_manifest(path)])


def pick(manifest, issue):
    return manifest.pick(issue) if isinstance(manifest, ManifestSet) else manifest


def load_manifest(path):
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    try:
        m = Manifest(
            version=raw["cosmos"]["version"], firmware=raw["firmware"],
            stop=raw["stop"]["order"], start=raw["start"]["order"],
            keep=raw["keep"]["scripts"], root=raw["paths"]["root"],
            files=raw["files"], launch=raw["launch"], ports=raw["expect"]["ports"], raw=raw)
    except KeyError as e:
        raise NebulaError(f"manifest {path}: missing {e}")
    for name in m.stop + m.start:
        if name in m.keep or name.startswith("klipper-firmware"):
            raise NebulaError(f"manifest lists protected script {name} in stop/start")
    if not m.root.startswith("/user-resource/nebula"):
        raise NebulaError("manifest root must be under /user-resource/nebula")
    if "nebula-ctl.sh" not in m.files:
        raise NebulaError("manifest has no nebula-ctl.sh")
    return m


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_pins(m, payload, only=None):
    problems = []
    for name, spec in m.files.items():
        if only is not None and name not in only:
            continue
        src = os.path.join(payload, spec["source"])
        if not spec.get("sha256"):
            problems.append(f"{name}: not pinned (run `pin`)")
        elif not os.path.isfile(src):
            problems.append(f"{name}: payload file {spec['source']} missing")
        elif sha256_file(src) != spec["sha256"]:
            problems.append(f"{name}: hash differs from the manifest")
    return problems


def pin_manifest(path, payload):
    m = load_manifest(path)
    text = open(path).read()
    for name, spec in m.files.items():
        src = os.path.join(payload, spec["source"])
        if not os.path.isfile(src):
            print(f"pin: skipping {name}, {src} not found")
            continue
        pat = re.compile(r'(\[files\."%s"\].*?sha256 = )"[^"]*"' % re.escape(name), re.S)
        text, n = pat.subn(lambda mo: mo.group(1) + '"%s"' % sha256_file(src), text, count=1)
        if n != 1:
            raise NebulaError(f"pin: could not find sha256 of {name}")
    open(path, "w").write(text)


def render_env(m):
    relay = m.launch["relay"].format(root="$NEBULA_ROOT", host_addr=m.host_addr)
    lines = {
        "STOP_ORDER": " ".join(m.stop), "START_ORDER": " ".join(m.start),
        "RELAY_CMD": relay, "HOST_ADDR": m.host_addr,
        "VERIFY_PORTS": " ".join(str(p) for p in m.ports),
        "WATCHDOG_MINUTES": str(m.launch.get("watchdog_minutes", 0)),
    }
    if m.screen:
        lines["SCREEN_CMD"] = m.screen["command"].format(root="$NEBULA_ROOT", host_addr=m.host_addr)
        lines["SCREEN_KILL"] = " ".join(m.screen.get("kill", []))
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in lines.items())



ASKPASS = "#!/bin/sh\nprintf '%s\\n' \"$NEBULA_PW\"\n"


class SshRunner:

    def __init__(self, host, password=None, key=None, user="root", port=22, workdir=None):
        import tempfile
        self.host, self.password, self.key, self.user, self.port = host, password, key, user, port
        self.dir = workdir or tempfile.mkdtemp(prefix="nebula-ssh-")
        os.chmod(self.dir, 0o700)
        self.askpass = os.path.join(self.dir, "askpass.sh")
        with open(self.askpass, "w") as f:
            f.write(ASKPASS)
        os.chmod(self.askpass, 0o700)

    def argv(self, command):
        a = ["ssh", "-p", str(self.port), "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8",
             "-o", "ControlMaster=auto", "-o", f"ControlPath={self.dir}/cm-%C", "-o", "ControlPersist=60"]
        if self.password is not None:
            a += ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
                  "-o", "NumberOfPasswordPrompts=1"]
        else:
            a += ["-o", "BatchMode=yes"]
            if self.key:
                a += ["-i", self.key]
        return a + [f"{self.user}@{self.host}", command]

    def env(self):
        env = dict(os.environ)
        if self.password is not None:
            env.update(SSH_ASKPASS=self.askpass, SSH_ASKPASS_REQUIRE="force", NEBULA_PW=self.password, DISPLAY="nebula")
        return env

    def run(self, cmd, timeout=120):
        p = subprocess.run(self.argv(cmd), capture_output=True, text=True, timeout=timeout, env=self.env())
        return p.returncode, p.stdout + p.stderr

    def put(self, data, remote, mode):
        tmp = remote + ".part"
        cmd = f"cat > {shlex.quote(tmp)} && chmod {mode} {shlex.quote(tmp)} && mv {shlex.quote(tmp)} {shlex.quote(remote)}"
        p = subprocess.run(self.argv(cmd), input=data, capture_output=True, timeout=300, env=self.env())
        if p.returncode != 0:
            raise NebulaError(f"put {remote}: {p.stderr.decode(errors='replace')}")

    def close(self):
        subprocess.run(["ssh", "-O", "exit", "-o", f"ControlPath={self.dir}/cm-%C", f"{self.user}@{self.host}"],
                       capture_output=True, timeout=10)



def facts(runner):
    cmds = {
        "issue": "cat /etc/issue",
        "bed_ver": "cat /etc/bed.ver; echo; cat /lib/firmware/klipper-bed.bin.ver",
        "toolhead_ver": "cat /etc/toolhead.ver; echo; cat /lib/firmware/klipper-toolhead.bin.ver",
        "mode": "cat /run/nebula-mode 2>/dev/null",
        "space_kb": "df -k /user-resource | tail -n 1",
        "moonraker": "wget -q -T 3 -O - 'http://127.0.0.1/printer/objects/query?print_stats&extruder&heater_bed' 2>/dev/null",
    }
    out = {}
    for k, c in cmds.items():
        rc, text = runner.run(c)
        out[k] = text.strip()
    return out


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def evaluate(m, f, need_kb=20000):
    checks = []
    ok = m.version in f["issue"]
    checks.append(Check("Cosmos version", ok, f"Cosmos {m.version}, supported" if ok else
                        f"found {f['issue'].split(chr(10))[0][:50]!r}; this Nebula works with {m.version} only"))
    for name in ("bed", "toolhead"):
        parts = [x.strip() for x in f[f"{name}_ver"].splitlines() if x.strip()]
        ok = len(parts) == 2 and parts[0] == parts[1] == m.firmware[name]
        short = m.firmware[name].split("-")[-1]
        checks.append(Check(f"{name} MCU firmware", ok, f"up to date: matches the version Nebula expects ({short})" if ok else
                            f"found {parts}, Nebula expects {m.firmware[name]}"))
    if f["mode"] == "hijacked":
        checks.append(Check("not already hijacked", False, "Nebula is already running the printer"))
    else:
        checks.append(Check("not already hijacked", True, "the printer is running its own software"))
    cols = f["space_kb"].split()
    avail = int(cols[3]) if len(cols) >= 4 and cols[3].isdigit() else 0
    checks.append(Check("free space", avail >= need_kb, f"{avail // 1024} MB free on the printer's storage"))
    if f["mode"] != "hijacked":
        try:
            st = json.loads(f["moonraker"])["result"]["status"]
            idle = (st["print_stats"]["state"] in IDLE_STATES and st["extruder"]["target"] == 0
                    and st["heater_bed"]["target"] == 0)
            checks.append(Check("printer idle", idle, "not printing, heaters off" if idle else
                                f"the printer is {st['print_stats']['state']}, heaters set to {st['extruder']['target']:g}/{st['heater_bed']['target']:g} °C"))
        except (ValueError, KeyError):
            checks.append(Check("printer idle", False, "the printer's own software did not answer, so Nebula cannot tell whether it is idle"))
    return checks


def plan(m, payload):
    steps = ["Check the printer once more (looking only): software version, firmware, free space, idle"]
    steps.append(f"Make a folder for Nebula on the printer: {m.root}")
    steps.append(f"Copy Nebula's {len(m.files)} files to the printer and check each copy")
    steps.append("Write Nebula's settings file on the printer")
    steps.append(f"Stop the printer's own software: {', '.join(m.stop)}")
    steps.append(f"Start Nebula's parts on the printer: cable keeper, relay, safety timer ({m.launch.get('watchdog_minutes', 0)} min)"
                 + (", touch screen" if m.screen else ""))
    steps.append("Check that everything is running")
    steps.append("If anything fails, pass control back to Cosmos automatically (start " + ", ".join(m.start) + ")")
    return steps


def push_all(runner, m, payload, only=None):
    rc, out = runner.run(f"mkdir -p {m.root}")
    if rc != 0:
        raise NebulaError(f"mkdir {m.root}: {out}")
    for name, spec in m.files.items():
        if only is not None and name not in only:
            continue
        dest = f"{m.root}/{name}"
        marker = None
        if spec.get("extract"):
            marker = f"{m.root}/.unpacked-{name}.sha256"
            rc, out = runner.run(f"cat {shlex.quote(marker)} 2>/dev/null")
            if rc == 0 and out.strip() == spec["sha256"]:
                continue
        with open(os.path.join(payload, spec["source"]), "rb") as f:
            data = f.read()
        if "/" in name:
            runner.run(f"mkdir -p {shlex.quote(posixpath.dirname(dest))}")
        runner.put(data, dest, spec["mode"])
        rc, out = runner.run(f"sha256sum {shlex.quote(dest)}")
        if rc != 0 or out.split()[0] != spec["sha256"]:
            raise NebulaError(f"hash mismatch after push of {name}")
        if marker:
            target = f"{m.root}/{spec['extract']}".rstrip("/.")
            rc, out = runner.run(f"mkdir -p {shlex.quote(target)} && tar xzf {shlex.quote(dest)} -C {shlex.quote(target)} "
                                 f"&& rm -f {shlex.quote(dest)} && echo {spec['sha256']} > {shlex.quote(marker)}", timeout=300)
            if rc != 0:
                raise NebulaError(f"unpacking {name} failed: {out.strip()[:200]}")
    runner.put(render_env(m).encode(), f"{m.root}/nebula.env", "0644")


def ctl(runner, m, action):
    return runner.run(f"NEBULA_ROOT={m.root} sh {m.root}/nebula-ctl.sh {action}")


def release(runner, m):
    rc, out = ctl(runner, m, "release")
    if rc != 0:
        raise NebulaError(f"release failed: {out}")
    return out


def refresh_controller(runner, m, payload):
    problems = check_pins(m, payload, only=("nebula-ctl.sh",))
    if problems:
        raise NebulaError("payload not ready: " + "; ".join(problems))
    push_all(runner, m, payload, only=("nebula-ctl.sh",))


def purge(runner, m):
    root = shlex.quote(m.root)
    rc, out = runner.run(f"[ -f {root}/nebula-ctl.sh ] || exit 0; NEBULA_ROOT={root} sh {root}/nebula-ctl.sh purge")
    if rc != 0:
        raise NebulaError(f"removing Nebula from the printer failed: {out.strip()[:300]}")
    rc, left = runner.run(f"ls -d {root} /etc/init.d/nebula-cable /etc/rc3.d/S97nebula-cable /etc/rc5.d/S97nebula-cable 2>/dev/null")
    return left.split()


BOOT_FILES = ("nebula-ctl.sh", "nebula-boot.sh", "udhcpc-address-only.sh")


def cable_boot(runner, m, payload, action):
    if action == "on":
        problems = check_pins(m, payload, only=BOOT_FILES)
        if problems:
            raise NebulaError("payload not ready: " + "; ".join(problems))
        push_all(runner, m, payload, only=BOOT_FILES)
    rc, out = ctl(runner, m, f"persist {action}")
    if rc != 0:
        raise NebulaError(f"boot hook {action} failed: {out.strip()[:200]}")
    rc, out = ctl(runner, m, "persist status")
    return rc == 0 and "persist: on" in out


def apply(runner, m, payload, log=print):
    problems = check_pins(m, payload)
    if problems:
        raise NebulaError("payload not ready: " + "; ".join(problems))
    checks = evaluate(m, facts(runner))
    for c in checks:
        log(f"{'ok  ' if c.ok else 'FAIL'} {c.name} {c.detail}")
    bad = [c for c in checks if not c.ok]
    if bad:
        raise NebulaError("preflight failed: " + ", ".join(c.name for c in bad))
    push_all(runner, m, payload)
    log("pushed files")
    rc, out = ctl(runner, m, "hijack")
    log(out.strip())
    if rc == 0:
        rc, out = ctl(runner, m, "verify")
        log(out.strip())
    if rc != 0:
        log("hijack or verify failed, releasing")
        release(runner, m)
        raise NebulaError("hijack failed; the printer was released to stock")
    return True
