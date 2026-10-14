#!/bin/bash
# Wait for an OBDLink in pairing mode (button pressed), pair + trust it, bind it to /dev/rfcomm0.
set -u
deadline=$((SECONDS + ${1:-900}))
bluetoothctl pairable on >/dev/null
mac=""
while [ -z "$mac" ] && [ $SECONDS -lt $deadline ]; do
  bluetoothctl --timeout 10 scan on >/dev/null 2>&1
  mac=$(bluetoothctl devices | awk '/OBDLink/ {print $2; exit}')
done
[ -n "$mac" ] || { echo "$(date +%T) no OBDLink seen"; exit 1; }
echo "$(date +%T) found $(bluetoothctl devices | grep "$mac")"
bluetoothctl pair "$mac"
bluetoothctl trust "$mac"
channel=$(sdptool search --bdaddr "$mac" SP 2>/dev/null | awk '/Channel:/ {print $2; exit}')
echo "SPP channel: ${channel:-unknown, using 1}"
sudo rfcomm release 0 2>/dev/null
sudo rfcomm bind 0 "$mac" "${channel:-1}"
ls -l /dev/rfcomm0 && echo "$(date +%T) READY"
