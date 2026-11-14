import os

from . import calibration, scenarios_data
from .hijack import engine


class DemoBackend:
    demo = True

    def __init__(self):
        self.scenario = "ready"
        self.names = scenarios_data.NAMES

    def facts(self, address):
        return scenarios_data.printer_facts(self.scenario)

    def reachable(self, address):
        return True, f"{address or '10.78.0.100'} answers on port 22 (demo)"

    def config_files(self, address):
        return scenarios_data.printer_config(self.scenario)

    def apply(self, manifest, payload, address, log):
        for s in engine.plan(manifest, payload)[1:-1]:
            log("would: " + s)

    def release(self, manifest, address, log):
        log("released (demo): stock services started in order " + ", ".join(manifest.start))

    boot_hook_on = False

    def cable_boot(self, manifest, payload, address, action):
        if action != "status":
            self.boot_hook_on = action == "on"
        return self.boot_hook_on


class RealBackend:
    demo = False

    def __init__(self, runner_factory=None, remember_file=None, stack=None):
        self.names = []
        self.stack = stack
        self.password = None
        self.remember_file = remember_file
        self.runner_factory = runner_factory or (lambda addr: engine.SshRunner(addr, password=self.password))
        self.runners = {}

    def _runner(self, address):
        if address not in self.runners:
            self.runners[address] = self.runner_factory(address)
        return self.runners[address]

    def login(self, address, password):
        old = self.password
        self.password = password
        self.runners.pop(address, None)
        ok, detail = self.reachable(address)
        if not ok:
            self.password = old
            self.runners.pop(address, None)
        elif self.remember_file:
            fd = os.open(self.remember_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(password)
        return ok, detail

    def load_remembered(self):
        if self.remember_file and os.path.exists(self.remember_file):
            self.password = open(self.remember_file).read()

    def reachable(self, address):
        try:
            rc, out = self._runner(address).run("echo nebula-ok", timeout=20)
        except Exception as ex:
            return False, self.cable_hint(address, f"{address}: {ex}")
        if rc == 0 and "nebula-ok" in out:
            return True, f"{address} answers and the password works"
        return False, self.cable_hint(address, f"{address}: {out.strip()[:120] or 'no answer'}")

    @staticmethod
    def cable_hint(address, detail):
        if address == "10.78.0.100" and ("No route" in detail or "timed out" in detail or "no answer" in detail):
            return detail + ". If the cable was only just connected, restart the printer once: it sets up its end of the cable only when it starts."
        return detail

    def facts(self, address):
        return engine.facts(self._runner(address))

    def config_files(self, address):
        return calibration.read_files(self._runner(address))

    def apply(self, manifest, payload, address, log):
        engine.apply(self._runner(address), manifest, payload, log=log)
        if self.stack:
            try:
                self.stack.start(log)
            except engine.NebulaError:
                self.stack.stop(log)
                engine.release(self._runner(address), manifest)
                raise

    def cable_boot(self, manifest, payload, address, action):
        return engine.cable_boot(self._runner(address), manifest, payload, action)

    def release(self, manifest, address, log):
        if self.stack:
            self.stack.stop(log)
        log(engine.release(self._runner(address), manifest).strip())
