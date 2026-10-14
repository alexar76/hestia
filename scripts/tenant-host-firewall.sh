#!/bin/sh
# Close the one path a per-tenant Internal bridge leaves open: tenant -> engine host.
#
# Docker's Internal networks filter FORWARD, not INPUT. The bridge's gateway
# address belongs to the engine host, so without this a tenant can open a
# connection to every host process listening on a wildcard address (and to a
# Docker API on tcp://0.0.0.0, which is an engine takeover).
#
# Hestia names every tenant bridge `hst` + 12 hex characters, so one rule on
# `-i hst+` covers them all. New connections FROM a tenant bridge to the host are
# dropped. Replies to connections the host opened (the edge proxying to the
# tenant) are ESTABLISHED and still pass.
#
# Run as root on the engine host, before Hestia starts tenants, and again after
# every reboot (for example from a systemd unit ordered before hestia).
#
#   tenant-host-firewall.sh apply    install or refresh the rules (idempotent)
#   tenant-host-firewall.sh check    exit 0 only if the rules are in place AND the
#                                    jump is the first INPUT rule (an earlier
#                                    ACCEPT would make the DROP dead)
#   tenant-host-firewall.sh remove   take them out again
set -eu

CHAIN="HESTIA-TENANT-INPUT"
IFACE="${HESTIA_TENANT_BRIDGE_MATCH:-hst+}"

families() {
    command -v iptables >/dev/null 2>&1 && echo iptables
    command -v ip6tables >/dev/null 2>&1 && echo ip6tables
    return 0
}

jump_is_first() {
    # `-S INPUT` prints the policy first, then rules in order.
    [ "$("$1" -w -S INPUT 2>/dev/null | sed -n '2p')" = "-A INPUT -i $IFACE -j $CHAIN" ]
}

check_family() {
    t="$1"
    jump_is_first "$t" \
        && [ "$("$t" -w -S "$CHAIN" 2>/dev/null)" = "$(printf '%s\n' \
            "-N $CHAIN" \
            "-A $CHAIN -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN" \
            "-A $CHAIN -j DROP")" ]
}

apply_family() {
    t="$1"
    if check_family "$t"; then
        return 0
    fi
    # Build the new chain beside the live one and switch over, so there is never
    # a moment without the DROP (a connection opened then would stay ESTABLISHED).
    new="${CHAIN}-NEW"
    # Left over from an interrupted apply: start the new chain from scratch.
    while "$t" -w -C INPUT -i "$IFACE" -j "$new" 2>/dev/null; do
        "$t" -w -D INPUT -i "$IFACE" -j "$new"
    done
    "$t" -w -N "$new" 2>/dev/null || "$t" -w -F "$new"
    "$t" -w -A "$new" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    "$t" -w -A "$new" -j DROP
    "$t" -w -I INPUT 1 -i "$IFACE" -j "$new"
    while "$t" -w -C INPUT -i "$IFACE" -j "$CHAIN" 2>/dev/null; do
        "$t" -w -D INPUT -i "$IFACE" -j "$CHAIN"
    done
    "$t" -w -F "$CHAIN" 2>/dev/null || true
    "$t" -w -X "$CHAIN" 2>/dev/null || true
    "$t" -w -E "$new" "$CHAIN"  # the jump follows the rename
}

remove_family() {
    t="$1"
    for chain in "$CHAIN" "${CHAIN}-NEW"; do
        while "$t" -w -C INPUT -i "$IFACE" -j "$chain" 2>/dev/null; do
            "$t" -w -D INPUT -i "$IFACE" -j "$chain"
        done
        "$t" -w -F "$chain" 2>/dev/null || true
        "$t" -w -X "$chain" 2>/dev/null || true
    done
}

found=""
for t in $(families); do
    found="yes"
    case "${1:-}" in
        apply) apply_family "$t" ;;
        check)
            if ! check_family "$t"; then
                echo "tenant host firewall is NOT in place ($t)" >&2
                exit 1
            fi
            ;;
        remove) remove_family "$t" ;;
        *)
            echo "usage: $0 apply|check|remove" >&2
            exit 2
            ;;
    esac
done
if [ -z "$found" ]; then
    echo "neither iptables nor ip6tables is installed" >&2
    exit 1
fi
if ! command -v ip6tables >/dev/null 2>&1; then
    # Tenant networks have IPv6 off, so this is defence in depth only.
    echo "note: ip6tables not found; only IPv4 is covered" >&2
fi
[ "${1:-}" = "check" ] && echo "tenant host firewall is in place"
exit 0
