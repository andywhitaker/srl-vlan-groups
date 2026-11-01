import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import vlan_groups_app
from vlan_groups_app import VlanGroupsApp, _parse_gnmi_path
from vlan_groups_show import Plugin


def _make_config_notif(js_path, keys, data_dict=None):
    return SimpleNamespace(
        key=SimpleNamespace(js_path=js_path, keys=list(keys)),
        data=SimpleNamespace(json=json.dumps(data_dict) if data_dict is not None else "")
    )


class TestVlanGroupsApp(unittest.TestCase):
    def setUp(self):
        self.app = VlanGroupsApp(connect=False)
        self.app.telemetry_stub = MagicMock()
        self.app.gnmi_stub = MagicMock()
        self.app.sdk_stub = MagicMock()
        self.app.config_stub = MagicMock()
        self.app.notification_stub = MagicMock()

    def test_parse_gnmi_path(self):
        p = _parse_gnmi_path("/network-instance[name=vlan-10]/interface[name=ethernet-1/1.10]")
        self.assertEqual(len(p.elem), 2)
        self.assertEqual(p.elem[0].name, "network-instance")
        self.assertEqual(dict(p.elem[0].key), {"name": "vlan-10"})
        self.assertEqual(p.elem[1].name, "interface")
        self.assertEqual(dict(p.elem[1].key), {"name": "ethernet-1/1.10"})

    def test_config_notifications_and_override_replacement(self):
        # 1. Create group with untagged override and tag 10 override
        self.app.handle_config_notification(
            _make_config_notif(
                ".vlan_groups.vlan_group.untagged",
                ["trunk"],
                {"untagged": {"subinterface_id": {"value": 5}, "mac_vrf": {"value": "native-vrf"}}}
            )
        )
        self.app.handle_config_notification(
            _make_config_notif(
                ".vlan_groups.vlan_group.tag",
                ["trunk", "10"],
                {"tag": {"subinterface_id": {"value": 100}, "mac_vrf": {"value": "custom-10"}}}
            )
        )
        self.assertEqual(
            self.app.vlan_groups["trunk"]["untagged"],
            {"subinterface-id": 5, "mac-vrf": "native-vrf"}
        )
        self.assertEqual(
            self.app.vlan_groups["trunk"]["tag"],
            [{"vlan-id": 10, "subinterface-id": 100, "mac-vrf": "custom-10"}]
        )

        # 2. Remove overrides on untagged and tag 10 -> must NOT retain stale overrides in memory
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.untagged", ["trunk"], {"untagged": {}})
        )
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.tag", ["trunk", "10"], {"tag": {}})
        )
        self.assertEqual(self.app.vlan_groups["trunk"]["untagged"], {})
        self.assertEqual(self.app.vlan_groups["trunk"]["tag"], [{"vlan-id": 10}])

    def test_unwrapped_notification_payloads_and_deletions(self):
        # Unwrapped tag payload and top-level container notification
        self.app.handle_config_notification(
            _make_config_notif(
                ".vlan_groups.vlan_group.tag",
                ["g1", "20"],
                {"subinterface-id": 200, "mac-vrf": "vrf-20"}
            )
        )
        self.assertEqual(
            self.app.vlan_groups["g1"]["tag"],
            [{"vlan-id": 20, "subinterface-id": 200, "mac-vrf": "vrf-20"}]
        )

        # Interface container notification
        self.app.handle_config_notification(
            _make_config_notif(
                ".interface.vlan_groups",
                ["ethernet-1/1"],
                {"vlan_groups": {"vlan_group": [{"name": {"value": "g1"}}]}}
            )
        )
        self.assertEqual(
            self.app.interfaces["ethernet-1/1"]["vlan-group"],
            [{"name": "g1"}]
        )

        # Delete interface vlan-group entry -> cleans up empty interface entry
        self.app.handle_config_notification(
            _make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "g1"], None)
        )
        self.assertNotIn("ethernet-1/1", self.app.interfaces)

        # Delete tag 20 and group g1
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.tag", ["g1", "20"], None)
        )
        self.assertEqual(self.app.vlan_groups["g1"]["tag"], [])
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group", ["g1"], None)
        )
        self.assertNotIn("g1", self.app.vlan_groups)

    def test_reconcile_gnmi_ordering_and_mac_vrf_rebind(self):
        gnmi_calls = []

        def fake_apply_gnmi(updates, deletes=None):
            gnmi_calls.append((list(updates), list(deletes or [])))
            return True

        self.app._apply_gnmi = fake_apply_gnmi

        # Configure group with untagged + tag 10 on ethernet-1/1
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.untagged", ["trunk"], {"untagged": {}})
        )
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.tag", ["trunk", "10"], {"tag": {}})
        )
        self.app.handle_config_notification(
            _make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "trunk"], {})
        )

        self.app.reconcile()

        # Must execute 2 separate gNMI calls: Step 1 vlan-tagging, Step 2 subinterfaces & network-instances
        self.assertEqual(len(gnmi_calls), 2)
        tagging_updates, tagging_deletes = gnmi_calls[0]
        self.assertEqual(tagging_deletes, [])
        self.assertEqual(
            tagging_updates,
            [{"path": "/interface[name=ethernet-1/1]/vlan-tagging", "value": True}]
        )

        sub_updates, sub_deletes = gnmi_calls[1]
        self.assertEqual(sub_deletes, [])
        sub_paths = [u["path"] for u in sub_updates]
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=1]", sub_paths)
        self.assertIn("/network-instance[name=vlan-1]", sub_paths)
        self.assertIn("/network-instance[name=vlan-1]/interface[name=ethernet-1/1.1]", sub_paths)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=10]", sub_paths)
        self.assertIn("/network-instance[name=vlan-10]", sub_paths)
        self.assertIn("/network-instance[name=vlan-10]/interface[name=ethernet-1/1.10]", sub_paths)

        # Now change tag 10's mac-vrf from default vlan-10 to custom-vrf (keeping subinterface-id 10)
        gnmi_calls.clear()
        self.app.handle_config_notification(
            _make_config_notif(
                ".vlan_groups.vlan_group.tag",
                ["trunk", "10"],
                {"tag": {"mac_vrf": {"value": "custom-vrf"}}}
            )
        )
        self.app.reconcile()

        _, rebind_deletes = gnmi_calls[1]
        # Must unbind ethernet-1/1.10 from old vlan-10 without deleting subinterface[index=10]
        self.assertEqual(
            rebind_deletes,
            ["/network-instance[name=vlan-10]/interface[name=ethernet-1/1.10]"]
        )

        # Now delete tag 10 -> must delete both network-instance binding and subinterface[index=10]
        gnmi_calls.clear()
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.tag", ["trunk", "10"], None)
        )
        self.app.reconcile()
        _, tag_del_deletes = gnmi_calls[1]
        self.assertEqual(
            tag_del_deletes,
            [
                "/network-instance[name=custom-vrf]/interface[name=ethernet-1/1.10]",
                "/interface[name=ethernet-1/1]/subinterface[index=10]",
            ]
        )

    def test_subinterface_id_collision_protection(self):
        self.app._apply_gnmi = MagicMock(return_value=True)
        # Both untagged (default sid=1) and tag 1 (default sid=1) on same group
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.untagged", ["g1"], {"untagged": {}})
        )
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.tag", ["g1", "1"], {"tag": {}})
        )
        self.app.handle_config_notification(
            _make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "g1"], {})
        )

        self.app.reconcile()
        # Only one subinterface[index=1] update should be emitted
        other_updates = self.app._apply_gnmi.call_args_list[1][0][0]
        sub1_updates = [
            u for u in other_updates
            if u["path"] == "/interface[name=ethernet-1/1]/subinterface[index=1]"
        ]
        self.assertEqual(len(sub1_updates), 1)
        self.assertIn("untagged", sub1_updates[0]["value"]["vlan"]["encap"])

    def test_gnmi_failure_preserves_deployed_subs_for_retry(self):
        # First reconcile succeeds
        self.app._apply_gnmi = MagicMock(return_value=True)
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.tag", ["g1", "10"], {"tag": {}})
        )
        self.app.handle_config_notification(
            _make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "g1"], {})
        )
        self.app.reconcile()
        self.assertEqual(self.app.deployed_subs, {"ethernet-1/1": {"g1": {10: "vlan-10"}}})

        # Delete group from interface, but gNMI fails -> deployed_subs must remain intact for next retry
        self.app._apply_gnmi = MagicMock(return_value=False)
        self.app.handle_config_notification(
            _make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "g1"], None)
        )
        self.app.reconcile()
        self.assertEqual(self.app.deployed_subs, {"ethernet-1/1": {"g1": {10: "vlan-10"}}})

        # Next reconcile succeeds -> deployed_subs is now updated to empty
        self.app._apply_gnmi = MagicMock(return_value=True)
        self.app.reconcile()
        self.assertEqual(self.app.deployed_subs, {})

    def test_telemetry_delete_before_update_and_effective_defaults(self):
        call_order = []
        self.app.telemetry_stub.TelemetryDelete.side_effect = lambda req, metadata=None: call_order.append(
            ("delete", [k.js_path for k in req.keys])
        )
        self.app.telemetry_stub.TelemetryAddOrUpdate.side_effect = lambda req, metadata=None: call_order.append(
            ("update", {s.key.js_path: json.loads(s.data.json_content) for s in req.states})
        )

        # Initial telemetry with tag 10 override
        self.app.handle_config_notification(
            _make_config_notif(
                ".vlan_groups.vlan_group.tag",
                ["g1", "10"],
                {"tag": {"subinterface_id": {"value": 100}, "mac_vrf": {"value": "custom-10"}}}
            )
        )
        self.app.reconcile()
        self.assertEqual(len(call_order), 1)
        self.assertEqual(call_order[0][0], "update")
        self.assertEqual(
            call_order[0][1]['.vlan_groups.vlan_group{.name=="g1"}.tag{.vlan-id=="10"}'],
            {"vlan-id": 10, "subinterface-id": 100, "mac-vrf": "custom-10"}
        )

        # Remove override on tag 10 -> telemetry must explicitly write default subinterface-id=10 and mac-vrf=vlan-10,
        # and delete orphaned network-instance custom-10 BEFORE updating!
        call_order.clear()
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.tag", ["g1", "10"], {"tag": {}})
        )
        self.app.reconcile()
        self.assertEqual([c[0] for c in call_order], ["delete", "update"])
        self.assertIn(
            '.vlan_groups.vlan_group{.name=="g1"}.created.network_instances.network_instance{.name=="custom-10"}',
            call_order[0][1]
        )
        self.assertEqual(
            call_order[1][1]['.vlan_groups.vlan_group{.name=="g1"}.tag{.vlan-id=="10"}'],
            {"vlan-id": 10, "subinterface-id": 10, "mac-vrf": "vlan-10"}
        )

    def test_untagged_deletion_via_group_notification_and_telemetry_clear(self):
        self.app._apply_gnmi = MagicMock(return_value=True)
        telemetry_updates = []
        self.app.telemetry_stub.TelemetryAddOrUpdate.side_effect = lambda req, metadata=None: telemetry_updates.append(
            {s.key.js_path: json.loads(s.data.json_content) for s in req.states}
        )

        # SR Linux NDK sends untagged creation on .vlan_groups.vlan_group
        self.app.handle_config_notification(
            _make_config_notif(
                ".vlan_groups.vlan_group",
                ["trunk"],
                {"vlan_group": {"untagged": {"subinterface_id": {"value": "1"}, "mac_vrf": {"value": "vlan-1"}}}}
            )
        )
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.tag", ["trunk", "10"], {})
        )
        self.app.handle_config_notification(
            _make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "trunk"], {})
        )
        self.app.reconcile()
        self.assertIn("untagged", self.app.vlan_groups["trunk"])
        self.assertEqual(
            telemetry_updates[-1]['.vlan_groups.vlan_group{.name=="trunk"}'],
            {"untagged": {"subinterface-id": 1, "mac-vrf": "vlan-1"}}
        )

        # When untagged is deleted in SR Linux CLI, NDK sends .vlan_groups.vlan_group with Data: {}
        self.app._apply_gnmi.reset_mock()
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group", ["trunk"], {})
        )
        self.app.reconcile()

        self.assertNotIn("untagged", self.app.vlan_groups["trunk"])
        self.assertEqual(self.app.vlan_groups["trunk"]["tag"], [{"vlan-id": 10}])
        # Must delete subinterface 1 and its vlan-1 binding via gNMI
        _, deletes = self.app._apply_gnmi.call_args_list[1][0]
        self.assertIn("/network-instance[name=vlan-1]/interface[name=ethernet-1/1.1]", deletes)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=1]", deletes)
        # Must overwrite .vlan_groups.vlan_group{.name=="trunk"} with {} in telemetry to clear untagged state
        self.assertEqual(telemetry_updates[-1]['.vlan_groups.vlan_group{.name=="trunk"}'], {})

    def test_encap_transition_deletes_old_vlan_encap(self):
        gnmi_calls = []

        def fake_apply_gnmi(updates, deletes=None):
            gnmi_calls.append((list(updates), list(deletes or [])))
            return True

        self.app._apply_gnmi = fake_apply_gnmi

        # Start with tag 1 (single-tagged vlan-id 1 on subinterface 1)
        self.app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.tag", ["g1", "1"], {})
        )
        self.app.handle_config_notification(
            _make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "g1"], {})
        )
        self.app.reconcile()

        # Now add untagged (default subinterface 1) to g1 -> subinterface 1 transitions from single-tagged to untagged
        gnmi_calls.clear()
        self.app.handle_config_notification(
            _make_config_notif(
                ".vlan_groups.vlan_group",
                ["g1"],
                {"vlan_group": {"untagged": {"subinterface_id": {"value": "1"}, "mac_vrf": {"value": "vlan-1"}}}}
            )
        )
        self.app.reconcile()

        _, sub_deletes = gnmi_calls[1]
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=1]/vlan/encap", sub_deletes)

    def test_commit_end_and_noop_notifications_ignored(self):
        self.assertFalse(
            self.app.handle_config_notification(
                _make_config_notif(".commit.end", [], {"commit_seq": 42})
            )
        )
        self.assertFalse(
            self.app.handle_config_notification(
                _make_config_notif(".interface.vlan_groups", ["mgmt0"], {})
            )
        )
        self.assertFalse(
            self.app.handle_config_notification(
                _make_config_notif(".vlan_groups", [], {})
            )
        )


class TestVlanGroupsCliPlugin(unittest.TestCase):
    def setUp(self):
        self.plugin = Plugin()
        self.vg_payload = {
            "vlan_groups": {
                "vlan_group": [
                    {
                        "name": {"value": "group-a"},
                        "vlan_group": {
                            "untagged": {
                                "subinterface_id": {"value": 1},
                                "mac_vrf": {"value": "vlan-1"}
                            },
                            "tag": [
                                {"vlan_id": {"value": 20}, "tag": {}},
                                {
                                    "vlan_id": {"value": 10},
                                    "tag": {
                                        "subinterface_id": {"value": 100},
                                        "mac_vrf": {"value": "custom-10"}
                                    }
                                }
                            ]
                        }
                    },
                    {
                        "name": {"value": "group-b"},
                        "vlan_group": {
                            "tag": [
                                {"vlan_id": {"value": 30}, "tag": {}}
                            ]
                        }
                    }
                ]
            }
        }
        self.if_payload = {
            "interface": [
                {
                    "name": {"value": "mgmt0"},
                    "interface": {}
                },
                {
                    "name": {"value": "ethernet-1/1"},
                    "interface": {
                        "vlan_groups": {
                            "vlan_group": [
                                {"name": {"value": "group-a"}},
                                {"name": {"value": "group-b"}}
                            ]
                        }
                    }
                }
            ]
        }

        def fake_get_proto_json(path, **_kwargs):
            path_str = str(path)
            if path_str == "/vlan-groups":
                return json.dumps(self.vg_payload)
            if path_str == "/interface":
                return json.dumps(self.if_payload)
            return ""

        self.state = SimpleNamespace(
            server=SimpleNamespace(get_proto_json=fake_get_proto_json),
            line_commands=None
        )

    def test_fetch_data_filters_unmanaged_interfaces_and_sorts_tags(self):
        vg_data, if_data = self.plugin._fetch_data(self.state)

        # Unmanaged mgmt0 interface must not pollute if_data or suggestions
        self.assertNotIn("mgmt0", if_data)
        self.assertEqual(self.plugin._suggest_interfaces(None, self.state), ["ethernet-1/1"])
        self.assertEqual(self.plugin._suggest_groups(None, self.state), ["group-a", "group-b"])

        # Tags in group-a must be sorted: untagged first, then 10, then 20
        self.assertEqual(vg_data["group-a"]["tags"], ["untagged", "10", "20"])
        self.assertEqual(vg_data["group-a"]["subs"], ["1", "100", "20"])
        self.assertEqual(vg_data["group-a"]["mac_vrfs"], ["vlan-1", "custom-10", "vlan-20"])

    def test_print_interfaces_with_multiple_groups(self):
        lines = []
        output = SimpleNamespace(print_line=lambda s: lines.append(s))
        self.plugin._print_interface(self.state, output, {"interface-name": "ethernet-1/1"})

        rendered = "\n".join(lines)
        self.assertIn("ethernet-1/1", rendered)
        self.assertIn("group-a, group-b", rendered)
        self.assertIn("untagged(1)[vlan-1]", rendered)
        self.assertIn("10(100)[custom-10]", rendered)
        self.assertIn("20(20)[vlan-20]", rendered)
        self.assertIn("30(30)[vlan-30]", rendered)

    def test_print_interfaces_skips_colliding_subinterface_ids(self):
        # Add tag 1 (default subinterface 1) to group-b, which collides with group-a's untagged(1)
        self.vg_payload["vlan_groups"]["vlan_group"][1]["vlan_group"]["tag"].append(
            {"vlan_id": {"value": 1}, "tag": {}}
        )
        lines = []
        output = SimpleNamespace(print_line=lambda s: lines.append(s))
        self.plugin._print_interface(self.state, output, {"interface-name": "ethernet-1/1"})

        rendered = "\n".join(lines)
        self.assertIn("untagged(1)[vlan-1]", rendered)
        self.assertNotIn("1(1)[vlan-1]", rendered)

    def test_print_not_found_messages(self):
        lines = []
        output = SimpleNamespace(print_line=lambda s: lines.append(s))
        self.plugin._print_group(self.state, output, {"group-name": "missing"})
        self.plugin._print_interface(self.state, output, {"interface-name": "mgmt0"})

        self.assertEqual(lines[0], "VLAN group 'missing' not found.")
        self.assertEqual(lines[1], "Interface 'mgmt0' not found.")


if __name__ == "__main__":
    unittest.main()
