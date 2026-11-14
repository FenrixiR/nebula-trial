import json
import os
import subprocess
import threading
import time

COMMAND = "/usr/local/sbin/nebula-update"
STATUS_FILE = "/var/lib/nebula/update.json"


class UpdateError(Exception):
    pass


class Updater:
    def __init__(self, version_file, command=COMMAND, status_file=STATUS_FILE, run=subprocess.run, popen=subprocess.Popen):
        self.version_file, self.command, self.status_file = version_file, command, status_file
        self.run, self.popen = run, popen

    def version(self):
        try:
            with open(self.version_file) as f:
                return f.read().strip() or "unknown"
        except OSError:
            return "unknown"

    def check(self):
        try:
            p = self.run([self.command, "--check"], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as ex:
            raise UpdateError(f"could not look for updates: {ex}")
        if p.returncode != 0:
            raise UpdateError(_last_line(p.stderr) or "could not look for updates")
        try:
            return json.loads(p.stdout)
        except ValueError:
            raise UpdateError("the update information could not be read")

    def start(self):
        try:
            self.popen(["sudo", "-n", self.command, "--update", "--yes"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as ex:
            raise UpdateError(f"could not start the update: {ex}")

    def status(self):
        try:
            with open(self.status_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None


def _last_line(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1].split(": ", 1)[-1] if lines else ""


class DemoUpdater:

    def __init__(self, seconds=3):
        self.current, self.seconds, self._status = "0.2.0", seconds, None

    def version(self):
        return self.current

    def check(self):
        notes = ["The Nebula menu can now check for updates and install them", "Kinder wording when the printer cannot be reached"]
        return {"installed": self.current, "available": "0.3.0", "newer": self.current != "0.3.0", "released": "2026-11-12", "notes": notes}

    def start(self):
        self._status = {"state": "running", "from": self.current, "to": "0.3.0", "message": "installing 0.3.0 (demo)", "at": time.strftime("%F %T")}

        def finish():
            time.sleep(self.seconds)
            self.current = "0.3.0"
            self._status = {"state": "done", "from": "0.2.0", "to": "0.3.0", "message": "updated from 0.2.0 to 0.3.0 (demo)", "at": time.strftime("%F %T")}
        threading.Thread(target=finish, daemon=True).start()

    def status(self):
        return self._status
