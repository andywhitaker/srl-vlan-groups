import sys
import time
import logging
import json
import grpc
import threading
import re
import traceback

sys.path.append('/opt/vlan-groups/src/vendor')

from ndk import sdk_service_pb2, sdk_service_pb2_grpc
from ndk import telemetry_service_pb2, telemetry_service_pb2_grpc
from ndk import config_service_pb2, config_service_pb2_grpc
import gnmi.gnmi_pb2 as gnmi_pb2
import gnmi.gnmi_pb2_grpc as gnmi_pb2_grpc

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('vlan-groups')

def parse_path(path_str):
    path = gnmi_pb2.Path()
    pattern = r'/([^/\[]+)(?:\[([^=]+)=([^\]]+)\])?'
    for match in re.finditer(pattern, path_str if path_str.startswith('/') else '/' + path_str):
        name = match.group(1)
        keys = {match.group(2): match.group(3)} if match.group(2) else {}
        path.elem.append(gnmi_pb2.PathElem(name=name, key=keys))
    return path

class VlanGroupsApp:
    def __init__(self):
        self.sdk_channel = grpc.insecure_channel('unix:///opt/srlinux/var/run/sr_sdk_service_manager:50053')
        self.gnmi_channel = grpc.insecure_channel('unix:///opt/srlinux/var/run/sr_grpc_server_insecure-mgmt')
        
        self.sdk_stub = sdk_service_pb2_grpc.SdkMgrServiceStub(self.sdk_channel)
        self.notif_stub = sdk_service_pb2_grpc.SdkNotificationServiceStub(self.sdk_channel)
        self.config_stub = config_service_pb2_grpc.SdkMgrConfigServiceStub(self.sdk_channel)
        
        self.gnmi_stub = gnmi_pb2_grpc.gNMIStub(self.gnmi_channel)
        self.metadata = [('username', 'admin'), ('password', 'NokiaSrl1!')]
        self.sdk_metadata = [('agent_name', 'vlan-groups')]

        self.agent_id = None
        self.vlan_groups = {} 
        self.interfaces = {}  
        self.current_ifs = {}
        self.current_nis = {}

    def register_agent(self):
        req = sdk_service_pb2.AgentRegistrationRequest()
        req.wait_config_ack = True
        res = self.sdk_stub.AgentRegister(req, metadata=self.sdk_metadata)
        self.agent_id = getattr(res, 'app_id', 0)
        logger.info(f"Agent registered with ID: {self.agent_id}")

    def gnmi_set(self, updates=None, deletes=None):
        if not updates and not deletes:
            return
        
        req = gnmi_pb2.SetRequest()
        if deletes:
            for p in deletes:
                req.delete.append(parse_path(p))
            
        if updates:
            for p, val in updates:
                upd = req.update.add()
                upd.path.CopyFrom(parse_path(p))
                upd.val.json_ietf_val = json.dumps(val).encode('utf-8')
            
        try:
            logger.info(f"Applying gNMI SetRequest")
            self.gnmi_stub.Set(req, metadata=self.metadata)
        except Exception as e:
            logger.error(f"Set error: {e}")

    def gnmi_get(self, path_str):
        req = gnmi_pb2.GetRequest(
            path=[parse_path(path_str)],
            encoding=gnmi_pb2.JSON_IETF,
            type=gnmi_pb2.GetRequest.CONFIG
        )
        try:
            res = self.gnmi_stub.Get(req, metadata=self.metadata)
            if not res.notification:
                return None
            for update in res.notification[0].update:
                return json.loads(update.val.json_ietf_val)
        except Exception as e:
            logger.error(f"Get error for {path_str}: {e}")
        return None

    def sync_vlan_groups(self):
        try:
            config = self.gnmi_get('/vlan-groups')
            if config and 'srl_nokia-vlan-groups:group' in config:
                self.vlan_groups = {g['name']: g for g in config['srl_nokia-vlan-groups:group']}
            elif config and 'vlan-groups:group' in config:
                self.vlan_groups = {g['name']: g for g in config['vlan-groups:group']}
            elif config and 'group' in config:
                self.vlan_groups = {g['name']: g for g in config['group']}
            else:
                self.vlan_groups = {}
            
            ifs = self.gnmi_get('/interface')
            self.interfaces = {}
            self.current_ifs = {}
            
            if ifs:
                if_list = ifs.get('srl_nokia-interfaces:interface', ifs.get('interface', []))
                for i in if_list:
                    self.current_ifs[i['name']] = i
                    vg = i.get('srl_nokia-vlan-groups:vlan-groups', i.get('vlan-groups'))
                    if vg:
                        self.interfaces[i['name']] = vg
            
            nis = self.gnmi_get('/network-instance')
            self.current_nis = {}
            if nis:
                ni_list = nis.get('srl_nokia-network-instance:network-instance', nis.get('network-instance', []))
                for ni in ni_list:
                    self.current_nis[ni['name']] = ni

            self.reconcile()
        except Exception as e:
            logger.error(f"Error in sync_vlan_groups: {e}")
            logger.error(traceback.format_exc())

    def reconcile(self):
        updates = []
        deletes_ni = []
        deletes_if = []
        
        # Track desired subinterfaces per interface
        desired_subs = {}
        
        for if_name, if_group_cfg in self.interfaces.items():
            group_name = if_group_cfg.get('vlan-group')
            if not group_name or group_name not in self.vlan_groups:
                continue
                
            desired_subs[if_name] = {}
            
            curr_if = self.current_ifs.get(if_name, {})
            if not curr_if.get('vlan-tagging'):
                updates.append((f'/interface[name={if_name}]', {'vlan-tagging': True}))
            
            group = self.vlan_groups[group_name]
            overrides = {o['vlan-id']: o for o in if_group_cfg.get('per-interface-override', [])}
            
            if 'untagged' in group:
                sub_id = group['untagged'].get('subinterface-id', 1)
                mac_vrf = group['untagged'].get('mac-vrf', 'vlan-1')
                desired_subs[if_name][str(sub_id)] = mac_vrf
                self.apply_subinterface(updates, curr_if, if_name, sub_id, mac_vrf, untagged=True)
                
            for tag in group.get('tag', []):
                vid = tag['vlan-id']
                if vid in overrides and not overrides[vid].get('enabled', True):
                    continue
                
                sub_id = tag.get('subinterface-id', vid)
                mac_vrf = tag.get('mac-vrf', f'vlan-{vid}')
                desired_subs[if_name][str(sub_id)] = mac_vrf
                self.apply_subinterface(updates, curr_if, if_name, sub_id, mac_vrf, vlan_id=vid)

        # Cleanup pass
        for if_name, curr_if in self.current_ifs.items():
            if_desired = desired_subs.get(if_name, {})
            for s in curr_if.get('subinterface', []):
                if s.get('description') == 'vlan-groups-managed':
                    sid = str(s.get('index'))
                    if sid not in if_desired:
                        deletes_if.append(f'/interface[name={if_name}]/subinterface[index={sid}]')
                        # Remove interface from mac-vrf
                        for ni_name, curr_ni in self.current_nis.items():
                            if 'mac-vrf' in curr_ni.get('type', ''):
                                for ni_if in curr_ni.get('interface', []):
                                    if ni_if.get('name') == f'{if_name}.{sid}':
                                        deletes_ni.append(f'/network-instance[name={ni_name}]/interface[name={if_name}.{sid}]')

        if updates or deletes_ni or deletes_if:
            logger.info(f"Applying {len(updates)} updates, {len(deletes_ni)} NI deletes, {len(deletes_if)} IF deletes via gNMI")
            if deletes_ni:
                self.gnmi_set(deletes=deletes_ni)
            if deletes_if:
                self.gnmi_set(deletes=deletes_if)
            if updates:
                self.gnmi_set(updates=updates)

    def apply_subinterface(self, updates, curr_if, if_name, sub_id, mac_vrf, untagged=False, vlan_id=None):
        sub_path = f'/interface[name={if_name}]/subinterface[index={sub_id}]'
        
        # Check if subinterface already exists with correct config
        curr_sub = None
        for s in curr_if.get('subinterface', []):
            if str(s.get('index')) == str(sub_id):
                curr_sub = s
                break
                
        needs_sub_update = False
        if not curr_sub:
            needs_sub_update = True
        else:
            if 'bridged' not in curr_sub.get('type', ''): needs_sub_update = True
            if curr_sub.get('description') != 'vlan-groups-managed': needs_sub_update = True
            vlan_cfg = curr_sub.get('vlan', {}).get('encap', {})
            if untagged and 'untagged' not in vlan_cfg: needs_sub_update = True
            if vlan_id is not None:
                st = vlan_cfg.get('single-tagged', {})
                if str(st.get('vlan-id')) != str(vlan_id): needs_sub_update = True

        if needs_sub_update:
            sub_cfg = {
                'index': sub_id,
                'description': 'vlan-groups-managed',
                'type': 'bridged',
                'vlan': {
                    'encap': {
                        'untagged': {} if untagged else None,
                        'single-tagged': {'vlan-id': vlan_id} if vlan_id is not None else None
                    }
                }
            }
            if not untagged:
                del sub_cfg['vlan']['encap']['untagged']
            if vlan_id is None:
                del sub_cfg['vlan']['encap']['single-tagged']
            updates.append((sub_path, sub_cfg))
        
        vrf_path = f'/network-instance[name={mac_vrf}]'
        curr_ni = self.current_nis.get(mac_vrf)
        needs_ni_update = False
        if not curr_ni:
            needs_ni_update = True
        else:
            if 'mac-vrf' not in curr_ni.get('type', ''): needs_ni_update = True
            found_if = False
            for i in curr_ni.get('interface', []):
                if i.get('name') == f'{if_name}.{sub_id}':
                    found_if = True
                    break
            if not found_if: needs_ni_update = True
            
        if needs_ni_update:
            vrf_cfg = {
                'name': mac_vrf,
                'type': 'mac-vrf',
                'interface': [{'name': f'{if_name}.{sub_id}'}]
            }
            updates.append((vrf_path, vrf_cfg))



    def config_stream_loop(self):
        # Create the stream first
        req_create = sdk_service_pb2.NotificationRegisterRequest(
            op=sdk_service_pb2.NotificationRegisterRequest.OPERATION_CREATE
        )
        try:
            res_create = self.sdk_stub.NotificationRegister(req_create, metadata=self.sdk_metadata)
            stream_id = res_create.stream_id
            
            # Now add the subscription
            req_sub = sdk_service_pb2.NotificationRegisterRequest(
                op=sdk_service_pb2.NotificationRegisterRequest.OPERATION_ADD_SUBSCRIPTION,
                stream_id=stream_id,
                config=config_service_pb2.ConfigSubscriptionRequest()
            )
            self.sdk_stub.NotificationRegister(req_sub, metadata=self.sdk_metadata)
            
            logger.info(f"Registered for config notifications, stream_id: {stream_id}")
            
            stream_req = sdk_service_pb2.NotificationStreamRequest(stream_id=stream_id)
            for notif_resp in self.notif_stub.NotificationStream(stream_req, metadata=self.sdk_metadata):
                logger.info(f"Received notification response with {len(notif_resp.notifications)} notifications")
                ack_paths = []
                for notif in notif_resp.notifications:
                    if notif.HasField('config'):
                        cfg_notif = notif.config
                        path = cfg_notif.key.js_path_with_keys or cfg_notif.key.js_path
                        if path:
                            ack_paths.append(path)
                
                ack = config_service_pb2.AcknowledgeConfigRequest()
                for p in ack_paths:
                    info = ack.infos.add()
                    info.js_path_with_keys = p
                self.config_stub.AcknowledgeConfig(ack, metadata=self.sdk_metadata)
                
                logger.info(f"Acked {len(ack_paths)} config paths")
                if '.commit.end' in ack_paths:
                    self.sync_vlan_groups()
        except Exception as e:
            logger.error(f"Config stream error: {e}")
            logger.error(traceback.format_exc())

    def run(self):

        self.register_agent()
        threading.Thread(target=self.config_stream_loop, daemon=True).start()
        
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Exiting...")

if __name__ == '__main__':
    app = VlanGroupsApp()
    app.run()
