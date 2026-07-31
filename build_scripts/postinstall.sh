#!/bin/bash

# Link CLI plugin
PLUGIN_DIR="/etc/opt/srlinux/cli/plugins"
if [ -d "$PLUGIN_DIR" ]; then
    ln -sf /opt/vlan-groups/cli-plugins/vlan_groups_show.py "$PLUGIN_DIR/vlan_groups_show.py"
    echo "Symlinked CLI plugin to $PLUGIN_DIR"
fi
