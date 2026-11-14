import json
import os
import shutil
import subprocess
import tempfile
import unittest

from nebula import updates


class Done:
    def __init__(self, out="", err="", rc=0):
        self.stdout, self.stderr, self.returncode = out, err, rc


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.version, self.status = self.d + "/VERSION", self.d + "/update.json"

    def make(self, run=None, popen=None):
        return updates.Updater(self.version, command="/x/nebula-update", status_file=self.status,
                               run=run or (lambda *a, **k: Done()), popen=popen or (lambda *a, **k: None))

    def test_version_comes_from_the_installed_file_and_is_unknown_without_it(self):
        u = self.make()
        self.assertEqual(u.version(), "unknown")
        with open(self.version, "w") as f:
            f.write("0.2.2\n")
        self.assertEqual(u.version(), "0.2.2")

    def test_check_asks_the_installed_command_and_returns_its_answer(self):
        seen = []
        info = {"installed": "1", "available": "2", "newer": True, "notes": []}

        def run(argv, **kw):
            seen.append(argv)
            return Done(json.dumps(info))
        self.assertEqual(self.make(run).check(), info)
        self.assertEqual(seen, [["/x/nebula-update", "--check"]])

    def test_a_failed_check_says_why_in_plain_words(self):
        u = self.make(lambda *a, **k: Done(err="nebula-install: the signature on the download does not match: nothing was installed\n", rc=1))
        with self.assertRaisesRegex(updates.UpdateError, "signature on the download does not match"):
            u.check()
        u = self.make(lambda *a, **k: Done("not json"))
        with self.assertRaisesRegex(updates.UpdateError, "could not be read"):
            u.check()

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("x", 60)
        with self.assertRaisesRegex(updates.UpdateError, "could not look for updates"):
            self.make(boom).check()

    def test_start_runs_only_the_one_allowed_command_detached(self):
        seen = {}

        def popen(argv, **kw):
            seen.update(argv=argv, **kw)
        self.make(popen=popen).start()
        self.assertEqual(seen["argv"], ["sudo", "-n", "/x/nebula-update", "--update", "--yes"])
        self.assertTrue(seen["start_new_session"])

    def test_status_is_what_the_update_wrote_or_nothing(self):
        u = self.make()
        self.assertIsNone(u.status())
        with open(self.status, "w") as f:
            json.dump({"state": "done", "message": "updated"}, f)
        self.assertEqual(u.status()["state"], "done")
        with open(self.status, "w") as f:
            f.write("{broken")
        self.assertIsNone(u.status())


if __name__ == "__main__":
    unittest.main()
