import os
import tempfile
import unittest

from . import autosave


class AutosaveTest(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.cfg = os.path.join(self.d.name, "printer.cfg")

    def write(self, text):
        with open(self.cfg, "w") as f:
            f.write(text)

    def read(self):
        with open(self.cfg) as f:
            return f.read()

    def test_the_shipped_defaults_are_a_factory_block(self):
        with open(autosave.DEFAULTS) as f:
            text = f.read()
        self.assertIn(autosave.MARK, text)
        self.assertIn(autosave.FACTORY, text)
        self.assertIn("position_endstop", text)

    def test_a_config_without_a_block_gets_the_factory_values_once(self):
        self.write("[include machine/*.cfg]\n")
        self.assertTrue(autosave.ensure_defaults(self.cfg))
        self.assertIn("position_endstop", self.read())
        self.assertTrue(self.read().startswith("[include machine/*.cfg]\n"))
        self.assertFalse(autosave.ensure_defaults(self.cfg))
        self.assertEqual(self.read().count(autosave.MARK), 1)

    def test_real_tuning_is_never_touched(self):
        real = "[include machine/*.cfg]\n\n#*# <---------------------- SAVE_CONFIG ---------------------->\n#*# [stepper_z]\n#*# position_endstop = 5.669\n"
        self.write(real)
        self.assertFalse(autosave.ensure_defaults(self.cfg))
        self.assertFalse(autosave.drop_defaults(self.cfg))
        self.assertEqual(self.read(), real)

    def test_the_factory_block_is_removed_again_for_imported_tuning(self):
        self.write("[include machine/*.cfg]\n")
        autosave.ensure_defaults(self.cfg)
        self.assertTrue(autosave.drop_defaults(self.cfg))
        self.assertNotIn(autosave.MARK, self.read())
        self.assertNotIn("position_endstop", self.read())
        self.assertTrue(self.read().startswith("[include machine/*.cfg]\n"))
        self.assertFalse(autosave.drop_defaults(self.cfg))

    def test_a_missing_file_is_left_alone(self):
        self.assertFalse(autosave.ensure_defaults(self.cfg))
        self.assertFalse(autosave.drop_defaults(self.cfg))
        self.assertFalse(os.path.exists(self.cfg))
