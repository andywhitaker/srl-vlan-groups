import json
import re
import textwrap

try:
    from srlinux.mgmt.cli.cli_plugin import CliPlugin
    from srlinux.mgmt.cli.required_plugin import RequiredPlugin
    from srlinux.mgmt.cli.observer import Observer
    from srlinux.mgmt.cli.cli_mode import CliMode
    from srlinux.mgmt.cli.command_type import CommandType
    from srlinux.mgmt.cli.plugins import subtree_navigation
    from srlinux.syntax import Syntax
    from srlinux.schema.data_store import DataStore
    from srlinux.location import build_path
except ImportError:
    class CliPlugin:
        pass

    class RequiredPlugin:
        def __init__(self, name):
            self.name = name

    class Observer:
        def after_executing(self, state, input_line, commands):
            pass

    class CliMode:
        Candidate = "candidate"
        Running = "running"
        Show = "show"
        State = "state"
        Tools = "tools"

    class CommandType:
        YangBasedConfig = "yang_based_config"
        Normal = "normal"

    subtree_navigation = None

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
        Candidate = "candidate"
        Running = "running"
        State = "state"
        Baseline = "baseline"

    def build_path(path):
        return path


class TrackedUnit:
    def __init__(self, key, owner, update_paths=None, delete_path="", encap_signature=None, prior_user_paths=None):
        self.key = key
        self.owner = owner  # "vg" or "user"
        self.update_paths = list(update_paths or [])
        self.delete_path = delete_path
        self.encap_signature = encap_signature
        self.prior_user_paths = list(prior_user_paths or [])


class CandidateOwnershipLedger:
    def __init__(self):
        self.units = {}  # key -> TrackedUnit
        self.active_vg_units = set()
        self.active_if_vgs = {}  # if_name -> [vg_name, ...]
        self.seeded = False

    def reset(self):
        self.units.clear()
        self.active_vg_units.clear()
        self.active_if_vgs.clear()
        self.seeded = False


class VlanGroupsCandidateObserver(Observer):
    LEDGER_KEY = "vlan_groups_candidate_ledger"

    def __init__(self, plugin=None):
        self.plugin = plugin
        self._in_reconcile = False
        self._fallback_ledger = CandidateOwnershipLedger()

    def _get_ledger(self, state):
        custom_data = getattr(state, "custom_data", None)
        if not isinstance(custom_data, dict):
            return self._fallback_ledger
        if self.LEDGER_KEY not in custom_data:
            custom_data[self.LEDGER_KEY] = CandidateOwnershipLedger()
        return custom_data[self.LEDGER_KEY]

    def _clear_ledger(self, state):
        custom_data = getattr(state, "custom_data", None)
        if isinstance(custom_data, dict) and self.LEDGER_KEY in custom_data:
            custom_data[self.LEDGER_KEY].reset()
            del custom_data[self.LEDGER_KEY]
        self._fallback_ledger.reset()

    @staticmethod
    def _extract_val(val, default=None):
        if isinstance(val, dict):
            return val.get("value", default)
        if val is None:
            return default
        return val

    @classmethod
    def _clean_proto_json(cls, data):
        if isinstance(data, dict):
            if "value" in data and len(data) == 1:
                return cls._clean_proto_json(data["value"])
            out = {}
            for k, v in data.items():
                clean_k = k.split(":", 1)[-1].replace("_", "-")
                if clean_k == "vlan-id-uint16":
                    clean_k = "vlan-id"
                cleaned_v = cls._clean_proto_json(v)
                if isinstance(cleaned_v, dict) and len(cleaned_v) == 1 and clean_k in cleaned_v:
                    cleaned_v = cleaned_v[clean_k]
                out[clean_k] = cleaned_v
            return out
        if isinstance(data, list):
            return [cls._clean_proto_json(item) for item in data]
        if isinstance(data, str) and data.startswith("srl_nokia-"):
            return data.split(":", 1)[-1]
        return data

    @staticmethod
    def _unwrap_entry(item, wrapper_key):
        if not isinstance(item, dict):
            return {}
        merged = dict(item)
        inner = merged.pop(wrapper_key, None)
        if isinstance(inner, dict):
            for k, v in inner.items():
                merged.setdefault(k, v)
        return merged

    @staticmethod
    def _strip_value_suffix(path_str):
        # Splits '/interface[name=ethernet-1/1]/vlan-tagging:true' into ('/interface[name=ethernet-1/1]/vlan-tagging', 'true')
        last_slash = path_str.rfind("/")
        last_bracket = path_str.rfind("]")
        colon_idx = path_str.find(":", max(last_slash, last_bracket) + 1)
        if colon_idx != -1:
            return path_str[:colon_idx], path_str[colon_idx + 1:]
        return path_str, None

    @classmethod
    def _compress_path_strings(cls, path_strs):
        cleaned = []
        for p in path_strs:
            if not p:
                continue
            s = str(p).strip()
            if s and s != "/":
                if not s.startswith("/"):
                    s = "/" + s
                cleaned.append(s)

        result = []
        for i, p in enumerate(cleaned):
            base_p, _ = cls._strip_value_suffix(p)
            is_strict_ancestor = False
            for j, other in enumerate(cleaned):
                if i == j:
                    continue
                base_other, _ = cls._strip_value_suffix(other)
                if base_other != base_p and base_other.startswith(base_p + "/"):
                    is_strict_ancestor = True
                    break
            if not is_strict_ancestor and p not in result:
                result.append(p)
        return result

    def _get_subtree_paths(self, state, first_cmd, cmd_name):
        line_data = getattr(state, "line_custom_data", None) or {}
        if isinstance(line_data.get(cmd_name), dict) and "paths" in line_data[cmd_name]:
            return list(line_data[cmd_name]["paths"])

        if subtree_navigation is not None:
            try:
                node = getattr(first_cmd, "node", first_cmd)
                st = subtree_navigation.get_subtree(state, node)
                if st is not None and not getattr(st, "is_empty", True) and hasattr(st, "get_paths_and_local_args"):
                    return list(st.get_paths_and_local_args().paths)
            except Exception:
                pass

        for v in line_data.values():
            if hasattr(v, "get_paths_and_local_args") and not getattr(v, "is_empty", True):
                try:
                    return list(v.get_paths_and_local_args().paths)
                except Exception:
                    pass
        return []

    def _extract_touched_paths(self, state, commands):
        if not commands:
            return None, []

        if hasattr(commands, "nodes_without_root"):
            raw_cmd_list = list(commands.nodes_without_root)
        elif isinstance(commands, (list, tuple)):
            raw_cmd_list = list(commands)
        else:
            raw_cmd_list = []

        cmd_list = [c for c in raw_cmd_list if getattr(c, "name", "") != "/"]
        if not cmd_list:
            return None, []

        first_cmd = cmd_list[0]
        first_name = getattr(first_cmd, "name", "") or getattr(getattr(first_cmd, "node", None), "name", "")

        if first_name in ("enter", "candidate"):
            return "enter", []

        if first_name == "commit":
            sub_names = {getattr(c, "name", "") for c in cmd_list[1:]}
            if "validate" in sub_names or "check" in sub_names:
                return None, []
            return "commit", []

        if first_name == "discard":
            raw_paths = self._get_subtree_paths(state, first_cmd, "discard")
            if not raw_paths:
                return "discard_all", []
            return "discard", self._compress_path_strings(raw_paths)

        if first_name in ("set", "delete", "replace"):
            raw_paths = self._get_subtree_paths(state, first_cmd, first_name)
            return first_name, self._compress_path_strings(raw_paths)

        start_loc = getattr(state, "start_location", None)
        curr_prefix = str(getattr(start_loc, "path", "") or "").rstrip("/")
        if curr_prefix == "/":
            curr_prefix = ""

        yang_paths = []
        for cmd in raw_cmd_list:
            if getattr(cmd, "is_output_modifier", False):
                break
            cmd_name = getattr(cmd, "name", "") or getattr(getattr(cmd, "node", None), "name", "")
            if cmd_name == "/":
                curr_prefix = ""
                continue
            if cmd_name == "..":
                curr_prefix = curr_prefix.rsplit("/", 1)[0] if "/" in curr_prefix else ""
                continue

            node_obj = getattr(cmd, "node", cmd)
            cmd_type = getattr(node_obj, "type", None)
            if cmd_type != CommandType.YangBasedConfig:
                continue

            schema = getattr(node_obj, "schema_or_none", getattr(node_obj, "_schema", None))
            if schema is None and getattr(cmd, "has_schema", getattr(node_obj, "has_schema", True)) is not False:
                try:
                    schema = getattr(cmd, "schema", None)
                except BaseException:
                    schema = None
            if schema is None:
                continue

            if not getattr(schema, "is_node", True):
                if getattr(schema, "is_leaflist", False):
                    vals = cmd.get_or("values", []) if hasattr(cmd, "get_or") else []
                    if isinstance(vals, (str, int)):
                        vals = [vals]
                    for v in vals:
                        if curr_prefix:
                            yang_paths.append(f"{curr_prefix}/{cmd_name}:{v}")
                else:
                    val = cmd.get_or("value", "") if hasattr(cmd, "get_or") else ""
                    if curr_prefix:
                        yang_paths.append(f"{curr_prefix}/{cmd_name}:{val}")
                    else:
                        parent = getattr(cmd, "parent", None)
                        parent_path = str(getattr(parent, "path", "") or "").rstrip("/")
                        p_base, _ = self._strip_value_suffix(parent_path)
                        if p_base.endswith("/" + cmd_name):
                            yang_paths.append(parent_path)
                        else:
                            yang_paths.append(f"{p_base}/{cmd_name}:{val}")
            else:
                local_args = getattr(cmd, "local_arguments", None)
                if isinstance(local_args, dict) and cmd_name:
                    seg = cmd_name
                    for k, v in local_args.items():
                        seg += f"[{k}={v}]"
                    curr_prefix = f"{curr_prefix}/{seg}"
                    yang_paths.append(curr_prefix)
                else:
                    cmd_path = getattr(cmd, "path", None)
                    if cmd_path is not None:
                        p_str = str(cmd_path).rstrip("/")
                        if p_str and not p_str.startswith("/"):
                            p_str = "/" + p_str
                        if curr_prefix and not p_str.startswith(curr_prefix + "/") and p_str != curr_prefix:
                            curr_prefix = f"{curr_prefix}{p_str}"
                        else:
                            curr_prefix = p_str
                        yang_paths.append(curr_prefix)

        if yang_paths:
            return "yang", self._compress_path_strings(yang_paths)

        return None, []

    def _fetch_datastore_trees(self, state, data_store):
        try:
            vg_raw = state.server.get_proto_json(
                build_path("/vlan-groups"),
                recursive=True,
                data_store=data_store,
                include_field_defaults=True,
            )
        except Exception:
            vg_raw = None

        try:
            if_raw = state.server.get_proto_json(
                build_path("/interface"),
                recursive=True,
                data_store=data_store,
                include_field_defaults=False,
            )
        except Exception:
            if_raw = None

        try:
            ni_raw = state.server.get_proto_json(
                build_path("/network-instance"),
                recursive=True,
                data_store=data_store,
                include_field_defaults=False,
            )
        except Exception:
            ni_raw = None

        vg_defs = {}
        if vg_raw:
            try:
                vg_parsed = self._clean_proto_json(json.loads(vg_raw))
            except Exception:
                vg_parsed = {}
            vg_container = vg_parsed.get("vlan-groups", vg_parsed) if isinstance(vg_parsed, dict) else {}
            group_list = vg_container.get("vlan-group", []) if isinstance(vg_container, dict) else []
            if isinstance(group_list, dict):
                group_list = [group_list]
            for g in group_list:
                g_merged = self._unwrap_entry(g, "vlan-group")
                g_name = str(self._extract_val(g_merged.get("name"), "") or "")
                if not g_name:
                    continue
                g_entry = {}
                if "untagged" in g_merged and isinstance(g_merged["untagged"], dict):
                    ut = g_merged["untagged"]
                    sid_raw = self._extract_val(ut.get("subinterface-id"), 1)
                    mvrf_raw = self._extract_val(ut.get("mac-vrf"), "vlan-1")
                    g_entry["untagged"] = {
                        "subinterface-id": int(sid_raw) if str(sid_raw).isdigit() else 1,
                        "mac-vrf": str(mvrf_raw or "vlan-1"),
                    }
                if "tag" in g_merged:
                    tag_list = g_merged["tag"]
                    if isinstance(tag_list, dict):
                        tag_list = [tag_list]
                    tags = []
                    for t in tag_list:
                        t_merged = self._unwrap_entry(t, "tag")
                        vid_raw = self._extract_val(t_merged.get("vlan-id"), None)
                        if vid_raw is None or str(vid_raw) == "":
                            continue
                        vid = int(vid_raw) if str(vid_raw).isdigit() else vid_raw
                        sid_raw = self._extract_val(t_merged.get("subinterface-id"), vid)
                        sid = int(sid_raw) if str(sid_raw).isdigit() else sid_raw
                        mvrf = str(self._extract_val(t_merged.get("mac-vrf"), f"vlan-{vid}") or f"vlan-{vid}")
                        tags.append({
                            "vlan-id": vid,
                            "subinterface-id": sid,
                            "mac-vrf": mvrf,
                        })
                    tags.sort(key=lambda x: x["vlan-id"] if isinstance(x["vlan-id"], int) else 0)
                    g_entry["tag"] = tags
                vg_defs[g_name] = g_entry

        if_vgs = {}
        if_map = {}
        if if_raw:
            try:
                if_parsed = self._clean_proto_json(json.loads(if_raw))
            except Exception:
                if_parsed = {}
            if_list = if_parsed.get("interface", []) if isinstance(if_parsed, dict) else []
            if isinstance(if_list, dict):
                if_list = [if_list]
            for i in if_list:
                i_merged = self._unwrap_entry(i, "interface")
                if_name = str(self._extract_val(i_merged.get("name"), "") or "")
                if not if_name:
                    continue
                # Normalize subinterfaces list into a dict map as well
                subs = i_merged.get("subinterface", [])
                if isinstance(subs, dict):
                    subs = [subs]
                norm_subs = []
                for s in subs:
                    norm_subs.append(self._unwrap_entry(s, "subinterface"))
                if norm_subs:
                    i_merged["subinterface"] = norm_subs
                if_map[if_name] = i_merged

                vgs_container = i_merged.get("vlan-groups", {})
                if isinstance(vgs_container, dict):
                    vg_list = vgs_container.get("vlan-group", [])
                    if isinstance(vg_list, dict):
                        vg_list = [vg_list]
                    names = []
                    for vg_item in vg_list:
                        if isinstance(vg_item, dict):
                            vg_item_merged = self._unwrap_entry(vg_item, "vlan-group")
                            v_name = str(self._extract_val(vg_item_merged.get("name"), "") or "")
                        else:
                            v_name = str(vg_item or "")
                        if v_name and v_name not in names:
                            names.append(v_name)
                    if names:
                        if_vgs[if_name] = names

        ni_map = {}
        if ni_raw:
            try:
                ni_parsed = self._clean_proto_json(json.loads(ni_raw))
            except Exception:
                ni_parsed = {}
            ni_list = ni_parsed.get("network-instance", []) if isinstance(ni_parsed, dict) else []
            if isinstance(ni_list, dict):
                ni_list = [ni_list]
            for n in ni_list:
                n_merged = self._unwrap_entry(n, "network-instance")
                ni_name = str(self._extract_val(n_merged.get("name"), "") or "")
                if not ni_name:
                    continue
                ni_ifs = n_merged.get("interface", [])
                if isinstance(ni_ifs, dict):
                    ni_ifs = [ni_ifs]
                norm_ni_ifs = [self._unwrap_entry(ni_if, "interface") for ni_if in ni_ifs]
                if norm_ni_ifs:
                    n_merged["interface"] = norm_ni_ifs
                ni_map[ni_name] = n_merged

        return vg_defs, if_vgs, if_map, ni_map

    def _is_preexisting_conflicting_sub(self, ledger, cand_if_map, cand_ni_map, if_name, sid, mvrf, encap_sig):
        if ledger is None or f"sub:{if_name}:{sid}" in ledger.active_vg_units:
            return False

        sub_dict = self._find_sub((cand_if_map or {}).get(if_name, {}), sid)
        sub_unit = ledger.units.get(f"sub:{if_name}:{sid}")
        if sub_dict is None and (sub_unit is None or sub_unit.owner != "user"):
            return False

        # 1. Explicit non-bridged subinterface type
        if sub_dict is not None and "type" in sub_dict and sub_dict["type"] != "bridged":
            return True
        st_unit = ledger.units.get(f"sub_type:{if_name}:{sid}")
        if st_unit is not None and st_unit.owner == "user" and any(not p.endswith(":bridged") for p in st_unit.update_paths):
            return True

        # 2. Explicit non-matching VLAN encapsulation
        if sub_dict is not None and "vlan" in sub_dict and sub_dict["vlan"]:
            if not self._sub_encap_matches(sub_dict, encap_sig):
                return True

        # 3. Routed L3 subinterface configuration
        if sub_dict is not None and ("ipv4" in sub_dict or "ipv6" in sub_dict):
            return True

        # 4. Already bound to a different network-instance or non-mac-vrf network-instance
        if_sub_name = f"{if_name}.{sid}"
        for ni_name, ni_dict in (cand_ni_map or {}).items():
            for ni_if in ni_dict.get("interface", []):
                if str(ni_if.get("name", "")) == if_sub_name:
                    if ni_name != mvrf or ni_dict.get("type") not in (None, "mac-vrf"):
                        return True

        return False

    def _compute_desired_vg_units(self, vg_defs, if_vgs, ledger=None, cand_if_map=None, cand_ni_map=None):
        desired = {}
        for if_name, vg_names in sorted(if_vgs.items()):
            valid_vgs = [vg for vg in vg_names if vg in vg_defs]
            if not valid_vgs:
                continue

            claimed_sids = set()
            if_units = {}
            for vg_name in valid_vgs:
                g_cfg = vg_defs[vg_name]
                specs = []
                if "untagged" in g_cfg:
                    sid = str(g_cfg["untagged"].get("subinterface-id", 1))
                    mvrf = str(g_cfg["untagged"].get("mac-vrf", "vlan-1"))
                    specs.append((
                        sid,
                        mvrf,
                        "untagged",
                        [f"/interface[name={if_name}]/subinterface[index={sid}]/vlan/encap/untagged"],
                    ))
                for tag in g_cfg.get("tag", []):
                    vid = tag["vlan-id"]
                    sid = str(tag.get("subinterface-id", vid))
                    mvrf = str(tag.get("mac-vrf", f"vlan-{vid}"))
                    specs.append((
                        sid,
                        mvrf,
                        ("single-tagged", int(vid) if str(vid).isdigit() else vid),
                        [f"/interface[name={if_name}]/subinterface[index={sid}]/vlan/encap/single-tagged/vlan-id:{vid}"],
                    ))

                for sid, mvrf, encap_sig, encap_paths in specs:
                    if sid in claimed_sids:
                        continue
                    claimed_sids.add(sid)

                    if self._is_preexisting_conflicting_sub(
                        ledger, cand_if_map, cand_ni_map, if_name, sid, mvrf, encap_sig
                    ):
                        continue

                    if_units[f"sub:{if_name}:{sid}"] = TrackedUnit(
                        key=f"sub:{if_name}:{sid}",
                        owner="vg",
                        update_paths=[f"/interface[name={if_name}]/subinterface[index={sid}]"],
                        delete_path=f"/interface[name={if_name}]/subinterface[index={sid}]",
                    )
                    if_units[f"sub_type:{if_name}:{sid}"] = TrackedUnit(
                        key=f"sub_type:{if_name}:{sid}",
                        owner="vg",
                        update_paths=[f"/interface[name={if_name}]/subinterface[index={sid}]/type:bridged"],
                        delete_path=f"/interface[name={if_name}]/subinterface[index={sid}]/type",
                    )
                    if_units[f"sub_encap:{if_name}:{sid}"] = TrackedUnit(
                        key=f"sub_encap:{if_name}:{sid}",
                        owner="vg",
                        update_paths=encap_paths,
                        delete_path=f"/interface[name={if_name}]/subinterface[index={sid}]/vlan",
                        encap_signature=encap_sig,
                    )
                    if_units[f"ni:{mvrf}"] = TrackedUnit(
                        key=f"ni:{mvrf}",
                        owner="vg",
                        update_paths=[f"/network-instance[name={mvrf}]"],
                        delete_path=f"/network-instance[name={mvrf}]",
                    )
                    if_units[f"ni_type:{mvrf}"] = TrackedUnit(
                        key=f"ni_type:{mvrf}",
                        owner="vg",
                        update_paths=[f"/network-instance[name={mvrf}]/type:mac-vrf"],
                        delete_path=f"/network-instance[name={mvrf}]/type",
                    )
                    if_units[f"ni_if:{mvrf}:{if_name}.{sid}"] = TrackedUnit(
                        key=f"ni_if:{mvrf}:{if_name}.{sid}",
                        owner="vg",
                        update_paths=[f"/network-instance[name={mvrf}]/interface[name={if_name}.{sid}]"],
                        delete_path=f"/network-instance[name={mvrf}]/interface[name={if_name}.{sid}]",
                    )

            if if_units or not claimed_sids:
                desired[f"if:{if_name}"] = TrackedUnit(
                    key=f"if:{if_name}",
                    owner="vg",
                    update_paths=[f"/interface[name={if_name}]"],
                    delete_path=f"/interface[name={if_name}]",
                )
                desired[f"if_vt:{if_name}"] = TrackedUnit(
                    key=f"if_vt:{if_name}",
                    owner="vg",
                    update_paths=[f"/interface[name={if_name}]/vlan-tagging:true"],
                    delete_path=f"/interface[name={if_name}]/vlan-tagging",
                )
                desired.update(if_units)
        return desired

    @staticmethod
    def _sub_encap_matches(sub_dict, encap_sig):
        vlan = sub_dict.get("vlan", {})
        if not isinstance(vlan, dict):
            return False
        encap = vlan.get("encap", {})
        if not isinstance(encap, dict):
            return False
        if encap_sig == "untagged":
            return "untagged" in encap and "single-tagged" not in encap
        if isinstance(encap_sig, tuple) and len(encap_sig) == 2 and encap_sig[0] == "single-tagged":
            st = encap.get("single-tagged", {})
            if not isinstance(st, dict):
                return False
            vid_val = st.get("vlan-id", st.get("vlan-id-uint16"))
            while isinstance(vid_val, dict) and len(vid_val) == 1:
                vid_val = next(iter(vid_val.values()))
            return str(vid_val) == str(encap_sig[1])
        return False

    def _seed_from_baseline(self, state, ledger):
        vg_defs, if_vgs, if_map, ni_map = self._fetch_datastore_trees(state, DataStore.Baseline)

        desired = self._compute_desired_vg_units(vg_defs, if_vgs)
        for key, unit in desired.items():
            if key.startswith("if:"):
                # Existing baseline interface stays user-owned so interface entry itself isn't deleted
                unit.owner = "user"
            elif key.startswith("sub:"):
                _, if_name, sid = key.split(":", 2)
                sub_dict = self._find_sub(if_map.get(if_name, {}), sid)
                if sub_dict and (set(sub_dict.keys()) - {"index", "type", "vlan"}):
                    unit.owner = "user"
            elif key.startswith("sub_type:"):
                _, if_name, sid = key.split(":", 2)
                sub_dict = self._find_sub(if_map.get(if_name, {}), sid)
                if sub_dict and "type" in sub_dict and sub_dict["type"] != "bridged":
                    unit.owner = "user"
                    if f"sub:{if_name}:{sid}" in desired:
                        desired[f"sub:{if_name}:{sid}"].owner = "user"
            elif key.startswith("sub_encap:"):
                _, if_name, sid = key.split(":", 2)
                sub_dict = self._find_sub(if_map.get(if_name, {}), sid)
                if sub_dict and not self._sub_encap_matches(sub_dict, unit.encap_signature):
                    unit.owner = "user"
                    if f"sub:{if_name}:{sid}" in desired:
                        desired[f"sub:{if_name}:{sid}"].owner = "user"
            elif key.startswith("ni:"):
                _, mvrf = key.split(":", 1)
                ni_dict = ni_map.get(mvrf, {})
                if set(ni_dict.keys()) - {"name", "type", "interface"}:
                    unit.owner = "user"
                else:
                    for ni_if in ni_dict.get("interface", []):
                        if_sub_name = str(ni_if.get("name", ""))
                        if if_sub_name and f"ni_if:{mvrf}:{if_sub_name}" not in desired:
                            unit.owner = "user"
                            break
            ledger.units[key] = unit

        # Record any existing non-VG baseline interfaces, subinterfaces, and network-instances as user-owned
        self._capture_preexisting_candidate_units(ledger, list(if_map.keys()), if_map, ni_map, if_map)

        ledger.active_vg_units = set(desired.keys())
        ledger.active_if_vgs = {k: list(v) for k, v in if_vgs.items()}
        ledger.seeded = True

    @staticmethod
    def _find_sub(if_dict, sid):
        for s in if_dict.get("subinterface", []):
            if isinstance(s, dict) and str(s.get("index")) == str(sid):
                return s
        return None

    def _promote_unit(self, ledger, key, update_path, delete_path):
        if key in ledger.units:
            ledger.units[key].owner = "user"
            if update_path:
                ledger.units[key].update_paths = [update_path]
            ledger.units[key].prior_user_paths = []
        else:
            ledger.units[key] = TrackedUnit(
                key=key,
                owner="user",
                update_paths=[update_path] if update_path else [],
                delete_path=delete_path,
            )

    def _record_manual_touches(self, ledger, op, touched_paths):
        vg_touched = False
        for p in touched_paths:
            base_p, val = self._strip_value_suffix(p)

            if base_p == "/vlan-groups" or base_p.startswith("/vlan-groups/"):
                vg_touched = True
                continue

            if_match = re.match(r"^/interface\[name=([^\]]+)\](?:/(.*))?$", base_p)
            if if_match:
                if_name = if_match.group(1)
                rest = if_match.group(2) or ""

                if rest == "vlan-groups" or rest.startswith("vlan-groups/") or rest == "vlan_groups" or rest.startswith("vlan_groups/"):
                    vg_touched = True
                    continue

                if op in ("delete", "discard") and rest == "":
                    for k in list(ledger.units.keys()):
                        if k in (f"if:{if_name}", f"if_vt:{if_name}") or k.startswith((f"sub:{if_name}:", f"sub_type:{if_name}:", f"sub_encap:{if_name}:")):
                            if k in ledger.active_vg_units:
                                ledger.units[k].owner = "vg"
                                ledger.units[k].prior_user_paths = []
                            else:
                                ledger.units.pop(k, None)
                    if if_name in ledger.active_if_vgs:
                        vg_touched = True
                    continue

                if op in ("set", "replace", "yang"):
                    # Only promote if:<if_name> if user explicitly set/replaced the interface entry or if it didn't exist in ledger yet
                    if (rest == "" and op in ("set", "replace")) or f"if:{if_name}" not in ledger.units:
                        self._promote_unit(
                            ledger,
                            f"if:{if_name}",
                            f"/interface[name={if_name}]",
                            f"/interface[name={if_name}]",
                        )

                    if rest in ("vlan-tagging", "vlan_tagging"):
                        vt_val = val if val is not None else "true"
                        self._promote_unit(
                            ledger,
                            f"if_vt:{if_name}",
                            f"/interface[name={if_name}]/vlan-tagging:{vt_val}",
                            f"/interface[name={if_name}]/vlan-tagging",
                        )
                        continue

                    sub_match = re.match(r"^subinterface\[index=([^\]]+)\](?:/(.*))?$", rest)
                    if sub_match:
                        sid = str(sub_match.group(1))
                        sub_rest = sub_match.group(2) or ""
                        if (sub_rest == "" and op in ("set", "replace")) or f"sub:{if_name}:{sid}" not in ledger.units:
                            self._promote_unit(
                                ledger,
                                f"sub:{if_name}:{sid}",
                                f"/interface[name={if_name}]/subinterface[index={sid}]",
                                f"/interface[name={if_name}]/subinterface[index={sid}]",
                            )
                        if sub_rest == "type":
                            t_val = val if val is not None else "bridged"
                            self._promote_unit(
                                ledger,
                                f"sub_type:{if_name}:{sid}",
                                f"/interface[name={if_name}]/subinterface[index={sid}]/type:{t_val}",
                                f"/interface[name={if_name}]/subinterface[index={sid}]/type",
                            )
                        elif sub_rest == "vlan" or sub_rest.startswith("vlan/"):
                            self._promote_unit(
                                ledger,
                                f"sub_encap:{if_name}:{sid}",
                                p,
                                f"/interface[name={if_name}]/subinterface[index={sid}]/vlan",
                            )
                elif op in ("delete", "discard"):
                    if rest in ("vlan-tagging", "vlan_tagging"):
                        ledger.units.pop(f"if_vt:{if_name}", None)
                    else:
                        sub_match = re.match(r"^subinterface\[index=([^\]]+)\](?:/(.*))?$", rest)
                        if sub_match:
                            sid = str(sub_match.group(1))
                            sub_rest = sub_match.group(2) or ""
                            if sub_rest == "":
                                ledger.units.pop(f"sub:{if_name}:{sid}", None)
                                ledger.units.pop(f"sub_type:{if_name}:{sid}", None)
                                ledger.units.pop(f"sub_encap:{if_name}:{sid}", None)
                            elif sub_rest == "type":
                                if f"sub_type:{if_name}:{sid}" in ledger.active_vg_units and f"sub_type:{if_name}:{sid}" in ledger.units:
                                    ledger.units[f"sub_type:{if_name}:{sid}"].owner = "vg"
                                else:
                                    ledger.units.pop(f"sub_type:{if_name}:{sid}", None)
                            elif sub_rest == "vlan" or sub_rest.startswith("vlan/"):
                                if f"sub_encap:{if_name}:{sid}" in ledger.active_vg_units and f"sub_encap:{if_name}:{sid}" in ledger.units:
                                    ledger.units[f"sub_encap:{if_name}:{sid}"].owner = "vg"
                                else:
                                    ledger.units.pop(f"sub_encap:{if_name}:{sid}", None)
                continue

            ni_match = re.match(r"^/network-instance\[name=([^\]]+)\](?:/(.*))?$", base_p)
            if ni_match:
                ni_name = ni_match.group(1)
                ni_rest = ni_match.group(2) or ""
                if op in ("set", "replace", "yang"):
                    if (ni_rest == "" and op in ("set", "replace")) or f"ni:{ni_name}" not in ledger.units:
                        self._promote_unit(
                            ledger,
                            f"ni:{ni_name}",
                            f"/network-instance[name={ni_name}]",
                            f"/network-instance[name={ni_name}]",
                        )
                    if ni_rest == "type":
                        t_val = val if val is not None else "mac-vrf"
                        self._promote_unit(
                            ledger,
                            f"ni_type:{ni_name}",
                            f"/network-instance[name={ni_name}]/type:{t_val}",
                            f"/network-instance[name={ni_name}]/type",
                        )
                    else:
                        ni_if_match = re.match(r"^interface\[name=([^\]]+)\]", ni_rest)
                        if ni_if_match:
                            if_sub = ni_if_match.group(1)
                            self._promote_unit(
                                ledger,
                                f"ni_if:{ni_name}:{if_sub}",
                                f"/network-instance[name={ni_name}]/interface[name={if_sub}]",
                                f"/network-instance[name={ni_name}]/interface[name={if_sub}]",
                            )
                elif op in ("delete", "discard"):
                    if ni_rest == "":
                        ledger.units.pop(f"ni:{ni_name}", None)
                        ledger.units.pop(f"ni_type:{ni_name}", None)
                    elif ni_rest == "type":
                        ledger.units.pop(f"ni_type:{ni_name}", None)
                    else:
                        ni_if_match = re.match(r"^interface\[name=([^\]]+)\]", ni_rest)
                        if ni_if_match:
                            if_sub = ni_if_match.group(1)
                            ledger.units.pop(f"ni_if:{ni_name}:{if_sub}", None)

        return vg_touched

    def _capture_preexisting_candidate_units(self, ledger, if_vgs, cand_if_map, cand_ni_map, base_if_map):
        for if_name in if_vgs:
            if_dict = cand_if_map.get(if_name, {})
            if f"if:{if_name}" not in ledger.units:
                non_vg_keys = set(if_dict.keys()) - {"name", "vlan-groups", "vlan_groups"}
                existed_before = (if_name in base_if_map) or bool(non_vg_keys)
                ledger.units[f"if:{if_name}"] = TrackedUnit(
                    key=f"if:{if_name}",
                    owner="user" if existed_before else "vg",
                    update_paths=[f"/interface[name={if_name}]"],
                    delete_path=f"/interface[name={if_name}]",
                )

            if f"if_vt:{if_name}" not in ledger.units and "vlan-tagging" in if_dict:
                vt_val = if_dict.get("vlan-tagging")
                if vt_val is True or str(vt_val).lower() == "true":
                    ledger.units[f"if_vt:{if_name}"] = TrackedUnit(
                        key=f"if_vt:{if_name}",
                        owner="user",
                        update_paths=[f"/interface[name={if_name}]/vlan-tagging:true"],
                        delete_path=f"/interface[name={if_name}]/vlan-tagging",
                    )
                else:
                    ledger.units[f"if_vt:{if_name}"] = TrackedUnit(
                        key=f"if_vt:{if_name}",
                        owner="vg",
                        update_paths=[f"/interface[name={if_name}]/vlan-tagging:true"],
                        delete_path=f"/interface[name={if_name}]/vlan-tagging",
                        prior_user_paths=[f"/interface[name={if_name}]/vlan-tagging:false"],
                    )

            for sub_dict in if_dict.get("subinterface", []):
                if not isinstance(sub_dict, dict):
                    continue
                sid = str(sub_dict.get("index", ""))
                if not sid:
                    continue
                if f"sub:{if_name}:{sid}" not in ledger.units:
                    ledger.units[f"sub:{if_name}:{sid}"] = TrackedUnit(
                        key=f"sub:{if_name}:{sid}",
                        owner="user",
                        update_paths=[f"/interface[name={if_name}]/subinterface[index={sid}]"],
                        delete_path=f"/interface[name={if_name}]/subinterface[index={sid}]",
                    )
                if "type" in sub_dict and f"sub_type:{if_name}:{sid}" not in ledger.units:
                    ledger.units[f"sub_type:{if_name}:{sid}"] = TrackedUnit(
                        key=f"sub_type:{if_name}:{sid}",
                        owner="user",
                        update_paths=[f"/interface[name={if_name}]/subinterface[index={sid}]/type:{sub_dict['type']}"],
                        delete_path=f"/interface[name={if_name}]/subinterface[index={sid}]/type",
                    )
                if "vlan" in sub_dict and f"sub_encap:{if_name}:{sid}" not in ledger.units:
                    ledger.units[f"sub_encap:{if_name}:{sid}"] = TrackedUnit(
                        key=f"sub_encap:{if_name}:{sid}",
                        owner="user",
                        update_paths=[],
                        delete_path=f"/interface[name={if_name}]/subinterface[index={sid}]/vlan",
                    )

        for ni_name, ni_dict in cand_ni_map.items():
            if f"ni:{ni_name}" not in ledger.units:
                ledger.units[f"ni:{ni_name}"] = TrackedUnit(
                    key=f"ni:{ni_name}",
                    owner="user",
                    update_paths=[f"/network-instance[name={ni_name}]"],
                    delete_path=f"/network-instance[name={ni_name}]",
                )
            if "type" in ni_dict and f"ni_type:{ni_name}" not in ledger.units:
                ledger.units[f"ni_type:{ni_name}"] = TrackedUnit(
                    key=f"ni_type:{ni_name}",
                    owner="user",
                    update_paths=[f"/network-instance[name={ni_name}]/type:{ni_dict['type']}"],
                    delete_path=f"/network-instance[name={ni_name}]/type",
                )
            for ni_if in ni_dict.get("interface", []):
                if_sub = str(ni_if.get("name", ""))
                if if_sub and f"ni_if:{ni_name}:{if_sub}" not in ledger.units:
                    ledger.units[f"ni_if:{ni_name}:{if_sub}"] = TrackedUnit(
                        key=f"ni_if:{ni_name}:{if_sub}",
                        owner="user",
                        update_paths=[f"/network-instance[name={ni_name}]/interface[name={if_sub}]"],
                        delete_path=f"/network-instance[name={ni_name}]/interface[name={if_sub}]",
                    )

    @staticmethod
    def _filter_redundant_deletes(delete_paths):
        unique = []
        for p in delete_paths:
            if p and p not in unique:
                unique.append(p)
        filtered = []
        for p in unique:
            if not any(other != p and p.startswith(other + "/") for other in unique):
                filtered.append(p)
        return filtered

    def _reconcile_candidate(self, state, ledger):
        vg_defs, if_vgs, cand_if_map, cand_ni_map = self._fetch_datastore_trees(state, DataStore.Candidate)
        _, _, base_if_map, _ = self._fetch_datastore_trees(state, DataStore.Baseline)

        self._capture_preexisting_candidate_units(ledger, if_vgs, cand_if_map, cand_ni_map, base_if_map)
        desired = self._compute_desired_vg_units(vg_defs, if_vgs, ledger, cand_if_map, cand_ni_map)

        updates_to_apply = []
        deletes_to_apply = []

        # 1. Process desired units
        for key, d_unit in desired.items():
            existing = ledger.units.get(key)
            if existing is not None and existing.owner == "user":
                if key.startswith("if_vt:") and any(p.endswith(":false") for p in existing.update_paths):
                    existing.owner = "vg"
                    existing.prior_user_paths = list(existing.update_paths)
                    existing.update_paths = list(d_unit.update_paths)
                    updates_to_apply.extend(d_unit.update_paths)
                else:
                    continue
            else:
                if key.startswith("sub_encap:") and existing is not None and existing.encap_signature is not None:
                    if existing.encap_signature != d_unit.encap_signature:
                        if existing.encap_signature == "untagged" and d_unit.encap_signature != "untagged":
                            deletes_to_apply.append(f"{d_unit.delete_path}/encap/untagged")
                        elif existing.encap_signature != "untagged" and d_unit.encap_signature == "untagged":
                            deletes_to_apply.append(f"{d_unit.delete_path}/encap/single-tagged")
                if existing is not None and existing.prior_user_paths:
                    d_unit.prior_user_paths = list(existing.prior_user_paths)
                ledger.units[key] = d_unit
                updates_to_apply.extend(d_unit.update_paths)

        # 2. Identify removed VG units
        removed_keys = [k for k in ledger.active_vg_units if k not in desired]
        retained_vg_keys = set()

        # Track which subinterfaces and network-instance bindings are being deleted
        deleted_subs = set()  # (if_name, sid)
        deleted_sub_encaps = set()  # (if_name, sid)
        deleted_ni_ifs = set()  # (mvrf, if_sub)

        # 2a. Handle ni_if removals first
        for key in removed_keys:
            if not key.startswith("ni_if:"):
                continue
            unit = ledger.units.get(key)
            if unit is None or unit.owner != "vg":
                continue
            _, mvrf, if_sub = key.split(":", 2)
            deleted_ni_ifs.add((mvrf, if_sub))
            deletes_to_apply.append(unit.delete_path)
            ledger.units.pop(key, None)

        surviving_user_ni_ifs = set()
        for k, u in ledger.units.items():
            if k.startswith("ni_if:") and u.owner == "user":
                _, _, if_sub = k.split(":", 2)
                surviving_user_ni_ifs.add(if_sub)

        # 2b. Handle sub removals
        for key in removed_keys:
            if not key.startswith("sub:"):
                continue
            unit = ledger.units.get(key)
            if unit is None or unit.owner != "vg":
                continue
            _, if_name, sid = key.split(":", 2)
            sub_dict = self._find_sub(cand_if_map.get(if_name, {}), sid) or {}

            extra_sub_keys = set(sub_dict.keys()) - {"index", "type", "vlan"}
            vlan_dict = sub_dict.get("vlan") if isinstance(sub_dict.get("vlan"), dict) else {}
            extra_vlan_keys = set(vlan_dict.keys()) - {"encap"}
            st_unit = ledger.units.get(f"sub_type:{if_name}:{sid}")
            se_unit = ledger.units.get(f"sub_encap:{if_name}:{sid}")
            has_user_child = (
                bool(extra_sub_keys)
                or bool(extra_vlan_keys)
                or (st_unit is not None and st_unit.owner == "user")
                or (se_unit is not None and se_unit.owner == "user")
                or (f"{if_name}.{sid}" in surviving_user_ni_ifs)
            )
            if has_user_child:
                retained_vg_keys.add(key)
            else:
                deleted_subs.add((if_name, sid))
                deletes_to_apply.append(unit.delete_path)
                ledger.units.pop(key, None)

        # 2c. Helper to check if an interface will still require vlan-tagging: true
        def _if_requires_vlan_tagging(if_name):
            if f"if_vt:{if_name}" in desired:
                return True
            vt_unit = ledger.units.get(f"if_vt:{if_name}")
            if vt_unit is not None and vt_unit.owner == "user" and not any(p.endswith(":false") for p in vt_unit.update_paths):
                return True
            if_dict = cand_if_map.get(if_name, {})
            for s in if_dict.get("subinterface", []):
                s_idx = str(s.get("index", ""))
                if (if_name, s_idx) in deleted_subs:
                    continue
                se_key = f"sub_encap:{if_name}:{s_idx}"
                se_unit = ledger.units.get(se_key)
                if se_unit is not None and se_unit.owner == "user":
                    return True
                if se_key not in removed_keys and "vlan" in s and s["vlan"]:
                    return True
            return False

        # 2d. Now handle sub_encap and sub_type on surviving subinterfaces
        for key in removed_keys:
            if not key.startswith("sub_encap:"):
                continue
            unit = ledger.units.get(key)
            if unit is None or unit.owner != "vg":
                continue
            _, if_name, sid = key.split(":", 2)
            if (if_name, sid) in deleted_subs:
                deleted_sub_encaps.add((if_name, sid))
                ledger.units.pop(key, None)
            elif _if_requires_vlan_tagging(if_name):
                retained_vg_keys.add(key)
            else:
                deleted_sub_encaps.add((if_name, sid))
                deletes_to_apply.append(unit.delete_path)
                ledger.units.pop(key, None)

        for key in removed_keys:
            if not key.startswith("sub_type:"):
                continue
            unit = ledger.units.get(key)
            if unit is None or unit.owner != "vg":
                continue
            _, if_name, sid = key.split(":", 2)
            if (if_name, sid) in deleted_subs:
                ledger.units.pop(key, None)
            else:
                # Schema guard: if subinterface retains untagged encap or is bound to mac-vrf, keep type:bridged
                sub_dict = self._find_sub(cand_if_map.get(if_name, {}), sid)
                has_untagged = (
                    (if_name, sid) not in deleted_sub_encaps
                    and sub_dict is not None
                    and "untagged" in sub_dict.get("vlan", {}).get("encap", {})
                )
                in_user_ni = f"{if_name}.{sid}" in surviving_user_ni_ifs
                if has_untagged or in_user_ni:
                    retained_vg_keys.add(key)
                else:
                    deletes_to_apply.append(unit.delete_path)
                    ledger.units.pop(key, None)

        # 2e. Now handle network-instances (ni and ni_type)
        deleted_nis = set()
        for key in removed_keys:
            if not key.startswith("ni:"):
                continue
            unit = ledger.units.get(key)
            if unit is None or unit.owner != "vg":
                continue
            _, mvrf = key.split(":", 1)
            ni_dict = cand_ni_map.get(mvrf, {})
            extra_keys = set(ni_dict.keys()) - {"name", "type", "interface"}
            surviving_ifs = [
                ni_if for ni_if in ni_dict.get("interface", [])
                if (mvrf, str(ni_if.get("name", ""))) not in deleted_ni_ifs
            ]
            nt_unit = ledger.units.get(f"ni_type:{mvrf}")
            has_user_ni_type = nt_unit is not None and nt_unit.owner == "user"
            if not extra_keys and not surviving_ifs and not has_user_ni_type:
                deleted_nis.add(mvrf)
                deletes_to_apply.append(unit.delete_path)
                ledger.units.pop(key, None)
            else:
                retained_vg_keys.add(key)

        for key in removed_keys:
            if not key.startswith("ni_type:"):
                continue
            unit = ledger.units.get(key)
            if unit is None or unit.owner != "vg":
                continue
            _, mvrf = key.split(":", 1)
            if mvrf in deleted_nis:
                ledger.units.pop(key, None)
            else:
                retained_vg_keys.add(key)

        # 2f. Now handle if_vt (vlan-tagging) with schema validity guard
        for key in removed_keys:
            if not key.startswith("if_vt:"):
                continue
            unit = ledger.units.get(key)
            if unit is None or unit.owner != "vg":
                continue
            _, if_name = key.split(":", 1)
            if_dict = cand_if_map.get(if_name, {})
            # Check if any surviving subinterface on if_name still has vlan encap
            any_surviving_vlan = False
            for sub_dict in if_dict.get("subinterface", []):
                sid = str(sub_dict.get("index", ""))
                if (if_name, sid) in deleted_subs or (if_name, sid) in deleted_sub_encaps:
                    continue
                if "vlan" in sub_dict and sub_dict["vlan"]:
                    any_surviving_vlan = True
                    break
                encap_unit = ledger.units.get(f"sub_encap:{if_name}:{sid}")
                if encap_unit is not None and encap_unit.owner == "user":
                    any_surviving_vlan = True
                    break

            if not any_surviving_vlan:
                if unit.prior_user_paths:
                    updates_to_apply.extend(unit.prior_user_paths)
                    unit.owner = "user"
                    unit.update_paths = list(unit.prior_user_paths)
                    unit.prior_user_paths = []
                else:
                    deletes_to_apply.append(unit.delete_path)
                    ledger.units.pop(key, None)
            else:
                retained_vg_keys.add(key)

        # 2g. Finally handle interface container cleanup (if:<if_name> and /interface[name=<if_name>]/vlan-groups)
        loc_str = str(getattr(getattr(state, "location", None), "path", "") or "")
        for if_name in set(ledger.active_if_vgs.keys()) | {k.split(":", 1)[1] for k in removed_keys if k.startswith("if:")}:
            if if_name in if_vgs:
                continue
            if if_name not in cand_if_map:
                ledger.units.pop(f"if:{if_name}", None)
                continue
            if_unit = ledger.units.get(f"if:{if_name}")
            if_dict = cand_if_map.get(if_name, {})
            surviving_subs = [
                s for s in if_dict.get("subinterface", [])
                if (if_name, str(s.get("index", ""))) not in deleted_subs
            ]
            other_if_keys = set(if_dict.keys()) - {"name", "vlan-groups", "vlan_groups", "vlan-tagging", "subinterface"}
            still_has_vt = f"if_vt:{if_name}" in ledger.units
            in_if_context = loc_str.startswith(f"/interface[name={if_name}]")

            if (
                if_unit is not None
                and if_unit.owner == "vg"
                and if_name not in base_if_map
                and not surviving_subs
                and not other_if_keys
                and not still_has_vt
                and not in_if_context
            ):
                deletes_to_apply.append(f"/interface[name={if_name}]")
                ledger.units.pop(f"if:{if_name}", None)
            else:
                if if_unit is not None and if_unit.owner == "vg":
                    retained_vg_keys.add(f"if:{if_name}")
                if ("vlan-groups" in if_dict or "vlan_groups" in if_dict) and not loc_str.startswith(
                    f"/interface[name={if_name}]/vlan-groups"
                ):
                    deletes_to_apply.append(f"/interface[name={if_name}]/vlan-groups")

        ledger.active_vg_units = set(desired.keys()) | retained_vg_keys
        ledger.active_if_vgs = {k: list(v) for k, v in if_vgs.items()}

        clean_deletes = self._filter_redundant_deletes(deletes_to_apply)
        clean_updates = []
        for u in updates_to_apply:
            if u and u not in clean_updates:
                clean_updates.append(u)

        if clean_updates or clean_deletes:
            state.server.set_configuration(
                [build_path(p) for p in clean_updates],
                [build_path(p) for p in clean_deletes],
                [],
                DataStore.Candidate,
            )

    def after_executing(self, state, input_line, commands):
        if self._in_reconcile:
            return
        try:
            self._after_executing_impl(state, input_line, commands)
        except Exception:
            self._in_reconcile = False

    def _after_executing_impl(self, state, input_line, commands):
        if getattr(state, "mode", None) != CliMode.Candidate:
            self._clear_ledger(state)
            return

        if not getattr(state, "last_executed_command_was_successful", True):
            return

        ledger = self._get_ledger(state)
        op, touched_paths = self._extract_touched_paths(state, commands)

        if op == "enter":
            if not ledger.seeded:
                self._seed_from_baseline(state, ledger)
            return

        if op == "commit":
            # User ran 'commit stay' and stayed in candidate mode
            for key in list(ledger.active_vg_units):
                if key in ledger.units:
                    ledger.units[key].prior_user_paths = []
                    if key.startswith("if:"):
                        ledger.units[key].owner = "user"
            return

        if op == "discard_all":
            # User ran 'discard stay'
            ledger.reset()
            self._seed_from_baseline(state, ledger)
            return

        if not ledger.seeded:
            self._seed_from_baseline(state, ledger)

        if op is None or not touched_paths:
            return

        vg_touched = self._record_manual_touches(ledger, op, touched_paths)
        if vg_touched:
            self._in_reconcile = True
            try:
                self._reconcile_candidate(state, ledger)
            finally:
                self._in_reconcile = False


class Plugin(CliPlugin):
    def __init__(self):
        super().__init__()
        self._observer = VlanGroupsCandidateObserver(self)

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

        if hasattr(state, 'add_observer'):
            custom_data = getattr(state, 'custom_data', None)
            if isinstance(custom_data, dict):
                if not custom_data.get('_vlan_groups_observer_registered'):
                    state.add_observer(self._observer)
                    custom_data['_vlan_groups_observer_registered'] = True
            else:
                state.add_observer(self._observer)

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
