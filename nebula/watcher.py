import contextlib
import time
from dataclasses import dataclass, field

from .hijack import engine


@dataclass
class Watcher:
    manifest: object
    payload: str
    runner_factory: object
    address: str
    log: object = print
    base_delay: float = 10.0
    max_delay: float = 300.0
    sleep: object = time.sleep
    stack: object = None
    lock: object = None
    on_start: object = None
    delay: float = field(default=10.0, init=False)

    def __post_init__(self):
        self.delay = self.base_delay

    def tick(self):
        r = self.runner_factory(self.address)
        try:
            rc, out = r.run("echo nebula-ok", timeout=10)
        except Exception as ex:
            self.log(f"watcher: cannot reach the printer ({ex})")
            return "unreachable"
        if rc != 0 or "nebula-ok" not in out:
            return "unreachable"
        with self.lock or contextlib.nullcontext():
            return self._look_and_hijack(r)

    def _revive_stack(self):
        if not self.stack or self.stack.running():
            return "already"
        self.log("watcher: the printer is already taken over but Kalico is not running on this device, starting it")
        try:
            self.stack.start(self.log)
        except engine.NebulaError as ex:
            self.log(f"watcher: starting Kalico failed: {ex}")
            self.stack.stop(self.log)
            return "failed"
        return "hijacked"

    def _look_and_hijack(self, r):
        f = engine.facts(r)
        if f["mode"] == "hijacked":
            return self._revive_stack()
        m = engine.pick(self.manifest, f["issue"])
        checks = engine.evaluate(m, f)
        bad = [c for c in checks if not c.ok]
        if bad:
            self.log("watcher: not taking over: " + "; ".join(f"{c.name} ({c.detail})" for c in bad))
            return "busy"
        if self.on_start:
            self.on_start()
        try:
            engine.apply(r, m, self.payload, log=self.log)
        except engine.NebulaError as ex:
            self.log(f"watcher: take-over failed: {ex}")
            return "failed"
        if self.stack:
            try:
                self.stack.start(self.log)
            except engine.NebulaError as ex:
                self.log(f"watcher: starting Kalico failed: {ex}")
                self.stack.stop(self.log)
                engine.release(r, m)
                return "failed"
        self.log("watcher: took over the printer")
        return "hijacked"

    def run(self, stop=lambda: False, max_ticks=None, on_result=None):
        n = 0
        while not stop() and (max_ticks is None or n < max_ticks):
            n += 1
            try:
                result = self.tick()
            except Exception as ex:
                self.log(f"watcher: unexpected error, will look again: {ex!r}")
                result = "failed"
            if on_result:
                on_result(result)
            self.delay = self.base_delay if result in ("already", "hijacked", "unreachable") else min(self.delay * 2, self.max_delay)
            self.sleep(self.delay)
