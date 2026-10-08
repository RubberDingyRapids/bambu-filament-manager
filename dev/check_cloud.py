"""Read-only check of the cloud client against the real Bambu account.

Uses the session the Open Bambu Networking plugin saved in
%APPDATA%\\OrcaSlicer\\obn.auth.json. Only GET requests: lists spools, the
filament catalogue and bound printers, and prints shapes (no secrets).

    python dev/check_cloud.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import glob as _glob
import importlib.util as _ilu
_plugin_file = sorted(_glob.glob(os.path.join(ROOT, "bambu_filament_manager*.py")))[0]
_spec = _ilu.spec_from_file_location("bambu_filament_manager", _plugin_file)
fm = _ilu.module_from_spec(_spec)
sys.modules["bambu_filament_manager"] = fm
_spec.loader.exec_module(fm)
base = os.path.join(os.environ.get("APPDATA", ""), "OrcaSlicer")
auth = fm.Auth(base).refresh()
st = auth.status()
print("auth:", {k: v for k, v in st.items() if k != "user"} | {"user": "<set>" if st["user"] else ""})
if not st["ok"]:
    sys.exit(1)

client = fm.CloudClient(auth, ca_fallback=os.path.join(base, "obn_cloud_ca.pem"))


def mask(s):
    s = dict(s)
    if s.get("RFID"):
        s["RFID"] = s["RFID"][:4] + "…" + s["RFID"][-4:]
    for k in ("devId", "amsSn"):
        if s.get(k):
            s[k] = str(s[k])[:3] + "…"
    return s


spools = client.list_spools()
print("spools:", len(spools))
keys = sorted({k for s in spools for k in s.keys()})
print("spool keys:", keys)
for s in spools[:3]:
    print(" ", json.dumps(mask(s), ensure_ascii=False))

cat = client.get_catalog()
print("catalog keys:", sorted(cat.keys()), "entries:", len(cat.get("filamentSettings") or []), "categories:", cat.get("categories"))

devs = client.list_devices()
print("devices:", [(d.get("dev_id", "")[:3] + "…", d.get("name"), d.get("dev_product_name"), d.get("online")) for d in devs])
print("device keys:", sorted({k for d in devs for k in d.keys()}))
