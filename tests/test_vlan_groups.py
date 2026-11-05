import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import vlan_groups_app
from vlan_groups_app import VlanGroupsApp, _parse_gnmi_path
from vlan_groups_show import CliMode, DataStore, Plugin, VlanGroupsCandidateObserver


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

    def test_process_notifications_waits_for_commit_end_before_reconcile(self):
        events = []

        def _make_stream_notif(cfg_notif):
            return SimpleNamespace(
                HasField=lambda field: field == "config",
                config=cfg_notif,
            )

        batch1 = SimpleNamespace(
            notifications=[
                _make_stream_notif(_make_config_notif(".vlan_groups.vlan_group.tag", ["trunk", "10"], {})),
                _make_stream_notif(_make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "trunk"], {})),
            ]
        )
        batch2 = SimpleNamespace(
            notifications=[
                _make_stream_notif(_make_config_notif(".commit.end", [], {"commit_seq": 1})),
            ]
        )

        def fake_stream(_req, metadata=None):
            events.append("yield_batch1")
            yield batch1
            events.append("yield_batch2")
            yield batch2

        self.app.notification_stub.NotificationStream.side_effect = fake_stream
        self.app.config_stub.AcknowledgeConfig.side_effect = lambda req, metadata=None: events.append("ack")
        self.app.reconcile = lambda: events.append("reconcile")

        self.app.process_notifications(stream_id=1)

        # Must acknowledge batch1 before batch2, and only run reconcile() once after batch2 (.commit.end) is acknowledged
        self.assertEqual(
            events,
            ["yield_batch1", "ack", "yield_batch2", "ack", "reconcile"]
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

    def test_reconcile_skips_redundant_gnmi_sets_when_running_already_converged(self):
        app = VlanGroupsApp(connect=False)
        app.telemetry_stub = MagicMock()
        app._apply_gnmi = MagicMock(return_value=True)

        app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.untagged", ["trunk"], {"untagged": {}})
        )
        app.handle_config_notification(
            _make_config_notif(".vlan_groups.vlan_group.tag", ["trunk", "10"], {"tag": {}})
        )
        app.handle_config_notification(
            _make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "trunk"], {})
        )

        running_snapshot = {
            "/interface[name=ethernet-1/1]": {
                "name": "ethernet-1/1",
                "vlan-tagging": True,
                "subinterface": [
                    {"index": 1, "type": "bridged", "vlan": {"encap": {"untagged": {}}}},
                    {"index": 10, "type": "bridged", "vlan": {"encap": {"single-tagged": {"vlan-id": 10}}}},
                ],
            },
            "/network-instance[name=vlan-1]": {
                "name": "vlan-1",
                "type": "mac-vrf",
                "interface": [{"name": "ethernet-1/1.1"}],
            },
            "/network-instance[name=vlan-10]": {
                "name": "vlan-10",
                "type": "mac-vrf",
                "interface": [{"name": "ethernet-1/1.10"}],
            },
        }
        app._get_gnmi_json = lambda path: running_snapshot.get(path, {})

        app.reconcile()

        # Zero gNMI Set calls because Running was already converged by CLI commit
        app._apply_gnmi.assert_not_called()
        self.assertEqual(app.deployed_subs, {"ethernet-1/1": {"trunk": {1: "vlan-1", 10: "vlan-10"}}})
        app.telemetry_stub.TelemetryAddOrUpdate.assert_called_once()

        # Now simulate CLI backout where subinterface 10 was preserved with manual description (vlan stripped)
        app.handle_config_notification(
            _make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "trunk"], None)
        )
        running_after_backout = {
            "/interface[name=ethernet-1/1]": {
                "name": "ethernet-1/1",
                "subinterface": [
                    {"index": 10, "description": "manual-server"},
                ],
            },
            "/network-instance[name=vlan-1]": {},
            "/network-instance[name=vlan-10]": {},
        }
        app._get_gnmi_json = lambda path: running_after_backout.get(path, {})

        app.reconcile()
        app._apply_gnmi.assert_not_called()
        self.assertEqual(app.deployed_subs, {})

        # Also verify cross-session backout where subinterface 10 retained identical vlan-id 10 (unbound from vlan-10 by CLI commit)
        app.handle_config_notification(
            _make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "trunk"], {})
        )
        app._get_gnmi_json = lambda path: running_snapshot.get(path, {})
        app.reconcile()
        app._apply_gnmi.assert_not_called()

        app.handle_config_notification(
            _make_config_notif(".interface.vlan_groups.vlan_group", ["ethernet-1/1", "trunk"], None)
        )
        running_after_identical_vid_preserve = {
            "/interface[name=ethernet-1/1]": {
                "name": "ethernet-1/1",
                "vlan-tagging": True,
                "subinterface": [
                    {"index": 10, "vlan": {"encap": {"single-tagged": {"vlan-id": 10}}}},
                ],
            },
            "/network-instance[name=vlan-1]": {},
            "/network-instance[name=vlan-10]": {},
        }
        app._get_gnmi_json = lambda path: running_after_identical_vid_preserve.get(path, {})
        app.reconcile()
        app._apply_gnmi.assert_not_called()


class TestVlanGroupsCandidateObserver(unittest.TestCase):
    def setUp(self):
        self.plugin = Plugin()
        self.observer = self.plugin._observer
        self.candidate_vg = {}
        self.candidate_if = {
            "interface": [
                {"name": {"value": "ethernet-1/1"}, "interface": {"admin_state": {"value": "enable"}}}
            ]
        }
        self.candidate_ni = {}
        self.baseline_vg = {}
        self.baseline_if = {
            "interface": [
                {"name": {"value": "ethernet-1/1"}, "interface": {"admin_state": {"value": "enable"}}}
            ]
        }
        self.baseline_ni = {}
        self.set_calls = []

        def fake_get_proto_json(path, data_store=None, **_kwargs):
            p = str(path)
            if data_store == DataStore.Baseline:
                if p == "/vlan-groups":
                    return json.dumps(self.baseline_vg) if self.baseline_vg else ""
                if p == "/interface":
                    return json.dumps(self.baseline_if) if self.baseline_if else ""
                if p == "/network-instance":
                    return json.dumps(self.baseline_ni) if self.baseline_ni else ""
                return ""
            if p == "/vlan-groups":
                return json.dumps(self.candidate_vg) if self.candidate_vg else ""
            if p == "/interface":
                return json.dumps(self.candidate_if) if self.candidate_if else ""
            if p == "/network-instance":
                return json.dumps(self.candidate_ni) if self.candidate_ni else ""
            return ""

        def fake_set_configuration(updates, deletes, replaces, data_store):
            self.set_calls.append(([str(u) for u in updates], [str(d) for d in deletes], data_store))

        self.state = SimpleNamespace(
            mode=CliMode.Candidate,
            last_executed_command_was_successful=True,
            custom_data={},
            line_custom_data={},
            location=SimpleNamespace(path="/"),
            server=SimpleNamespace(
                get_proto_json=fake_get_proto_json,
                set_configuration=fake_set_configuration,
            ),
        )

    def _run_cmd(self, name, paths=None):
        self.state.line_custom_data = {name: {"paths": list(paths or [])}} if paths is not None else {}
        cmd = SimpleNamespace(name=name, node=SimpleNamespace(name=name, type="normal"))
        self.observer.after_executing(self.state, name, [cmd])

    def test_pre_commit_expansion_and_clean_backout_on_existing_and_new_interfaces(self):
        # Define trunk (untagged + tag 10 + tag 20) and apply to existing ethernet-1/1
        self.candidate_vg = {
            "vlan_groups": {
                "vlan_group": [
                    {
                        "name": {"value": "trunk"},
                        "vlan_group": {
                            "untagged": {"subinterface_id": {"value": 1}, "mac_vrf": {"value": "vlan-1"}},
                            "tag": [
                                {"vlan_id": {"value": 10}, "tag": {"subinterface_id": {"value": 10}, "mac_vrf": {"value": "vlan-10"}}},
                                {"vlan_id": {"value": 20}, "tag": {"subinterface_id": {"value": 20}, "mac_vrf": {"value": "vlan-20"}}},
                            ],
                        },
                    }
                ]
            }
        }
        self.candidate_if["interface"][0]["interface"]["vlan_groups"] = {
            "vlan_group": [{"name": {"value": "trunk"}}]
        }

        self._run_cmd("set", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=trunk]"])

        self.assertEqual(len(self.set_calls), 1)
        upds, dels, ds = self.set_calls[-1]
        self.assertEqual(ds, DataStore.Candidate)
        self.assertEqual(dels, [])
        self.assertIn("/interface[name=ethernet-1/1]/vlan-tagging:true", upds)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=1]/type:bridged", upds)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=1]/vlan/encap/untagged", upds)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=10]/vlan/encap/single-tagged/vlan-id:10", upds)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=20]/vlan/encap/single-tagged/vlan-id:20", upds)
        self.assertIn("/network-instance[name=vlan-1]/type:mac-vrf", upds)
        self.assertIn("/network-instance[name=vlan-10]/interface[name=ethernet-1/1.10]", upds)
        self.assertIn("/network-instance[name=vlan-20]/interface[name=ethernet-1/1.20]", upds)

        # Back out trunk from ethernet-1/1 -> deletes subinterfaces, vlan-tagging, network-instances, and empty vlan-groups container,
        # but does NOT delete /interface[name=ethernet-1/1] since it existed in Baseline!
        self.set_calls.clear()
        self.candidate_if["interface"][0]["interface"] = {"admin_state": {"value": "enable"}, "vlan_groups": {}}
        self._run_cmd("delete", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=trunk]"])

        upds, dels, _ = self.set_calls[-1]
        self.assertEqual(upds, [])
        self.assertNotIn("/interface[name=ethernet-1/1]", dels)
        self.assertIn("/interface[name=ethernet-1/1]/vlan-groups", dels)
        self.assertIn("/interface[name=ethernet-1/1]/vlan-tagging", dels)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=1]", dels)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=10]", dels)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=20]", dels)
        self.assertIn("/network-instance[name=vlan-1]", dels)
        self.assertIn("/network-instance[name=vlan-10]", dels)
        self.assertIn("/network-instance[name=vlan-20]", dels)

        # Now apply trunk to brand-new unconfigured interface ethernet-1/2 (not in Baseline) and back out ->
        # must delete /interface[name=ethernet-1/2] itself so zero residual diff remains!
        self.set_calls.clear()
        self.candidate_if["interface"].append({
            "name": {"value": "ethernet-1/2"},
            "interface": {"vlan_groups": {"vlan_group": [{"name": {"value": "trunk"}}]}},
        })
        self._run_cmd("set", ["/interface[name=ethernet-1/2]/vlan-groups/vlan-group[name=trunk]"])
        self.assertEqual(len(self.set_calls), 1)

        self.set_calls.clear()
        self.candidate_if["interface"][1]["interface"] = {"vlan_groups": {}}
        self._run_cmd("delete", ["/interface[name=ethernet-1/2]/vlan-groups/vlan-group[name=trunk]"])
        _, dels2, _ = self.set_calls[-1]
        self.assertIn("/interface[name=ethernet-1/2]", dels2)

    def test_manual_description_and_vlan_override_preserved_on_backout(self):
        # Apply trunk (tag 10 + tag 20) to ethernet-1/1
        self.candidate_vg = {
            "vlan_groups": {
                "vlan_group": [
                    {
                        "name": {"value": "trunk"},
                        "vlan_group": {
                            "tag": [
                                {"vlan_id": {"value": 10}, "tag": {"subinterface_id": {"value": 10}, "mac_vrf": {"value": "vlan-10"}}},
                                {"vlan_id": {"value": 20}, "tag": {"subinterface_id": {"value": 20}, "mac_vrf": {"value": "vlan-20"}}},
                            ],
                        },
                    }
                ]
            }
        }
        self.candidate_if["interface"][0]["interface"]["vlan_groups"] = {
            "vlan_group": [{"name": {"value": "trunk"}}]
        }
        self._run_cmd("set", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=trunk]"])

        # Scenario 4A: Manually add description to subinterface 10
        self._run_cmd("set", ["/interface[name=ethernet-1/1]/subinterface[index=10]/description:manual-server"])
        self.candidate_if["interface"][0]["interface"]["subinterface"] = [
            {
                "index": 10,
                "type": "bridged",
                "description": "manual-server",
                "vlan": {"encap": {"single-tagged": {"vlan-id": 10}}},
            },
            {
                "index": 20,
                "type": "bridged",
                "vlan": {"encap": {"single-tagged": {"vlan-id": 20}}},
            },
        ]

        # Back out trunk -> subinterface 20 is deleted; subinterface 10 is preserved but its VG type & vlan are deleted
        self.set_calls.clear()
        self.candidate_if["interface"][0]["interface"].pop("vlan_groups", None)
        self._run_cmd("delete", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=trunk]"])

        _, dels, _ = self.set_calls[-1]
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=20]", dels)
        self.assertNotIn("/interface[name=ethernet-1/1]/subinterface[index=10]", dels)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=10]/vlan", dels)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=10]/type", dels)
        self.assertIn("/interface[name=ethernet-1/1]/vlan-tagging", dels)

        # Scenario 4B: Apply trunk again, then manually override vlan-id on subinterface 10 to 110
        self.candidate_if["interface"][0]["interface"]["vlan_groups"] = {
            "vlan_group": [{"name": {"value": "trunk"}}]
        }
        self._run_cmd("set", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=trunk]"])
        self._run_cmd(
            "set",
            ["/interface[name=ethernet-1/1]/subinterface[index=10]/vlan/encap/single-tagged/vlan-id:110"],
        )
        self.candidate_if["interface"][0]["interface"]["subinterface"][0]["vlan"] = {
            "encap": {"single-tagged": {"vlan-id": 110}}
        }

        self.set_calls.clear()
        self.candidate_if["interface"][0]["interface"].pop("vlan_groups", None)
        self._run_cmd("delete", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=trunk]"])

        _, dels_4b, _ = self.set_calls[-1]
        self.assertNotIn("/interface[name=ethernet-1/1]/subinterface[index=10]", dels_4b)
        self.assertNotIn("/interface[name=ethernet-1/1]/subinterface[index=10]/vlan", dels_4b)
        # Schema validity guard: vlan-tagging must NOT be deleted because subinterface 10 retains single-tagged vlan-id 110
        self.assertNotIn("/interface[name=ethernet-1/1]/vlan-tagging", dels_4b)

    def test_vlan_tagging_false_restoration_and_session_exit_clears_ledger(self):
        self.candidate_vg = {
            "vlan_groups": {
                "vlan_group": [
                    {
                        "name": {"value": "trunk"},
                        "vlan_group": {
                            "tag": [{"vlan_id": {"value": 10}, "tag": {"subinterface_id": {"value": 10}, "mac_vrf": {"value": "vlan-10"}}}]
                        },
                    }
                ]
            }
        }
        # User manually sets vlan-tagging false first
        self._run_cmd("set", ["/interface[name=ethernet-1/1]/vlan-tagging:false"])
        # User applies trunk -> overrides vlan-tagging to true
        self.candidate_if["interface"][0]["interface"]["vlan_groups"] = {
            "vlan_group": [{"name": {"value": "trunk"}}]
        }
        self._run_cmd("set", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=trunk]"])
        upds, _, _ = self.set_calls[-1]
        self.assertIn("/interface[name=ethernet-1/1]/vlan-tagging:true", upds)

        # User backs out trunk -> restores vlan-tagging:false!
        self.set_calls.clear()
        self.candidate_if["interface"][0]["interface"].pop("vlan_groups", None)
        self._run_cmd("delete", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=trunk]"])
        upds_restore, dels_restore, _ = self.set_calls[-1]
        self.assertIn("/interface[name=ethernet-1/1]/vlan-tagging:false", upds_restore)
        self.assertNotIn("/interface[name=ethernet-1/1]/vlan-tagging", dels_restore)

        # Leaving candidate mode clears the ledger completely (no persistence outside config session)
        self.assertIn(VlanGroupsCandidateObserver.LEDGER_KEY, self.state.custom_data)
        self.state.mode = CliMode.Running
        self._run_cmd("exit")
        self.assertNotIn(VlanGroupsCandidateObserver.LEDGER_KEY, self.state.custom_data)

    def test_cross_session_seed_with_proto_vlan_id_uint16_and_backout(self):
        # Simulate SR Linux get_proto_json Baseline where single-tagged uses 'vlan_id_uint16'
        self.baseline_vg = {
            "vlan_groups": {
                "vlan_group": [
                    {
                        "name": {"value": "trunk"},
                        "vlan_group": {
                            "tag": [
                                {"vlan_id": {"value": 10}, "tag": {"subinterface_id": {"value": 10}, "mac_vrf": {"value": "vlan-10"}}},
                                {"vlan_id": {"value": 20}, "tag": {"subinterface_id": {"value": 20}, "mac_vrf": {"value": "vlan-20"}}},
                            ]
                        },
                    }
                ]
            }
        }
        self.baseline_if = {
            "interface": [
                {
                    "name": {"value": "ethernet-1/1"},
                    "interface": {
                        "vlan_tagging": {"value": True},
                        "subinterface": [
                            {
                                "index": {"value": "10"},
                                "subinterface": {
                                    "type": "srl_nokia-interfaces:bridged",
                                    "vlan": {"encap": {"single_tagged": {"vlan_id_uint16": {"value": "10"}}}},
                                },
                            },
                            {
                                "index": {"value": "20"},
                                "subinterface": {
                                    "type": "srl_nokia-interfaces:bridged",
                                    "vlan": {"encap": {"single_tagged": {"vlan_id_uint16": {"value": "20"}}}},
                                },
                            },
                        ],
                        "vlan_groups": {"vlan_group": [{"name": {"value": "trunk"}}]},
                    },
                }
            ]
        }
        self.baseline_ni = {
            "network_instance": [
                {"name": {"value": "vlan-10"}, "network_instance": {"type": "mac-vrf", "interface": [{"name": {"value": "ethernet-1/1.10"}}]}},
                {"name": {"value": "vlan-20"}, "network_instance": {"type": "mac-vrf", "interface": [{"name": {"value": "ethernet-1/1.20"}}]}},
            ]
        }
        self.candidate_vg = json.loads(json.dumps(self.baseline_vg))
        self.candidate_if = json.loads(json.dumps(self.baseline_if))
        self.candidate_ni = json.loads(json.dumps(self.baseline_ni))

        self._run_cmd("enter")
        self.candidate_if["interface"][0]["interface"].pop("vlan_groups", None)
        self._run_cmd("delete", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=trunk]"])

        _, dels, _ = self.set_calls[-1]
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=10]", dels)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=20]", dels)
        self.assertIn("/interface[name=ethernet-1/1]/vlan-tagging", dels)
        self.assertIn("/network-instance[name=vlan-10]", dels)
        self.assertIn("/network-instance[name=vlan-20]", dels)

    def test_preexisting_conflicting_subinterface_skipped_and_temp_description_delete_clean(self):
        self.candidate_vg = {
            "vlan_groups": {
                "vlan_group": [
                    {
                        "name": {"value": "trunk"},
                        "vlan_group": {
                            "tag": [
                                {"vlan_id": {"value": 10}, "tag": {"subinterface_id": {"value": 10}, "mac_vrf": {"value": "vlan-10"}}},
                                {"vlan_id": {"value": 20}, "tag": {"subinterface_id": {"value": 20}, "mac_vrf": {"value": "vlan-20"}}},
                            ]
                        },
                    }
                ]
            }
        }
        # User configures subinterface 10 as type routed before applying trunk
        self._run_cmd("set", ["/interface[name=ethernet-1/1]/subinterface[index=10]/type:routed"])
        self.candidate_if["interface"][0]["interface"]["subinterface"] = [
            {"index": 10, "type": "routed"}
        ]
        self.candidate_if["interface"][0]["interface"]["vlan_groups"] = {
            "vlan_group": [{"name": {"value": "trunk"}}]
        }
        self._run_cmd("set", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=trunk]"])

        upds, _, _ = self.set_calls[-1]
        # Conflicting subinterface 10 is skipped completely (not overwritten or bound to mac-vrf vlan-10)
        self.assertNotIn("/interface[name=ethernet-1/1]/subinterface[index=10]/type:bridged", upds)
        self.assertNotIn("/interface[name=ethernet-1/1]/subinterface[index=10]/vlan/encap/single-tagged/vlan-id:10", upds)
        self.assertNotIn("/network-instance[name=vlan-10]/interface[name=ethernet-1/1.10]", upds)
        # Non-conflicting subinterface 20 is expanded
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=20]/vlan/encap/single-tagged/vlan-id:20", upds)
        self.assertIn("/network-instance[name=vlan-20]/interface[name=ethernet-1/1.20]", upds)

        # Now user temporarily adds and then deletes description on VG-created subinterface 20, then backs out trunk
        self.candidate_if["interface"][0]["interface"]["subinterface"].append(
            {"index": 20, "type": "bridged", "vlan": {"encap": {"single-tagged": {"vlan-id": 20}}}}
        )
        self._run_cmd("set", ["/interface[name=ethernet-1/1]/subinterface[index=20]/description:temp"])
        self._run_cmd("delete", ["/interface[name=ethernet-1/1]/subinterface[index=20]/description"])

        self.set_calls.clear()
        self.candidate_if["interface"][0]["interface"].pop("vlan_groups", None)
        self._run_cmd("delete", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=trunk]"])
        _, dels, _ = self.set_calls[-1]
        # Subinterface 20 must be deleted (no orphaned empty subinterface 20 stub!)
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=20]", dels)

    def test_multi_group_subinterface_sharing_and_partial_tag_removal_schema_guard(self):
        self.candidate_vg = {
            "vlan_groups": {
                "vlan_group": [
                    {
                        "name": {"value": "group-a"},
                        "vlan_group": {
                            "tag": [{"vlan_id": {"value": 10}, "tag": {"subinterface_id": {"value": 100}, "mac_vrf": {"value": "vlan-10"}}}]
                        },
                    },
                    {
                        "name": {"value": "group-b"},
                        "vlan_group": {
                            "tag": [
                                {"vlan_id": {"value": 20}, "tag": {"subinterface_id": {"value": 100}, "mac_vrf": {"value": "vlan-20"}}},
                                {"vlan_id": {"value": 30}, "tag": {"subinterface_id": {"value": 30}, "mac_vrf": {"value": "vlan-30"}}},
                            ]
                        },
                    },
                ]
            }
        }
        self.candidate_if["interface"][0]["interface"]["vlan_groups"] = {
            "vlan_group": [{"name": {"value": "group-a"}}, {"name": {"value": "group-b"}}]
        }
        self._run_cmd("set", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=group-a]"])
        self.candidate_ni = {
            "network_instance": [
                {"name": {"value": "vlan-10"}, "network_instance": {"type": "mac-vrf", "interface": [{"name": {"value": "ethernet-1/1.100"}}]}},
                {"name": {"value": "vlan-30"}, "network_instance": {"type": "mac-vrf", "interface": [{"name": {"value": "ethernet-1/1.30"}}]}},
            ]
        }

        # Delete group-a so group-b claims subinterface 100 with vlan-id 20
        self.set_calls.clear()
        self.candidate_if["interface"][0]["interface"]["vlan_groups"] = {
            "vlan_group": [{"name": {"value": "group-b"}}]
        }
        self._run_cmd("delete", ["/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=group-a]"])
        upds, dels, _ = self.set_calls[-1]
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=100]/vlan/encap/single-tagged/vlan-id:20", upds)
        self.assertNotIn("/interface[name=ethernet-1/1]/subinterface[index=100]/vlan/encap/single-tagged", dels)
        self.assertIn("/network-instance[name=vlan-10]", dels)

        # Now add description on subinterface 30, and remove only tag 30 from group-b while tag 20 (sub 100) stays active
        self.candidate_if["interface"][0]["interface"]["subinterface"] = [
            {"index": 100, "type": "bridged", "vlan": {"encap": {"single-tagged": {"vlan-id": 20}}}},
            {"index": 30, "type": "bridged", "description": "server-30", "vlan": {"encap": {"single-tagged": {"vlan-id": 30}}}},
        ]
        self._run_cmd("set", ["/interface[name=ethernet-1/1]/subinterface[index=30]/description:server-30"])
        self.set_calls.clear()
        self.candidate_vg["vlan_groups"]["vlan_group"][1]["vlan_group"]["tag"] = [
            {"vlan_id": {"value": 20}, "tag": {"subinterface_id": {"value": 100}, "mac_vrf": {"value": "vlan-20"}}}
        ]
        self._run_cmd("delete", ["/vlan-groups/vlan-group[name=group-b]/tag[vlan-id=30]"])
        _, dels_partial, _ = self.set_calls[-1]
        # Schema validity guard: because ethernet-1/1 still has vlan-tagging:true (for sub 100),
        # surviving subinterface 30 must retain its vlan encap!
        self.assertNotIn("/interface[name=ethernet-1/1]/subinterface[index=30]", dels_partial)
        self.assertNotIn("/interface[name=ethernet-1/1]/subinterface[index=30]/vlan", dels_partial)

    def test_double_dot_without_schema_and_context_mode_navigation(self):
        class _AssertingDotDotNode:
            name = ".."
            type = "yang_based_config"
            has_schema = False
            schema_or_none = None

            @property
            def schema(self):
                raise AssertionError("CommandNode '..' has no schema")

        class _AssertingDotDotCmd:
            def __init__(self):
                self.name = ".."
                self.node = _AssertingDotDotNode()
                self.has_schema = False
                self.local_arguments = {}

            @property
            def schema(self):
                return self.node.schema

        # 1. Running '..' inside [ vlan-groups ] must not raise AssertionError or trigger set_configuration
        self.state.start_location = SimpleNamespace(path="/vlan-groups")
        self.state.location = SimpleNamespace(path="/")
        self.observer.after_executing(self.state, "..", [_AssertingDotDotCmd()])
        self.assertEqual(self.set_calls, [])

        # 2. Configuring vlan-group on ethernet-1/1 via context mode from start_location='/interface[name=ethernet-1/1]'
        self.candidate_vg = {
            "vlan_groups": {
                "vlan_group": [
                    {
                        "name": {"value": "servers"},
                        "vlan_group": {
                            "tag": [{"vlan_id": {"value": 10}, "tag": {"subinterface_id": {"value": 10}, "mac_vrf": {"value": "vlan-10"}}}]
                        },
                    }
                ]
            }
        }
        self.candidate_if["interface"][0]["interface"]["vlan_groups"] = {
            "vlan_group": [{"name": {"value": "servers"}}]
        }
        self.state.start_location = SimpleNamespace(path="/interface[name=ethernet-1/1]")
        self.state.location = SimpleNamespace(path="/interface[name=ethernet-1/1]/vlan-groups/vlan-group[name=servers]")

        vg_container_cmd = SimpleNamespace(
            name="vlan-groups",
            local_arguments={},
            has_schema=True,
            node=SimpleNamespace(
                name="vlan-groups",
                type="yang_based_config",
                has_schema=True,
                schema_or_none=SimpleNamespace(is_node=True, is_leaflist=False),
            ),
        )
        vg_item_cmd = SimpleNamespace(
            name="vlan-group",
            local_arguments={"name": "servers"},
            has_schema=True,
            node=SimpleNamespace(
                name="vlan-group",
                type="yang_based_config",
                has_schema=True,
                schema_or_none=SimpleNamespace(is_node=True, is_leaflist=False),
            ),
        )
        self.observer.after_executing(
            self.state,
            "vlan-groups vlan-group servers",
            [vg_container_cmd, vg_item_cmd],
        )
        self.assertEqual(len(self.set_calls), 1)
        upds, _, _ = self.set_calls[-1]
        self.assertIn("/interface[name=ethernet-1/1]/subinterface[index=10]/vlan/encap/single-tagged/vlan-id:10", upds)
        self.assertIn("/network-instance[name=vlan-10]/interface[name=ethernet-1/1.10]", upds)


if __name__ == "__main__":
    unittest.main()
