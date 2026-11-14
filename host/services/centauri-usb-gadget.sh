#!/bin/sh
G=/sys/kernel/config/usb_gadget/centauri
UDC=$(ls /sys/class/udc 2>/dev/null | head -n1)
case "$1" in
start)
    modprobe libcomposite 2>/dev/null
    mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
    [ -n "$UDC" ] || { echo "no UDC"; exit 1; }
    [ -d $G ] && { echo "already created"; exit 0; }
    mkdir -p $G && cd $G || exit 1
    echo 0x1d6b > idVendor; echo 0x0104 > idProduct
    echo 0x0200 > bcdUSB
    mkdir -p strings/0x409
    echo centauri-bridge > strings/0x409/manufacturer
    echo "Centauri host link" > strings/0x409/product
    echo "$(hostname)" > strings/0x409/serialnumber
    mkdir -p configs/c.1/strings/0x409 functions/ecm.usb0
    echo "ECM" > configs/c.1/strings/0x409/configuration
    echo 100 > configs/c.1/MaxPower
    echo ${HOST_MAC:-00:c3:00:00:00:01} > functions/ecm.usb0/host_addr
    echo 02:c3:00:00:00:02 > functions/ecm.usb0/dev_addr
    ln -s functions/ecm.usb0 configs/c.1/
    echo "$UDC" > UDC
    ;;
stop)
    [ -d $G ] || exit 0
    echo "" > $G/UDC
    rm -f $G/configs/c.1/ecm.usb0
    rmdir $G/configs/c.1/strings/0x409 $G/configs/c.1 $G/functions/ecm.usb0 $G/strings/0x409 $G
    ;;
status)
    echo "udc: $UDC state: $(cat /sys/class/udc/$UDC/state 2>&1)"
    ip -br link show usb0 2>&1
    ;;
esac
