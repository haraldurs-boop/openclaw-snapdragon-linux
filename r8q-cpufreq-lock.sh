#!/bin/sh
set -eu

lock_policy() {
    p="/sys/devices/system/cpu/cpufreq/$1"
    target="$2"
    [ -d "$p" ] || return 0
    echo performance > "$p/scaling_governor"
    cat "$p/cpuinfo_max_freq" > "$p/scaling_max_freq"
    cat "$p/cpuinfo_min_freq" > "$p/scaling_min_freq"
    echo "$target" > "$p/scaling_max_freq"
    echo "$target" > "$p/scaling_min_freq"
}

lock_policy policy0 1171200
lock_policy policy4 1478400
lock_policy policy7 1632000
