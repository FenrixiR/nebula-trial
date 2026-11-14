import json
import subprocess
import time
import urllib.parse
import urllib.request

from .hijack.engine import NebulaError

UNITS = ("nebula-relay-client", "nebula-kalico", "nebula-moonraker")


def _run(argv):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr


def _http(method, url, timeout=5):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


class HostStack:
    def __init__(self, run=_run, http=_http, sleep=time.sleep, units=UNITS, api="http://127.0.0.1:7125", ready_timeout=90):
        self.run, self.http, self.sleep = run, http, sleep
        self.units, self.api, self.ready_timeout = units, api, ready_timeout

    def _systemctl(self, verb, unit):
        return self.run(["sudo", "-n", "systemctl", verb, unit])

    def running(self):
        return all(self._systemctl("is-active", u)[0] == 0 for u in self.units)

    def any_running(self):
        return any(self._systemctl("is-active", u)[0] == 0 for u in self.units)

    def state(self):
        try:
            r = self.http("GET", f"{self.api}/printer/info").get("result", {})
            return r.get("state"), r.get("state_message", "")
        except Exception:
            return None, ""

    def ready(self):
        return self.state()[0] == "ready"

    def start(self, log=print):
        if self.any_running():
            log("Kalico: restarting parts left over from an earlier session")
            self.stop(log)
        for u in self.units:
            rc, out = self._systemctl("start", u)
            if rc != 0:
                raise NebulaError(f"host service {u} would not start: {out.strip()[:160]}")
        waited = 0
        while True:
            state, message = self.state()
            if state == "ready":
                break
            if state in ("error", "shutdown"):
                raise NebulaError(f"Kalico stopped with an error: {' '.join(message.split())[:300]}")
            if waited >= self.ready_timeout:
                raise NebulaError(f"Kalico did not report ready within {self.ready_timeout} s")
            self.sleep(2)
            waited += 2
        log("Kalico is ready")

    def stop(self, log=print):
        for script in ("M104 S0", "M140 S0"):
            try:
                self.http("POST", f"{self.api}/printer/gcode/script?script={urllib.parse.quote(script)}")
            except Exception:
                pass
        for u in reversed(self.units):
            self._systemctl("stop", u)
        log("Kalico stopped")
