"""Offline tests for bambu_filament_manager.py (no OrcaSlicer, no network).

Run from the plugin folder:  python -m unittest tests.test_offline -v
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import glob as _glob
import importlib.util as _ilu
_plugin_file = sorted(_glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bambu_filament_manager*.py")))[0]
_spec = _ilu.spec_from_file_location("bambu_filament_manager", _plugin_file)
fm = _ilu.module_from_spec(_spec)
sys.modules["bambu_filament_manager"] = fm
_spec.loader.exec_module(fm)
RFID = "C0150FF4913D492495F86923A61F8195"

PUSH_STATUS = {
    "print": {
        "command": "push_status", "gcode_state": "IDLE", "subtask_name": "", "mc_percent": 100,
        "ams": {
            "tray_exist_bits": "7", "tray_now": "255", "ams_exist_bits": "1",
            "ams": [{
                "id": "0", "humidity": "3", "humidity_raw": "41", "temp": "24.5", "info": "11942E23",
                "tray": [
                    {"id": "0", "tray_type": "PLA", "tray_info_idx": "GFA00", "tray_color": "FF6A13FF",
                     "tray_uuid": RFID, "tag_uid": "0123456789ABCDEF", "remain": 62, "tray_weight": "1000",
                     "tray_sub_brands": "PLA Basic", "tray_id_name": "A00-A0", "cols": ["FF6A13FF"]},
                    {"id": "1", "tray_type": "PETG", "tray_info_idx": "GFG99", "tray_color": "000000FF",
                     "tray_uuid": "00000000000000000000000000000000", "tag_uid": "0000000000000000",
                     "remain": -1, "tray_weight": "0", "tray_sub_brands": "", "cols": ["000000FF"]},
                    {"id": "2", "tray_type": "PLA", "tray_info_idx": "GFA01", "tray_color": "FFFFFFFF",
                     "tray_uuid": "0000000000000000", "remain": 10, "tray_weight": "1000", "cols": ["FFFFFFFF"]},
                    {"id": "3"},
                ],
            }],
        },
        "vt_tray": {"id": "254", "tray_type": "", "tray_info_idx": "", "tray_color": "00000000"},
    }
}

GET_VERSION = {"info": {"command": "get_version", "module": [
    {"name": "ota", "project_name": "P1S", "sn": "01P00A000000001"},
    {"name": "ams/0", "sn": "AMS000SERIAL", "sw_ver": "00.00.06.49", "hw_ver": "AMS08"},
]}}

CATALOG = {"categories": ["PLA", "PETG"], "filamentSettings": [
    {"filamentVendor": "Bambu Lab", "filamentType": "PLA", "filamentName": "PLA Basic", "filamentId": "GFA00", "isSupport": False},
    {"filamentVendor": "Bambu Lab", "filamentType": "PLA", "filamentName": "PLA Matte", "filamentId": "GFA01", "isSupport": False},
]}

SPOOLS = [
    {"id": 1, "createType": "ams", "filamentVendor": "Bambu Lab", "filamentType": "PLA", "filamentName": "PLA Basic",
     "filamentId": "GFA00", "RFID": RFID, "color": "#FF6A13", "colorType": 2, "netWeight": 900, "totalNetWeight": 1000,
     "note": "", "updatedAt": 1777418842, "status": 0, "trayIdName": "A00-A0"},
    {"id": 2, "createType": "manual", "filamentVendor": "Bambu Lab", "filamentType": "PLA", "filamentName": "PLA Matte",
     "filamentId": "GFA01", "RFID": "", "color": "#FFFFFF", "netWeight": 500, "totalNetWeight": 1000, "note": "white",
     "devId": "01P00A000000001", "deviceName": "P1S", "amsId": 0, "slotId": "1", "inPrinter": True},
    {"id": 3, "createType": "manual", "filamentVendor": "Sunlu", "filamentType": "PETG", "filamentName": "PETG Black",
     "filamentId": "", "RFID": "", "color": "#000000", "netWeight": 250, "totalNetWeight": 1000, "note": ""},
]


def log_line(dev_id, obj, kind="origin"):
    return ("[info]\t2026-10-05 12:00:00.000000[Thread 0x0001]:parse_json: dev_id=%s, %s playload=%s"
            % (dev_id, kind, json.dumps(obj, separators=(",", ":"))))


class FakeClient:
    """Records calls and answers like the cloud would."""

    def __init__(self, spools=None, catalog=None):
        self.spools = [dict(s) for s in (spools if spools is not None else SPOOLS)]
        self.catalog = catalog if catalog is not None else CATALOG
        self.calls = []
        self.next_id = 100

    def list_spools(self):
        self.calls.append(("list",))
        return [dict(s) for s in self.spools]

    def get_catalog(self):
        self.calls.append(("catalog",))
        return self.catalog

    def list_devices(self):
        self.calls.append(("devices",))
        return [{"dev_id": "01P00A000000001", "name": "Workshop P1S", "dev_product_name": "P1S"}]

    def create_spool(self, body):
        self.calls.append(("create", body))
        s = dict(body)
        s["id"] = self.next_id
        self.next_id += 1
        s.setdefault("updatedAt", int(time.time()))
        self.spools.append(s)
        return {}

    def update_spool(self, body):
        self.calls.append(("update", body))
        for s in self.spools:
            if s["id"] == body["id"]:
                s.update(body)
                return {"filamentV2": dict(s)}
        raise fm.CloudError(404, "no such spool")

    def delete_spools(self, ids):
        self.calls.append(("delete", list(ids)))
        self.spools = [s for s in self.spools if s["id"] not in set(int(i) for i in ids)]
        return {}

    def ams_sync(self, dev_id, items):
        self.calls.append(("ams_sync", dev_id, items))
        return {"createdRFIDs": None, "results": [{"amsSn": i["amsSn"], "slotId": i["slotId"], "spoolId": 1} for i in items]}

    def slot_mappings_sync(self, dev_id, mappings):
        self.calls.append(("slot_mappings", dev_id, mappings))
        return {"results": [{"code": 0} for _ in mappings]}

    # soft-match queue: by default the "server" records unknown RFIDs straight away;
    # with park=True it parks them and proposes candidates instead.
    park = False
    parked = {}

    def ams_sync(self, dev_id, items):  # noqa: F811  (overrides the simple version above)
        self.calls.append(("ams_sync", dev_id, items))
        created = []
        for it in items:
            rfid = it.get("RFID")
            if rfid and not any(s.get("RFID") == rfid for s in self.spools):
                if self.park:
                    hit = dict(it, id=self.next_id, createType="ams")
                    self.next_id += 1
                    cands = [s for s in self.spools if s.get("filamentId") == it.get("filamentId")]
                    self.parked[rfid] = (hit, cands)
                else:
                    s = dict(it, id=self.next_id, createType="ams", updatedAt=int(time.time()))
                    self.next_id += 1
                    self.spools.append(s)
                    created.append(rfid)
        return {"createdRFIDs": created or None, "results": []}

    def soft_match_pending(self, dev_id="", ams_sn=""):
        self.calls.append(("soft_match_get", dev_id, ams_sn))
        hits = [h for h, _ in self.parked.values()]
        cands = [{"pendingId": h["id"], "candidates": c} for h, c in self.parked.values()]
        return {"hits": hits, "candidates": cands}

    def soft_match_action(self, action, spool_id, target_spool_id=0):
        self.calls.append(("soft_match_post", action, spool_id, target_spool_id))
        for rfid, (hit, cands) in list(self.parked.items()):
            if hit["id"] == spool_id:
                del self.parked[rfid]
                if action == "create_new":
                    self.spools.append(dict(hit))
                elif action in ("accept", "link_other"):
                    target = target_spool_id if action == "link_other" else (cands[0]["id"] if cands else 0)
                    for s in self.spools:
                        if s["id"] == target:
                            s["RFID"] = rfid
                return {}
        raise fm.CloudError(404, "The server cannot find the requested resource.")


def make_core(tmp, client=None):
    with open(os.path.join(tmp, "obn.auth.json"), "w", encoding="utf-8") as fh:
        json.dump({"region": "GLOBAL", "access_token": "tok", "refresh_token": "r", "nick_name": "Elliott",
                   "expires_at": "2099-01-01T00:00:00Z", "user_id": "1"}, fh)
    with open(os.path.join(tmp, "obn.state.json"), "w", encoding="utf-8") as fh:
        json.dump({"selected_machine": "01P00A000000001"}, fh)
    core = fm.FilamentManagerCore(base_dir=tmp, client=client or FakeClient(), start_tailer=False)
    core.state.ingest("01P00A000000001", PUSH_STATUS)
    core.state.ingest("01P00A000000001", GET_VERSION)
    return core


class LogParsing(unittest.TestCase):
    def test_origin_and_merged_lines(self):
        for kind in ("origin", "merged"):
            r = fm.parse_log_line(log_line("01P00A000000001", PUSH_STATUS, kind))
            self.assertIsNotNone(r)
            self.assertEqual(r[0], "01P00A000000001")
            self.assertEqual(r[1]["print"]["command"], "push_status")

    def test_trace_line_with_tunnel(self):
        line = "[trace]\t2026-10-05 12:00:00.000000[Thread 0x1]:parse_json: dev_id=ABC, tunnel is=cloud, merged playload={\"print\":{\"gcode_state\":\"RUNNING\"}}"
        r = fm.parse_log_line(line)
        self.assertEqual(r[1]["print"]["gcode_state"], "RUNNING")

    def test_rejects_other_lines(self):
        self.assertIsNone(fm.parse_log_line("[info] something else"))
        self.assertIsNone(fm.parse_log_line("parse_json: dev_id=X, origin playload={not json"))


class StateMerge(unittest.TestCase):
    def test_ingest_and_partial_update(self):
        st = fm.DeviceState()
        st.ingest("DEV", PUSH_STATUS)
        st.ingest("DEV", GET_VERSION)
        st.ingest("DEV", {"print": {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_info_idx": "GFA00",
                                                                                  "tray_uuid": RFID, "remain": 55, "tray_weight": "1000",
                                                                                  "tray_color": "FF6A13FF"}]}]}}})
        st.ingest("DEV", {"print": {"gcode_state": "RUNNING"}})
        printers, last, n = st.snapshot()
        p = printers["DEV"]
        self.assertEqual(n, 4)
        self.assertEqual(p["gcode_state"], "RUNNING")
        self.assertEqual(p["ams"]["0"]["trays"]["0"]["remain"], 55)
        self.assertEqual(p["ams"]["0"]["trays"]["1"]["tray_type"], "PETG")   # untouched by the partial frame
        self.assertEqual(p["ams"]["0"]["humidity"], "3")
        self.assertEqual(p["ams_sn"]["0"], "AMS000SERIAL")
        self.assertEqual(p["model"], "P1S")

    def test_helpers(self):
        self.assertEqual(fm.ams_unit_label(0), "A")
        self.assertEqual(fm.ams_unit_label("3"), "D")
        self.assertEqual(fm.ams_unit_label(128), "HT1")
        self.assertEqual(fm.ams_type_from_info("11942E23"), 3)
        self.assertEqual(fm.ams_type_from_info(None, 128), 4)
        self.assertEqual(fm.norm_hex("ff6a13ff"), "#FF6A13")
        self.assertEqual(fm.norm_hex("#898989"), "#898989")
        self.assertEqual(fm.norm_hex("nope"), "")
        self.assertEqual(fm.remain_grams({"remain": 62}, 1000), 620)
        self.assertIsNone(fm.remain_grams({"remain": -1}, 1000))
        self.assertIsNone(fm.remain_grams({"remain": 50}, 0))
        self.assertTrue(fm.tray_is_empty({"id": "3"}))
        self.assertTrue(fm.tray_is_empty({"id": "3", "tray_type": "PLA"}, "7", "0"))   # exist bit 3 clear
        self.assertFalse(fm.tray_is_empty({"id": "0", "tray_type": "PLA"}, "7", "0"))


class Views(unittest.TestCase):
    def test_printers_view_matching(self):
        st = fm.DeviceState()
        st.ingest("01P00A000000001", PUSH_STATUS)
        st.ingest("01P00A000000001", GET_VERSION)
        printers, _, _ = st.snapshot()
        view = fm.build_printers_view(printers, CATALOG, SPOOLS, {"01P00A000000001": {"name": "Workshop P1S", "dev_product_name": "P1S"}},
                                      "01P00A000000001")
        self.assertEqual(len(view), 1)
        p = view[0]
        self.assertEqual(p["name"], "Workshop P1S")
        self.assertTrue(p["selected"])
        self.assertFalse(p["busy"])
        u = p["units"][0]
        self.assertEqual((u["label"], u["type_name"], u["sn"]), ("A", "AMS 2 Pro", "AMS000SERIAL"))
        s0, s1, s2, s3 = u["slots"]
        self.assertEqual(s0["label"], "A1")
        self.assertTrue(s0["rfid"])
        self.assertEqual(s0["match"]["kind"], "rfid")
        self.assertEqual(s0["match"]["computed_net"], 620)
        self.assertEqual(s0["name"], "PLA Basic")           # from catalogue via tray_info_idx
        self.assertEqual(s0["color"], "#FF6A13")
        self.assertFalse(s1["rfid"])
        self.assertIsNone(s1["match"])                       # GFG99 unknown, black: no soft match (spool 3 has no filamentId)
        self.assertEqual(s2["match"]["kind"], "soft")        # GFA01 + white -> spool 2
        self.assertEqual(s2["match"]["id"], 2)
        self.assertTrue(s3["empty"])
        self.assertIsNone(p["external"])
        # soft matching can be turned off
        view2 = fm.build_printers_view(printers, CATALOG, SPOOLS, {}, "", soft_match=False)
        self.assertIsNone(view2[0]["units"][0]["slots"][2]["match"])

    def test_create_from_tray_and_sync_item(self):
        tray = PUSH_STATUS["print"]["ams"]["ams"][0]["tray"][0]
        body = fm.build_create_from_tray(tray, CATALOG)
        self.assertEqual(body["createType"], "ams")
        self.assertEqual(body["RFID"], RFID)
        self.assertEqual(body["filamentName"], "PLA Basic")
        self.assertEqual(body["netWeight"], 620)
        self.assertEqual(body["totalNetWeight"], 1000)
        self.assertEqual(body["color"], "#FF6A13FF")
        self.assertEqual(body["trayIdName"], "A00-A0")
        manual = fm.build_create_from_tray(PUSH_STATUS["print"]["ams"]["ams"][0]["tray"][1], CATALOG)
        self.assertEqual(manual["createType"], "manual")
        self.assertNotIn("RFID", manual)
        self.assertEqual(manual["filamentType"], "PETG")
        item = fm.build_ams_sync_item(tray, "0", "AMS000SERIAL", 3, SPOOLS[0])
        self.assertEqual(sorted(item.keys()), sorted(["RFID", "amsId", "amsSn", "amsType", "color", "colorType", "colors", "createNew",
                                                      "filamentId", "filamentName", "filamentType", "filamentVendor", "isSupport",
                                                      "netWeight", "note", "slotId", "totalNetWeight", "trayIdName"]))
        self.assertEqual(item["netWeight"], 620)
        self.assertEqual(item["slotId"], "0")
        self.assertEqual(item["amsType"], 3)

    def test_spool_location(self):
        self.assertEqual(fm.spool_location(SPOOLS[1]), "P1S A2")
        self.assertEqual(fm.spool_location(SPOOLS[2]), "")

    def test_icon(self):
        png = fm.icon_png_bytes(16)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IEND", png)


class Core(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client = FakeClient()
        self.core = make_core(self.tmp, self.client)

    def test_auth(self):
        st = self.core.auth.status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["user"], "Elliott")
        self.assertEqual(st["host"], "api.bambulab.com")
        empty = fm.Auth(tempfile.mkdtemp()).refresh().status()
        self.assertFalse(empty["ok"])
        self.assertIn("obn.auth.json", empty["error"])

    def test_ready_pulls_and_state(self):
        r = self.core.handle({"action": "ready"})
        self.assertTrue(r["ok"])
        st = r["state"]
        self.assertEqual(len(st["spools"]), 3)
        self.assertEqual(st["catalog_size"], 2)
        self.assertEqual(st["printers"][0]["name"], "Workshop P1S")
        self.assertEqual(st["log"]["payloads"], 2)
        self.assertEqual(st["log"]["error"], "log tailer not running")
        s1 = [s for s in st["spools"] if s["id"] == 1][0]
        self.assertEqual(s1["_location"], "Workshop P1S A1")
        self.assertTrue(s1["_live_here"])
        s3 = [s for s in st["spools"] if s["id"] == 3][0]
        self.assertEqual(s3["_location"], "")
        self.assertIn(("list",), self.client.calls)

    def test_create_update_delete(self):
        self.core.handle({"action": "ready"})
        r = self.core.handle({"action": "create", "spool": {"filamentVendor": "Sunlu", "filamentType": "PLA", "filamentName": "PLA Red",
                                                            "color": "#FF0000", "totalNetWeight": 1000, "netWeight": 1000, "note": "n"}})
        self.assertTrue(r["ok"], r.get("error"))
        created = [c for c in self.client.calls if c[0] == "create"][0][1]
        self.assertEqual(created["createType"], "manual")
        self.assertEqual(created["color"], "#FF0000")
        self.assertEqual(len(r["state"]["spools"]), 4)
        r = self.core.handle({"action": "update", "spool": {"id": 3, "filamentName": "PETG Black", "netWeight": 200, "note": "low",
                                                            "color": "#000000", "filamentVendor": "Sunlu", "filamentType": "PETG",
                                                            "totalNetWeight": 1000, "filamentId": ""}})
        self.assertTrue(r["ok"], r.get("error"))
        upd = [c for c in self.client.calls if c[0] == "update"][-1][1]
        self.assertEqual(upd, {"id": 3, "filamentName": "PETG Black", "netWeight": 200, "note": "low"})
        self.assertEqual([s for s in self.core.spools if s["id"] == 3][0]["netWeight"], 200)
        r = self.core.handle({"action": "delete", "ids": [3]})
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["state"]["spools"]), 3)

    def test_add_from_tray(self):
        self.core.handle({"action": "ready"})
        r = self.core.handle({"action": "add_from_tray", "dev_id": "01P00A000000001", "ams_id": "0", "tray_id": "0"})
        self.assertFalse(r["ok"])
        self.assertIn("already", r["error"])
        r = self.core.handle({"action": "add_from_tray", "dev_id": "01P00A000000001", "ams_id": "0", "tray_id": "1"})
        self.assertTrue(r["ok"], r.get("error"))
        created = [c for c in self.client.calls if c[0] == "create"][-1][1]
        self.assertEqual(created["filamentType"], "PETG")
        self.assertEqual(created["createType"], "manual")
        r = self.core.handle({"action": "add_from_tray", "dev_id": "01P00A000000001", "ams_id": "0", "tray_id": "3"})
        self.assertFalse(r["ok"])

    def test_sync_ams_and_unbind(self):
        self.core.handle({"action": "ready"})
        r = self.core.handle({"action": "sync_ams", "dev_id": "01P00A000000001", "force": True})
        self.assertTrue(r["ok"], r.get("error"))
        syncs = [c for c in self.client.calls if c[0] == "ams_sync"]
        self.assertEqual(len(syncs), 1)
        self.assertEqual(syncs[0][1], "01P00A000000001")
        self.assertEqual([i["RFID"] for i in syncs[0][2]], [RFID])
        self.assertEqual(syncs[0][2][0]["amsSn"], "AMS000SERIAL")
        # spool 2 claims slot A2 but A2 holds a non-RFID PETG tray -> unbind
        unb = [c for c in self.client.calls if c[0] == "slot_mappings"]
        self.assertEqual(len(unb), 1)
        self.assertEqual(unb[0][2], [{"amsId": 0, "amsSn": "AMS000SERIAL", "amsType": 3, "rfid": None, "slotId": "1", "spoolId": None}])
        # a second non-forced sync with nothing changed sends nothing new
        n_before = len(self.client.calls)
        self.core.sync_ams("01P00A000000001", force=False)
        self.assertEqual([c[0] for c in self.client.calls[n_before:] if c[0] == "ams_sync"], [])

    def test_sync_two_units_without_serials(self):
        """Two AMS units, no get_version seen: one request per unit, serial taken
        from a cloud spool record when it has one (the 'duplicate bind slot' bug)."""
        rfid_b = "D" * 32
        spool_b = dict(SPOOLS[0], id=7, RFID=rfid_b, filamentName="PLA Basic B", devId="01P00A000000001", amsId=1, amsSn="AMSB_SERIAL", slotId="0")
        client = FakeClient(spools=SPOOLS + [spool_b])
        core = make_core(tempfile.mkdtemp(), client)
        core.state.printers["01P00A000000001"]["ams_sn"] = {}               # forget the version reply
        core.state.ingest("01P00A000000001", {"print": {"ams": {"tray_exist_bits": "17", "ams": [{"id": "1", "tray": [
            dict(PUSH_STATUS["print"]["ams"]["ams"][0]["tray"][0], tray_uuid=rfid_b)]}]}}})
        core.handle({"action": "ready"})
        r = core.handle({"action": "sync_ams", "dev_id": "01P00A000000001", "force": True})
        self.assertTrue(r["ok"], r.get("error"))
        syncs = [c for c in client.calls if c[0] == "ams_sync"]
        self.assertEqual(len(syncs), 2)                                      # one per unit
        by_unit = {items[0]["amsId"]: items for _, _, items in syncs}
        self.assertEqual(sorted(by_unit), [0, 1])
        self.assertEqual(by_unit[0][0]["amsSn"], "")                         # unit A: nothing known
        self.assertEqual(by_unit[1][0]["amsSn"], "AMSB_SERIAL")              # unit B: from the spool record
        self.assertEqual(r["state"]["printers"][0]["units"][1]["sn"], "AMSB_SERIAL")

    def test_push_remaining_and_throttle(self):
        self.core.handle({"action": "ready"})
        n = self.core.push_remaining()
        self.assertEqual(n, 1)
        upd = [c for c in self.client.calls if c[0] == "update"][-1][1]
        self.assertEqual(upd, {"id": 1, "filamentName": "PLA Basic", "netWeight": 620})
        self.assertEqual(self.core.push_remaining(), 0)                   # no change
        # printer busy, remain changes -> throttled for 10 min
        self.core.state.ingest("01P00A000000001", {"print": {"gcode_state": "RUNNING", "ams": {"ams": [{"id": "0", "tray": [
            dict(PUSH_STATUS["print"]["ams"]["ams"][0]["tray"][0], remain=60)]}]}}})
        self.assertEqual(self.core.push_remaining(), 0)
        self.core.last_push[RFID] = (time.time() - 700, 620)
        self.assertEqual(self.core.push_remaining(), 1)
        self.assertEqual([c for c in self.client.calls if c[0] == "update"][-1][1]["netWeight"], 600)

    def test_tick_respects_settings(self):
        self.core.handle({"action": "ready"})
        self.core.handle({"action": "settings", "settings": {"auto_push_remaining": False, "auto_sync_mounts": False}})
        n_before = len(self.client.calls)
        r = self.core.handle({"action": "tick"})
        self.assertTrue(r["ok"])
        self.assertEqual(len(self.client.calls), n_before)
        self.assertFalse(r["state"]["settings"]["auto_push_remaining"])

    def test_cloud_error_is_reported(self):
        class Boom(FakeClient):
            def list_spools(self):
                raise fm.CloudError(401, "token rejected")
        core = make_core(self.tmp, Boom())
        r = core.handle({"action": "refresh"})
        self.assertFalse(r["ok"])
        self.assertIn("token rejected", r["error"])
        self.assertIn("token rejected", r["state"]["last_error"])


class FakeHost:
    """Stand-in for the orca.host device bindings of the custom build."""

    def __init__(self):
        self.status = {"01P00A000000001": (1, json.dumps(PUSH_STATUS))}
        self.calls = []

    def devices(self):
        self.calls.append("devices")
        return [{"dev_id": "01P00A000000001", "dev_name": "Bench P1S", "printer_type": "C12", "is_connected": True, "is_selected": True}]

    def selected_device_id(self):
        return "01P00A000000001"

    def device_status_seq(self, dev_id):
        return self.status.get(dev_id, (0, None))[0]

    def device_status(self, dev_id):
        self.calls.append("status")
        return self.status.get(dev_id, (0, None))[1]


class HostApi(unittest.TestCase):
    def test_prefers_host_bindings_over_log(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "obn.auth.json"), "w", encoding="utf-8") as fh:
            json.dump({"region": "GLOBAL", "access_token": "tok", "expires_at": "2099-01-01T00:00:00Z"}, fh)
        host = FakeHost()
        core = fm.FilamentManagerCore(base_dir=tmp, client=FakeClient(), host=host, start_tailer=True)
        self.assertIsNone(core.tailer)                       # no log tailer when the host feed exists
        r = core.handle({"action": "ready"})
        st = r["state"]
        self.assertEqual(st["log"]["source"], "host")
        self.assertEqual(st["log"]["payloads"], 1)
        self.assertEqual(st["printers"][0]["name"], "Bench P1S")   # host name wins over the cloud bind list
        self.assertTrue(st["printers"][0]["selected"])
        self.assertEqual(st["printers"][0]["units"][0]["slots"][0]["match"]["kind"], "rfid")
        # unchanged seq -> no re-read; new seq -> re-read and merge
        core.handle({"action": "state"})
        self.assertEqual(host.calls.count("status"), 1)
        host.status["01P00A000000001"] = (2, json.dumps({"print": {"gcode_state": "RUNNING"}}))
        r = core.handle({"action": "state"})
        self.assertEqual(host.calls.count("status"), 2)
        self.assertTrue(r["state"]["printers"][0]["busy"])
        self.assertEqual(r["state"]["printers"][0]["units"][0]["slots"][0]["type"], "PLA")   # earlier trays kept

    def test_not_available_without_all_functions(self):
        class Partial:
            def devices(self):
                return []
        self.assertFalse(fm.HostFeed.available(Partial()))
        self.assertFalse(fm.HostFeed.available(None))
        self.assertTrue(fm.HostFeed.available(FakeHost()))


NEW_RFID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
NEW_TRAY_FRAME = {"print": {"ams": {"tray_exist_bits": "F", "ams": [{"id": "0", "tray": [
    {"id": "3", "tray_type": "PLA", "tray_info_idx": "GFA01", "tray_color": "F0F0F0FF", "tray_uuid": NEW_RFID,
     "tag_uid": "0000FEDCBA98" + "0100", "remain": 100, "tray_weight": "1000", "tray_sub_brands": "PLA Matte",
     "tray_id_name": "A01-W0", "cols": ["F0F0F0FF"], "ctype": 0}]}]}}}


class NewRfidSpools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client = FakeClient()
        self.client.parked = {}
        self.core = make_core(self.tmp, self.client)
        self.notes = []
        self.core.notify_fn = lambda text, important=False: self.notes.append((important, text))
        self.core.handle({"action": "ready"})

    def test_known_and_unofficial_trays_are_ignored(self):
        self.assertEqual([c for c in self.client.calls if c[0] == "ams_sync"], [])
        self.assertEqual(self.core.badges, {})
        self.assertFalse(fm.is_official_rfid_tray(PUSH_STATUS["print"]["ams"]["ams"][0]["tray"][0]))   # tag bytes 12..13 are "CD"
        self.assertTrue(fm.is_official_rfid_tray(NEW_TRAY_FRAME["print"]["ams"]["ams"][0]["tray"][0]))

    def test_recorded_by_server(self):
        self.core.state.ingest("01P00A000000001", NEW_TRAY_FRAME)
        r = self.core.handle({"action": "state"})
        syncs = [c for c in self.client.calls if c[0] == "ams_sync"]
        self.assertEqual(len(syncs), 1)
        item = syncs[0][2][0]
        self.assertEqual(item["RFID"], NEW_RFID)
        self.assertEqual((item["filamentName"], item["filamentVendor"], item["amsSn"], item["slotId"], item["createNew"]),
                         ("PLA Matte", "Bambu Lab", "AMS000SERIAL", "3", False))
        self.assertEqual(item["netWeight"], 1000)
        badges = r["state"]["badges"]
        self.assertEqual(len(badges), 1)
        self.assertEqual(badges[0]["kind"], "recorded")
        self.assertEqual(badges[0]["slot"], "A4")
        self.assertEqual(badges[0]["spool"]["RFID"], NEW_RFID)
        self.assertEqual(r["state"]["printers"][0]["units"][0]["slots"][3]["badge"], "recorded")
        self.assertEqual(r["state"]["printers"][0]["units"][0]["slots"][3]["match"]["kind"], "rfid")
        self.assertEqual(len(self.notes), 1)
        self.assertFalse(self.notes[0][0])
        # a second poll does not register it again; OK clears the card
        self.core.handle({"action": "state"})
        self.assertEqual(len([c for c in self.client.calls if c[0] == "ams_sync"]), 1)
        r = self.core.handle({"action": "badge", "key": badges[0]["key"], "choice": "ok"})
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["state"]["badges"], [])

    def test_parked_with_candidates_link_other(self):
        self.client.park = True
        self.core.state.ingest("01P00A000000001", NEW_TRAY_FRAME)
        r = self.core.handle({"action": "state"})
        b = r["state"]["badges"][0]
        self.assertEqual(b["kind"], "decision")
        self.assertEqual([c["id"] for c in b["candidates"]], [2])          # PLA Matte GFA01 in the library
        self.assertTrue(self.notes[0][0])                                  # important notification
        self.assertIn("new filament", self.notes[0][1].lower())
        r = self.core.handle({"action": "badge", "key": b["key"], "choice": "link", "target_id": 2})
        self.assertTrue(r["ok"], r.get("error"))
        post = [c for c in self.client.calls if c[0] == "soft_match_post"][-1]
        self.assertEqual(post[1:], ("link_other", b["hit"]["id"], 2))
        self.assertEqual(r["state"]["badges"], [])
        linked = [s for s in r["state"]["spools"] if s["id"] == 2][0]
        self.assertEqual(linked["RFID"], NEW_RFID)
        self.assertEqual(r["state"]["printers"][0]["units"][0]["slots"][3]["match"]["id"], 2)

    def test_parked_accept_and_create_new(self):
        self.client.park = True
        self.core.state.ingest("01P00A000000001", NEW_TRAY_FRAME)
        b = self.core.handle({"action": "state"})["state"]["badges"][0]
        # choosing the hit itself (or nothing) means "accept"
        self.core.handle({"action": "badge", "key": b["key"], "choice": "link", "target_id": b["hit"]["id"]})
        self.assertEqual([c for c in self.client.calls if c[0] == "soft_match_post"][-1][1], "accept")
        # new spool, parked again, user says add as new
        self.core.state.ingest("01P00A000000001", {"print": {"ams": {"ams": [{"id": "0", "tray": [
            dict(NEW_TRAY_FRAME["print"]["ams"]["ams"][0]["tray"][0], tray_uuid="B" * 32)]}]}}})
        b2 = self.core.handle({"action": "state"})["state"]["badges"][0]
        self.assertEqual(b2["uuid"], "B" * 32)
        r = self.core.handle({"action": "badge", "key": b2["key"], "choice": "new"})
        self.assertEqual([c for c in self.client.calls if c[0] == "soft_match_post"][-1][1:3], ("create_new", b2["hit"]["id"]))
        self.assertTrue(any(s.get("RFID") == "B" * 32 for s in r["state"]["spools"]))

    def test_later_and_setting_off(self):
        self.client.park = True
        self.core.state.ingest("01P00A000000001", NEW_TRAY_FRAME)
        b = self.core.handle({"action": "state"})["state"]["badges"][0]
        r = self.core.handle({"action": "badge", "key": b["key"], "choice": "later"})
        self.assertEqual(r["state"]["badges"], [])
        self.core.handle({"action": "state"})
        self.assertEqual(len([c for c in self.client.calls if c[0] == "ams_sync"]), 1)   # not re-registered
        core2 = make_core(tempfile.mkdtemp(), FakeClient())
        core2.handle({"action": "ready"})
        core2.handle({"action": "settings", "settings": {"auto_register_rfid": False}})
        core2.state.ingest("01P00A000000001", NEW_TRAY_FRAME)
        core2.handle({"action": "state"})
        self.assertEqual([c for c in core2.client.calls if c[0] == "ams_sync"], [])

    def test_badge_dropped_when_spool_removed(self):
        self.client.park = True
        self.core.state.ingest("01P00A000000001", NEW_TRAY_FRAME)
        self.core.handle({"action": "state"})
        self.assertEqual(len(self.core.badges), 1)
        self.core.state.ingest("01P00A000000001", {"print": {"ams": {"ams": [{"id": "0", "tray": [
            dict(NEW_TRAY_FRAME["print"]["ams"]["ams"][0]["tray"][0], tray_uuid="C" * 32)]}]}}})
        self.core.handle({"action": "state"})
        self.assertEqual([b["uuid"] for b in self.core.badges.values()], ["C" * 32])


class Tailer(unittest.TestCase):
    def test_follows_newest_file(self):
        tmp = tempfile.mkdtemp()
        log_dir = os.path.join(tmp, "log")
        os.makedirs(log_dir)
        old = os.path.join(log_dir, "debug_Mon_Oct_05_09_00_00_1.log.0")
        with open(old, "w", encoding="utf-8") as fh:
            fh.write(log_line("OLD", {"print": {"gcode_state": "IDLE"}}) + "\n")
        time.sleep(0.05)
        new = os.path.join(log_dir, "debug_Mon_Oct_05_10_00_00_2.log.0")
        with open(new, "w", encoding="utf-8") as fh:
            fh.write("[info] header line\n")
            fh.write(log_line("NEW", PUSH_STATUS) + "\n")
        state = fm.DeviceState()
        t = fm.LogTailer(log_dir, state)
        t.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and state.payload_count < 1:
                time.sleep(0.05)
            printers, _, n = state.snapshot()
            self.assertIn("NEW", printers)
            self.assertNotIn("OLD", printers)
            with open(new, "a", encoding="utf-8") as fh:
                fh.write(log_line("NEW", GET_VERSION) + "\n")
            deadline = time.time() + 5
            while time.time() < deadline and state.payload_count < 2:
                time.sleep(0.05)
            printers, _, _ = state.snapshot()
            self.assertEqual(printers["NEW"]["ams_sn"]["0"], "AMS000SERIAL")
            self.assertEqual(os.path.basename(t.current), os.path.basename(new))
        finally:
            t.stop_event.set()
            t.join(3)


GCODE_HEAD = """; HEADER_BLOCK_START
; generated by OrcaSlicer 2.5.0-dev on 2026-10-08
; model printing time: 14m 8s; total estimated time: 20m 7s
; total layer number: 41
; filament_density: 1.24,1.24,1.27
; filament_diameter: 1.75,1.75,1.75
; max_z_height: 8.20
; filament: 1,3
; HEADER_BLOCK_END
"""
GCODE_TAIL = """; filament used [mm] = 1113.07, 0, 450.10
; filament used [cm3] = 2.68, 0, 1.08
; filament used [g] = 12.34, 0, 5.60
; filament cost = 0.25, 0, 0.11
; total filament used [g] = 17.94
; CONFIG_BLOCK_START
; filament_colour = #FF6A13;#FFFFFF;#000000
; filament_ids = GFA00;GFA01;GFG99
; filament_settings_id = "Bambu PLA Basic @BBL X1C";"Bambu PLA Matte @BBL X1C";"Sunlu PETG"
; filament_type = PLA;PLA;PETG
; filament_vendor = "Bambu Lab";"Bambu Lab";"Sunlu"
; printer_settings_id = "Bambu Lab P1S 0.4 nozzle"
; CONFIG_BLOCK_END
"""


def write_gcode(body_lines):
    path = os.path.join(tempfile.mkdtemp(), "plate_1.gcode")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(GCODE_HEAD)
        fh.write("G1 X10 Y10 E0.1\n" * body_lines)
        fh.write(GCODE_TAIL)
    return path


def fil(filament_id, color, ftype, grams, name=""):
    return {"index": 0, "grams": grams, "filament_id": filament_id, "color": color, "type": ftype,
            "name": name or ftype, "vendor": ""}


class SliceCheck(unittest.TestCase):
    def printers_view(self):
        st = fm.DeviceState()
        st.ingest("01P00A000000001", PUSH_STATUS)
        printers, _, _ = st.snapshot()
        return fm.build_printers_view(printers, CATALOG, SPOOLS, {"01P00A000000001": {"name": "Workshop P1S"}}, "01P00A000000001")

    def test_parse_gcode(self):
        info = fm.parse_slice_gcode(write_gcode(50))
        self.assertEqual([f["index"] for f in info["filaments"]], [0, 2])   # extruder 2 printed nothing
        a, b = info["filaments"]
        self.assertEqual((a["grams"], a["filament_id"], a["color"], a["type"]), (12.3, "GFA00", "#FF6A13", "PLA"))
        self.assertEqual((a["name"], a["vendor"]), ("Bambu PLA Basic @BBL X1C", "Bambu Lab"))
        self.assertEqual((b["grams"], b["filament_id"], b["color"], b["type"], b["vendor"]), (5.6, "GFG99", "#000000", "PETG", "Sunlu"))
        self.assertEqual(info["total_g"], 17.9)
        self.assertEqual(info["time"], "14m 8s")
        self.assertEqual(info["printer_preset"], "Bambu Lab P1S 0.4 nozzle")

    def test_parse_reads_only_head_and_tail(self):
        path = write_gcode(20000)                                   # ~300 KB of moves in the middle
        info = fm.parse_slice_gcode(path, head_bytes=4096, tail_bytes=4096)
        self.assertEqual([f["grams"] for f in info["filaments"]], [12.3, 5.6])
        self.assertEqual(info["time"], "14m 8s")

    def test_check_against_ams(self):
        view = self.printers_view()
        a1 = view[0]["units"][0]["slots"][0]
        self.assertEqual(fm.tray_remaining(a1), (620, "library"))   # RFID spool 1: 62% of 1000 g
        a3 = view[0]["units"][0]["slots"][2]
        self.assertEqual(fm.tray_remaining(a3), (100, "library"))   # soft match spool 2: 10% of 1000 g
        a2 = view[0]["units"][0]["slots"][1]
        self.assertEqual(fm.tray_remaining(a2), (None, ""))         # non-RFID, remain -1, not in library

        def one(f):
            return fm.check_slice({"filaments": [f], "total_g": f["grams"], "when": 1, "time": ""}, view, "01P00A000000001")

        c = one(fil("GFA00", "#FF6A13", "PLA", 100))
        self.assertEqual((c["status"], c["printer"]), ("ok", "Workshop P1S"))
        self.assertEqual(c["rows"][0]["slots"][0]["label"], "A1")
        self.assertEqual(c["rows"][0]["best_g"], 620)
        self.assertEqual(one(fil("GFA00", "#FF6A13", "PLA", 700))["status"], "low")
        self.assertEqual(one(fil("GFA01", "#FFFFFF", "PLA", 50))["status"], "ok")
        self.assertEqual(one(fil("GFA01", "#FFFFFF", "PLA", 150))["status"], "low")
        self.assertEqual(one(fil("GFG99", "#000000", "PETG", 10))["status"], "unknown")
        miss = one(fil("GFA00", "#0000FF", "PLA", 10))               # blue PLA: not loaded
        self.assertEqual(miss["status"], "missing")
        self.assertEqual(miss["rows"][0]["others"], ["A1", "A3"])
        # a type+colour match stands in when the filament id differs (third-party preset)
        self.assertEqual(one(fil("GFL99", "#FF6A13", "PLA", 10))["rows"][0]["slots"][0]["label"], "A1")
        # the worst row decides
        both = fm.check_slice({"filaments": [fil("GFA00", "#FF6A13", "PLA", 10), fil("GFA00", "#0000FF", "PLA", 10)],
                               "total_g": 20, "when": 1, "time": ""}, view, "01P00A000000001")
        self.assertEqual(both["status"], "missing")
        self.assertEqual(one(fil("GFA00", "#FF6A13", "PLA", 100))["rows"][0]["status"], "ok")
        self.assertEqual(fm.check_slice({"filaments": [fil("GFA00", "#FF6A13", "PLA", 1)], "total_g": 1, "when": 1}, [], "")["status"],
                         "no_printer")

    def test_backup_spool(self):
        slot = lambda label, g: {"id": "0", "label": label, "empty": False, "filament_id": "GFA00", "color": "#FF6A13",  # noqa: E731
                                 "type": "PLA", "name": "PLA Basic", "remain_g": g, "match": None}
        view = [{"dev_id": "X", "name": "X", "units": [{"label": "A", "slots": [slot("A1", 60), slot("A2", 50)]}], "external": None}]
        def status(g):
            return fm.check_slice({"filaments": [fil("GFA00", "#FF6A13", "PLA", g)], "total_g": g, "when": 1}, view)["status"]
        self.assertEqual(status(55), "ok")
        self.assertEqual(status(80), "backup")
        self.assertEqual(status(120), "low")

    def test_messages(self):
        view = self.printers_view()
        def msg(f):
            return fm.slice_check_message(fm.check_slice({"filaments": [f], "total_g": f["grams"], "when": 1}, view, "01P00A000000001"))
        self.assertIn("enough in Workshop P1S", msg(fil("GFA00", "#FF6A13", "PLA", 100)))
        self.assertIn("A1 has ~620 g left", msg(fil("GFA00", "#FF6A13", "PLA", 700, "PLA Basic")))
        self.assertIn("not loaded in Workshop P1S", msg(fil("GFA00", "#0000FF", "PLA", 10)))
        self.assertIn("can't tell", msg(fil("GFG99", "#000000", "PETG", 10)))
        self.assertIn("no printer data", fm.slice_check_message(fm.check_slice({"filaments": [], "total_g": 0, "when": 1}, [])))

    def test_core_consumes_and_notifies_once(self):
        core = make_core(tempfile.mkdtemp())
        notes = []
        core.notify_fn = lambda text, important: notes.append((text, important))
        core.handle({"action": "ready"})
        self.assertIsNone(core.handle({"action": "state"})["state"]["slice_check"])
        info = {"filaments": [fil("GFA00", "#FF6A13", "PLA", 100)], "total_g": 100, "when": time.time(), "time": "1h", "path": "x"}
        fm.publish_slice(info)
        st = core.handle({"action": "state"})["state"]
        self.assertEqual(st["slice_check"]["status"], "ok")
        self.assertEqual(len(notes), 1)
        self.assertFalse(notes[0][1])
        self.assertIn("enough", notes[0][0])
        fm.publish_slice(dict(info, path="copy-at-export"))          # the same slice again (export): no second notification
        core.handle({"action": "state"})
        self.assertEqual(len(notes), 1)
        fm.publish_slice(dict(info, filaments=[fil("GFA00", "#FF6A13", "PLA", 700)]))
        st = core.handle({"action": "tick"})["state"]
        self.assertEqual(st["slice_check"]["status"], "low")
        self.assertEqual(len(notes), 2)
        self.assertTrue(notes[1][1])                                  # running out is the important kind
        core.handle({"action": "settings", "settings": {"slice_check_notify": False}})
        fm.publish_slice(dict(info, filaments=[fil("GFA01", "#FFFFFF", "PLA", 20)]))
        core.handle({"action": "state"})
        self.assertEqual(len(notes), 2)
        r = core.handle({"action": "recheck"})
        self.assertIn("enough", r["message"])
        self.assertIsNone(fm.take_slice())

    def test_recheck_without_slice(self):
        core = make_core(tempfile.mkdtemp())
        self.assertIn("No slice seen yet", core.handle({"action": "recheck"})["message"])


if __name__ == "__main__":
    unittest.main()
