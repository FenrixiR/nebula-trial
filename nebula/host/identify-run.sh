#!/bin/bash
cd ~
~/bin/centauri-link pty mainboard=10.78.0.100:7001@$HOME/printer_data/comms/mcu-mainboard bed=10.78.0.100:7002@$HOME/printer_data/comms/mcu-bed toolhead=10.78.0.100:7003@$HOME/printer_data/comms/mcu-toolhead > ~/printer_data/logs/relay.log 2>&1 &
RP=$!
sleep 3
~/klippy-env/bin/python ~/kalico/klippy/klippy.py ~/printer_data/config/identify.cfg -l ~/printer_data/logs/klippy.log -a /tmp/klippy_uds > ~/printer_data/logs/klippy.out 2>&1 &
KP=$!
sleep ${1:-40}
kill $KP; sleep 2; kill $RP
