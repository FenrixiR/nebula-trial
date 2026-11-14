#!/bin/sh
case "$1" in
    bound|renew) ip addr replace "$ip/${mask:-24}" dev "$interface" ;;
esac
exit 0
