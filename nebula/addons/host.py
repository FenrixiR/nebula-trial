import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

from .engine import AddonError

MAX_DOWNLOAD = 100 * 1024 * 1024
PROTECTED = {"/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/opt", "/proc", "/root", "/run", "/sbin", "/srv",
             "/sys", "/tmp", "/usr", "/var", "/var/lib", "/var/www", "/etc/nginx", "/etc/systemd", "/etc/systemd/system",
             "/usr/local", "/usr/local/bin", "/usr/local/sbin"}


class SystemHost:
    def __init__(self, sudo=("sudo", "-n")):
        self.sudo = list(sudo)

    def _do(self, argv):
        rc, out = self.run(argv)
        if rc != 0:
            raise AddonError(f"{' '.join(argv[:2])} failed: {out.strip()[-200:]}")

    def run(self, argv):
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        try:
            p = subprocess.run(self.sudo + list(argv), capture_output=True, text=True, timeout=900, env=env)
        except (OSError, subprocess.TimeoutExpired) as ex:
            return 1, str(ex)
        return p.returncode, p.stdout + p.stderr

    def fetch(self, url):
        if not url.startswith("https://"):
            raise AddonError("downloads must be https")
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read(MAX_DOWNLOAD + 1)
        except (urllib.error.URLError, OSError) as ex:
            raise AddonError(f"could not download {url}: {ex}")
        if len(data) > MAX_DOWNLOAD:
            raise AddonError(f"{url} is larger than expected")
        return data

    def probe(self, url):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as ex:
            return ex.code
        except (urllib.error.URLError, OSError):
            return 0

    def exists(self, path):
        return os.path.lexists(path)

    def read(self, path):
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _guard(self, path):
        parts = [p for p in path.split("/") if p]
        if not path.startswith("/") or ".." in parts or path.rstrip("/") in PROTECTED or len(parts) < 3:
            raise AddonError(f"refusing to touch {path}")

    def put(self, path, data, mode=0o644):
        self._guard(path)
        with tempfile.NamedTemporaryFile(delete=False) as t:
            t.write(data)
        try:
            self._do(["install", "-D", "-m", format(mode, "o"), t.name, path])
        finally:
            os.unlink(t.name)

    def put_tree(self, files, dest):
        self._guard(dest)
        tmp = tempfile.mkdtemp()
        try:
            for rel, data in files.items():
                p = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(data)
            self._do(["rm", "-rf", dest])
            self._do(["mkdir", "-p", dest])
            self._do(["cp", "-a", tmp + "/.", dest])
            self._do(["chmod", "-R", "a+rX", dest])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def remove(self, path):
        self._guard(path)
        self._do(["rm", "-rf", path])

    def rename(self, a, b):
        self._guard(a)
        self._guard(b)
        self._do(["mkdir", "-p", os.path.dirname(b)])
        self._do(["mv", a, b])

    def facts(self):
        mem = 0
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    mem = int(line.split()[1]) // 1024
        st = os.statvfs("/")
        try:
            with open("/proc/device-tree/model", "rb") as f:
                model = f.read().replace(b"\0", b"").decode(errors="replace").strip()
        except OSError:
            model = ""
        return {"ram_free_mb": mem, "disk_free_mb": st.f_bavail * st.f_frsize // (1024 * 1024), "model": model}


class MemoryHost:

    def __init__(self):
        self.files, self.trees, self.calls = {}, {}, []
        self.web = {}
        self.fail = set()
        self.status = 200
        self.ram, self.disk, self.model = 300, 20000, "Raspberry Pi Zero 2 W Rev 1.0"
        self.packages, self.enabled = set(), set()

    def facts(self):
        return {"ram_free_mb": self.ram, "disk_free_mb": self.disk, "model": self.model}

    def fetch(self, url):
        self.calls.append(f"fetch {url}")
        return self.web[url]

    def probe(self, url):
        self.calls.append(f"probe {url}")
        return self.status

    def run(self, argv):
        line = " ".join(argv)
        self.calls.append(line)
        if any(line.startswith(p) for p in self.fail):
            return 1, "failed on purpose"
        if argv[:2] == ["systemctl", "is-enabled"]:
            return (0, "") if argv[2] in self.enabled else (1, "")
        if argv[:2] == ["systemctl", "enable"]:
            self.enabled.add(argv[2])
        if argv[:2] == ["dpkg", "-s"]:
            return (0, "") if argv[2] in self.packages else (1, "")
        if argv[:2] == ["apt-get", "install"]:
            self.packages.add(argv[-1])
        return 0, ""

    def exists(self, path):
        return path in self.files or path in self.trees

    def read(self, path):
        return self.files.get(path)

    def put(self, path, data, mode=0o644):
        self.calls.append(f"put {path}")
        self.files[path] = data

    def put_tree(self, files, dest):
        self.calls.append(f"put_tree {dest}")
        self.trees[dest] = dict(files)

    def remove(self, path):
        self.files.pop(path, None)
        self.trees.pop(path, None)

    def rename(self, a, b):
        self.calls.append(f"rename {a} {b}")
        store = self.trees if a in self.trees else self.files
        store[b] = store.pop(a)
