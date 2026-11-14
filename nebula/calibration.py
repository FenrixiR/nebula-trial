import fnmatch
import math
import posixpath
import re
from dataclasses import dataclass, field

FACTORY_TARE = 71000
FACTORY_PID_KP = 24.860427
SAVE_MARK = "#*# <---------------------- SAVE_CONFIG ---------------------->"

CONFIG_DIR = "/etc/klipper/config"


def _parse_text(text, sections, order):
    cur = None
    last = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith(("#", ";")):
            continue
        if raw[0] in " \t" and cur is not None and last is not None:
            sections[cur][last] += "\n" + line.strip()
            continue
        m = re.match(r"\[(.+?)\]\s*$", line)
        if m:
            cur = " ".join(m.group(1).split())
            sections.setdefault(cur, {})
            order.append(cur)
            last = None
            continue
        m = re.match(r"([^:=\s][^:=]*?)\s*[:=]\s*(.*)$", line)
        if m and cur is not None:
            last = m.group(1).strip().lower()
            sections[cur][last] = m.group(2).split(" #")[0].strip()


def parse_config(files, entry="printer.cfg"):
    sections, order = {}, []

    def load(path):
        text = files.get(path)
        if text is None:
            return
        body, _, saved = text.partition(SAVE_MARK)
        chunk = []
        for raw in body.splitlines():
            m = re.match(r"\[include\s+(.+?)\]\s*$", raw.strip())
            if m:
                _parse_text("\n".join(chunk), sections, order)
                chunk = []
                pat = posixpath.normpath(posixpath.join(posixpath.dirname(path), m.group(1)))
                for name in sorted(files):
                    if fnmatch.fnmatch(name, pat):
                        load(name)
            else:
                chunk.append(raw)
        _parse_text("\n".join(chunk), sections, order)
        if saved:
            saved_lines = [re.sub(r"^#\*# ?", "", l) for l in saved.splitlines() if l.startswith("#*#")]
            _parse_text("\n".join(saved_lines), sections, order)

    load(entry)
    return sections


@dataclass
class Part:
    name: str
    ok: bool
    detail: str
    values: dict = field(default_factory=dict)


def _floats(text):
    try:
        v = [float(x) for x in re.split(r"[,\s]+", text.strip()) if x]
    except ValueError:
        return None
    return v if v and all(math.isfinite(x) for x in v) else None


def check(cfg):
    parts = []
    sh = cfg.get("input_shaper", {})
    fx, fy = _floats(sh.get("shaper_freq_x", "0")), _floats(sh.get("shaper_freq_y", "0"))
    ok = bool(fx and fy and fx[0] > 0 and fy[0] > 0 and fx[0] < 500 and fy[0] < 500)
    parts.append(Part("input shaper", ok, f"X {sh.get('shaper_freq_x', '0')} Hz, Y {sh.get('shaper_freq_y', '0')} Hz",
                      {"shaper_freq_x": sh.get("shaper_freq_x"), "shaper_freq_y": sh.get("shaper_freq_y"),
                       "shaper_type_x": sh.get("shaper_type_x"), "shaper_type_y": sh.get("shaper_type_y")}))
    meshes = sorted(k for k in cfg if k.startswith("bed_mesh "))
    ok = "bed_mesh default" in cfg and _floats(cfg["bed_mesh default"].get("points", "").replace("\n", " ")) is not None
    parts.append(Part("bed mesh", ok, "profile 'default' present" if ok else
                      ("profiles: " + ", ".join(m[9:] for m in meshes) + " (none named 'default')" if meshes else "no profile"),
                      {"profiles": [m[9:] for m in meshes]}))
    lc = cfg.get("load_cell_probe", {})
    tare = _floats(lc.get("reference_tare_counts", str(FACTORY_TARE)))
    ok = bool(tare) and sum(tare) != FACTORY_TARE * len(tare)
    parts.append(Part("load cell", ok, "reference tare counts " + (", ".join(f"{x:g}" for x in tare) if tare else "unreadable")
                      + ("" if ok else " (factory placeholder)"), {"reference_tare_counts": lc.get("reference_tare_counts")}))
    ex = cfg.get("extruder", {})
    kp = _floats(ex.get("pid_kp", "0"))
    control = ex.get("control", "")
    ok = (control != "pid" or (kp is not None and abs(kp[0] - FACTORY_PID_KP) > 1e-6)) and (control != "pid" or kp[0] > 0)
    parts.append(Part("extruder PID", ok, f"control {control or 'unset'}, pid_kp {ex.get('pid_kp', '?')}" + ("" if ok else " (factory value)"),
                      {k: ex.get(k) for k in ("control", "pid_kp", "pid_ki", "pid_kd")}))
    return parts


FRIENDLY = {"input shaper": "vibration tuning", "bed mesh": "bed flatness map", "load cell": "touch sensor", "extruder PID": "nozzle heater tuning"}


@dataclass
class Verdict:
    parts: list
    use_existing_allowed: bool
    missing: list
    message: str


def verdict(files):
    cfg = parse_config(files)
    parts = check(cfg)
    missing = [p.name for p in parts if not p.ok]
    if missing:
        msg = "Your printer needs a full tuning. Not yet tuned: " + ", ".join(FRIENDLY.get(n, n) for n in missing) + "."
    else:
        msg = "All four parts are already tuned. You can keep the saved tuning or tune again (tuning again is recommended)."
    return Verdict(parts, not missing, missing, msg)


def read_files(runner):
    cmd = (f"cd {CONFIG_DIR} && for f in printer.cfg klipper-readonly/*.cfg; do "
           "echo \"@@@FILE $f\"; cat \"$f\"; done")
    rc, out = runner.run(cmd, timeout=60)
    if rc != 0 and "@@@FILE" not in out:
        raise RuntimeError("cannot read the printer config: " + out[:200])
    files, cur = {}, None
    for line in out.splitlines():
        if line.startswith("@@@FILE "):
            cur = line[8:]
            files[cur] = []
        elif cur:
            files[cur].append(line)
    return {k: "\n".join(v) for k, v in files.items()}
