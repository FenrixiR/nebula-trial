import os
import re
import unittest

HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "host")
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "host", "config", "machine")


class PolkitRuleTest(unittest.TestCase):
    def setUp(self):
        self.text = open(os.path.join(HOST, "50-nebula-moonraker.rules")).read()

    def test_only_the_moonraker_user_is_handled(self):
        self.assertIn('subject.user != "nebula"', self.text)
        self.assertIn("NOT_HANDLED", self.text)

    def test_reboot_and_shutdown_are_allowed(self):
        for a in ("login1.reboot", "login1.reboot-multiple-sessions", "login1.power-off", "login1.power-off-multiple-sessions"):
            self.assertIn(f'"org.freedesktop.{a}"', self.text)

    def test_services_are_limited_to_nebulas_own_and_nginx(self):
        units = re.search(r"var units = \[(.*?)\];", self.text, re.S).group(1)
        self.assertEqual(sorted(re.findall(r'"([^"]+)"', units)),
                         sorted(["nebula-relay-client.service", "nebula-kalico.service", "nebula-moonraker.service",
                                 "nebula-panel.service", "nebula-crowsnest.service", "nginx.service"]))

    def test_the_internet_block_cannot_be_stopped_from_a_web_page(self):
        self.assertNotIn("nebula-block", re.search(r"var units = \[(.*?)\];", self.text, re.S).group(1))

    def test_nothing_else_is_granted(self):
        self.assertNotIn("packagekit", self.text)
        self.assertNotIn("halt", self.text)
        self.assertEqual(len(re.findall(r"polkit\.Result\.YES", self.text)), 2)


class SdcardPathTest(unittest.TestCase):
    def test_the_gcode_folder_follows_whichever_user_runs_the_host(self):
        for name in os.listdir(CONFIG):
            if not name.endswith(".cfg"):
                continue
            for m in re.finditer(r"\[virtual_sdcard\]\s*\npath:\s*(\S+)", open(os.path.join(CONFIG, name)).read()):
                self.assertEqual(m.group(1), "~/printer_data/gcodes", name)
        for name in ("hardware.cfg", "client.cfg"):
            self.assertRegex(open(os.path.join(CONFIG, name)).read(), r"\[virtual_sdcard\]\s*\npath: ~/printer_data/gcodes", name)


if __name__ == "__main__":
    unittest.main()
