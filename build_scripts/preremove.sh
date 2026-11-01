#!/bin/bash
export PATH="/usr/local/bin:/opt/srlinux/bin:$PATH"
action="${1:-remove}"

if [ "$action" = "remove" ] || [ "$action" = "purge" ] || [ "$action" = "upgrade" ]; then
    if command -v sr_cli >/dev/null 2>&1; then
        sr_cli "tools system app-management application vlan-groups stop" >/dev/null 2>&1 || true
    fi
fi
exit 0
