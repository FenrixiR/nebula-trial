import json

NAMES = ["ready", "uncalibrated", "busy", "wrong-firmware"]

_V = "4c37a204e5c2ed31b979baba7fb4d32a587aa590-r11"
_CALM = {"result": {"status": {"print_stats": {"state": "standby"}, "extruder": {"target": 0.0}, "heater_bed": {"target": 0.0}}}}
_BUSY = {"result": {"status": {"print_stats": {"state": "printing"}, "extruder": {"target": 215.0}, "heater_bed": {"target": 60.0}}}}


def printer_facts(scenario):
    f = {"issue": "OpenCentauri Cosmos 26.09.0 \\n \\l", "bed_ver": f"{_V}\n{_V}", "toolhead_ver": f"{_V}\n{_V}", "mode": "",
         "space_kb": "/dev/mmcblk0p13 6200000 180000 5800000 3% /user-resource",
         "moonraker": json.dumps(_BUSY if scenario == "busy" else _CALM)}
    if scenario == "wrong-firmware":
        f["bed_ver"] = f"{_V}\nother-r9"
    return f


_DEFAULTS = """[extruder]
control: pid
pid_kp: 24.860427
[input_shaper]
shaper_type_x: mzv
[bed_mesh]
mesh_min: 10, 10
[load_cell_probe]
reference_tare_counts: 71000, 71000, 71000, 71000
"""

_SAVED = """[include klipper-readonly/*.cfg]

#*# <---------------------- SAVE_CONFIG ---------------------->
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


def printer_config(scenario):
    saved = _SAVED
    if scenario == "uncalibrated":
        saved = "[include klipper-readonly/*.cfg]\n"
    elif scenario == "busy":
        saved = _SAVED.replace("[bed_mesh default]", "[bed_mesh pla]")
    return {"klipper-readonly/machine.cfg": _DEFAULTS, "printer.cfg": saved}


def host_facts(demo):
    return {"name": "demo-host" if demo else "this host", "link": "USB cable 10.78.0.1",
            "doctor": "watching the USB state", "version": "26.09.0"}
