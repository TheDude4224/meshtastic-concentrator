#!/bin/bash
# Watchdog: restart concentratord if it stops receiving for >5 minutes
# Checks last 6 stats intervals (3 min); if all zero AND uptime > 5min, restart

UPTIME=$(systemctl show chirpstack-concentratord --property=ActiveEnterTimestampMonotonic --value)
NOW=$(cat /proc/uptime | awk '{print int($1*1000000)}')
AGE_US=$((NOW - UPTIME))
AGE_MIN=$((AGE_US / 60000000))

# Only watchdog after service has been up 5+ minutes
if [ "$AGE_MIN" -lt 5 ]; then
    exit 0
fi

# Check last 3 minutes of stats - any rx_received > 0?
RX=$(journalctl -u chirpstack-concentratord --no-pager --since "3 minutes ago" \
    | grep "rx_received: [^0]" | wc -l)

if [ "$RX" -eq 0 ]; then
    logger "concentratord-watchdog: no RX in 3min (uptime ${AGE_MIN}min), restarting"
    systemctl restart chirpstack-concentratord
fi
