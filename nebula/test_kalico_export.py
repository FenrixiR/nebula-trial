import unittest

from . import calibration as c
from . import kalico_export as k
from .test_calibration import CALIBRATED, stock_files


def calibrated_files():
    f = stock_files(CALIBRATED)
    return f


class ExportTest(unittest.TestCase):
    def test_refuses_uncalibrated(self):
        with self.assertRaises(ValueError) as cm:
            k.export(stock_files())
        self.assertIn("input shaper", str(cm.exception))

    def test_round_trip_keeps_every_calibration_value(self):
        text = k.export(calibrated_files())
        back = c.parse_config({"printer.cfg": text})
        orig = c.parse_config(calibrated_files())
        for sec in ("input_shaper", "bed_mesh default", "load_cell_probe"):
            self.assertEqual(back[sec], {o: orig[sec][o] for o in back[sec]})
        self.assertEqual(back["bed_mesh default"], orig["bed_mesh default"])
        self.assertEqual(back["extruder"]["pid_kp"], "21.337")
        self.assertTrue(c.verdict({"printer.cfg": text}).use_existing_allowed)

    def test_only_calibration_sections_are_exported(self):
        text = k.export(calibrated_files())
        names = [l for l in text.splitlines() if l.startswith("[")]
        self.assertEqual(names, ["[input_shaper]", "[bed_mesh default]", "[load_cell_probe]", "[extruder]"])
        self.assertNotIn("mesh_min", text)

    def test_multiline_points_are_indented(self):
        text = k.export(calibrated_files())
        self.assertIn("points:", text)
        self.assertIn("\n    0.01, 0.02\n    0.00, -0.01\n", text)


class ZOffsetTest(unittest.TestCase):
    def test_saved_z_endstop_is_exported_with_the_rest(self):
        files = calibrated_files()
        files["printer.cfg"] += "#*#\n#*# [stepper_z]\n#*# position_endstop = 5.625\n"
        text = k.export(files)
        self.assertIn("[stepper_z]\nposition_endstop: 5.625", text)
        self.assertEqual(c.parse_config({"printer.cfg": text})["stepper_z"]["position_endstop"], "5.625")


if __name__ == "__main__":
    unittest.main()
