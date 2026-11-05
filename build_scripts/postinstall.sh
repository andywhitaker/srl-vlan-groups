#!/bin/bash
set -e
export PATH="/usr/local/bin:/opt/srlinux/bin:$PATH"

# Link CLI plugin
PLUGIN_DIR="/etc/opt/srlinux/cli/plugins"
if [ -d "$PLUGIN_DIR" ]; then
    ln -sf /opt/vlan-groups/cli-plugins/vlan_groups_show.py "$PLUGIN_DIR/vlan_groups_show.py"
    echo "Symlinked CLI plugin to $PLUGIN_DIR"
fi

# Reload application manager if running on SR Linux so YANG schema and app are loaded
if command -v sr_cli >/dev/null 2>&1; then
    for _ in 1 2 3 4 5; do
        sr_cli "tools system app-management application app_mgr reload" >/dev/null 2>&1 && break
        sleep 1
    done
    for _ in 1 2 3 4 5; do
        sr_cli "tools system app-management application vlan-groups start" >/dev/null 2>&1 || true
        if sr_cli -s -- "info system app-management application vlan-groups state" 2>/dev/null | grep -q "running"; then
            break
        fi
        sleep 1
    done
fi
