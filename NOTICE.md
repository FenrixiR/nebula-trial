# Notice

Nebula is free software under the GNU General Public License, version 3 (see `LICENSE`). Copyright (c) 2026 FenrixiR.

Nebula is an independent project. It is not made, endorsed or supported by Elegoo, OpenCentauri, or the authors of the projects below.

## Material taken from other projects

| Path | Origin | Licence |
|---|---|---|
| `host/config/machine/` | OpenCentauri Cosmos printer configuration (James Turton and contributors); some files modified for use on a separate host | GPL-3.0 |
| `host/config/machine/client.cfg` | Mainsail client macros, Alex Zellner | GPL-3.0 |
| `host/pack/plugins/centauri_hx711s.py` | OpenCentauri `hx711s.py` (James Turton, Gareth Farrington); a registration hook and an alias were added | GPL-3.0 |

## Downloaded at install time, not included here

The installer fetches these from their own repositories at fixed versions and does not modify them: Kalico (and Klipper), Moonraker, and, when you choose them, Mainsail, Fluidd, Crowsnest and HelixScreen. Each keeps its own licence.

OpenCentauri Cosmos runs on the printer and is not modified on disk; Nebula adds files beside it and removes them again when control is handed back.
