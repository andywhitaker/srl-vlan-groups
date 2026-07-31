# SR Linux VLAN Groups Application

The VLAN Groups application for Nokia SR Linux is a custom NDK agent that simplifies the management of large sets of VLANs. It automates the boilerplate configuration required to deploy VLANs across multiple interfaces by introducing the concept of a **VLAN Group**.

Instead of manually creating a subinterface and associating it with a MAC-VRF on every single interface, you can define a VLAN group once, and simply apply that group to any number of interfaces. The agent will automatically generate the required subinterfaces and MAC-VRF network-instance bindings in the background.

## Features
- **Centralized VLAN Management**: Define groups of VLAN tags in a single location.
- **Automatic Defaults**: When configuring tags, the `subinterface-id` and `mac-vrf` names are automatically derived (e.g., VLAN tag `10` creates subinterface `10` and binds to network-instance `vlan-10`).
- **Untagged Support**: Easily manage native/untagged traffic for a group, with automatic defaults (subinterface `1`, MAC-VRF `vlan-1`).
- **Per-Interface Overrides**: Need to prune a specific VLAN from a trunk? You can disable specific tags on a per-interface basis without modifying the core VLAN group.
- **Custom CLI Views**: Includes a custom `show vlan-groups` CLI plugin for clear, table-based operational views.
- **Safe MAC-VRF Handling**: The app automatically binds interfaces to MAC-VRFs. However, it will *never* automatically delete a MAC-VRF when a group is removed, preventing accidental deletion of manually shared network-instances.

## Configuration Guide

### 1. Defining a VLAN Group
You define a VLAN group under the `/vlan-groups` hierarchy. 

```text
--{ candidate shared default }--[  ]--
# set / vlan-groups group trunk-to-servers
# set / vlan-groups group trunk-to-servers untagged
# set / vlan-groups group trunk-to-servers tag 10
# set / vlan-groups group trunk-to-servers tag 20
```

By default, the above configuration will:
- Map `untagged` to subinterface `1` and MAC-VRF `vlan-1`.
- Map `tag 10` to subinterface `10` and MAC-VRF `vlan-10`.
- Map `tag 20` to subinterface `20` and MAC-VRF `vlan-20`.

You can override these defaults if desired:
```text
# set / vlan-groups group trunk-to-servers tag 10 subinterface-id 100 mac-vrf my-custom-vrf
```

### 2. Applying a VLAN Group to an Interface
To enable the VLAN group on an interface, simply reference the group name. The application will automatically set the interface to `vlan-tagging true` and generate the subinterfaces.

```text
--{ candidate shared default }--[  ]--
# set / interface ethernet-1/1 vlan-groups vlan-group trunk-to-servers
```

### 3. Per-Interface Overrides
If you want to apply a group to an interface but disable a specific VLAN (pruning), you can use a per-interface override:

```text
--{ candidate shared default }--[  ]--
# set / interface ethernet-1/1 vlan-groups per-interface-override vlan-id 20 enabled false
```

## Operational Commands

The application provides native SR Linux `show` commands to view the state of your VLAN groups and interfaces. Tab-completion is fully supported for group and interface names.

* `show vlan-groups` - Displays all configured VLAN groups and their tag/MAC-VRF mappings.
* `show vlan-groups group <name>` - Displays information for a specific VLAN group.
* `show vlan-groups interface` - Displays all interfaces managed by VLAN groups and their active subinterfaces.
* `show vlan-groups interface <name>` - Displays VLAN group mappings for a specific interface.

## Building and Packaging

This project uses [nfpm](https://nfpm.goreleaser.com/) to package the Python application and YANG models into a deployable Debian package (`.deb`).

1. Ensure you have Docker installed (used to run `nfpm` via the `Makefile`).
2. Run `make package` from the root of the repository.

This will generate a `vlan-groups.deb` file in the project root.

## Testing locally with Containerlab

A test topology is provided in the `lab/` directory to quickly spin up a test environment using [Containerlab](https://containerlab.dev/).

1. **Deploy the lab:**
   ```bash
   sudo clab deploy -t lab/srl-vlan-groups.clab.yml
   ```

2. **Install the package:**
   ```bash
   docker cp vlan-groups.deb clab-srl-vlan-groups-srl:/tmp/
   docker exec clab-srl-vlan-groups-srl apt-get install -y /tmp/vlan-groups.deb
   ```

3. **Log in to SR Linux:**
   ```bash
   ssh admin@clab-srl-vlan-groups-srl
   # password: NokiaSrl1!
   ```

4. **Reload the Application Manager:**
   Because we installed a new YANG schema and application, we need to instruct the Application Manager to parse the new configuration from within the CLI:
   ```text
   --{ + running }--[  ]--
   A:srl# tools system app-management application app_mgr reload
   ```
