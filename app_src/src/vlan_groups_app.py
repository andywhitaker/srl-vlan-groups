#!/usr/bin/env python3
import json
import logging
import os
import re
import signal
import sys
import time

VENDOR_PATH = os.environ.get('VLAN_GROUPS_VENDOR_PATH', '/opt/vlan-groups/src/vendor')
if VENDOR_PATH not in sys.path:
    sys.path.append(VENDOR_PATH)
_LOCAL_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor')
if os.path.isdir(_LOCAL_VENDOR) and _LOCAL_VENDOR not in sys.path:
    sys.path.append(_LOCAL_VENDOR)

try:
    import grpc
except ImportError:
    grpc = None

try:
    from ndk import sdk_service_pb2
    from ndk import sdk_service_pb2_grpc
    from ndk import config_service_pb2
    from ndk import config_service_pb2_grpc
    from ndk import telemetry_service_pb2
    from ndk import telemetry_service_pb2_grpc
except ImportError:
    class _RepeatedContainer(list):
        def __init__(self, factory):
            super().__init__()
            self._factory = factory

        def add(self):
            item = self._factory()
            self.append(item)
            return item

    class _TelemetryKey:
        def __init__(self):
            self.js_path = ""

    class _TelemetryData:
        def __init__(self):
            self.json_content = ""

    class _TelemetryInfo:
        def __init__(self):
            self.key = _TelemetryKey()
            self.data = _TelemetryData()

    class _TelemetryUpdateRequest:
        def __init__(self):
            self.states = _RepeatedContainer(_TelemetryInfo)

    class _TelemetryDeleteRequest:
        def __init__(self):
            self.keys = _RepeatedContainer(_TelemetryKey)

    class _AgentRegistrationRequest:
        def __init__(self, js_paths=None):
            self.js_paths = list(js_paths or [])

    class _NotificationRegisterRequest:
        OPERATION_CREATE = 0
        OPERATION_ADD_SUBSCRIPTION = 1

        def __init__(self, op=0, stream_id=0, config=None):
            self.op = op
            self.stream_id = stream_id
            self.config = config

    class _NotificationStreamRequest:
        def __init__(self, stream_id=0):
            self.stream_id = stream_id

    class _ConfigSubscriptionRequest:
        pass

    class _AcknowledgeConfigRequest:
        pass

    class sdk_service_pb2:
        AgentRegistrationRequest = _AgentRegistrationRequest
        NotificationRegisterRequest = _NotificationRegisterRequest
        NotificationStreamRequest = _NotificationStreamRequest

    class config_service_pb2:
        ConfigSubscriptionRequest = _ConfigSubscriptionRequest
        AcknowledgeConfigRequest = _AcknowledgeConfigRequest

    class telemetry_service_pb2:
        TelemetryUpdateRequest = _TelemetryUpdateRequest
        TelemetryDeleteRequest = _TelemetryDeleteRequest

    sdk_service_pb2_grpc = None
    config_service_pb2_grpc = None
    telemetry_service_pb2_grpc = None

try:
    from gnmi import gnmi_pb2
    from gnmi import gnmi_pb2_grpc
except ImportError:
    try:
        from pygnmi.spec.v080 import gnmi_pb2
        from pygnmi.spec.v080 import gnmi_pb2_grpc
    except ImportError:
        class _GnmiPathElem:
            def __init__(self, name="", key=None):
                self.name = name
                self.key = dict(key or {})

        class _GnmiElemList(list):
            def add(self):
                elem = _GnmiPathElem()
                self.append(elem)
                return elem

        class _GnmiPath:
            def __init__(self):
                self.elem = _GnmiElemList()

            def CopyFrom(self, other):
                self.elem = _GnmiElemList(
                    [_GnmiPathElem(e.name, dict(e.key)) for e in other.elem]
                )

        class _GnmiTypedValue:
            def __init__(self):
                self.json_ietf_val = b""

        class _GnmiUpdate:
            def __init__(self):
                self.path = _GnmiPath()
                self.val = _GnmiTypedValue()

        class _GnmiDeleteList(list):
            def add(self):
                p = _GnmiPath()
                self.append(p)
                return p

        class _GnmiUpdateList(list):
            def add(self):
                u = _GnmiUpdate()
                self.append(u)
                return u

        class _GnmiSetRequest:
            def __init__(self):
                self.delete = _GnmiDeleteList()
                self.update = _GnmiUpdateList()

        class gnmi_pb2:
            Path = _GnmiPath
            PathElem = _GnmiPathElem
            SetRequest = _GnmiSetRequest

        gnmi_pb2_grpc = None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('vlan-groups')


def _parse_gnmi_path(path_str):
    """Parse an XPath-style gNMI path string into a gnmi_pb2.Path protobuf object."""
    path = gnmi_pb2.Path()
    parts = re.findall(r'([^/\[]+(?:\[[^\]]+\])*)', path_str)
    for part in parts:
        elem = path.elem.add()
        if '[' in part and part.endswith(']'):
            name, rest = part.split('[', 1)
            elem.name = name
            for k, v in re.findall(r'\[([^\]=]+)=([^\]]+)\]', '[' + rest):
                elem.key[k] = v
        else:
            elem.name = part
    return path


class VlanGroupsApp:
    def __init__(self, connect=True):
        self.agent_name = 'vlan-groups'
        self.sdk_metadata = [('agent_name', self.agent_name)]
        self.sdk_socket = os.environ.get(
            'SRL_SDK_SOCKET',
            'unix:///opt/srlinux/var/run/sr_sdk_service_manager:50053'
        )
        self.gnmi_socket = os.environ.get(
            'SRL_GNMI_SOCKET',
            'unix:///opt/srlinux/var/run/sr_grpc_server_insecure-mgmt'
        )
        self.gnmi_user = os.environ.get('SRL_GNMI_USER', 'admin')
        self.gnmi_password = os.environ.get('SRL_GNMI_PASSWORD', 'NokiaSrl1!')

        self.channel = None
        self.sdk_stub = None
        self.telemetry_stub = None
        self.notification_stub = None
        self.config_stub = None
        self.gnmi_channel = None
        self.gnmi_stub = None

        self.agent_id = None
        self.vlan_groups = {}
        self.interfaces = {}
        self.deployed_subs = {}  # Format: {if_name: {vlan_group_name: {sub_id: mac_vrf}}}
        self.deployed_encaps = {}  # Format: {if_name: {sub_id: encap_kind}}
        self.active_telemetry_paths = set()
        self._running = True

        if connect:
            self._connect()

    def _connect(self):
        while self._running:
            channel = grpc.insecure_channel(self.sdk_socket)
            try:
                grpc.channel_ready_future(channel).result(timeout=5)
                self.channel = channel
                break
            except Exception as e:
                channel.close()
                logger.info(f"Waiting for SDK manager socket... Error: {e}")
                time.sleep(2)

        if not self.channel:
            return

        self.sdk_stub = sdk_service_pb2_grpc.SdkMgrServiceStub(self.channel)
        self.telemetry_stub = telemetry_service_pb2_grpc.SdkMgrTelemetryServiceStub(self.channel)
        self.notification_stub = sdk_service_pb2_grpc.SdkNotificationServiceStub(self.channel)
        self.config_stub = config_service_pb2_grpc.SdkMgrConfigServiceStub(self.channel)

        self.gnmi_channel = grpc.insecure_channel(self.gnmi_socket)
        self.gnmi_stub = gnmi_pb2_grpc.gNMIStub(self.gnmi_channel)

    def register_agent(self):
        req = sdk_service_pb2.AgentRegistrationRequest(
            js_paths=['.vlan_groups', '.interface.vlan_groups']
        )
        res = self.sdk_stub.AgentRegister(req, metadata=self.sdk_metadata)
        self.agent_id = res.app_id
        logger.info(f"Agent registered with ID: {res.app_id}")
        return res.app_id

    def unregister_agent(self):
        if self.sdk_stub is not None and self.agent_id is not None:
            try:
                req = sdk_service_pb2.AgentRegistrationRequest()
                self.sdk_stub.AgentUnRegister(req, metadata=self.sdk_metadata)
                logger.info(f"Agent {self.agent_id} unregistered cleanly")
            except Exception as e:
                logger.warning(f"Error unregistering agent: {e}")
            finally:
                self.agent_id = None

    def close(self):
        self._running = False
        self.unregister_agent()
        if self.gnmi_channel is not None:
            try:
                self.gnmi_channel.close()
            except Exception:
                pass
            self.gnmi_channel = None
        if self.channel is not None:
            try:
                self.channel.close()
            except Exception:
                pass
            self.channel = None

    def subscribe_config(self):
        # Create stream
        req = sdk_service_pb2.NotificationRegisterRequest(
            op=sdk_service_pb2.NotificationRegisterRequest.OPERATION_CREATE
        )
        res = self.sdk_stub.NotificationRegister(req, metadata=self.sdk_metadata)
        stream_id = res.stream_id

        # Add config subscription (inherits js_paths from AgentRegistrationRequest)
        req_sub = sdk_service_pb2.NotificationRegisterRequest(
            op=sdk_service_pb2.NotificationRegisterRequest.OPERATION_ADD_SUBSCRIPTION,
            stream_id=stream_id,
            config=config_service_pb2.ConfigSubscriptionRequest()
        )
        self.sdk_stub.NotificationRegister(req_sub, metadata=self.sdk_metadata)

        logger.info(f"Registered for config notifications, stream_id: {stream_id}")
        return stream_id

    def sync_telemetry(self, desired_telemetry):
        desired_paths = set(desired_telemetry.keys())
        paths_to_delete = self.active_telemetry_paths - desired_paths

        # Execute deletes BEFORE adds/updates so deleting a parent path never wipes out child updates
        delete_ok = True
        if paths_to_delete:
            del_req = telemetry_service_pb2.TelemetryDeleteRequest()
            for path in sorted(paths_to_delete):
                key = del_req.keys.add()
                key.js_path = path
                logger.info(f"Deleting telemetry path: {path}")
            try:
                self.telemetry_stub.TelemetryDelete(del_req, metadata=self.sdk_metadata)
            except Exception as e:
                logger.error(f"Error deleting telemetry: {e}")
                delete_ok = False

        update_ok = True
        if desired_telemetry:
            req = telemetry_service_pb2.TelemetryUpdateRequest()
            for path, data in desired_telemetry.items():
                info = req.states.add()
                info.key.js_path = path
                info.data.json_content = data
            try:
                self.telemetry_stub.TelemetryAddOrUpdate(req, metadata=self.sdk_metadata)
            except Exception as e:
                logger.error(f"Error updating telemetry: {e}")
                update_ok = False

        if delete_ok and update_ok:
            self.active_telemetry_paths = desired_paths
        elif delete_ok:
            self.active_telemetry_paths = self.active_telemetry_paths - paths_to_delete
        elif update_ok:
            self.active_telemetry_paths = self.active_telemetry_paths | desired_paths

    def process_notifications(self, stream_id):
        req = sdk_service_pb2.NotificationStreamRequest(stream_id=stream_id)
        logger.info(f"Starting to listen on NotificationStream for stream {stream_id}")
        try:
            for res in self.notification_stub.NotificationStream(req, metadata=self.sdk_metadata):
                if not self._running:
                    break
                logger.info("Raw notification stream response received")

                has_config = False
                if res.notifications:
                    logger.info(f"Received notification response with {len(res.notifications)} notifications")
                    for notif in res.notifications:
                        if notif.HasField('config'):
                            if self.handle_config_notification(notif.config):
                                has_config = True

                # Ack the config when we receive a stream response
                ack_req = config_service_pb2.AcknowledgeConfigRequest()
                self.config_stub.AcknowledgeConfig(ack_req, metadata=self.sdk_metadata)
                logger.info("Acknowledged config successfully!")

                if has_config:
                    self.reconcile()
        except Exception as e:
            if self._running:
                logger.error(f"NotificationStream loop crashed: {e}")
                time.sleep(2)
                raise
        logger.info("NotificationStream loop exited")

    def handle_config_notification(self, config_notif):
        path = config_notif.key.js_path
        if path == '.commit.end':
            return False

        keys = list(config_notif.key.keys)
        data = config_notif.data.json
        if data:
            try:
                parsed_data = json.loads(data)
                logger.info(f"Path: {path}, Keys: {keys}, Data: {parsed_data}")

                if path == '.vlan_groups':
                    vg_container = parsed_data.get('vlan_groups', parsed_data)
                    vg_list = vg_container.get('vlan_group', [])
                    if isinstance(vg_list, dict):
                        vg_list = [vg_list]
                    changed = False
                    for g in vg_list:
                        g_name = self._extract_scalar(g.get('name'))
                        if g_name:
                            self._update_group_from_dict(g_name, g.get('vlan_group', g))
                            changed = True
                    return changed

                elif path == '.vlan_groups.vlan_group' and len(keys) >= 1:
                    group_name = keys[0]
                    group_data = parsed_data.get('vlan_group', parsed_data)
                    self._update_group_from_dict(group_name, group_data)
                    return True

                elif path == '.vlan_groups.vlan_group.untagged' and len(keys) >= 1:
                    group_name = keys[0]
                    if group_name not in self.vlan_groups:
                        self.vlan_groups[group_name] = {'name': group_name}

                    untagged_data = parsed_data.get('untagged', parsed_data)
                    self.vlan_groups[group_name]['untagged'] = self._parse_untagged_data(untagged_data)
                    return True

                elif path == '.vlan_groups.vlan_group.tag' and len(keys) >= 2:
                    group_name = keys[0]
                    vlan_id = int(keys[1])
                    if group_name not in self.vlan_groups:
                        self.vlan_groups[group_name] = {'name': group_name}

                    if 'tag' not in self.vlan_groups[group_name]:
                        self.vlan_groups[group_name]['tag'] = []

                    tag_data = parsed_data.get('tag', parsed_data)
                    tag_entry = self._parse_tag_data(vlan_id, tag_data)
                    self._upsert_tag(group_name, tag_entry)
                    return True

                elif path == '.interface.vlan_groups' and len(keys) >= 1:
                    if_name = keys[0]
                    vgs_container = parsed_data.get('vlan_groups', parsed_data)
                    vg_list = vgs_container.get('vlan_group', [])
                    if isinstance(vg_list, dict):
                        vg_list = [vg_list]
                    changed = False
                    for vg_item in vg_list:
                        vg_name = self._extract_scalar(vg_item.get('name') if isinstance(vg_item, dict) else vg_item)
                        if vg_name:
                            self._add_interface_group(if_name, vg_name)
                            changed = True
                    return changed

                elif path == '.interface.vlan_groups.vlan_group' and len(keys) >= 2:
                    if_name = keys[0]
                    vlan_group_name = keys[1]
                    self._add_interface_group(if_name, vlan_group_name)
                    return True

            except (json.JSONDecodeError, ValueError, TypeError) as e:
                logger.error(f"Failed to parse config notification JSON ({e}): {data}")
                return False
        else:
            # Handling deletions
            logger.info(f"Delete Path: {path}, Keys: {keys}")
            if path == '.vlan_groups':
                self.vlan_groups.clear()
                return True
            elif path == '.vlan_groups.vlan_group' and len(keys) >= 1:
                group_name = keys[0]
                self.vlan_groups.pop(group_name, None)
                return True
            elif path == '.vlan_groups.vlan_group.untagged' and len(keys) >= 1:
                group_name = keys[0]
                if group_name in self.vlan_groups:
                    self.vlan_groups[group_name].pop('untagged', None)
                return True
            elif path == '.vlan_groups.vlan_group.tag' and len(keys) >= 2:
                group_name = keys[0]
                vlan_id = int(keys[1])
                if group_name in self.vlan_groups and 'tag' in self.vlan_groups[group_name]:
                    self.vlan_groups[group_name]['tag'] = [
                        t for t in self.vlan_groups[group_name]['tag'] if t.get('vlan-id') != vlan_id
                    ]
                return True
            elif path == '.interface.vlan_groups' and len(keys) >= 1:
                if_name = keys[0]
                self.interfaces.pop(if_name, None)
                return True
            elif path == '.interface.vlan_groups.vlan_group' and len(keys) >= 2:
                if_name = keys[0]
                vlan_group_name = keys[1]
                if if_name in self.interfaces and 'vlan-group' in self.interfaces[if_name]:
                    self.interfaces[if_name]['vlan-group'] = [
                        vg for vg in self.interfaces[if_name]['vlan-group']
                        if vg.get('name') != vlan_group_name
                    ]
                    if not self.interfaces[if_name]['vlan-group']:
                        del self.interfaces[if_name]
                return True
        return False

    def _update_group_from_dict(self, group_name, group_data):
        if group_name not in self.vlan_groups:
            self.vlan_groups[group_name] = {'name': group_name}
        if not isinstance(group_data, dict):
            self.vlan_groups[group_name].pop('untagged', None)
            return
        if 'untagged' in group_data:
            self.vlan_groups[group_name]['untagged'] = self._parse_untagged_data(group_data['untagged'])
        else:
            self.vlan_groups[group_name].pop('untagged', None)
        if 'tag' in group_data:
            if 'tag' not in self.vlan_groups[group_name]:
                self.vlan_groups[group_name]['tag'] = []
            tag_list = group_data['tag']
            if isinstance(tag_list, dict):
                tag_list = [tag_list]
            for t in tag_list:
                vid_raw = self._extract_scalar(t.get('vlan_id', t.get('vlan-id')))
                if vid_raw is not None:
                    t_inner = t.get('tag', t)
                    tag_entry = self._parse_tag_data(int(vid_raw), t_inner)
                    self._upsert_tag(group_name, tag_entry)

    def _upsert_tag(self, group_name, tag_entry):
        for i, t in enumerate(self.vlan_groups[group_name]['tag']):
            if t.get('vlan-id') == tag_entry['vlan-id']:
                self.vlan_groups[group_name]['tag'][i] = tag_entry
                return
        self.vlan_groups[group_name]['tag'].append(tag_entry)

    def _add_interface_group(self, if_name, vlan_group_name):
        if if_name not in self.interfaces:
            self.interfaces[if_name] = {'vlan-group': []}
        if 'vlan-group' not in self.interfaces[if_name]:
            self.interfaces[if_name]['vlan-group'] = []
        for vg in self.interfaces[if_name]['vlan-group']:
            if vg.get('name') == vlan_group_name:
                return
        self.interfaces[if_name]['vlan-group'].append({'name': vlan_group_name})

    @staticmethod
    def _extract_scalar(val):
        if isinstance(val, dict) and 'value' in val:
            return val['value']
        return val

    def _generate_gnmi_updates(self, if_name, group_cfg, claimed_sids=None, encaps_out=None):
        updates = []
        desired_subs = {}
        if claimed_sids is None:
            claimed_sids = set()

        base_if = if_name

        # Ensure interface has vlan-tagging enabled for subinterfaces
        updates.append({
            "path": f"/interface[name={base_if}]/vlan-tagging",
            "value": True
        })

        # Handle untagged subinterface
        if 'untagged' in group_cfg:
            sid = group_cfg['untagged'].get('subinterface-id', 1)
            mac_vrf = group_cfg['untagged'].get('mac-vrf', 'vlan-1')

            if sid in claimed_sids:
                logger.warning(
                    f"Subinterface index {sid} on {base_if} is already claimed; "
                    f"skipping conflicting untagged subinterface"
                )
            else:
                claimed_sids.add(sid)
                sub_path = f"/interface[name={base_if}]/subinterface[index={sid}]"
                updates.append({
                    "path": sub_path,
                    "value": {
                        "type": "bridged",
                        "vlan": {
                            "encap": {
                                "untagged": {}
                            }
                        }
                    }
                })

                updates.append({
                    "path": f"/network-instance[name={mac_vrf}]",
                    "value": {
                        "type": "mac-vrf"
                    }
                })
                ni_path = f"/network-instance[name={mac_vrf}]/interface[name={base_if}.{sid}]"
                updates.append({
                    "path": ni_path,
                    "value": {}
                })
                desired_subs[sid] = mac_vrf
                if encaps_out is not None:
                    encaps_out[sid] = "untagged"

        # Handle tagged subinterfaces
        for tag in sorted(group_cfg.get('tag', []), key=lambda t: t.get('vlan-id', 0)):
            vid = tag['vlan-id']
            sid = tag.get('subinterface-id', vid)
            mac_vrf = tag.get('mac-vrf', f'vlan-{vid}')

            if sid in claimed_sids:
                logger.warning(
                    f"Subinterface index {sid} on {base_if} is already claimed; "
                    f"skipping conflicting VLAN tag {vid}"
                )
                continue

            claimed_sids.add(sid)
            sub_path = f"/interface[name={base_if}]/subinterface[index={sid}]"
            updates.append({
                "path": sub_path,
                "value": {
                    "type": "bridged",
                    "vlan": {
                        "encap": {
                            "single-tagged": {
                                "vlan-id": vid
                            }
                        }
                    }
                }
            })

            updates.append({
                "path": f"/network-instance[name={mac_vrf}]",
                "value": {
                    "type": "mac-vrf"
                }
            })
            ni_path = f"/network-instance[name={mac_vrf}]/interface[name={base_if}.{sid}]"
            updates.append({
                "path": ni_path,
                "value": {}
            })
            desired_subs[sid] = mac_vrf
            if encaps_out is not None:
                encaps_out[sid] = ("single-tagged", vid)

        return updates, desired_subs

    @staticmethod
    def _deduplicate_updates(updates):
        seen = {}
        for u in updates:
            seen[u["path"]] = u
        return list(seen.values())

    @staticmethod
    def _deduplicate_paths(paths):
        seen = set()
        result = []
        for p in paths:
            if p not in seen:
                seen.add(p)
                result.append(p)
        return result

    def reconcile(self):
        updates = []
        deletes = []

        desired_subs = {}
        desired_encaps = {}
        group_interfaces = {}

        for if_name, if_cfg in self.interfaces.items():
            if 'vlan-group' in if_cfg:
                claimed_sids = set()
                if_encaps = {}
                for vg in if_cfg['vlan-group']:
                    g_name = vg['name']
                    if g_name in self.vlan_groups:
                        if g_name not in group_interfaces:
                            group_interfaces[g_name] = []
                        if if_name not in group_interfaces[g_name]:
                            group_interfaces[g_name].append(if_name)

                        upds, subs = self._generate_gnmi_updates(
                            if_name,
                            self.vlan_groups[g_name],
                            claimed_sids=claimed_sids,
                            encaps_out=if_encaps
                        )
                        updates.extend(upds)

                        if if_name not in desired_subs:
                            desired_subs[if_name] = {}
                        if g_name not in desired_subs[if_name]:
                            desired_subs[if_name][g_name] = {}
                        desired_subs[if_name][g_name].update(subs)
                if if_encaps:
                    desired_encaps[if_name] = if_encaps

        # Compute Deletions (including MAC-VRF re-bindings, encap transitions, and multi-group subinterface sharing)
        for if_name, deployed_vgs in self.deployed_subs.items():
            still_desired_on_if = {}
            for vg_map in desired_subs.get(if_name, {}).values():
                still_desired_on_if.update(vg_map)

            old_encaps_on_if = self.deployed_encaps.get(if_name, {})
            new_encaps_on_if = desired_encaps.get(if_name, {})

            for vg_name, deployed_sub_map in deployed_vgs.items():
                for sid, old_mac_vrf in deployed_sub_map.items():
                    base_if = if_name
                    if sid not in still_desired_on_if:
                        deletes.append(f"/network-instance[name={old_mac_vrf}]/interface[name={base_if}.{sid}]")
                        deletes.append(f"/interface[name={base_if}]/subinterface[index={sid}]")
                    else:
                        if still_desired_on_if[sid] != old_mac_vrf:
                            deletes.append(f"/network-instance[name={old_mac_vrf}]/interface[name={base_if}.{sid}]")
                        old_encap = old_encaps_on_if.get(sid)
                        new_encap = new_encaps_on_if.get(sid)
                        if old_encap is not None and new_encap is not None and old_encap != new_encap:
                            deletes.append(f"/interface[name={base_if}]/subinterface[index={sid}]/vlan/encap")

        deletes = self._deduplicate_paths(deletes)

        # Separate and deduplicate vlan-tagging updates from subinterface updates
        tagging_updates = self._deduplicate_updates(
            [u for u in updates if u["path"].endswith("/vlan-tagging")]
        )
        other_updates = self._deduplicate_updates(
            [u for u in updates if not u["path"].endswith("/vlan-tagging")]
        )

        gnmi_ok = True
        # Step 1: Enable vlan-tagging on target interfaces first
        if tagging_updates:
            logger.info(f"Enabling vlan-tagging on {len(tagging_updates)} interfaces via gNMI")
            if self._apply_gnmi(tagging_updates) is False:
                gnmi_ok = False

        # Step 2: Execute subinterface and network-instance updates/deletes
        if gnmi_ok and (other_updates or deletes):
            logger.info(f"Applying {len(other_updates)} subinterface updates, {len(deletes)} deletes via gNMI")
            if self._apply_gnmi(other_updates, deletes) is False:
                gnmi_ok = False

        # Update our memory only if gNMI transactions succeeded
        if gnmi_ok:
            self.deployed_subs = desired_subs
            self.deployed_encaps = desired_encaps

        # Update Telemetry State
        desired_telemetry = {}
        for g_name, group_cfg in self.vlan_groups.items():
            # Mirror the group and untagged configuration with effective defaults
            group_data = {}
            if 'untagged' in group_cfg:
                ut_clean = self._clean_ndk_json(group_cfg['untagged'])
                group_data['untagged'] = {
                    'subinterface-id': ut_clean.get('subinterface-id', 1),
                    'mac-vrf': ut_clean.get('mac-vrf', 'vlan-1'),
                }
            desired_telemetry[f'.vlan_groups.vlan_group{{.name=="{g_name}"}}'] = json.dumps(group_data)

            # Mirror the tag configuration with effective defaults
            if 'tag' in group_cfg and group_cfg['tag']:
                for tag_data in group_cfg['tag']:
                    tag_id = tag_data.get('vlan-id')
                    if tag_id is not None:
                        clean_tag = self._clean_ndk_json(tag_data)
                        clean_tag['vlan-id'] = tag_id
                        clean_tag.setdefault('subinterface-id', tag_id)
                        clean_tag.setdefault('mac-vrf', f'vlan-{tag_id}')
                        desired_telemetry[
                            f'.vlan_groups.vlan_group{{.name=="{g_name}"}}.tag{{.vlan-id=="{tag_id}"}}'
                        ] = json.dumps(clean_tag)

            # Mirror applied interfaces
            if g_name in group_interfaces:
                for if_name in group_interfaces[g_name]:
                    desired_telemetry[
                        f'.vlan_groups.vlan_group{{.name=="{g_name}"}}.applied.interfaces.interface{{.name=="{if_name}"}}'
                    ] = "{}"

            # Mirror created network-instances
            created_mvrfs = set()
            if 'untagged' in group_cfg:
                val = group_cfg['untagged'].get('mac-vrf', 'vlan-1')
                if isinstance(val, dict):
                    val = val.get('value', 'vlan-1')
                created_mvrfs.add(val)
            for tag_data in group_cfg.get('tag', []):
                vid = tag_data.get('vlan-id')
                if isinstance(vid, dict):
                    vid = vid.get('value')
                val = tag_data.get('mac-vrf', f'vlan-{vid}')
                if isinstance(val, dict):
                    val = val.get('value', f'vlan-{vid}')
                created_mvrfs.add(val)

            for mvrf in sorted(created_mvrfs):
                desired_telemetry[
                    f'.vlan_groups.vlan_group{{.name=="{g_name}"}}.created.network_instances.network_instance{{.name=="{mvrf}"}}'
                ] = "{}"

        # Mirror interface vlan-groups config and state
        for if_name, if_cfg in self.interfaces.items():
            if 'vlan-group' in if_cfg:
                for vg in if_cfg['vlan-group']:
                    vg_name = vg['name']
                    desired_telemetry[
                        f'.interface{{.name=="{if_name}"}}.vlan_groups.vlan_group{{.name=="{vg_name}"}}'
                    ] = "{}"
                    if if_name in self.deployed_subs and vg_name in self.deployed_subs[if_name]:
                        for sid, mvrf in self.deployed_subs[if_name][vg_name].items():
                            data = {"associated-mac-vrf": mvrf}
                            desired_telemetry[
                                f'.interface{{.name=="{if_name}"}}.vlan_groups.vlan_group{{.name=="{vg_name}"}}.created.subinterface{{.index=={sid}}}'
                            ] = json.dumps(data)

        self.sync_telemetry(desired_telemetry)

    def _parse_untagged_data(self, untagged_data):
        entry = {}
        if not isinstance(untagged_data, dict):
            return entry
        if 'subinterface_id' in untagged_data:
            val = untagged_data['subinterface_id']
            if isinstance(val, dict) and 'value' in val:
                entry['subinterface-id'] = int(val['value'])
            elif isinstance(val, (int, str)) and str(val).isdigit():
                entry['subinterface-id'] = int(val)
        elif 'subinterface-id' in untagged_data:
            val = untagged_data['subinterface-id']
            if isinstance(val, dict) and 'value' in val:
                entry['subinterface-id'] = int(val['value'])
            elif isinstance(val, (int, str)) and str(val).isdigit():
                entry['subinterface-id'] = int(val)

        if 'mac_vrf' in untagged_data:
            val = untagged_data['mac_vrf']
            if isinstance(val, dict) and 'value' in val:
                entry['mac-vrf'] = val['value']
            elif isinstance(val, str):
                entry['mac-vrf'] = val
        elif 'mac-vrf' in untagged_data:
            val = untagged_data['mac-vrf']
            if isinstance(val, dict) and 'value' in val:
                entry['mac-vrf'] = val['value']
            elif isinstance(val, str):
                entry['mac-vrf'] = val

        return entry

    def _parse_tag_data(self, vlan_id, tag_data):
        tag_entry = {'vlan-id': int(vlan_id)}
        if not isinstance(tag_data, dict):
            return tag_entry
        if 'subinterface_id' in tag_data:
            val = tag_data['subinterface_id']
            if isinstance(val, dict) and 'value' in val:
                tag_entry['subinterface-id'] = int(val['value'])
            elif isinstance(val, (int, str)) and str(val).isdigit():
                tag_entry['subinterface-id'] = int(val)
        elif 'subinterface-id' in tag_data:
            val = tag_data['subinterface-id']
            if isinstance(val, dict) and 'value' in val:
                tag_entry['subinterface-id'] = int(val['value'])
            elif isinstance(val, (int, str)) and str(val).isdigit():
                tag_entry['subinterface-id'] = int(val)

        if 'mac_vrf' in tag_data:
            val = tag_data['mac_vrf']
            if isinstance(val, dict) and 'value' in val:
                tag_entry['mac-vrf'] = val['value']
            elif isinstance(val, str):
                tag_entry['mac-vrf'] = val
        elif 'mac-vrf' in tag_data:
            val = tag_data['mac-vrf']
            if isinstance(val, dict) and 'value' in val:
                tag_entry['mac-vrf'] = val['value']
            elif isinstance(val, str):
                tag_entry['mac-vrf'] = val

        return tag_entry

    def _clean_ndk_json(self, data):
        if isinstance(data, dict):
            if 'value' in data and len(data) == 1:
                val = data['value']
                if isinstance(val, str) and val.isdigit():
                    return int(val)
                return val
            result = {}
            for k, v in data.items():
                new_key = k.replace('_', '-')
                result[new_key] = self._clean_ndk_json(v)
            return result
        elif isinstance(data, list):
            return [self._clean_ndk_json(item) for item in data]
        else:
            return data

    def _apply_gnmi(self, updates, deletes=None):
        if not updates and not deletes:
            return True

        try:
            if self.gnmi_stub is None:
                self.gnmi_channel = grpc.insecure_channel(self.gnmi_socket)
                self.gnmi_stub = gnmi_pb2_grpc.gNMIStub(self.gnmi_channel)

            req = gnmi_pb2.SetRequest()
            if deletes:
                for d in deletes:
                    del_path = req.delete.add()
                    del_path.CopyFrom(_parse_gnmi_path(d))

            if updates:
                for u in updates:
                    update = req.update.add()
                    update.path.CopyFrom(_parse_gnmi_path(u["path"]))
                    update.val.json_ietf_val = json.dumps(u["value"]).encode('utf-8')

            self.gnmi_stub.Set(
                req,
                metadata=[("username", self.gnmi_user), ("password", self.gnmi_password)]
            )
            logger.info("gNMI update successful")
            return True
        except Exception as e:
            logger.error(f"gNMI update failed: {e}")
            return False

    def run(self):
        def _handle_signal(signum, _frame):
            logger.info(f"Received signal {signum}, shutting down vlan-groups agent...")
            self.close()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        while self._running:
            try:
                self.register_agent()
                stream_id = self.subscribe_config()
                while self._running:
                    self.process_notifications(stream_id)
            except Exception as e:
                if not self._running:
                    break
                logger.error(f"Agent run loop error, reconnecting: {e}")
                time.sleep(2)


if __name__ == '__main__':
    app = VlanGroupsApp()
    app.run()
