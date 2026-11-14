from . import calibration

HEADER = (
    "# Imported from the printer by Nebula. Calibration only. Regenerate, do not edit by hand.\n"
    "# Include from printer.cfg:  [include nebula/calibration.cfg]\n"
)

INPUT_SHAPER_KEYS = ("shaper_type_x", "shaper_freq_x", "shaper_type_y", "shaper_freq_y")
EXTRUDER_PID_KEYS = ("control", "pid_kp", "pid_ki", "pid_kd")


def _emit(section, options):
    lines = [f"[{section}]"]
    for key, value in options.items():
        if "\n" in value:
            first, *rest = value.split("\n")
            lines.append(f"{key}: {first}")
            lines += [f"    {r}" for r in rest]
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


def export(files):
    v = calibration.verdict(files)
    if not v.use_existing_allowed:
        raise ValueError("cannot export, not calibrated: " + ", ".join(v.missing))
    cfg = calibration.parse_config(files)
    out = [HEADER]
    out.append(_emit("input_shaper", {k: cfg["input_shaper"][k] for k in INPUT_SHAPER_KEYS if k in cfg["input_shaper"]}))
    out.append(_emit("bed_mesh default", cfg["bed_mesh default"]))
    out.append(_emit("load_cell_probe", {"reference_tare_counts": cfg["load_cell_probe"]["reference_tare_counts"]}))
    ex = cfg["extruder"]
    out.append(_emit("extruder", {k: ex[k] for k in EXTRUDER_PID_KEYS if k in ex}))
    z = cfg.get("stepper_z", {}).get("position_endstop")
    if z is not None:
        out.append(_emit("stepper_z", {"position_endstop": z}))
    return "\n".join(out)
