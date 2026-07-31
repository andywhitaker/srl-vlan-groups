from srlinux.mgmt.cli.cli_plugin import CliPlugin
from srlinux.mgmt.cli.required_plugin import RequiredPlugin
from srlinux.syntax import Syntax
from srlinux.schema.data_store import DataStore
from srlinux.location import build_path
import json
import textwrap

class Plugin(CliPlugin):
    def get_required_plugins(self):
        return [RequiredPlugin('show')]

    def load(self, cli, **_kwargs):
        pass

    def _suggest_groups(self, *args, **kwargs):
        state = args[1] if len(args) > 1 else None
        if not state: return []
        vg_data, _ = self._fetch_data(state)
        return list(vg_data.keys())

    def _suggest_interfaces(self, *args, **kwargs):
        state = args[1] if len(args) > 1 else None
        if not state: return []
        _, if_data = self._fetch_data(state)
        return list(if_data.keys())

    def on_schema_ready(self, state):
        root = Syntax("vlan-groups", help="Show VLAN groups")
        group_syntax = Syntax("group", help="Show specific VLAN group")
        group_syntax.add_unnamed_argument("group-name", help="Optional: Specific VLAN group name", default="", suggestions=self._suggest_groups)
        
        if_syntax = Syntax("interface", help="Show VLAN groups on interfaces")
        if_syntax.add_unnamed_argument("interface-name", help="Optional: Specific interface name", default="", suggestions=self._suggest_interfaces)
        
        root_node = state.command_tree.show_mode.root.add_command(root, update_location=False, callback=self._print_root)
        root_node.add_command(group_syntax, update_location=False, callback=self._print_group)
        root_node.add_command(if_syntax, update_location=False, callback=self._print_interface)

    def _fetch_data(self, state):
        vg_json = state.server.get_proto_json(build_path('/vlan-groups'), recursive=True, data_store=DataStore.Running, include_field_defaults=True)
        if_json = state.server.get_proto_json(build_path('/interface'), recursive=True, data_store=DataStore.Running, include_field_defaults=False)
        
        vg_data = {}
        if vg_json:
            vg_parsed = json.loads(vg_json)
            group_list = vg_parsed.get('vlan_groups', {}).get('group', [])
            if not group_list:
                group_list = vg_parsed.get('group', [])
                
            for g in group_list:
                name = g.get('name', {}).get('value', '')
                if not name: continue
                
                tags = []
                mac_vrfs = []
                subs = []
                
                inner = g.get('group', g)
                
                if 'untagged' in inner:
                    ut = inner['untagged']
                    sub_id = ut.get('subinterface_id', {}).get('value', 1)
                    mac_vrf = ut.get('mac_vrf', {}).get('value', 'vlan-1')
                    tags.append('untagged')
                    subs.append(str(sub_id))
                    mac_vrfs.append(mac_vrf)
                    
                if 'tag' in inner:
                    for t in inner['tag']:
                        vid = t.get('vlan_id', {}).get('value', '')
                        t_inner = t.get('tag', t)
                        sub_id = t_inner.get('subinterface_id', {}).get('value', vid)
                        mac_vrf = t_inner.get('mac_vrf', {}).get('value', f'vlan-{vid}')
                        tags.append(str(vid))
                        subs.append(str(sub_id))
                        mac_vrfs.append(mac_vrf)
                        
                vg_data[name] = {
                    'tags': tags,
                    'mac_vrfs': mac_vrfs,
                    'subs': subs,
                    'interfaces': []
                }
                
        if_data = {}
        if if_json:
            if_parsed = json.loads(if_json)
            if_list = if_parsed.get('interface', [])
            for i in if_list:
                if_name = i.get('name', {}).get('value', '')
                inner = i.get('interface', i)
                vg = inner.get('vlan_groups', {}).get('vlan_group', {}).get('value', '')
                if vg and vg in vg_data:
                    vg_data[vg]['interfaces'].append(if_name)
                if_data[if_name] = vg
                
        return vg_data, if_data

    def _print_table(self, output, headers, widths, data_rows):
        def format_row(cols):
            return " ".join([f"{col:<{widths[i]}}" for i, col in enumerate(cols)])
            
        res = []
        res.append(format_row(headers))
        res.append("-" * (sum(widths) + len(widths) - 1))
        
        for row in data_rows:
            wrapped_cols = [textwrap.wrap(str(text), widths[i]) for i, text in enumerate(row)]
            max_lines = max([len(col) for col in wrapped_cols]) if wrapped_cols else 1
            
            for line_idx in range(max_lines):
                line_cols = []
                for col in wrapped_cols:
                    if line_idx < len(col):
                        line_cols.append(col[line_idx])
                    else:
                        line_cols.append("")
                res.append(format_row(line_cols))
                
        output.print_line("\n".join(res))
                
    def _print_root(self, state, output, arguments, **_kwargs):
        # Prevent duplicate execution if a child command is being executed
        if hasattr(state, 'line_commands') and state.line_commands:
            if state.line_commands.has_node("group") or state.line_commands.has_node("interface"):
                return
        
        self._print_groups(state, output, '')

    def _print_group(self, state, output, arguments, **_kwargs):
        group_name = arguments.get('group-name') or ''
        self._print_groups(state, output, group_name)

    def _print_interface(self, state, output, arguments, **_kwargs):
        interface_name = arguments.get('interface-name') or ''
        self._print_interfaces(state, output, interface_name)

    def _print_groups(self, state, output, group_filter):
        vg_data, _ = self._fetch_data(state)
        
        if group_filter and group_filter not in vg_data:
            output.print_line(f"VLAN group '{group_filter}' not found.")
            return

        res = []

        headers = ['Group Name', 'Tags (Subinterface) [MAC-VRF]', 'Interfaces']
        widths = [20, 50, 30]
        data_rows = []
        
        for name, data in vg_data.items():
            if group_filter and name != group_filter:
                continue
                
            tags_subs_mvrfs = []
            for sub, tag, mvrf in zip(data.get('subs', []), data.get('tags', []), data.get('mac_vrfs', [])):
                tags_subs_mvrfs.append(f"{tag}({sub})[{mvrf}]")
                
            tags_str = ", ".join(tags_subs_mvrfs) if tags_subs_mvrfs else "none"
            ifs_str = ", ".join(data['interfaces']) if data['interfaces'] else "none"
            
            data_rows.append([name, tags_str, ifs_str])
            
        self._print_table(output, headers, widths, data_rows)

    def _print_interfaces(self, state, output, if_filter):
        vg_data, if_data = self._fetch_data(state)
        
        if if_filter and if_filter not in if_data:
            output.print_line(f"Interface '{if_filter}' not found.")
            return

        headers = ['Interface', 'VLAN Group', 'Tags (Subinterface) [MAC-VRF]']
        widths = [25, 20, 50]
        data_rows = []
        
        for if_name in sorted(if_data.keys()):
            if if_filter and if_name != if_filter:
                continue
                
            vg = if_data[if_name]
            if not vg:
                data_rows.append([if_name, 'none', 'none'])
            else:
                data = vg_data.get(vg, {})
                subs_tags = []
                for sub, tag, mvrf in zip(data.get('subs', []), data.get('tags', []), data.get('mac_vrfs', [])):
                    subs_tags.append(f"{tag}({sub})[{mvrf}]")
                st_str = ", ".join(subs_tags) if subs_tags else "none"
                data_rows.append([if_name, vg, st_str])
                
        self._print_table(output, headers, widths, data_rows)
