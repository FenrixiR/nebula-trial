import os
import unittest

from nebula.hijack import engine

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "manifests")


class ManifestSetTest(unittest.TestCase):
    def setUp(self):
        self.set = engine.load_manifests(DIR)

    def test_every_shipped_version_is_loaded_oldest_first(self):
        versions = [m.version for m in self.set.all]
        self.assertIn("26.09.0", versions)
        self.assertIn("26.10.1", versions)
        self.assertEqual(versions, sorted(versions, key=lambda v: tuple(int(x) for x in v.split("."))))

    def test_the_printers_version_picks_its_manifest(self):
        self.assertEqual(self.set.pick("OpenCentauri Cosmos 26.09.0 \\n \\l").version, "26.09.0")
        self.assertEqual(self.set.pick("OpenCentauri Cosmos 26.10.1 \\n \\l").version, "26.10.1")

    def test_an_unknown_version_gets_the_newest_so_the_checks_explain(self):
        m = self.set.pick("OpenCentauri Cosmos 27.01.0 \\n \\l")
        self.assertEqual(m.version, self.set.all[-1].version)
        checks = engine.evaluate(m, {"issue": "OpenCentauri Cosmos 27.01.0", "bed_ver": "x\nx", "toolhead_ver": "x\nx",
                                     "mode": "", "space_kb": "0 0 0 99999999 0", "moonraker": ""})
        self.assertFalse(checks[0].ok)

    def test_a_single_manifest_is_passed_through(self):
        one = self.set.all[0]
        self.assertIs(engine.pick(one, "anything"), one)


if __name__ == "__main__":
    unittest.main()
