import json
import textwrap

try:
    from srlinux.mgmt.cli.cli_plugin import CliPlugin
    from srlinux.mgmt.cli.required_plugin import RequiredPlugin
    from srlinux.syntax import Syntax
    from srlinux.schema.data_store import DataStore
    from srlinux.location import build_path
except ImportError:
    class CliPlugin:
        pass

    class RequiredPlugin:
        def __init__(self, name):
            self.name = name

    class Syntax:
        def __init__(self, name, help=""):
            self.name = name
            self.help = help
            self.arguments = []

        def add_unnamed_argument(self, name, help="", default="", suggestions=None):
            self.arguments.append({
                "name": name,
                "help": help,
                "default": default,
                "suggestions": suggestions,
            })

    class DataStore:
        Running = "running"
        State = "state"

    def build_path(path):
        return path


class Plugin(CliPlugin):
    def get_required_plugins(self):
        return [RequiredPlugin('show')]

    def load(self, cli, **_kwargs):
        pass

    @staticmethod
    def _extract_val(val, default=''):
        if isinstance(val, dict):
            return val.get('value', default)
        if val is None:
            return default
        return val

    def _suggest_groups(self, *args, **_kwargs):
        state = args[1] if len(args) > 1 else None
        if not state:
            return []
        vg_data, _ = self._fetch_data(state)
        return sorted(vg_data.keys())

    def _suggest_interfaces(self, *args, **_kwargs):
        state = args[1] if len(args) > 1 else None
        if not state:
            return []
        _, if_data = self._fetch_data(state)
        return sorted(if_data.keys())

    def on_schema_ready(self, state):
        root = Syntax("vlan-groups", help="Show VLAN groups")
        group_syntax = Syntax("group", help="Show specific VLAN group")
        group_syntax.add_unnamed_argument(
            "group-name",
            help="Optional: Specific VLAN group name",
            default="",
            suggestions=self._suggest_groups
        )

        if_syntax = Syntax("interface", help="Show VLAN groups on interfaces")
        if_syntax.add_unnamed_argument(
            "interface-name",
            help="Optional: Specific interface name",
            default="",
            suggestions=self._suggest_interfaces
        )

        root_node = state.command_tree.show_mode.root.add_command(
            root, update_location=False, callback=self._print_root
        )
        root_node.add_command(group_syntax, update_location=False, callback=self._print_group)
        root_node.add_command(if_syntax, update_location=False, callback=self._print_interface)

    def _fetch_data(self, state):
        try:
            vg_json = state.server.get_proto_json(
                build_path('/vlan-groups'),
                recursive=True,
                data_store=DataStore.Running,
                include_field_defaults=True
            )
        except Exception:
            vg_json = None

        try:
            if_json = state.server.get_proto_json(
                build_path('/interface'),
                recursive=True,
                data_store=DataStore.Running,
                include_field_defaults=False
            )
        except Exception:
            if_json = None

        vg_data = {}
        if vg_json:
            vg_parsed = json.loads(vg_json)
            group_list = vg_parsed.get('vlan_groups', {}).get('vlan_group', [])
            if not group_list:
                group_list = vg_parsed.get('vlan_group', [])
            if isinstance(group_list, dict):
                group_list = [group_list]

            for g in group_list:
                name = self._extract_val(g.get('name'), '')
                if not name:
                    continue

                tags = []
                mac_vrfs = []
                subs = []

                inner = g.get('vlan_group', g)

                if 'untagged' in inner and isinstance(inner['untagged'], dict):
                    ut = inner['untagged']
                    sub_id = self._extract_val(ut.get('subinterface_id', ut.get('subinterface-id')), 1)
                    mac_vrf = self._extract_val(ut.get('mac_vrf', ut.get('mac-vrf')), 'vlan-1')
                    tags.append('untagged')
                    subs.append(str(sub_id))
                    mac_vrfs.append(str(mac_vrf))

                if 'tag' in inner:
                    tag_list = inner['tag']
                    if isinstance(tag_list, dict):
                        tag_list = [tag_list]

                    parsed_tags = []
                    for t in tag_list:
                        vid = self._extract_val(t.get('vlan_id', t.get('vlan-id')), '')
                        if vid == '':
                            continue
                        t_inner = t.get('tag', t)
                        sub_id = self._extract_val(
                            t_inner.get('subinterface_id', t_inner.get('subinterface-id')), vid
                        )
                        mac_vrf = self._extract_val(
                            t_inner.get('mac_vrf', t_inner.get('mac-vrf')), f'vlan-{vid}'
                        )
                        parsed_tags.append((int(vid) if str(vid).isdigit() else 0, str(vid), str(sub_id), str(mac_vrf)))

                    for _, vid_str, sub_str, mvrf_str in sorted(parsed_tags, key=lambda x: x[0]):
                        tags.append(vid_str)
                        subs.append(sub_str)
                        mac_vrfs.append(mvrf_str)

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
            if isinstance(if_list, dict):
                if_list = [if_list]
            for i in if_list:
                if_name = self._extract_val(i.get('name'), '')
                if not if_name:
                    continue
                inner = i.get('interface', i)
                vgs_container = inner.get('vlan_groups', inner.get('vlan-groups', {}))
                if not isinstance(vgs_container, dict):
                    continue
                vg_list = vgs_container.get('vlan_group', vgs_container.get('vlan-group', []))
                if isinstance(vg_list, dict):
                    vg_list = [vg_list]

                vgs = []
                for vg_item in vg_list:
                    vg = self._extract_val(vg_item.get('name') if isinstance(vg_item, dict) else vg_item, '')
                    if vg:
                        vgs.append(vg)
                        if vg in vg_data and if_name not in vg_data[vg]['interfaces']:
                            vg_data[vg]['interfaces'].append(if_name)

                if vgs:
                    if_data[if_name] = vgs

        return vg_data, if_data

    def _print_table(self, output, headers, widths, data_rows):
        def format_row(cols):
            return " ".join([f"{col:<{widths[i]}}" for i, col in enumerate(cols)])

        res = [
            format_row(headers),
            "-" * (sum(widths) + len(widths) - 1)
        ]

        for row in data_rows:
            wrapped_cols = [textwrap.wrap(str(text), widths[i]) or [""] for i, text in enumerate(row)]
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

        headers = ['Group Name', 'Tags (Subinterface) [MAC-VRF]', 'Interfaces']
        widths = [20, 50, 30]
        data_rows = []

        for name in sorted(vg_data.keys()):
            if group_filter and name != group_filter:
                continue
            data = vg_data[name]

            tags_subs_mvrfs = []
            for sub, tag, mvrf in zip(data.get('subs', []), data.get('tags', []), data.get('mac_vrfs', [])):
                tags_subs_mvrfs.append(f"{tag}({sub})[{mvrf}]")

            tags_str = ", ".join(tags_subs_mvrfs) if tags_subs_mvrfs else "none"
            ifs_str = ", ".join(sorted(data.get('interfaces', []))) if data.get('interfaces') else "none"

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

            vg_entry = if_data[if_name]
            if isinstance(vg_entry, str):
                vg_names = [v.strip() for v in vg_entry.split(",") if v.strip()]
            elif isinstance(vg_entry, list):
                vg_names = vg_entry
            else:
                vg_names = []

            if not vg_names:
                data_rows.append([if_name, 'none', 'none'])
            else:
                subs_tags = []
                claimed_subs = set()
                for vg in vg_names:
                    data = vg_data.get(vg, {})
                    for sub, tag, mvrf in zip(data.get('subs', []), data.get('tags', []), data.get('mac_vrfs', [])):
                        if sub in claimed_subs:
                            continue
                        claimed_subs.add(sub)
                        subs_tags.append(f"{tag}({sub})[{mvrf}]")
                st_str = ", ".join(subs_tags) if subs_tags else "none"
                data_rows.append([if_name, ", ".join(vg_names), st_str])

        self._print_table(output, headers, widths, data_rows)
