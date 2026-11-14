import os
import unittest

from . import calibration as c

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REAL_STOCK = os.path.join(REPO, "recon", "cosmos-config", "config")


STOCK_DEFAULTS = """[extruder]
control: pid
pid_Kp: 24.860427
pid_Ki: 1.0
pid_Kd: 100.0

[input_shaper]
shaper_type_x: mzv
shaper_type_y: mzv

[bed_mesh]
mesh_min: 10, 10
mesh_max: 240, 240

[load_cell_probe]
reference_tare_counts: 71000, 71000, 71000, 71000
"""


def stock_files(printer_cfg="[include klipper-readonly/*.cfg]\n"):
    return {"klipper-readonly/machine.cfg": STOCK_DEFAULTS, "printer.cfg": printer_cfg}


CALIBRATED = """[include klipper-readonly/*.cfg]

#*# <---------------------- SAVE_CONFIG ---------------------->
#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.
#*#
#*# [input_shaper]
#*# shaper_type_x = mzv
#*# shaper_freq_x = 61.4
#*# shaper_type_y = mzv
#*# shaper_freq_y = 43.2
#*#
#*# [bed_mesh default]
#*# version = 1
#*# points =
#*# 	0.01, 0.02
#*# 	0.00, -0.01
#*# x_count = 2
#*# y_count = 2
#*# min_x = 10.0
#*# max_x = 240.0
#*# min_y = 10.0
#*# max_y = 240.0
#*#
#*# [load_cell_probe]
#*# reference_tare_counts = 70211, 70388, 70102, 70455
#*#
#*# [extruder]
#*# control = pid
#*# pid_kp = 21.337
#*# pid_ki = 1.5
#*# pid_kd = 90.1
"""


class CalibrationTest(unittest.TestCase):
    def test_stock_printer_has_nothing_to_import(self):
        v = c.verdict(stock_files())
        self.assertFalse(v.use_existing_allowed)
        self.assertEqual(v.missing, ["input shaper", "bed mesh", "load cell", "extruder PID"])

    @unittest.skipUnless(os.path.isdir(REAL_STOCK), "recon copy of the stock config not present")
    def test_real_stock_config_has_nothing_to_import(self):
        files = {}
        base = os.path.join(REAL_STOCK, "klipper-readonly")
        for n in os.listdir(base):
            files["klipper-readonly/" + n] = open(os.path.join(base, n)).read()
        files["printer.cfg"] = open(os.path.join(REAL_STOCK, "printer.cfg")).read()
        self.assertFalse(c.verdict(files).use_existing_allowed)

    def test_fully_calibrated(self):
        v = c.verdict(stock_files(CALIBRATED))
        self.assertTrue(v.use_existing_allowed, [(p.name, p.detail) for p in v.parts])
        self.assertIn("tuning again is recommended", v.message)

    def test_one_missing_part_blocks_use_existing(self):
        for drop in ("[input_shaper]", "[bed_mesh default]", "[load_cell_probe]", "[extruder]"):
            lines = CALIBRATED.splitlines()
            i = lines.index("#*# " + drop)
            j = i + 1
            while j < len(lines) and lines[j] != "#*#":
                j += 1
            text = "\n".join(lines[:i] + lines[j:])
            v = c.verdict(stock_files(text))
            self.assertFalse(v.use_existing_allowed, drop)
            self.assertEqual(len(v.missing), 1, (drop, v.missing))

    def test_bad_values_count_as_missing(self):
        v = c.verdict(stock_files(CALIBRATED.replace("shaper_freq_x = 61.4", "shaper_freq_x = nan")))
        self.assertIn("input shaper", v.missing)
        v = c.verdict(stock_files(CALIBRATED.replace("shaper_freq_y = 43.2", "shaper_freq_y = 9999")))
        self.assertIn("input shaper", v.missing)

    def test_factory_pid_and_tare_are_not_calibration(self):
        t = CALIBRATED.replace("pid_kp = 21.337", "pid_kp = 24.860427")
        self.assertIn("extruder PID", c.verdict(stock_files(t)).missing)
        t = CALIBRATED.replace("70211, 70388, 70102, 70455", "71000, 71000, 71000, 71000")
        self.assertIn("load cell", c.verdict(stock_files(t)).missing)

    def test_option_names_are_case_insensitive(self):
        v = c.verdict(stock_files(CALIBRATED))
        self.assertEqual(c.parse_config(stock_files(CALIBRATED))["extruder"]["pid_kp"], "21.337")
        self.assertTrue(v.use_existing_allowed)
        self.assertIn("pid_kp 24.860427", c.verdict(stock_files()).parts[3].detail)

    def test_other_mesh_profile_is_not_default(self):
        v = c.verdict(stock_files(CALIBRATED.replace("[bed_mesh default]", "[bed_mesh pla]")))
        self.assertIn("bed mesh", v.missing)

    def test_bypass_calibration_is_ignored(self):
        files = stock_files()
        files["cosmos.conf"] = "bypass_calibration = True\n"
        self.assertFalse(c.verdict(files).use_existing_allowed)

    def test_read_files_through_a_runner(self):
        class R:
            def run(self, cmd, timeout=0):
                return 0, "@@@FILE printer.cfg\n[include klipper-readonly/*.cfg]\n@@@FILE klipper-readonly/a.cfg\n[extruder]\ncontrol: pid\n"
        f = c.read_files(R())
        self.assertEqual(sorted(f), ["klipper-readonly/a.cfg", "printer.cfg"])
        self.assertEqual(c.parse_config(f)["extruder"]["control"], "pid")


if __name__ == "__main__":
    unittest.main()
