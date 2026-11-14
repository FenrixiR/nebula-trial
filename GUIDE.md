# Nebula guide

Nebula lets a small computer ("the host") run your Elegoo Centauri Carbon 1 with Kalico and Moonraker, over the printer's USB port, and pass the printer back to its stock software (OpenCentauri Cosmos) at any time. This guide takes you from printer running Cosmos with limited system resources to a printer running under Nebula without such restrictions, and back.

Nebula is an independent project. It changes how your printer is controlled. Read what the installer and the panel tell you before you confirm.

## 1. What you need

- A Centauri Carbon running OpenCentauri Cosmos (26.09 or 26.10 are supported). Check the version in the panel under Checks.
- A host computer:
  - a Raspberry Pi Zero 2 W (the recommended, tested choice), or another 64-bit ARM or x86-64 board or mini PC;
  - running Debian, Ubuntu, Raspberry Pi OS (64-bit) or Armbian, with systemd;
  - at least 350 MB of RAM and 1.5 GB of free disk;
  - a USB port that can act as a device (a "gadget" or OTG port). The Pi Zero 2 W has one: the port marked USB, not the one marked PWR. Many TV boxes have one too, but on some it needs a device-tree change that Nebula cannot do for you. The installer tells you if it finds none. 
- A USB cable from that port to the printer's front USB port. VBUS must be cut if host device is externally powered, to prevent potential backfeeding issues. A low-power board such as the Pi Zero 2 W can instead be powered from the printer's USB port with an uncut cable; larger boards draw too much for that.
- A network connection for the host while installing (the installer downloads Kalico, Moonraker and the screen software).

32-bit systems are not supported. 

## 2. Install

On the host, as a user that can run `sudo`:

    curl -fsSL https://github.com/FenrixiR/nebula-trial/releases/latest/download/install.sh | bash

The installer checks that your machine fits, lists what it is about to change, and asks you to confirm. It is safe to run again: it repairs and updates what is there, and it keeps your settings.

On a Raspberry Pi it switches on the USB device mode and asks for one reboot at the end. Do that reboot.

When it finishes it prints the address of the panel, for example `http://192.168.XXX.XXX:8080/nebula/`.

## 3. First run

1. Open the panel address in a browser on your home network. Choose a panel password (at least 8 characters).
2. **How is the printer connected?** Pick one:
   - *It is on my home network*: type the printer's address.
   - *Keep it off my network*: everything goes through the USB cable and the printer itself stays off Wi-Fi and the internet.

   Either way, plug the USB cable into the host and the printer, then **power cycle the printer**. The printer only sets up the cable when it starts. This is needed the first time you connect.
3. Enter the printer's login password. Cosmos ships with a public one; if you changed it, use yours.
4. **Checks.** Nebula only looks: the Cosmos version, the firmware of the printer's boards, that nothing is printing, and the heaters are off. Fix anything marked with a cross and check again.
5. **Tuning.** The printer's saved tuning (vibration, bed level, heating) can be kept, or you can tune again.
   - *Keep the saved tuning* is available only when everything on the printer is already calibrated and nebula is able to use it. 
   - *Tune again* starts Kalico with factory values. Use the printer's calibration steps afterwards; the console command `CHECK_CALIBRATION` tells you what is left.
6. **Take over control from Cosmos.** This takes about a minute and the printer's screen goes dark, then comes back with the HelixScreen interface. The printer must be idle: nothing printing, heaters off.

## 4. Using it

- The panel shows what is happening in its Activity list and lets you change options.
- **Take over again automatically** (an option on the take-over page): if the connection drops or the printer restarts, the host takes over again as soon as it sees the printer.
- **Safety net.** If the host stops talking to the printer (power cut, crash, cable pulled), the printer returns to Cosmos by itself after a short time. Switching the printer off and on also returns it to Cosmos if Nebula host is absent.
- **Pass control back to Cosmos** gives the printer back on purpose. Nothing is lost: your Cosmos settings are untouched.
- **Add-ons** (panel, Add-ons tab): Mainsail or Fluidd as a web interface, cameras, and an internet block for the printer. The web interface option is open to your home network without a password and is not reachable from the internet.

## 5. Updating

The Updates card in the panel has **Check for updates** and **Update now**. The update refuses to run while a print is running, is checked against the maintainer's signature before anything is installed, and goes back to the previous version if the new one does not start. From a shell: `sudo nebula-update --check` and `sudo nebula-update --update`.

Updates do not interrupt a printer that is already taken over.

## 6. Removing Nebula

    sudo nebula-uninstall

This hands the printer back to Cosmos, removes the files Nebula put on the printer, and removes everything from the host (the `nebula` user included). Add `--keep-data` to keep your settings and configuration. Dependencies installed through `apt` are left in place.

## 7. Possible issues

- **The printer does not answer after the first connection.** Switch the printer off and on once with the cable connected, or use the printer's Wi-Fi address for the first take-over.
- **"No USB device port (UDC) was found."** The host cannot act as a USB device on that port. On a Pi, reboot after the install. On other boards you need an OTG-capable port and a device tree that enables it.
- **Kalico shows an error.** Open the panel's Activity list and the log: `journalctl -u nebula-kalico`. Most errors name the setting.
- **The panel does not open.** `systemctl status nebula-panel` on the host.
- **Printer stuck or screen dark.** Switch the printer off and on: it returns to Cosmos.

Questions and bug reports belong on the project's issue page. 
