import json
import os
import shutil
import tempfile
import unittest

from nebula import uninstall
from nebula.hijack import engine as e

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFESTS = e.load_manifests(os.path.join(REPO, "nebula", "manifests"))


class Stack:
    def __init__(self, running):
        self.up, self.calls = running, []

    def any_running(self):
        return self.up

    def stop(self, log):
        self.calls.append("stop")
        self.up = False


class Runner:
    def __init__(self, mode="stock", left="", reachable=True):
        self.mode, self.left, self.reachable, self.cmds = mode, left, reachable, []

    def run(self, cmd, timeout=120):
        self.cmds.append(cmd)
        if not self.reachable:
            raise OSError("no route")
        if "/etc/issue" in cmd or "issue" in cmd:
            return 0, ""
        return 0, self.left if cmd.startswith("ls -d") else ""


class UninstallTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.log = []
        self.facts, self.refresh, self.release = e.facts, e.refresh_controller, e.release
        self.addCleanup(setattr, e, "facts", self.facts)
        self.addCleanup(setattr, e, "refresh_controller", self.refresh)
        self.addCleanup(setattr, e, "release", self.release)
        self.events = []
        e.refresh_controller = lambda r, m, payload: self.events.append("controller pushed")

    def state(self, address="10.78.0.100", password="pw"):
        with open(self.d + "/settings.json", "w") as f:
            json.dump({"path": "B", "address": address}, f)
        if password:
            with open(self.d + "/printer-password", "w") as f:
                f.write(password)

    def go(self, runner, stack, mode="stock"):
        e.facts = lambda r: {"issue": "Cosmos 26.10.1", "mode": mode}
        return uninstall.run(self.d, MANIFESTS, "payload", lambda addr, pw: runner, stack, log=self.log.append)

    def test_a_host_that_never_met_a_printer_only_stops_its_own_stack(self):
        stack = Stack(True)
        self.assertEqual(self.go(Runner(), stack), 0)
        self.assertEqual(stack.calls, ["stop"])

    def test_a_hijacked_printer_has_kalico_stopped_before_the_printer_is_purged(self):
        self.state()
        r, stack = Runner(), Stack(True)
        order = []
        e.refresh_controller = lambda r, m, payload: order.append("controller pushed")
        stack.stop = lambda log: order.append("stack stop")
        orig = r.run
        r.run = lambda cmd, timeout=120: (order.append("printer: " + cmd[:200]), orig(cmd))[1]
        self.assertEqual(self.go(r, stack, mode="hijacked"), 0)
        self.assertEqual(order[:2], ["controller pushed", "stack stop"])
        self.assertTrue(any("purge" in o for o in order))
        self.assertIn("nothing of Nebula is left", self.log[-1])

    def test_a_stock_printer_is_purged_without_touching_a_stack_that_is_not_running(self):
        self.state()
        stack = Stack(False)
        self.assertEqual(self.go(Runner(), stack), 0)
        self.assertEqual(stack.calls, [])

    def test_an_unreachable_printer_is_reported_and_the_host_stack_still_stopped(self):
        self.state()
        stack = Stack(True)
        e.facts = self.facts
        code = uninstall.run(self.d, MANIFESTS, "payload", lambda a, p: Runner(reachable=False), stack, log=self.log.append)
        self.assertEqual(code, uninstall.UNREACHABLE)
        self.assertEqual(stack.calls, ["stop"])
        self.assertIn("cannot be reached", self.log[-1])

    def test_when_the_purge_fails_the_printer_is_still_given_back_to_cosmos(self):
        self.state()
        released = []
        e.release = lambda r, m: released.append(1)

        class Failing(Runner):
            def run(self, cmd, timeout=120):
                if "purge" in cmd:
                    return 1, "boom"
                return super().run(cmd, timeout)

        with self.assertRaisesRegex(e.NebulaError, "removing Nebula from the printer failed"):
            self.go(Failing(), Stack(True), mode="hijacked")
        self.assertEqual(released, [1])

    def test_the_ssh_connection_is_closed_so_nothing_keeps_the_user_alive(self):
        self.state()
        r = Runner()
        closed = []
        r.close = lambda: closed.append(1)
        self.go(r, Stack(False))
        self.assertEqual(closed, [1])
        with self.assertRaises(e.NebulaError):
            self.go(Runner(left="x"), Stack(False))

    def test_leftovers_on_the_printer_are_an_error(self):
        self.state()
        with self.assertRaisesRegex(e.NebulaError, "still on the printer"):
            self.go(Runner(left="/user-resource/nebula\n"), Stack(False))


if __name__ == "__main__":
    unittest.main()
