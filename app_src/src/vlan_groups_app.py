#!/usr/bin/env python3
import sys
import os
import json
import logging
import grpc

sys.path.append('/opt/vlan-groups/src/vendor')

from ndk import sdk_service_pb2
from ndk import sdk_service_pb2_grpc
from ndk import config_service_pb2
from ndk import config_service_pb2_grpc
from ndk import telemetry_service_pb2
from ndk import telemetry_service_pb2_grpc

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('vlan-groups')

import time
class VlanGroupsApp:
    def __init__(self):
        while True:
            self.channel = grpc.insecure_channel('unix:///opt/srlinux/var/run/sr_sdk_service_manager:50053')
            try:
                grpc.channel_ready_future(self.channel).result(timeout=5)
                break
            except Exception as e:
                logger.info(f"Waiting for SDK manager socket... Error: {e}")
                time.sleep(2)
                
        self.sdk_stub = sdk_service_pb2_grpc.SdkMgrServiceStub(self.channel)
        self.telemetry_stub = telemetry_service_pb2_grpc.SdkMgrTelemetryServiceStub(self.channel)
        self.notification_stub = sdk_service_pb2_grpc.SdkNotificationServiceStub(self.channel)
        self.config_stub = config_service_pb2_grpc.SdkMgrConfigServiceStub(self.channel)
        
        self.agent_id = None
        self.vlan_groups = {}
        self.interfaces = {}
        self.deployed_subs = {} # Format: {if_name: {vlan_group_name: {sub_id: mac_vrf}}}
        self.active_telemetry_paths = set()


    def register_agent(self):
        req = sdk_service_pb2.AgentRegistrationRequest(
            js_paths=['.vlan_groups', '.interface.vlan_groups']
        )
        res = self.sdk_stub.AgentRegister(req, metadata=[('agent_name', 'vlan-groups')])
        self.agent_id = res.app_id
        logger.info(f"Agent registered with ID: {res.app_id}")
        return res.app_id

    def subscribe_config(self):
        # Create stream
        req = sdk_service_pb2.NotificationRegisterRequest(op=0)
        res = self.sdk_stub.NotificationRegister(req, metadata=[('agent_name', 'vlan-groups')])
        stream_id = res.stream_id
        
        # Add config subscription (inherits js_paths from AgentRegistrationRequest)
        from ndk.config_service_pb2 import ConfigSubscriptionRequest
        req_sub = sdk_service_pb2.NotificationRegisterRequest(
            op=sdk_service_pb2.NotificationRegisterRequest.OPERATION_ADD_SUBSCRIPTION,
            stream_id=stream_id,
            config=ConfigSubscriptionRequest()
        )
        self.sdk_stub.NotificationRegister(req_sub, metadata=[('agent_name', 'vlan-groups')])
        
        logger.info(f"Registered for config notifications, stream_id: {stream_id}")
        return stream_id

    def sync_telemetry(self, desired_telemetry):
        desired_paths = set(desired_telemetry.keys())
        paths_to_delete = self.active_telemetry_paths - desired_paths
        
        if desired_telemetry:
            req = telemetry_service_pb2.TelemetryUpdateRequest()
            for path, data in desired_telemetry.items():
                info = req.states.add()
                info.key.js_path = path
                info.data.json_content = data
            try:
                self.telemetry_stub.TelemetryAddOrUpdate(req, metadata=[('agent_name', 'vlan-groups')])
            except Exception as e:
                logger.error(f"Error updating telemetry: {e}")

        if paths_to_delete:
            del_req = telemetry_service_pb2.TelemetryDeleteRequest()
            for path in paths_to_delete:
                key = del_req.keys.add()
                key.js_path = path
                logger.info(f"Deleting telemetry path: {path}")
            try:
                self.telemetry_stub.TelemetryDelete(del_req, metadata=[('agent_name', 'vlan-groups')])
            except Exception as e:
                logger.error(f"Error deleting telemetry: {e}")
                
        self.active_telemetry_paths = desired_paths

    def process_notifications(self, stream_id):
        req = sdk_service_pb2.NotificationStreamRequest(stream_id=stream_id)
        logger.info(f"Starting to listen on NotificationStream for stream {stream_id}")
        try:
            for res in self.notification_stub.NotificationStream(req, metadata=[('agent_name', 'vlan-groups')]):
                logger.info(f"Raw notification stream response received")
                
                if res.notifications:
                    logger.info(f"Received notification response with {len(res.notifications)} notifications")
                    for notif in res.notifications:
                        if notif.HasField('config'):
                            self.handle_config_notification(notif.config)
                            
                # Ack the config ALWAYS when we receive a stream response
                ack_req = config_service_pb2.AcknowledgeConfigRequest()
                self.config_stub.AcknowledgeConfig(ack_req, metadata=[('agent_name', 'vlan-groups')])
                logger.info("Acknowledged config successfully!")
                
                self.reconcile()
        except Exception as e:
            logger.error(f"NotificationStream loop crashed: {e}")
            import time
            time.sleep(2)
        logger.error(f"NotificationStream loop EXITED normally!")

    def handle_config_notification(self, config_notif):
        path = config_notif.key.js_path
        keys = config_notif.key.keys
        data = config_notif.data.json
        if data:
            try:
                parsed_data = json.loads(data)
                logger.info(f"Path: {path}, Keys: {keys}, Data: {parsed_data}")
                
                if path == '.vlan_groups.vlan_group' and len(keys) >= 1:
                    group_name = keys[0]
                    if group_name not in self.vlan_groups:
                        self.vlan_groups[group_name] = {'name': group_name}
                    
                    group_data = parsed_data.get('vlan_group', parsed_data)
                    if 'untagged' in group_data:
                        self.vlan_groups[group_name]['untagged'] = self._parse_untagged_data(group_data['untagged'])

                elif path == '.vlan_groups.vlan_group.untagged' and len(keys) >= 1:
                    group_name = keys[0]
                    if group_name not in self.vlan_groups:
                        self.vlan_groups[group_name] = {'name': group_name}
                    
                    untagged_data = parsed_data.get('untagged', parsed_data)
                    self.vlan_groups[group_name]['untagged'] = self._parse_untagged_data(untagged_data)
                
                elif path == '.vlan_groups.vlan_group.tag' and len(keys) >= 2:
                    group_name = keys[0]
                    vlan_id = keys[1]
                    if group_name not in self.vlan_groups:
                        self.vlan_groups[group_name] = {'name': group_name}
                    
                    if 'tag' not in self.vlan_groups[group_name]:
                        self.vlan_groups[group_name]['tag'] = []
                    
                    # Update or add tag
                    tag_data = parsed_data.get('tag', {})
                    tag_entry = {'vlan-id': int(vlan_id)}
                    
                    if 'subinterface_id' in tag_data and 'value' in tag_data['subinterface_id']:
                        tag_entry['subinterface-id'] = int(tag_data['subinterface_id']['value'])
                    if 'mac_vrf' in tag_data and 'value' in tag_data['mac_vrf']:
                        tag_entry['mac-vrf'] = tag_data['mac_vrf']['value']
                        
                    # find if exists
                    existing = False
                    for i, t in enumerate(self.vlan_groups[group_name]['tag']):
                        if t.get('vlan-id') == tag_entry['vlan-id']:
                            self.vlan_groups[group_name]['tag'][i] = tag_entry
                            existing = True
                            break
                    if not existing:
                        self.vlan_groups[group_name]['tag'].append(tag_entry)
                            
                # Handle interface configuration
                elif path == '.interface.vlan_groups.vlan_group' and len(keys) >= 2:
                    if_name = keys[0]
                    vlan_group_name = keys[1]
                    
                    if if_name not in self.interfaces:
                        self.interfaces[if_name] = {'vlan-group': []}
                    
                    if 'vlan-group' not in self.interfaces[if_name]:
                        self.interfaces[if_name]['vlan-group'] = []
                        
                    # find if exists
                    existing = False
                    for i, vg in enumerate(self.interfaces[if_name]['vlan-group']):
                        if vg.get('name') == vlan_group_name:
                            existing = True
                            break
                    if not existing:
                        self.interfaces[if_name]['vlan-group'].append({'name': vlan_group_name})

            except json.JSONDecodeError:
                logger.error(f"Failed to parse JSON: {data}")
        else:
            # Handling deletions
            if path == '.vlan_groups.vlan_group' and len(keys) >= 1:
                group_name = keys[0]
                if group_name in self.vlan_groups:
                    del self.vlan_groups[group_name]
            elif path == '.vlan_groups.vlan_group.untagged' and len(keys) >= 1:
                group_name = keys[0]
                if group_name in self.vlan_groups and 'untagged' in self.vlan_groups[group_name]:
                    del self.vlan_groups[group_name]['untagged']
            elif path == '.vlan_groups.vlan_group.tag' and len(keys) >= 2:
                group_name = keys[0]
                vlan_id = int(keys[1])
                if group_name in self.vlan_groups and 'tag' in self.vlan_groups[group_name]:
                    self.vlan_groups[group_name]['tag'] = [t for t in self.vlan_groups[group_name]['tag'] if t.get('vlan-id') != vlan_id]
            elif path == '.interface.vlan_groups.vlan_group' and len(keys) >= 2:
                if_name = keys[0]
                vlan_group_name = keys[1]
                if if_name in self.interfaces and 'vlan-group' in self.interfaces[if_name]:
                    self.interfaces[if_name]['vlan-group'] = [vg for vg in self.interfaces[if_name]['vlan-group'] if vg.get('name') != vlan_group_name]

    def _generate_gnmi_updates(self, if_name, group_cfg):
        updates = []
        desired_subs = {}
        
        # Determine base interface type (e.g., ethernet-1/1 or lag-1)
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
            
            # Create subinterface
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
            
            # Associate with MAC-VRF
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

        # Handle tagged subinterfaces
        for tag in group_cfg.get('tag', []):
            vid = tag['vlan-id']
            sid = tag.get('subinterface-id', vid)
            mac_vrf = tag.get('mac-vrf', f'vlan-{vid}')
            
            # Create subinterface
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
            
            # Associate with MAC-VRF
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
            
        return updates, desired_subs


    def reconcile(self):
        updates = []
        deletes = []
        
        desired_subs = {}
        group_interfaces = {}

        for if_name, if_cfg in self.interfaces.items():
            if 'vlan-group' in if_cfg:
                for vg in if_cfg['vlan-group']:
                    g_name = vg['name']
                    if g_name in self.vlan_groups:
                        if g_name not in group_interfaces:
                            group_interfaces[g_name] = []
                        group_interfaces[g_name].append(if_name)
                        
                        upds, subs = self._generate_gnmi_updates(if_name, self.vlan_groups[g_name])
                        updates.extend(upds)
                        
                        if if_name not in desired_subs:
                            desired_subs[if_name] = {}
                        if g_name not in desired_subs[if_name]:
                            desired_subs[if_name][g_name] = {}
                        desired_subs[if_name][g_name].update(subs)

        # Compute Deletions
        for if_name, deployed_vgs in self.deployed_subs.items():
            for vg_name, deployed_sub_map in deployed_vgs.items():
                for sid, mac_vrf in deployed_sub_map.items():
                    # Check if this subinterface should still exist for this interface/group
                    if if_name not in desired_subs or vg_name not in desired_subs[if_name] or sid not in desired_subs[if_name][vg_name]:
                        base_if = if_name
                        deletes.append(f"/network-instance[name={mac_vrf}]/interface[name={base_if}.{sid}]")
                        deletes.append(f"/interface[name={base_if}]/subinterface[index={sid}]")
        
        # Update our memory
        self.deployed_subs = desired_subs

        # Separate vlan-tagging updates from subinterface updates
        tagging_updates = [u for u in updates if u["path"].endswith("/vlan-tagging")]
        other_updates = [u for u in updates if not u["path"].endswith("/vlan-tagging")]

        # Step 1: Enable vlan-tagging on target interfaces first
        if tagging_updates:
            logger.info(f"Enabling vlan-tagging on {len(tagging_updates)} interfaces via gNMI")
            self._apply_gnmi(tagging_updates)

        # Step 2: Execute subinterface and network-instance updates/deletes
        if other_updates or deletes:
            logger.info(f"Applying {len(other_updates)} subinterface updates, {len(deletes)} deletes via gNMI")
            self._apply_gnmi(other_updates, deletes)
            
        # Update Telemetry State
        desired_telemetry = {}
        for g_name, group_cfg in self.vlan_groups.items():
            # Mirror the untagged configuration
            group_data = {}
            if 'untagged' in group_cfg:
                group_data['untagged'] = self._clean_ndk_json(group_cfg['untagged'])
            if group_data:
                desired_telemetry[f'.vlan_groups.vlan_group{{.name=="{g_name}"}}'] = json.dumps(group_data)

            # Mirror the tag configuration
            if 'tag' in group_cfg and group_cfg['tag']:
                for tag_data in group_cfg['tag']:
                    tag_id = tag_data.get('vlan-id')
                    if tag_id is not None:
                        clean_tag = self._clean_ndk_json(tag_data)
                        desired_telemetry[f'.vlan_groups.vlan_group{{.name=="{g_name}"}}.tag{{.vlan-id=="{tag_id}"}}'] = json.dumps(clean_tag)

            # Mirror applied interfaces
            if g_name in group_interfaces:
                for if_name in group_interfaces[g_name]:
                    desired_telemetry[f'.vlan_groups.vlan_group{{.name=="{g_name}"}}.applied.interfaces.interface{{.name=="{if_name}"}}'] = "{}"
            
            # Mirror created network-instances
            created_mvrfs = set()
            if 'untagged' in group_cfg:
                val = group_cfg['untagged'].get('mac-vrf', 'vlan-1')
                if isinstance(val, dict):
                    val = val.get('value', 'vlan-1')
                created_mvrfs.add(val)
            for tag_data in group_cfg.get('tag', []):
                vid = tag_data.get('vlan-id')
                if isinstance(vid, dict): vid = vid.get('value')
                val = tag_data.get('mac-vrf', f'vlan-{vid}')
                if isinstance(val, dict): val = val.get('value', f'vlan-{vid}')
                created_mvrfs.add(val)
                
            for mvrf in created_mvrfs:
                desired_telemetry[f'.vlan_groups.vlan_group{{.name=="{g_name}"}}.created.network_instances.network_instance{{.name=="{mvrf}"}}'] = "{}"

        # Mirror interface vlan-groups config and state
        for if_name, if_cfg in self.interfaces.items():
            if 'vlan-group' in if_cfg:
                for vg in if_cfg['vlan-group']:
                    vg_name = vg['name']
                    desired_telemetry[f'.interface{{.name=="{if_name}"}}.vlan_groups.vlan_group{{.name=="{vg_name}"}}'] = "{}"
                    if if_name in self.deployed_subs and vg_name in self.deployed_subs[if_name]:
                        for sid, mvrf in self.deployed_subs[if_name][vg_name].items():
                            data = {"associated-mac-vrf": mvrf}
                            desired_telemetry[f'.interface{{.name=="{if_name}"}}.vlan_groups.vlan_group{{.name=="{vg_name}"}}.created.subinterface{{.index=={sid}}}'] = json.dumps(data)
                
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
            entry['subinterface-id'] = int(untagged_data['subinterface-id'])
            
        if 'mac_vrf' in untagged_data:
            val = untagged_data['mac_vrf']
            if isinstance(val, dict) and 'value' in val:
                entry['mac-vrf'] = val['value']
            elif isinstance(val, str):
                entry['mac-vrf'] = val
        elif 'mac-vrf' in untagged_data:
            entry['mac-vrf'] = untagged_data['mac-vrf']
            
        return entry

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
            return
            
        import grpc
        import json
        import re
        from gnmi import gnmi_pb2
        from gnmi import gnmi_pb2_grpc
        
        def _parse_gnmi_path(path_str):
            path = gnmi_pb2.Path()
            parts = re.findall(r'([^/\[]+(?:\[[^\]]+\])?)', path_str)
            for part in parts:
                elem = path.elem.add()
                if '[' in part and part.endswith(']'):
                    name, key_val = part.split('[', 1)
                    elem.name = name
                    key_val = key_val[:-1]
                    k, v = key_val.split('=', 1)
                    elem.key[k] = v
                else:
                    elem.name = part
            return path
            
        try:
            channel = grpc.insecure_channel('unix:///opt/srlinux/var/run/sr_grpc_server_insecure-mgmt')
            stub = gnmi_pb2_grpc.gNMIStub(channel)
            
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
                
            res = stub.Set(req, metadata=[("username", "admin"), ("password", "NokiaSrl1!")])
            logger.info(f"gNMI update successful")
        except Exception as e:
            logger.error(f"gNMI update failed: {e}")
    def run(self):
        app_id = self.register_agent()
        stream_id = self.subscribe_config()
        
        while True:
            self.process_notifications(stream_id)

if __name__ == '__main__':
    app = VlanGroupsApp()
    app.run()
