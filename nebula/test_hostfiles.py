import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED = ("host/config", "host/services", "nebula/host", "nebula/addons")


class HostFilesTest(unittest.TestCase):
    def test_no_file_we_ship_names_the_old_dev_host_user(self):
        bad = []
        for top in SHIPPED:
            for root, _, names in os.walk(os.path.join(REPO, top)):
                if "__pycache__" in root or "live-snapshots" in root:
                    continue
                for n in names:
                    if n == "centauri-link-pty.service":
                        continue
                    p = os.path.join(root, n)
                    try:
                        text = open(p, encoding="utf-8").read()
                    except (UnicodeDecodeError, OSError):
                        continue
                    if "/home/klipper" in text:
                        bad.append(os.path.relpath(p, REPO))
        self.assertEqual(bad, [])

    def test_mcu_serial_paths_use_the_home_placeholder(self):
        text = open(os.path.join(REPO, "host/config/machine/mcu.cfg")).read()
        serials = [l for l in text.splitlines() if l.startswith("serial:")]
        self.assertEqual(len(serials), 3)
        self.assertTrue(all("@HOME@/printer_data/comms/" in l for l in serials))


if __name__ == "__main__":
    unittest.main()
