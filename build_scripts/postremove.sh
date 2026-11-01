#!/bin/bash
export PATH="/usr/local/bin:/opt/srlinux/bin:$PATH"
action="${1:-remove}"

if [ "$action" = "remove" ] || [ "$action" = "purge" ]; then
    PLUGIN_LINK="/etc/opt/srlinux/cli/plugins/vlan_groups_show.py"
    if [ -L "$PLUGIN_LINK" ] || [ -f "$PLUGIN_LINK" ]; then
        rm -f "$PLUGIN_LINK"
    fi
    if command -v sr_cli >/dev/null 2>&1; then
        for _ in 1 2 3 4 5; do
            sr_cli "tools system app-management application app_mgr reload" >/dev/null 2>&1 && break
            sleep 1
        done
    fi
fi
exit 0
