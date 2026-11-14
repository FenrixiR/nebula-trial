import json
import os
import unittest

from .hijack import engine
from .hijack.test_engine import CALM, RELAY_OK, Sandbox, free_port, make_manifest, make_payload
from .watcher import Watcher
import shutil


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.payload = make_payload(RELAY_OK)
        self.m, _ = make_manifest(self.payload, [free_port() for _ in range(3)])
        self.logs = []
        self.sleeps = []
        self.w = Watcher(self.m, self.payload, lambda addr: self.sb, "10.78.0.100", log=self.logs.append,
                         sleep=self.sleeps.append)

    def tearDown(self):
        self.sb.cleanup()
        shutil.rmtree(self.payload, ignore_errors=True)

    def test_hijacks_an_idle_stock_printer_once(self):
        self.assertEqual(self.w.tick(), "hijacked")
        self.assertEqual(self.w.tick(), "already")
        self.assertEqual(self.sb.log().count("klipper stop"), 1)

    def test_a_closed_gate_keeps_the_watcher_from_touching_the_printer_and_says_why_once(self):
        self.w.gate = lambda: "not yet"
        self.assertEqual(self.w.tick(), "busy")
        self.assertEqual(self.w.tick(), "busy")
        self.assertEqual(self.logs.count("watcher: waiting: not yet"), 1)
        self.assertNotIn("klipper stop", self.sb.log())
        self.w.gate = lambda: None
        self.assertEqual(self.w.tick(), "hijacked")

    def test_the_printer_config_is_prepared_before_kalico_starts(self):
        order = []

        class Stack:
            def start(self, log):
                order.append("start")

            def stop(self, log):
                pass
        self.w.stack = Stack()
        self.w.prepare = lambda: order.append("prepare")
        self.assertEqual(self.w.tick(), "hijacked")
        self.assertEqual(order, ["prepare", "start"])

    def test_host_stack_is_started_after_a_hijack_and_a_failure_releases(self):
        class Stack:
            def __init__(self, fail=False):
                self.fail, self.calls = fail, []

            def start(self, log):
                self.calls.append("start")
                if self.fail:
                    raise engine.NebulaError("kalico not ready")

            def stop(self, log):
                self.calls.append("stop")
        ok = Stack()
        self.w.stack = ok
        self.assertEqual(self.w.tick(), "hijacked")
        self.assertEqual(ok.calls, ["start"])
        e2 = Watcher(self.m, self.payload, lambda addr: self.sb, "10.78.0.100", log=self.logs.append,
                     sleep=self.sleeps.append, stack=Stack(fail=True))
        engine.release(self.sb, self.m)
        self.assertEqual(e2.tick(), "failed")
        self.assertEqual(e2.stack.calls, ["start", "stop"])
        self.assertFalse(os.path.exists(self.sb.d + "/run/nebula-mode"))

    def test_a_rebooted_host_gets_its_stack_back_on_a_printer_that_is_still_hijacked(self):
        class Stack:
            def __init__(self):
                self.up, self.calls = False, []

            def running(self):
                return self.up

            def start(self, log):
                self.calls.append("start")
                self.up = True

            def stop(self, log):
                self.calls.append("stop")
        st = Stack()
        self.w.stack = st
        self.assertEqual(self.w.tick(), "hijacked")
        st.up = False
        self.assertEqual(self.w.tick(), "hijacked")
        self.assertEqual(st.calls, ["start", "start"])
        self.assertEqual(self.w.tick(), "already")
        self.assertEqual(st.calls, ["start", "start"])
        self.assertEqual(self.sb.log().count("klipper stop"), 1)

    def test_a_printer_that_is_still_booting_is_checked_at_the_base_rate(self):
        class Dead:
            def run(self, cmd, timeout=0):
                raise OSError("no route")
        w = Watcher(self.m, self.payload, lambda a: Dead(), "x", log=self.logs.append, sleep=self.sleeps.append)
        w.run(max_ticks=4)
        self.assertEqual(self.sleeps, [10.0, 10.0, 10.0, 10.0])

    def test_on_start_is_called_only_when_a_hijack_really_starts(self):
        started = []
        self.w.on_start = lambda: started.append(1)
        self.sb.moonraker = {"result": {"status": {"print_stats": {"state": "printing"}, "extruder": {"target": 200.0}, "heater_bed": {"target": 60.0}}}}
        self.assertEqual(self.w.tick(), "busy")
        self.assertEqual(started, [])
        self.sb.moonraker = CALM
        self.assertEqual(self.w.tick(), "hijacked")
        self.assertEqual(started, [1])

    def test_never_hijacks_a_busy_printer(self):
        self.sb.moonraker = {"result": {"status": {"print_stats": {"state": "printing"}, "extruder": {"target": 200.0}, "heater_bed": {"target": 60.0}}}}
        self.assertEqual(self.w.tick(), "busy")
        self.assertEqual(self.sb.log(), [])

    def test_unreachable_printer(self):
        class Dead:
            def run(self, cmd, timeout=0):
                raise OSError("no route")
        w = Watcher(self.m, self.payload, lambda a: Dead(), "x", log=self.logs.append, sleep=self.sleeps.append)
        self.assertEqual(w.tick(), "unreachable")

    def test_an_unexpected_error_does_not_end_the_watching(self):
        calls = []

        class Flaky:
            def run(self, cmd, timeout=0):
                calls.append(cmd)
                if len(calls) == 2:
                    raise ValueError("odd answer")
                return 0, "nebula-ok"
        w = Watcher(self.m, self.payload, lambda a: Flaky(), "x", log=self.logs.append, sleep=self.sleeps.append)
        w.run(max_ticks=2)
        self.assertTrue(any("unexpected error" in l for l in self.logs))
        self.assertGreaterEqual(len(calls), 2)

    def test_backoff_doubles_then_resets(self):
        self.sb.moonraker = None
        self.w.run(max_ticks=4)
        self.assertEqual(self.sleeps, [20.0, 40.0, 80.0, 160.0])
        self.sb.moonraker = CALM
        self.sleeps.clear()
        self.w.run(max_ticks=2)
        self.assertEqual(self.sleeps, [10.0, 10.0])

    def test_failed_hijack_is_reported_and_released(self):
        open(self.payload + "/relay/centauri-link", "w").write("#!/bin/sh\nexit 3\n")
        engine.pin_manifest(self.payload + "/manifest.toml", self.payload)
        self.w.manifest = engine.load_manifest(self.payload + "/manifest.toml")
        self.assertEqual(self.w.tick(), "failed")
        self.assertEqual(self.sb.log()[-3:], ["klipper start", "moonraker start", "gui-switcher start"])


if __name__ == "__main__":
    unittest.main()
