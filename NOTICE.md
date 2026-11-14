# Notice

## Copyright and licence

Copyright (c) 2026 Fenleigh 'FenrixiЯ' Åkerholm.

Nebula is free software: you can redistribute it and modify it under the terms of the GNU General Public License, version 3, as published by the Free Software Foundation. The full text is in `LICENSE`. Nebula is distributed in the hope that it will be useful, but without any warranty; see sections 15 and 16 of the licence.

This notice covers everything in this repository except the third-party material listed below, which keeps its own copyright holders and notices. Where a file in this repository carries its own copyright header, that header applies to the file.

Nebula is an independent project. It is not made, endorsed or supported by Elegoo, OpenCentauri, or the authors of the projects named below. The names Elegoo, Centauri Carbon, OpenCentauri, Cosmos, Kalico, Klipper, Moonraker, Mainsail, Fluidd, Crowsnest, HelixScreen, Raspberry Pi, Debian, Ubuntu and Armbian belong to their owners and are used only to say what Nebula works with.

## Source code and your rights

This repository is the complete source of Nebula. The packages that the installer downloads are built from it with `packaging/build-release.sh`; the only thing in them that is not in this repository as source is the relay program, compiled from `centauri-link/`. You may use, study, change and share it under the GNU GPL version 3. If you share a changed version you must keep the copyright notices, mark your changes with their date, and offer it under the same licence (see sections 4 to 6 of the licence).

## Third-party files included here, and what was changed

Nebula contains parts of OpenCentauri Cosmos (copyright James Turton and contributors, GPL-3.0) and of Mainsail (copyright Alex Zellner, GPL-3.0). Changes were made in 2026 (October and November) and are listed here, as the licence requires.

| Path | Origin | What was changed |
|---|---|---|
| `host/config/machine/calibration.cfg`, `_vars.cfg` | Cosmos printer configuration | Nothing. |
| `host/config/machine/client.cfg` | Mainsail client macros (Alex Zellner), as shipped by Cosmos | The G-code folder path points at the host's folder. |
| `host/config/machine/kamp.cfg` | Cosmos | One purge distance value changed. |
| `host/config/machine/macros.cfg` | Cosmos | Nozzle cleaning, priming and standby temperature handling changed. The `CLEAN_NOZZLE` macro moved to `host/config/macros/nozzle.cfg`. |
| `host/config/machine/hardware.cfg`, `mcu.cfg`, `probe.cfg` | Cosmos `machine.cfg`, split into three files | Printer MCU sections moved to `mcu.cfg` and reached through the relay; the load-cell probe moved to `probe.cfg`; the virtual SD card path and the mainboard fan speed adapted for a separate host. |
| `host/config/machine/shell.cfg` | Cosmos | Rewritten: the commands Cosmos runs on the printer itself stay defined but do nothing on the host. |
| `host/config/macros/`, `host/config/machine/geometry.cfg` | Written for Nebula, derived from Cosmos's nozzle-cleaning and kick macros | Nozzle stations, kick strokes and coordinates split out into their own files. They are GPL-3.0 like their source. |
| `host/pack/plugins/centauri_hx711s.py` | OpenCentauri/Kalico `hx711s.py` (James Turton; original HX711 driver by Gareth Farrington) | Absolute imports; a `register_components()` hook that registers the sensor types without touching Kalico's files; an alias so Kalico's tap analysis finds `motion_report.trapqs`. |

The files above carry the header they came with. Where a file also has a short description of its changes at the top, that description is part of this notice.

## Downloaded at install time, not included here

The installer fetches these from their own repositories at fixed versions and checks them against a recorded hash: Kalico (and the Klipper code it is based on), Moonraker, HelixScreen, and, when you choose them, Mainsail, Fluidd and Crowsnest. Each keeps its own licence and copyright. Nebula does not change their code. It adds its own configuration files for them, and links one plugin file (`centauri_hx711s.py`, above) into Kalico's plugin folder.

## What Nebula changes on the printer

OpenCentauri Cosmos is not modified. Nebula never rewrites or removes a Cosmos file and never flashes the printer's firmware. While the printer is taken over, Nebula stops three of Cosmos's services (its own screen interface, Moonraker and Klipper) and starts them again when control is handed back. Everything Nebula puts on the printer lives in `/user-resource/nebula`, with one exception: if you switch on "keep the cable up after a restart", it adds a small start script, `/etc/init.d/nebula-cable`, with two start links in `/etc/rc3.d` and `/etc/rc5.d`. Handing the printer back, or removing Nebula, deletes all of it.
