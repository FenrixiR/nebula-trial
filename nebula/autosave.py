import os

MARK = "#*# <---------------------- SAVE_CONFIG"
FACTORY = "reference_tare_counts = 71000, 71000, 71000, 71000"
DEFAULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults", "autosave-defaults.txt")


def _read(path):
    with open(path) as f:
        return f.read()


def ensure_defaults(cfg_path, defaults_path=DEFAULTS):
    if not os.path.exists(cfg_path):
        return False
    text = _read(cfg_path)
    if MARK in text:
        return False
    with open(cfg_path, "a") as f:
        f.write(("" if text.endswith("\n") else "\n") + "\n" + _read(defaults_path))
    return True


def drop_defaults(cfg_path):
    if not os.path.exists(cfg_path):
        return False
    text = _read(cfg_path)
    i = text.find(MARK)
    if i < 0 or FACTORY not in text[i:]:
        return False
    with open(cfg_path, "w") as f:
        f.write(text[:i].rstrip("\n") + "\n")
    return True
