import unittest

from .hijack.engine import NebulaError
from .host_stack import HostStack, UNITS


class Fake:
    def __init__(self, ready_after=0, fail_unit=None, active=()):
        self.calls, self.posts, self.n, self.ready_after, self.fail_unit = [], [], 0, ready_after, fail_unit
        self.error = False
        self.active = set(active)

    def run(self, argv):
        if argv[3] == "is-active":
            return (0, "active") if argv[4] in self.active else (3, "inactive")
        self.calls.append(argv[3:5])
        if argv[3] == "start":
            self.active.add(argv[4])
        elif argv[3] == "stop":
            self.active.discard(argv[4])
        if argv[3] == "start" and argv[4] == self.fail_unit:
            return 1, "boom"
        return 0, ""

    def http(self, method, url, timeout=5):
        if method == "POST":
            self.posts.append(url.rsplit("script=", 1)[1])
            return {}
        self.n += 1
        if self.n <= self.ready_after:
            raise OSError("refused")
        if self.error:
            return {"result": {"state": "error", "state_message": "pid_profile: 'pid_kp' has to be specified"}}
        return {"result": {"state": "ready"}}


def stack(f, **kw):
    return HostStack(run=f.run, http=f.http, sleep=lambda s: None, ready_timeout=10, **kw)


class HostStackTest(unittest.TestCase):
    def test_start_runs_units_in_order_and_waits_for_ready(self):
        f = Fake(ready_after=3)
        stack(f).start(log=lambda s: None)
        self.assertEqual([c[1] for c in f.calls], list(UNITS))
        self.assertGreater(f.n, 3)

    def test_services_left_over_from_an_earlier_session_are_restarted_not_adopted(self):
        f = Fake(active=("nebula-kalico", "nebula-moonraker"))
        stack(f).start(log=lambda s: None)
        verbs = [tuple(c) for c in f.calls]
        self.assertLess(verbs.index(("stop", "nebula-kalico")), verbs.index(("start", "nebula-kalico")))
        self.assertEqual([c[1] for c in f.calls if c[0] == "start"], list(UNITS))

    def test_nothing_is_stopped_when_nothing_was_running(self):
        f = Fake()
        stack(f).start(log=lambda s: None)
        self.assertFalse([c for c in f.calls if c[0] == "stop"])

    def test_start_fails_when_a_unit_will_not_start(self):
        f = Fake(fail_unit="nebula-kalico")
        with self.assertRaises(NebulaError):
            stack(f).start(log=lambda s: None)
        self.assertNotIn("nebula-moonraker", [c[1] for c in f.calls])

    def test_start_fails_when_kalico_never_gets_ready(self):
        with self.assertRaises(NebulaError):
            stack(Fake(ready_after=10 ** 6)).start(log=lambda s: None)

    def test_start_fails_fast_with_kalicos_own_message(self):
        f = Fake()
        f.error = True
        with self.assertRaises(NebulaError) as cm:
            stack(f).start(log=lambda s: None)
        self.assertIn("pid_kp", str(cm.exception))
        self.assertEqual(f.n, 1)

    def test_stop_turns_heaters_off_then_stops_in_reverse(self):
        f = Fake()
        stack(f).stop(log=lambda s: None)
        self.assertEqual(f.posts, ["M104%20S0", "M140%20S0"])
        self.assertEqual([c[1] for c in f.calls], list(reversed(UNITS)))

    def test_stop_survives_a_dead_moonraker(self):
        f = Fake()
        f.http = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        stack(f).stop(log=lambda s: None)
        self.assertEqual(len(f.calls), 3)


if __name__ == "__main__":
    unittest.main()
