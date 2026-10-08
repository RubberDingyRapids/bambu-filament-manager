# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Bambu Filament Manager"
# description = "Bambu Lab's cloud Filament Manager (spool library, AMS slot tracking, remaining-weight sync and a will-it-run-out check after every slice) inside OrcaSlicer. Needs the Open Bambu Networking plugin for the cloud login and OrcaSlicer's log level set to 'info' for live AMS data."
# author = "Elliott (Trevor Bolton Engineering Services)"
# version = "0.2.0"
# ///
"""Bambu Filament Manager for OrcaSlicer.

A pages plugin (a tab next to Prepare / Preview / Device) that shows the
Bambu Lab cloud spool library and the spools currently sitting in the AMS,
and keeps the two in step the way Bambu Studio's Filament Manager does.

How it gets its data
--------------------
* Cloud: the ``design-user-service/my/filament/v2`` REST API, using the
  session that the Open Bambu Networking (OBN) plugin stores in
  ``<data_dir>/obn.auth.json``. No separate login.
* Printer: OrcaSlicer writes every MQTT message it receives from the printer
  into ``<data_dir>/log/debug_*.log.N`` when Preferences > Log Level is
  "info" or above. A background thread tails that file and keeps the AMS
  tray state per printer. OrcaSlicer's plugin API has no device access, so
  this is the only in-process source.

The plugin API is read-only, so nothing here touches presets, the Device
tab or the printer. Writing a filament choice into an AMS slot stays with
OrcaSlicer's Device tab.

Slice check
-----------
A second capability, ``Filament check``, is a slicing-pipeline step. When it
is picked in Process settings > Others > Slicing Pipeline Plugin, OrcaSlicer
runs it right after every slice with the sliced G-code. It reads the
``; filament used [g]`` and filament id/colour comments, and the tab then
compares that with what the AMS holds and says whether the plate will run
out. The step runs on the slicing worker, so it only parses; the comparison
and the notification happen on the UI thread.
"""
import base64
import datetime as _dt
import glob
import http.client
import json
import os
import re
import ssl
import struct
import sys
import threading
import time
import urllib.parse
import zlib

try:
    import orca
except ImportError:          # imported by the offline tests / preview harness
    orca = None

PLUGIN_VERSION = "0.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))

# Studio pushes remaining weight for a spool at most every 10 min while the
# printer is busy (AmsAutoPushThrottle.h, kMinIntervalBusy).
BUSY_PUSH_INTERVAL_S = 600
LOG_TAIL_INITIAL_BYTES = 6 * 1024 * 1024
LOG_LIVE_WINDOW_S = 20
HTTP_TIMEOUT_S = 20
PAGE_SIZE = 100
MAX_PAGES = 50

AMS_TYPE_NAMES = {0: "AMS", 1: "AMS", 2: "AMS Lite", 3: "AMS 2 Pro", 4: "AMS HT"}
BUSY_STATES = {"RUNNING", "PREPARE", "PAUSE", "SLICING", "INIT"}


def _log(msg):
    print("[filament_manager] %s" % msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def data_dir():
    """OrcaSlicer's data directory, derived from where the plugin was installed.

    Local installs land in <data_dir>/orca_plugins/<name>/, cloud installs in
    <data_dir>/orca_plugins/_subscribed/<uuid>/<uuid>/. Walk up to the
    ``orca_plugins`` folder and take its parent. Falls back to %APPDATA%.
    """
    p = HERE
    for _ in range(6):
        if os.path.basename(p).lower() == "orca_plugins":
            return os.path.dirname(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "OrcaSlicer")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/OrcaSlicer")
    return os.path.expanduser("~/.config/OrcaSlicer")


# ---------------------------------------------------------------------------
# Cloud session (from the Open Bambu Networking plugin)
# ---------------------------------------------------------------------------

class Auth:
    """Reads obn.auth.json and re-reads it whenever the file changes."""

    def __init__(self, base_dir):
        self.path = os.path.join(base_dir, "obn.auth.json")
        self._mtime = None
        self.data = {}
        self.error = ""

    def refresh(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            self.data, self._mtime = {}, None
            self.error = ("No Bambu Cloud session found (obn.auth.json). Install the Open Bambu "
                          "Networking plugin and log in to your Bambu account in OrcaSlicer.")
            return self
        if mtime != self._mtime:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self.data = json.load(fh)
                self._mtime = mtime
                self.error = ""
            except Exception as e:  # noqa: BLE001
                self.data, self.error = {}, "Could not read obn.auth.json: %s" % e
        if not self.data.get("access_token"):
            self.error = self.error or "obn.auth.json has no access token. Log in to Bambu Cloud in OrcaSlicer."
        return self

    @property
    def token(self):
        return self.data.get("access_token") or ""

    @property
    def host(self):
        region = str(self.data.get("region") or "").lower()
        return "api.bambulab.cn" if region in ("china", "cn") else "api.bambulab.com"

    @property
    def user(self):
        return self.data.get("nick_name") or self.data.get("user_name") or self.data.get("account") or ""

    def expires_at(self):
        raw = self.data.get("expires_at")
        if not raw:
            return None
        try:
            return _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None

    def status(self):
        exp = self.expires_at()
        expired = bool(exp and exp < _dt.datetime.now(_dt.timezone.utc))
        return {
            "ok": bool(self.token) and not expired and not self.error,
            "user": self.user,
            "host": self.host,
            "expires_at": exp.isoformat() if exp else "",
            "error": self.error or ("Bambu Cloud session has expired. Log in again in OrcaSlicer." if expired else ""),
        }


class CloudError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class CloudClient:
    """Minimal client for the Filament Manager endpoints.

    Uses http.client rather than urllib so that OrcaSlicer's plugin audit
    hook sees the host name (api.bambulab.com) as the permission target
    instead of one full URL per request.
    """

    def __init__(self, auth, ca_fallback=None):
        self.auth = auth
        self.ca_fallback = ca_fallback
        self._ctx = None

    def _context(self, use_fallback=False):
        ctx = ssl.create_default_context()
        if use_fallback and self.ca_fallback and os.path.isfile(self.ca_fallback):
            try:
                ctx.load_verify_locations(cafile=self.ca_fallback)
            except Exception as e:  # noqa: BLE001
                _log("CA fallback failed: %r" % e)
        return ctx

    def request(self, method, path, body=None, query=None):
        if not self.auth.token:
            raise CloudError(0, self.auth.error or "Not logged in to Bambu Cloud.")
        if query:
            path = path + "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
        headers = {
            "Authorization": "Bearer " + self.auth.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "BBL-Slicer/v02.08.01.51 (OrcaSlicer; bambu_filament_manager/%s)" % PLUGIN_VERSION,
        }
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        last_err = None
        for attempt in (0, 1):
            ctx = self._context(use_fallback=(attempt == 1))
            conn = http.client.HTTPSConnection(self.auth.host, 443, timeout=HTTP_TIMEOUT_S, context=ctx)
            try:
                conn.request(method, path, body=payload, headers=headers)
                resp = conn.getresponse()
                text = resp.read().decode("utf-8", "replace")
                status = resp.status
            except ssl.SSLError as e:
                last_err = e
                conn.close()
                continue
            except OSError as e:
                conn.close()
                raise CloudError(0, "Connection to %s failed: %s" % (self.auth.host, e))
            conn.close()
            if status == 401:
                raise CloudError(401, "Bambu Cloud rejected the session token (401). Log in again in OrcaSlicer.")
            if status < 200 or status >= 300:
                msg = text.strip()
                try:
                    j = json.loads(text)
                    msg = j.get("message") or j.get("error") or msg
                except Exception:  # noqa: BLE001
                    pass
                raise CloudError(status, "%s %s -> HTTP %d: %s" % (method, path.split("?")[0], status, msg[:300]))
            if not text.strip():
                return {}
            try:
                return json.loads(text)
            except ValueError:
                raise CloudError(status, "Non-JSON reply from %s" % path)
        raise CloudError(0, "TLS error talking to %s: %s" % (self.auth.host, last_err))

    # -- filament library -------------------------------------------------
    def list_spools(self):
        spools, offset = [], 0
        for _ in range(MAX_PAGES):
            data = self.request("GET", "/v1/design-user-service/my/filament/v2",
                                query={"offset": offset, "limit": PAGE_SIZE})
            hits = [h for h in (data.get("hits") or []) if isinstance(h, dict)]
            spools.extend(hits)
            total = data.get("total")
            offset += len(hits)
            if not hits or not isinstance(total, int) or offset >= total:
                break
        return spools

    def get_catalog(self):
        return self.request("GET", "/v1/design-user-service/filament/config")

    def create_spool(self, body):
        return self.request("POST", "/v1/design-user-service/my/filament/v2", body=body)

    def update_spool(self, body):
        return self.request("PUT", "/v1/design-user-service/my/filament/v2", body=body)

    def delete_spools(self, ids):
        return self.request("DELETE", "/v1/design-user-service/my/filament/v2/batch",
                            body={"ids": [int(i) for i in ids]})

    def ams_sync(self, dev_id, items):
        return self.request("POST", "/v1/design-user-service/my/filament/v2/ams/sync",
                            body={"devId": dev_id, "items": items})

    def slot_mappings_sync(self, dev_id, mappings):
        return self.request("POST", "/v1/design-user-service/my/filament/v2/slot-mappings/sync",
                            body={"devId": dev_id, "mappings": mappings})

    def list_devices(self):
        data = self.request("GET", "/v1/iot-service/api/user/bind")
        return [d for d in (data.get("devices") or []) if isinstance(d, dict)]

    def soft_match_pending(self, dev_id="", ams_sn=""):
        """Spools the cloud parked for a user decision (Studio's "new filament" dialog)."""
        return self.request("GET", "/v1/design-user-service/my/filament/v2/soft-match/pending",
                            query={"devId": dev_id, "amsSn": ams_sn})

    def soft_match_action(self, action, spool_id, target_spool_id=0):
        return self.request("POST", "/v1/design-user-service/my/filament/v2/soft-match/pending",
                            body={"action": action, "spoolId": int(spool_id), "targetSpoolId": int(target_spool_id or 0)})


# ---------------------------------------------------------------------------
# Printer state from OrcaSlicer's log
# ---------------------------------------------------------------------------

LOG_LINE_RE = re.compile(
    r"parse_json: dev_id=([0-9A-Za-z_\-]+), (?:tunnel is=\w+, )?(?:origin|merged) playload=(\{.*\})\s*$")
MODULE_RE = re.compile(r"^(?:ams|n3f|n3s)/(\d+)$")


def parse_log_line(line):
    """Return (dev_id, payload dict) for a parse_json log line, else None."""
    if "playload=" not in line:
        return None
    m = LOG_LINE_RE.search(line)
    if not m:
        return None
    try:
        return m.group(1), json.loads(m.group(2))
    except ValueError:
        return None


def ams_unit_label(ams_id):
    try:
        i = int(ams_id)
    except (TypeError, ValueError):
        return str(ams_id)
    if 0 <= i < 4:
        return chr(ord("A") + i)
    if i >= 128:
        return "HT%d" % (i - 128 + 1)
    return str(i)


def ams_type_from_info(info_hex, ams_id=0):
    try:
        t = int(str(info_hex), 16) & 0xF
    except (TypeError, ValueError):
        t = 0
    if t == 0:
        try:
            t = 4 if int(ams_id) >= 128 else 1
        except (TypeError, ValueError):
            t = 1
    return t


def _valid_uuid(value):
    s = str(value or "").strip()
    return bool(s) and set(s) != {"0"}


def _as_int(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def tray_is_empty(tray, exist_bits=None, ams_id=None):
    if exist_bits is not None and ams_id is not None:
        try:
            bit = int(ams_id) * 4 + int(tray.get("id", 0))
            if int(ams_id) < 4 and not (int(str(exist_bits), 16) >> bit) & 1:
                return True
        except (TypeError, ValueError):
            pass
    return not (tray.get("tray_type") or tray.get("tray_info_idx") or _valid_uuid(tray.get("tray_uuid")))


class DeviceState:
    """Merged AMS state per printer, fed from parse_json log lines."""

    def __init__(self):
        self.printers = {}
        self.lock = threading.Lock()
        self.last_payload_at = 0.0
        self.payload_count = 0

    def _printer(self, dev_id):
        p = self.printers.get(dev_id)
        if p is None:
            p = {"dev_id": dev_id, "ams": {}, "ams_sn": {}, "vt_tray": None, "vir_slot": {},
                 "gcode_state": "", "subtask_name": "", "mc_percent": None, "last_seen": 0.0,
                 "tray_exist_bits": None, "tray_now": None, "model": ""}
            self.printers[dev_id] = p
        return p

    def ingest(self, dev_id, obj, now=None):
        now = now or time.time()
        with self.lock:
            self.last_payload_at = now
            self.payload_count += 1
            p = self._printer(dev_id)
            p["last_seen"] = now
            pr = obj.get("print")
            if isinstance(pr, dict):
                for key in ("gcode_state", "subtask_name", "mc_percent"):
                    if key in pr:
                        p[key] = pr[key]
                ams = pr.get("ams")
                if isinstance(ams, dict):
                    for key in ("tray_exist_bits", "tray_now", "ams_exist_bits", "tray_is_bbl_bits"):
                        if key in ams:
                            p[key] = ams[key]
                    units = ams.get("ams")
                    if isinstance(units, list):
                        for u in units:
                            if not isinstance(u, dict) or "id" not in u:
                                continue
                            uid = str(u["id"])
                            unit = p["ams"].setdefault(uid, {"id": uid, "trays": {}})
                            for key in ("humidity", "humidity_raw", "info", "temp", "dry_time"):
                                if key in u:
                                    unit[key] = u[key]
                            trays = u.get("tray")
                            if isinstance(trays, list):
                                for t in trays:
                                    if isinstance(t, dict) and "id" in t:
                                        unit["trays"][str(t["id"])] = dict(t)
                vt = pr.get("vt_tray")
                if isinstance(vt, dict):
                    p["vt_tray"] = dict(vt)
                vs = pr.get("vir_slot")
                if isinstance(vs, list):
                    for t in vs:
                        if isinstance(t, dict) and "id" in t:
                            p["vir_slot"][str(t["id"])] = dict(t)
            info = obj.get("info")
            if isinstance(info, dict) and info.get("command") == "get_version":
                for m in info.get("module") or []:
                    if not isinstance(m, dict):
                        continue
                    name = str(m.get("name") or "")
                    mm = MODULE_RE.match(name)
                    if mm and m.get("sn"):
                        p["ams_sn"][mm.group(1)] = str(m["sn"])
                    elif name == "ota" and m.get("project_name"):
                        p["model"] = str(m["project_name"])

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.printers)), self.last_payload_at, self.payload_count


class LogTailer(threading.Thread):
    """Follows the newest OrcaSlicer debug log and feeds DeviceState."""

    def __init__(self, log_dir, state):
        super().__init__(name="filament-manager-log-tail", daemon=True)
        self.log_dir = log_dir
        self.state = state
        self.stop_event = threading.Event()
        self.current = None
        self.error = ""
        self.lines_seen = 0

    @staticmethod
    def newest_log(log_dir):
        try:
            files = glob.glob(os.path.join(log_dir, "debug_*.log*"))
        except OSError:
            return None
        best, best_m = None, -1
        for f in files:
            try:
                m = os.path.getmtime(f)
            except OSError:
                continue
            if m > best_m:
                best, best_m = f, m
        return best

    def run(self):
        fh, offset, leftover, last_scan = None, 0, b"", 0.0
        while not self.stop_event.is_set():
            now = time.time()
            if fh is None or now - last_scan > 5:
                last_scan = now
                newest = self.newest_log(self.log_dir)
                if newest is None:
                    self.error = "No OrcaSlicer log file found in %s" % self.log_dir
                    self.stop_event.wait(2)
                    continue
                if newest != self.current:
                    if fh:
                        fh.close()
                    try:
                        fh = open(newest, "rb")
                        size = os.path.getsize(newest)
                        offset = max(0, size - LOG_TAIL_INITIAL_BYTES) if self.current is None else 0
                        fh.seek(offset)
                        leftover = b""
                        self.current = newest
                        self.error = ""
                    except OSError as e:
                        self.error = "Cannot open %s: %s" % (newest, e)
                        fh = None
                        self.stop_event.wait(2)
                        continue
            try:
                size = os.path.getsize(self.current)
                if size < offset:          # truncated / rewritten
                    fh.seek(0)
                    offset, leftover = 0, b""
                chunk = fh.read(1024 * 1024)
            except OSError as e:
                self.error = "Read error: %s" % e
                self.stop_event.wait(1)
                continue
            if not chunk:
                self.stop_event.wait(1)
                continue
            offset += len(chunk)
            data = leftover + chunk
            lines = data.split(b"\n")
            leftover = lines.pop()
            for raw in lines:
                if b"playload=" not in raw:
                    continue
                parsed = parse_log_line(raw.decode("utf-8", "replace"))
                if parsed:
                    self.lines_seen += 1
                    self.state.ingest(parsed[0], parsed[1])
        if fh:
            fh.close()


class HostFeed:
    """Printer state through orca.host, when the build provides it.

    A custom OrcaSlicer build adds read-only, thread-safe bindings:
      orca.host.devices() -> [{dev_id, dev_name, printer_type, is_connected, is_selected}]
      orca.host.selected_device_id() -> str | None
      orca.host.device_status(dev_id) -> raw JSON str of the last status push | None
      orca.host.device_status_seq(dev_id) -> int (monotonic, 0 = nothing yet)
    Polled from the UI thread on every page message (every few seconds), which
    is plenty for spool tracking. Replaces the log tailer when present.
    """

    REQUIRED = ("devices", "device_status", "device_status_seq")

    @classmethod
    def available(cls, host):
        return host is not None and all(callable(getattr(host, n, None)) for n in cls.REQUIRED)

    def __init__(self, host, state):
        self.host = host
        self.state = state
        self.seq = {}
        self.names = {}
        self.error = ""
        self.polls = 0

    def poll(self):
        self.polls += 1
        try:
            devs = self.host.devices() or []
        except Exception as e:  # noqa: BLE001
            self.error = "orca.host.devices(): %s" % e
            return
        self.error = ""
        for d in devs:
            if not isinstance(d, dict) or not d.get("dev_id"):
                continue
            dev_id = str(d["dev_id"])
            self.names[dev_id] = {"dev_id": dev_id, "name": d.get("dev_name") or "",
                                  "dev_product_name": d.get("printer_type") or "", "online": d.get("is_connected"),
                                  "selected": bool(d.get("is_selected"))}
            try:
                seq = int(self.host.device_status_seq(dev_id) or 0)
                if not seq or seq == self.seq.get(dev_id):
                    continue
                raw = self.host.device_status(dev_id)
                if raw:
                    self.state.ingest(dev_id, json.loads(raw))
                self.seq[dev_id] = seq
            except Exception as e:  # noqa: BLE001
                self.error = "device_status(%s): %s" % (dev_id, e)

    def selected(self):
        fn = getattr(self.host, "selected_device_id", None)
        try:
            sel = fn() if callable(fn) else None
        except Exception:  # noqa: BLE001
            sel = None
        if sel:
            return str(sel)
        for dev_id, d in self.names.items():
            if d.get("selected"):
                return dev_id
        return ""


# ---------------------------------------------------------------------------
# Matching trays to cloud spools and building the cloud payloads
# ---------------------------------------------------------------------------

def norm_hex(color):
    s = str(color or "").strip().lstrip("#")
    if len(s) in (6, 8) and all(c in "0123456789abcdefABCDEF" for c in s):
        return "#" + s[:6].upper()
    return ""


def remain_grams(tray, total):
    pct = _as_int(tray.get("remain"), -1)
    if pct is None or pct < 0 or pct > 100 or not total or total <= 0:
        return None
    return int(round(total * pct / 100.0))


def catalog_lookup(catalog, filament_id):
    for entry in (catalog or {}).get("filamentSettings") or []:
        if isinstance(entry, dict) and entry.get("filamentId") == filament_id:
            return entry
    return None


def build_tray_view(tray, unit_id, printer, catalog, spools_by_rfid, spools_by_soft, soft_match=True):
    exist_bits = printer.get("tray_exist_bits")
    empty = tray_is_empty(tray, exist_bits, unit_id)
    tid = str(tray.get("id", "0"))
    view = {
        "id": tid,
        "label": "%s%s" % (ams_unit_label(unit_id), _as_int(tid, 0) + 1) if _as_int(unit_id, 0) < 128 else "%s-%s" % (ams_unit_label(unit_id), _as_int(tid, 0) + 1),
        "empty": empty,
    }
    if empty:
        return view
    uuid = str(tray.get("tray_uuid") or "").upper()
    rfid = _valid_uuid(uuid)
    fid = str(tray.get("tray_info_idx") or "")
    entry = catalog_lookup(catalog, fid) if fid else None
    vendor = (entry or {}).get("filamentVendor") or ("Bambu Lab" if rfid else "")
    name = (entry or {}).get("filamentName") or str(tray.get("tray_sub_brands") or tray.get("tray_type") or "")
    total = _as_int(tray.get("tray_weight"), 0) or 0
    cols = [norm_hex(c) for c in (tray.get("cols") or []) if norm_hex(c)]
    view.update({
        "type": str(tray.get("tray_type") or ""),
        "name": name,
        "vendor": vendor,
        "filament_id": fid,
        "color": norm_hex(tray.get("tray_color")),
        "colors": cols,
        "remain_pct": _as_int(tray.get("remain"), -1),
        "total_g": total,
        "uuid": uuid if rfid else "",
        "rfid": rfid,
        "tray_id_name": str(tray.get("tray_id_name") or ""),
        "match": None,
    })
    spool = spools_by_rfid.get(uuid) if rfid else None
    kind = "rfid" if spool else None
    if spool is None and soft_match and not rfid and fid:
        spool = spools_by_soft.get((fid, view["color"]))
        kind = "soft" if spool else None
    if spool:
        total_cloud = _as_int(spool.get("totalNetWeight"), 0) or 0
        view["match"] = {
            "id": spool.get("id"),
            "kind": kind,
            "name": spool.get("filamentName"),
            "net": _as_int(spool.get("netWeight"), 0),
            "total": total_cloud,
            "computed_net": remain_grams(tray, total_cloud or total),
        }
    view["remain_g"] = remain_grams(tray, total)
    return view


def build_printers_view(printers, catalog, spools, device_names, selected, soft_match=True, now=None):
    now = now or time.time()
    by_rfid, by_soft = {}, {}
    for s in spools:
        r = str(s.get("RFID") or "").upper()
        if _valid_uuid(r):
            by_rfid[r] = s
        key = (str(s.get("filamentId") or ""), norm_hex(s.get("color")))
        if key[0] and key not in by_soft:
            by_soft[key] = s
    out = []
    for dev_id, p in printers.items():
        dev = device_names.get(dev_id, {})
        units = []
        for uid in sorted(p["ams"].keys(), key=lambda k: _as_int(k, 0)):
            u = p["ams"][uid]
            t = ams_type_from_info(u.get("info"), uid)
            slots = [build_tray_view(u["trays"][k], uid, p, catalog, by_rfid, by_soft, soft_match)
                     for k in sorted(u["trays"].keys(), key=lambda k: _as_int(k, 0))]
            units.append({
                "id": uid, "label": ams_unit_label(uid), "type": t, "type_name": AMS_TYPE_NAMES.get(t, "AMS"),
                "humidity": u.get("humidity"), "humidity_raw": u.get("humidity_raw"),
                "temp": u.get("temp"), "sn": p["ams_sn"].get(uid, "") or next(
                    (str(s["amsSn"]) for s in spools if str(s.get("devId") or "") == dev_id
                     and _as_int(s.get("amsId"), -2) == _as_int(uid, -1) and s.get("amsSn")), ""),
                "slots": slots,
            })
        external = None
        vt = p.get("vt_tray")
        if isinstance(vt, dict) and not tray_is_empty(vt):
            external = build_tray_view(vt, "254", p, catalog, by_rfid, by_soft, soft_match)
            external["label"] = "Ext"
        out.append({
            "dev_id": dev_id,
            "name": dev.get("name") or p.get("model") or dev_id,
            "model": dev.get("dev_product_name") or dev.get("dev_model_name") or p.get("model") or "",
            "selected": dev_id == selected,
            "live": (now - p.get("last_seen", 0)) < LOG_LIVE_WINDOW_S,
            "age_s": int(now - p.get("last_seen", 0)),
            "gcode_state": p.get("gcode_state") or "",
            "busy": str(p.get("gcode_state") or "").upper() in BUSY_STATES,
            "subtask_name": p.get("subtask_name") or "",
            "mc_percent": p.get("mc_percent"),
            "units": units,
            "external": external,
        })
    out.sort(key=lambda x: (not x["selected"], x["name"]))
    return out


def tray_color_cloud(tray):
    s = str(tray.get("tray_color") or "").strip().lstrip("#")
    return "#" + s if s else "#FFFFFFFF"


def build_create_from_tray(tray, catalog, total_override=None):
    fid = str(tray.get("tray_info_idx") or "")
    entry = catalog_lookup(catalog, fid) or {}
    uuid = str(tray.get("tray_uuid") or "").upper()
    rfid = _valid_uuid(uuid)
    total = total_override or _as_int(tray.get("tray_weight"), 0) or 1000
    cols = [norm_hex(c) for c in (tray.get("cols") or []) if norm_hex(c)]
    body = {
        "createType": "ams" if rfid else "manual",
        "filamentVendor": entry.get("filamentVendor") or ("Bambu Lab" if rfid else "Generic"),
        "filamentType": entry.get("filamentType") or str(tray.get("tray_type") or ""),
        "filamentName": entry.get("filamentName") or str(tray.get("tray_sub_brands") or tray.get("tray_type") or "Filament"),
        "filamentId": fid,
        "isSupport": bool(entry.get("isSupport", False)),
        "color": tray_color_cloud(tray),
        # Spools the Bambu apps create from the AMS carry colorType 0 for a
        # single colour (seen on a live account), 1 for multi-colour.
        "colorType": 0 if len(cols) <= 1 else 1,
        "totalNetWeight": int(total),
        "netWeight": remain_grams(tray, total) if remain_grams(tray, total) is not None else int(total),
        "rolls": 1,
        "note": "",
    }
    if len(cols) > 1:
        body["colors"] = cols
    if rfid:
        body["RFID"] = uuid
        body["trayIdName"] = str(tray.get("tray_id_name") or "0")
    return body


def build_ams_sync_item(tray, unit_id, unit_sn, unit_type, spool):
    """One AMS mount record for POST .../ams/sync (research §8.15.7)."""
    total = _as_int(spool.get("totalNetWeight"), 0) or _as_int(tray.get("tray_weight"), 0) or 0
    net = remain_grams(tray, total)
    cols = [norm_hex(c) for c in (tray.get("cols") or []) if norm_hex(c)] or [norm_hex(spool.get("color")) or "#FFFFFF"]
    spool_color = str(spool.get("color") or "").strip()
    if spool_color and not spool_color.startswith("#"):
        spool_color = "#" + spool_color
    return {
        "RFID": str(spool.get("RFID") or tray.get("tray_uuid") or "").upper(),
        "amsId": _as_int(unit_id, 0),
        "amsSn": unit_sn or "",
        "amsType": int(unit_type),
        "color": tray_color_cloud(tray) if tray.get("tray_color") else (spool_color or "#FFFFFFFF"),
        "colorType": _as_int(spool.get("colorType"), 0) if spool.get("colorType") is not None else (0 if len(cols) <= 1 else 1),
        "colors": cols,
        "createNew": False,
        "filamentId": str(spool.get("filamentId") or tray.get("tray_info_idx") or ""),
        "filamentName": str(spool.get("filamentName") or ""),
        "filamentType": str(spool.get("filamentType") or tray.get("tray_type") or ""),
        "filamentVendor": str(spool.get("filamentVendor") or ""),
        "isSupport": bool(spool.get("isSupport", False)),
        "netWeight": net if net is not None else (_as_int(spool.get("netWeight"), 0) or 0),
        "note": str(spool.get("note") or ""),
        "slotId": str(tray.get("id", "0")),
        "totalNetWeight": total,
        "trayIdName": str(tray.get("tray_id_name") or spool.get("trayIdName") or "0"),
    }


def is_official_rfid_tray(tray):
    """Studio's gate for the auto-register path: a 16-char NFC tag id whose
    bytes 12..13 are "01" (official Bambu chip) and a valid tray UUID."""
    tag = str(tray.get("tag_uid") or "")
    return len(tag) == 16 and tag[12:14] == "01" and _valid_uuid(tray.get("tray_uuid"))


def build_register_item(tray, unit_id, unit_sn, unit_type, catalog):
    """The ams/sync item Studio sends for a brand-new official RFID tray
    (wgtFilaManagerSync::check_and_register_new_rfid_spools)."""
    fid = str(tray.get("tray_info_idx") or "")
    entry = catalog_lookup(catalog, fid) or {}
    total = _as_int(tray.get("tray_weight"), 0) or 0
    remain_g = _as_int(tray.get("remain_g"), -1)
    net = remain_g if remain_g is not None and remain_g >= 0 else (remain_grams(tray, total) or 0)
    cols = [norm_hex(c) for c in (tray.get("cols") or []) if norm_hex(c)]
    return {
        "RFID": str(tray.get("tray_uuid") or "").upper(),
        "amsId": _as_int(unit_id, 0),
        "amsSn": unit_sn or "",
        "amsType": int(unit_type),
        "color": tray_color_cloud(tray),
        "colorType": _as_int(tray.get("ctype"), 0) or 0,
        "colors": cols or [tray_color_cloud(tray)],
        "createNew": False,
        "filamentId": fid,
        "filamentName": str(tray.get("tray_sub_brands") or entry.get("filamentName") or tray.get("tray_type") or ""),
        "filamentType": str(tray.get("tray_type") or entry.get("filamentType") or ""),
        "filamentVendor": str(entry.get("filamentVendor") or "Bambu Lab"),
        "isSupport": bool(entry.get("isSupport", False)),
        "netWeight": int(net),
        "note": "",
        "slotId": str(tray.get("id", "0")),
        "totalNetWeight": total,
        "trayIdName": str(tray.get("tray_id_name") or ""),
    }


def spool_location(spool):
    """Human readable mount location from the cloud record, if any."""
    dev = spool.get("deviceName") or spool.get("devName") or spool.get("devId") or ""
    ams = spool.get("ams") if isinstance(spool.get("ams"), dict) else {}
    ams_id = spool.get("amsId", ams.get("amsId"))
    slot = spool.get("slotId", ams.get("slotId"))
    in_printer = spool.get("inPrinter", ams.get("inPrinter"))
    if not (dev or ams_id is not None) or in_printer is False:
        return ""
    label = ""
    if ams_id is not None and slot not in (None, ""):
        label = "%s%d" % (ams_unit_label(ams_id), _as_int(slot, 0) + 1)
    return (" ".join(x for x in (str(dev), label) if x)).strip()


# ---------------------------------------------------------------------------
# Icon (drawn at runtime so the plugin stays a single file)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Slice check: does the AMS hold enough for the plate that was just sliced?
# ---------------------------------------------------------------------------

SLICE_HEAD_BYTES = 64 * 1024
SLICE_TAIL_BYTES = 512 * 1024
_pending_slice = {"lock": threading.Lock(), "info": None}


def publish_slice(info):
    """Called on the slicing worker; the UI thread collects it with take_slice()."""
    with _pending_slice["lock"]:
        _pending_slice["info"] = info


def take_slice():
    with _pending_slice["lock"]:
        info, _pending_slice["info"] = _pending_slice["info"], None
    return info


def _config_list(value):
    """A CONFIG_BLOCK value such as '"Bambu PLA Basic @BBL X1C";"Generic PETG"' or '#000000;#FFFFFF'."""
    out = []
    for part in str(value).split(";"):
        part = part.strip()
        if len(part) >= 2 and part[0] == '"' and part[-1] == '"':
            part = part[1:-1]
        out.append(part)
    return out


def _float_list(value):
    out = []
    for part in str(value).split(","):
        try:
            out.append(float(part.strip()))
        except ValueError:
            out.append(0.0)
    return out


def parse_slice_gcode(path, head_bytes=SLICE_HEAD_BYTES, tail_bytes=SLICE_TAIL_BYTES):
    """Filament need per extruder, from the comments OrcaSlicer writes into a sliced G-code.

    Only the head (HEADER_BLOCK) and the tail (``; filament used [g]`` and the
    CONFIG_BLOCK) are read, so a big file costs nothing. ``filament used [g]`` is
    indexed by extruder, with zeros for extruders that did not print; the
    config lists are semicolon separated in the same order.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size <= head_bytes + tail_bytes:
            text = fh.read().decode("utf-8", "replace")
        else:
            head = fh.read(head_bytes)
            fh.seek(size - tail_bytes)
            text = (head + b"\n" + fh.read(tail_bytes)).decode("utf-8", "replace")
    grams, ids, colors, types, names, vendors = [], [], [], [], [], []
    printer, time_str = "", ""
    for line in text.splitlines():
        if not line.startswith("; "):
            continue
        body = line[2:]
        if " = " in body:
            key, value = body.split(" = ", 1)
        elif ": " in body:
            key, value = body.split(": ", 1)
        else:
            continue
        key, value = key.strip(), value.strip()
        if key == "filament used [g]":
            grams = _float_list(value)
        elif key == "filament_ids":
            ids = _config_list(value)
        elif key == "filament_colour":
            colors = _config_list(value)
        elif key == "filament_type":
            types = _config_list(value)
        elif key == "filament_settings_id":
            names = _config_list(value)
        elif key == "filament_vendor":
            vendors = _config_list(value)
        elif key == "printer_settings_id":
            printer = _config_list(value)[0] if value else ""
        elif key == "model printing time":
            time_str = value.split(";")[0].strip()
        elif key == "estimated printing time (normal mode)" and not time_str:
            time_str = value

    def pick(lst, i):
        return lst[i] if i < len(lst) else ""

    filaments = []
    for i, g in enumerate(grams):
        if g <= 0:
            continue
        filaments.append({
            "index": i, "grams": round(g, 1),
            "filament_id": pick(ids, i), "color": norm_hex(pick(colors, i)),
            "type": pick(types, i), "name": pick(names, i), "vendor": pick(vendors, i),
        })
    return {"path": path, "filaments": filaments, "total_g": round(sum(f["grams"] for f in filaments), 1),
            "time": time_str, "printer_preset": printer, "when": time.time()}


def tray_remaining(slot):
    """Best remaining estimate for a tray view: the linked library spool's weight, else the AMS estimate."""
    m = slot.get("match")
    if m:
        g = m.get("computed_net") if m.get("computed_net") is not None else m.get("net")
        if g is not None:
            return int(g), "library"
    if slot.get("remain_g") is not None:
        return int(slot["remain_g"]), "ams"
    return None, ""


CHECK_ORDER = {"low": 0, "missing": 1, "unknown": 2, "backup": 3, "ok": 4}


def check_slice(info, printers, selected=""):
    """Compare a parsed slice with the trays of the selected (else first) printer.

    Per filament: trays with the same filament id and colour, else the same type
    and colour. ``ok`` when one tray has enough, ``backup`` when only the sum of
    matching trays does, ``low`` when not even that, ``missing`` when nothing
    matching is loaded, ``unknown`` when the matching trays have no estimate.
    """
    printer = None
    for p in printers:
        if p.get("units") or p.get("external"):
            if p.get("dev_id") == selected:
                printer = p
                break
            printer = printer or p
    base = {"when": info.get("when"), "total_g": info.get("total_g"), "time": info.get("time", "")}
    if not printer:
        return dict(base, status="no_printer", printer="", dev_id="",
                    rows=[dict(f, status="no_printer", slots=[], best_g=None, sum_g=None, others=[]) for f in info["filaments"]])
    trays = [s for u in printer["units"] for s in u["slots"] if not s.get("empty")]
    if printer.get("external"):
        trays.append(printer["external"])
    rows = []
    for f in info["filaments"]:
        ftype = (f.get("type") or "").upper()
        same_color = lambda t: not f.get("color") or (t.get("color") or "") == f["color"]  # noqa: E731
        found = [t for t in trays if f.get("filament_id") and t.get("filament_id") == f["filament_id"] and same_color(t)]
        if not found and ftype:
            found = [t for t in trays if (t.get("type") or "").upper() == ftype and same_color(t)]
        others = [] if found else [t["label"] for t in trays if (t.get("type") or "").upper() == ftype]
        slots = []
        for t in found:
            g, src = tray_remaining(t)
            slots.append({"label": t["label"], "remaining": g, "source": src, "name": t.get("name", ""), "color": t.get("color", "")})
        known = [x["remaining"] for x in slots if x["remaining"] is not None]
        best = max(known) if known else None
        total = sum(known) if known else None
        need = f["grams"]
        if not slots:
            status = "missing"
        elif not known:
            status = "unknown"
        elif best >= need:
            status = "ok"
        elif total >= need:
            status = "backup"
        else:
            status = "low"
        rows.append(dict(f, status=status, slots=slots, best_g=best, sum_g=total, others=others))
    overall = min((r["status"] for r in rows), key=lambda x: CHECK_ORDER[x]) if rows else "ok"
    return dict(base, status=overall, printer=printer["name"], dev_id=printer["dev_id"], rows=rows)


def _fil_label(f):
    name = f.get("name") or f.get("type") or "filament"
    return name if not f.get("color") else "%s %s" % (name, f["color"])


def slice_check_message(check):
    """One line for the notification and the toast."""
    st = check.get("status")
    printer = check.get("printer") or "the printer"
    if st == "no_printer":
        return "Filament check: no printer data to compare the slice with"
    bad = [r for r in check["rows"] if r["status"] in ("low", "missing", "backup", "unknown")]
    if not bad:
        return "Filament check: enough in %s for this plate (%.0f g)" % (printer, check.get("total_g") or 0)
    parts = []
    for r in bad:
        lab = _fil_label(r)
        if r["status"] == "missing":
            parts.append("%s not loaded in %s (%.0f g needed%s)" % (
                lab, printer, r["grams"], ", %s loaded in other colours" % r["type"] if r["others"] else ""))
        elif r["status"] == "low":
            parts.append("%s needs %.0f g, %s has ~%d g left" % (
                lab, r["grams"], " + ".join(x["label"] for x in r["slots"]), r["sum_g"]))
        elif r["status"] == "backup":
            parts.append("%s needs %.0f g, no single spool has that (~%d g in %s, %d g together)" % (
                lab, r["grams"], r["best_g"], ", ".join(x["label"] for x in r["slots"]), r["sum_g"]))
        else:
            parts.append("can't tell how much %s is left in %s" % (lab, ", ".join(x["label"] for x in r["slots"])))
    return "Filament check: " + "; ".join(parts)


def icon_png_bytes(size=64):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = bytearray()
    c = (size - 1) / 2.0
    r_out, r_in = size * 0.46, size * 0.17
    for y in range(size):
        raw.append(0)
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            a = max(0.0, min(1.0, min(r_out - d, d - r_in) + 0.5))
            # flange marks: two darker bands across the ring
            band = 1 if abs(d - (r_in + r_out) / 2) < size * 0.04 else 0
            col = (0x1F, 0x8B, 0x4C) if not band else (0x16, 0x6B, 0x3A)
            raw += bytes((col[0], col[1], col[2], int(a * 255)))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    return png


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Filament Manager</title>
<style>
:root{--bg:var(--orca-bg,#f7f7f7);--fg:var(--orca-fg,#1f1f1f);--card:var(--orca-card,#ffffff);--muted:#6b6b6b;--line:#dcdcdc;--accent:var(--orca-accent,#009688);--accent-fg:#fff;--warn:#c77700;--bad:#c62828;--ok:#2e7d32;--chip:#eeeeee}
@media (prefers-color-scheme: dark){:root{--bg:var(--orca-bg,#1e1e1e);--fg:var(--orca-fg,#e6e6e6);--card:var(--orca-card,#2a2a2a);--muted:#9a9a9a;--line:#3a3a3a;--chip:#383838}}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);font:13px/1.45 "Segoe UI",system-ui,sans-serif}
body{padding:0 0 40px}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:17px;margin:0 8px 0 0;font-weight:600}
.pill{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:999px;background:var(--chip);font-size:12px;white-space:nowrap}
.pill .dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.pill.ok .dot{background:var(--ok)}.pill.warn .dot{background:var(--warn)}.pill.bad .dot{background:var(--bad)}
.spacer{flex:1}
button{font:inherit;padding:5px 11px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
button.danger{color:var(--bad)}
button:disabled{opacity:.5;cursor:default}
button.sm{padding:2px 8px;font-size:12px}
main{padding:12px 16px;display:grid;gap:18px}
section h2{font-size:14px;margin:0 0 8px;font-weight:600;display:flex;align-items:center;gap:10px}
section h2 .sub{font-weight:400;color:var(--muted);font-size:12px}
.notice{padding:10px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);display:flex;gap:10px;align-items:flex-start}
.notice.warn{border-color:var(--warn)}.notice.bad{border-color:var(--bad)}
.notice b{display:block;margin-bottom:2px}
.printer{border:1px solid var(--line);border-radius:10px;background:var(--card);padding:10px 12px}
.printer .ph{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.printer .ph .name{font-weight:600}
.units{display:flex;gap:14px;flex-wrap:wrap}
.unit{min-width:300px;flex:1 1 300px;border:1px solid var(--line);border-radius:8px;padding:8px}
.unit .uh{display:flex;gap:8px;align-items:center;color:var(--muted);font-size:12px;margin-bottom:6px}
.unit .uh b{color:var(--fg);font-size:13px}
.slots{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px}
.unit.ext .slots{grid-template-columns:1fr}
.slot{border:1px solid var(--line);border-radius:8px;padding:6px;min-height:118px;display:flex;flex-direction:column;gap:4px;position:relative}
.slot.empty{opacity:.55;justify-content:center;align-items:center;color:var(--muted)}
.slot .lab{position:absolute;top:3px;right:4px;font-size:10px;line-height:14px;padding:0 4px;border-radius:3px;background:var(--card);color:var(--muted);border:1px solid var(--line)}
.sw{width:100%;height:22px;border-radius:5px;border:1px solid rgba(0,0,0,.2)}
.slot .t{font-weight:600;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.slot .n{font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar{height:6px;border-radius:3px;background:var(--chip);overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent)}
.slot .m{font-size:11px;margin-top:auto}
.slot .m.none{color:var(--warn)}
.slot .m.soft{color:var(--muted)}
.slot.flag{border-color:var(--warn);box-shadow:0 0 0 1px var(--warn) inset}
.slot .dot{position:absolute;top:-5px;left:-5px;width:12px;height:12px;border-radius:50%;background:var(--warn);border:2px solid var(--card)}
.chk{border:1px solid var(--line);border-radius:10px;background:var(--card);padding:10px 12px}
.chk.ok{border-color:var(--ok)}.chk.low,.chk.missing{border-color:var(--bad)}.chk.unknown,.chk.backup,.chk.no_printer{border-color:var(--warn)}
.chk .row{display:grid;grid-template-columns:22px minmax(140px,1.4fr) 70px minmax(160px,2fr) auto;gap:10px;align-items:center;padding:4px 0;border-top:1px solid var(--line)}
.chk .row:first-of-type{border-top:0}
.chk .sw{width:18px;height:18px;border-radius:4px}
.chk .st{font-size:11px;padding:1px 8px;border-radius:999px;background:var(--chip);white-space:nowrap}
.chk .st.ok{background:var(--ok);color:#fff}.chk .st.low,.chk .st.missing{background:var(--bad);color:#fff}.chk .st.unknown,.chk .st.backup{background:var(--warn);color:#fff}
.chk .foot{display:flex;gap:10px;align-items:center;margin-top:8px;font-size:12px;color:var(--muted)}
.badge{border:1px solid var(--warn);border-radius:10px;background:var(--card);padding:10px 12px;margin-bottom:8px;display:grid;grid-template-columns:auto 1fr;gap:10px 14px;align-items:start}
.badge.recorded{border-color:var(--ok)}
.badge .sw{width:46px;height:46px;border-radius:8px}
.badge h3{margin:0 0 2px;font-size:13px}
.badge .cands{display:flex;flex-direction:column;gap:4px;margin:6px 0}
.badge .cands label{display:flex;gap:8px;align-items:center;cursor:pointer}
.badge .cands .sw{width:18px;height:18px;border-radius:4px}
.badge .btns{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.tag{display:inline-block;padding:0 5px;border-radius:4px;background:var(--chip);font-size:10px;margin-left:4px;vertical-align:1px}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
input[type=text],input[type=number],select,textarea{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
input[type=color]{width:38px;height:30px;padding:0;border:1px solid var(--line);border-radius:6px;background:var(--card)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}
th{font-weight:600;font-size:12px;color:var(--muted);cursor:pointer;user-select:none;white-space:nowrap}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:rgba(127,127,127,.08)}
td .sw{width:22px;height:22px;display:inline-block;vertical-align:middle}
td.num{text-align:right;white-space:nowrap}
.rem{display:flex;align-items:center;gap:8px;min-width:150px}
.rem .bar{flex:1}
.muted{color:var(--muted)}
.actions{white-space:nowrap}
.actions button{margin-left:4px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:20}
.modal.open{display:flex}
.dlg{background:var(--card);color:var(--fg);border-radius:10px;padding:16px 18px;width:min(560px,94vw);max-height:92vh;overflow:auto;border:1px solid var(--line)}
.dlg h3{margin:0 0 12px;font-size:15px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px}
.grid label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--muted)}
.grid label.full{grid-column:1/-1}
.grid input,.grid select,.grid textarea{width:100%}
.dlg .foot{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
.toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:8px 14px;border-radius:8px;font-size:12px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:30;max-width:80vw}
.toast.show{opacity:.95}
.settings{display:flex;gap:16px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--muted)}
.settings label{display:flex;gap:5px;align-items:center;cursor:pointer}
details.log{font-size:12px;color:var(--muted)}
details.log summary{cursor:pointer}
code{font-family:Consolas,monospace;font-size:12px;background:var(--chip);padding:0 4px;border-radius:3px}
</style></head>
<body>
<header>
  <h1>Filament Manager</h1>
  <span id="pillCloud" class="pill"><span class="dot"></span><span>Cloud</span></span>
  <span id="pillLog" class="pill"><span class="dot"></span><span>Printer feed</span></span>
  <span class="spacer"></span>
  <button id="btnAdd">+ Add spool</button>
  <button id="btnSyncAll" title="Push the current AMS contents (positions and remaining weight) of every live printer to the cloud">Sync AMS to cloud</button>
  <button id="btnRefresh" class="primary">Refresh</button>
</header>
<main>
  <div id="notices"></div>
  <section id="secSlice">
    <h2>Last slice <span class="sub" id="sliceSub"></span></h2>
    <div id="slice"></div>
  </section>
  <section id="secBadges" style="display:none">
    <h2>Action needed <span class="sub" id="badgesSub"></span></h2>
    <div id="badges"></div>
  </section>
  <section id="secPrinters">
    <h2>In the printer <span class="sub" id="printersSub"></span></h2>
    <div id="printers"></div>
  </section>
  <section id="secLibrary">
    <h2>Spool library <span class="sub" id="librarySub"></span></h2>
    <div class="toolbar">
      <input type="text" id="search" placeholder="Search name, vendor, note…" style="min-width:220px">
      <select id="filterType"><option value="">All types</option></select>
      <label class="muted" style="display:flex;gap:5px;align-items:center"><input type="checkbox" id="onlyMounted"> Only in a printer</label>
      <span class="spacer"></span>
      <div class="settings">
        <label title="Update the cloud remaining weight from the AMS remain estimate, like Bambu Studio does. RFID spools only."><input type="checkbox" id="optPush"> Auto-update remaining</label>
        <label title="Record which printer slot each RFID spool sits in, like Bambu Studio does."><input type="checkbox" id="optMount"> Auto-sync slot positions</label>
        <label title="Match non-RFID trays to library spools by filament id and colour."><input type="checkbox" id="optSoft"> Match by colour</label>
        <label title="When an official Bambu RFID spool that is not in the library appears in the AMS, send it to the cloud and ask whether to add it or link it to an existing spool, like Bambu Studio."><input type="checkbox" id="optRegister"> Detect new RFID spools</label>
        <label title="After each slice, say whether the AMS holds enough for the plate. Needs Filament check picked under Process settings › Others › Slicing Pipeline Plugin."><input type="checkbox" id="optCheck"> Notify after each slice</label>
      </div>
    </div>
    <table id="tbl"><thead><tr>
      <th data-k="color"></th><th data-k="filamentName">Name</th><th data-k="filamentVendor">Vendor</th><th data-k="filamentType">Type</th>
      <th data-k="netWeight">Remaining</th><th data-k="location">Location</th><th data-k="note">Note</th><th data-k="updatedAt">Updated</th><th></th>
    </tr></thead><tbody id="rows"></tbody></table>
  </section>
  <details class="log"><summary>Diagnostics</summary><pre id="diag" style="white-space:pre-wrap"></pre></details>
</main>

<div class="modal" id="modal"><div class="dlg">
  <h3 id="dlgTitle">Add spool</h3>
  <div class="grid">
    <label>Preset (Bambu catalogue)<select id="fCatalog"><option value="">Custom / third party</option></select></label>
    <label>Type<select id="fType"></select></label>
    <label>Vendor<input type="text" id="fVendor" list="vendors"><datalist id="vendors"></datalist></label>
    <label>Name<input type="text" id="fName"></label>
    <label>Nickname (optional)<input type="text" id="fDisplay" placeholder="shown instead of the name"></label>
    <label>Colour<div style="display:flex;gap:8px;align-items:center"><input type="color" id="fColor" value="#808080"><input type="text" id="fColorHex" value="#808080" style="width:110px"></div></label>
    <label>Filament id<input type="text" id="fId" placeholder="e.g. GFA00 (optional)"></label>
    <label>Spool size (net g)<input type="number" id="fTotal" min="1" step="1" value="1000"></label>
    <label>Remaining (g)<input type="number" id="fNet" min="0" step="1" value="1000"></label>
    <label class="full">Note<textarea id="fNote" rows="2"></textarea></label>
    <label class="full" id="fRfidRow" style="display:none">RFID<input type="text" id="fRfid" readonly></label>
  </div>
  <div class="foot"><button id="dlgCancel">Cancel</button><button id="dlgSave" class="primary">Save</button></div>
</div></div>
<div class="toast" id="toast"></div>

<script>
(function(){
'use strict';
var S = {state:null, sortKey:'filamentName', sortDir:1, editing:null, busy:false, lastDiag:''};
var $ = function(id){ return document.getElementById(id); };
var esc = function(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); };

function post(msg){ try{ if(window.orca && window.orca.postMessage) window.orca.postMessage(msg); }catch(e){ console.error(e); } }
function toast(t, ms){ var el=$('toast'); el.textContent=t; el.classList.add('show'); clearTimeout(el._t); el._t=setTimeout(function(){ el.classList.remove('show'); }, ms||2600); }
function setBusy(b){ S.busy=b; ['btnRefresh','btnSyncAll','btnAdd','dlgSave'].forEach(function(id){ $(id).disabled=b; }); }

function fmtAge(s){ if(s==null) return ''; if(s<60) return s+'s ago'; if(s<3600) return Math.floor(s/60)+' min ago'; if(s<86400) return Math.floor(s/3600)+' h ago'; return Math.floor(s/86400)+' d ago'; }
function fmtDate(ts){ if(!ts) return ''; var d=new Date(ts*1000); return d.toLocaleDateString(undefined,{day:'2-digit',month:'short'})+' '+d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}); }
function swatch(c, cols){ if(cols && cols.length>1){ return '<span class="sw" style="background:linear-gradient(90deg,'+cols.map(esc).join(',')+')"></span>'; } return '<span class="sw" style="background:'+esc(c||'#888')+'"></span>'; }
function pct(n,t){ if(!t||t<=0||n==null) return null; return Math.max(0,Math.min(100,Math.round(100*n/t))); }

function renderHeader(st){
  var c=$('pillCloud'); c.className='pill '+(st.auth.ok?'ok':'bad'); c.lastElementChild.textContent = st.auth.ok ? ('Cloud: '+(st.auth.user||'signed in')) : 'Cloud: not signed in';
  c.title = st.auth.ok ? (st.auth.host+(st.auth.expires_at?'\nSession valid until '+st.auth.expires_at:'')) : (st.auth.error||'');
  var l=$('pillLog'); var lg=st.log;
  var cls = lg.live ? 'ok' : (lg.payloads>0 ? 'warn' : 'bad');
  l.className='pill '+cls;
  l.lastElementChild.textContent = lg.live ? 'Printer feed: live' : (lg.payloads>0 ? 'Printer feed: stale ('+fmtAge(lg.age_s)+')' : 'Printer feed: no data');
  l.title = (lg.file?('Reading '+lg.file+'\n'):'')+(lg.error||'')+'\nMessages seen: '+lg.payloads;
}

var STATUS_TEXT = {ok:'enough', low:'not enough', missing:'not loaded', unknown:'unknown', backup:'needs a spool change', no_printer:'no printer data'};
function renderSlice(st){
  var c = st.slice_check, box=$('slice');
  if(!c){
    $('sliceSub').textContent='';
    box.innerHTML='<div class="muted" style="font-size:12px">Pick <code>Filament check</code> under <code>Process settings › Others › Slicing Pipeline Plugin</code> and every plate you slice is checked here against what the AMS holds.</div>';
    return;
  }
  $('sliceSub').textContent = (c.printer?('vs '+c.printer+' · '):'')+fmtAge(Math.max(0, Math.round(Date.now()/1000 - c.when)))+' ago';
  var rows = (c.rows||[]).map(function(r){
    var where;
    if(r.status==='no_printer') where='<span class="muted">no printer data</span>';
    else if(!r.slots.length) where='<span class="muted">nothing matching loaded'+(r.others&&r.others.length?' ('+esc(r.type)+' in other colours: '+esc(r.others.join(', '))+')':'')+'</span>';
    else where=r.slots.map(function(x){ return '<b>'+esc(x.label)+'</b> '+(x.remaining!=null?('~'+esc(x.remaining)+' g'+(x.source==='ams'?' <span class="muted">(AMS estimate)</span>':'')):'<span class="muted">remaining unknown</span>'); }).join(', ');
    var name = (r.vendor&&r.name&&r.name.indexOf(r.vendor)<0?r.vendor+' ':'')+(r.name||r.type||'filament');
    return '<div class="row">'+swatch(r.color||'#888')+'<div title="'+esc(name)+'"><b>'+esc(r.type||'')+'</b> '+esc(name)+'</div><div>'+esc(r.grams)+' g</div><div>'+where+'</div><span class="st '+esc(r.status)+'">'+esc(STATUS_TEXT[r.status]||r.status)+'</span></div>';
  }).join('');
  box.innerHTML='<div class="chk '+esc(c.status)+'">'+rows+'<div class="foot"><span>'+esc(c.total_g)+' g total'+(c.time?' · '+esc(c.time):'')+'</span><span class="spacer"></span><button class="sm" data-act="recheck">Check again</button></div></div>';
}

function renderNotices(st){
  var out=[];
  if(!st.auth.ok) out.push('<div class="notice bad"><div><b>Not signed in to Bambu Cloud</b>'+esc(st.auth.error||'')+'</div></div>');
  if(st.log.payloads===0 && st.log.source==='host') out.push('<div class="notice warn"><div><b>No printer data yet</b>Open the Device tab so the printer connects; this OrcaSlicer build hands the printer status to the plugin directly.'+(st.log.error?'<br>'+esc(st.log.error):'')+'</div></div>');
  else if(st.log.payloads===0) out.push('<div class="notice warn"><div><b>No printer data yet</b>Live AMS contents come from OrcaSlicer’s own log. Set <code>Preferences › Debug › Log Level</code> to <code>info</code> (or higher), then open the Device tab so the printer connects. The feed picks up within a few seconds.'+(st.log.error?'<br>'+esc(st.log.error):'')+'</div></div>');
  else if(!st.log.live) out.push('<div class="notice"><div><b>Printer feed is stale</b>Last message '+esc(fmtAge(st.log.age_s))+'. OrcaSlicer only receives printer messages while a printer is connected in the Device tab.</div></div>');
  if(st.last_error) out.push('<div class="notice bad"><div><b>Last cloud error</b>'+esc(st.last_error)+'</div></div>');
  $('notices').innerHTML=out.join('');
}

function slotHtml(p, u, s){
  if(s.empty) return '<div class="slot empty"><span class="lab">'+esc(s.label)+'</span>Empty</div>';
  var rem = s.remain_pct!=null && s.remain_pct>=0 ? s.remain_pct : null;
  var m = s.match;
  var flag = s.badge==='decision' || s.badge==='pending';
  var mh;
  if(m){ var g = (m.computed_net!=null?m.computed_net:m.net); mh='<div class="m '+(m.kind==='soft'?'soft':'')+'" title="'+esc(m.name)+'">'+(m.kind==='soft'?'Colour match: ':'In library: ')+esc(g)+' g'+(m.total?(' / '+esc(m.total)+' g'):'')+'</div>'; }
  else if(flag) mh='<div class="m none">New filament, see above</div>';
  else mh='<div class="m none">Not in library <button class="sm" data-act="add_tray" data-dev="'+esc(p.dev_id)+'" data-ams="'+esc(u.id)+'" data-tray="'+esc(s.id)+'">Add</button></div>';
  return '<div class="slot'+(flag?' flag':'')+'" title="'+esc((s.vendor?s.vendor+' ':'')+s.name+(s.uuid?'\nRFID '+s.uuid:'\nNo RFID tag'))+'">'+(flag?'<span class="dot" title="New filament, see Action needed"></span>':'')+'<span class="lab">'+esc(s.label)+'</span>'+
    swatch(s.color, s.colors)+'<div class="t">'+esc(s.type||'?')+(s.rfid?'<span class="tag">RFID</span>':'')+'</div><div class="n">'+esc(s.name||'')+'</div>'+
    (rem!=null?'<div class="bar" title="'+rem+'% remaining"><i style="width:'+rem+'%"></i></div><div class="n">'+rem+'%'+(s.remain_g!=null?' ≈ '+s.remain_g+' g':'')+'</div>':'<div class="n">remaining unknown</div>')+mh+'</div>';
}

function renderPrinters(st){
  var ps = st.printers||[]; var box=$('printers');
  $('printersSub').textContent = ps.length? '' : '(nothing seen yet)';
  if(!ps.length){ box.innerHTML=''; return; }
  box.innerHTML = ps.map(function(p){
    var units = (p.units||[]).map(function(u){
      var hum = u.humidity_raw!=null ? (u.humidity_raw+'% RH') : (u.humidity!=null?('humidity '+u.humidity):'' );
      return '<div class="unit"><div class="uh"><b>'+esc(u.label)+'</b> '+esc(u.type_name)+(hum?' · '+esc(hum):'')+(u.temp!=null?' · '+esc(u.temp)+'°C':'')+'</div><div class="slots">'+u.slots.map(function(s){ return slotHtml(p,u,s); }).join('')+'</div></div>';
    }).join('');
    if(p.external) units += '<div class="unit ext" style="flex:0 0 150px;min-width:150px"><div class="uh"><b>External</b></div><div class="slots">'+slotHtml(p,{id:'254'},p.external)+'</div></div>';
    var state = p.gcode_state ? (p.gcode_state.charAt(0)+p.gcode_state.slice(1).toLowerCase()) : '';
    return '<div class="printer"><div class="ph"><span class="name">'+esc(p.name)+'</span><span class="muted">'+esc(p.model||p.dev_id)+'</span>'+
      '<span class="pill '+(p.live?'ok':'warn')+'"><span class="dot"></span>'+(p.live?'live':'last seen '+fmtAge(p.age_s))+'</span>'+
      (state?'<span class="pill"><span class="dot"></span>'+esc(state)+(p.mc_percent!=null&&p.busy?' '+esc(p.mc_percent)+'%':'')+'</span>':'')+
      '<span class="spacer"></span><button class="sm" data-act="sync_dev" data-dev="'+esc(p.dev_id)+'">Sync this AMS to cloud</button></div>'+
      (units?'<div class="units">'+units+'</div>':'<div class="muted">No AMS reported.</div>')+'</div>';
  }).join('');
}

function spoolRemaining(s){ var n = s.netWeight, t = s.totalNetWeight; var p = pct(n,t); return {n:n,t:t,p:p}; }

function spoolLine(s){ var r=spoolRemaining(s); var loc=(s.deviceName||'')+(s.slotId!=null&&s.amsId!=null?(' '+String.fromCharCode(65+(s.amsId%4))+(parseInt(s.slotId,10)+1)):''); return esc(s.filamentName||'')+(s.filamentType?' · '+esc(s.filamentType):'')+(r.n!=null?' · '+esc(r.n)+(r.t?' / '+esc(r.t):'')+' g':'')+(loc.trim()?' · '+esc(loc.trim()):''); }
function colorOf(s){ var c=String(s.color||'').replace('#',''); return c.length>=6?'#'+c.slice(0,6):'#888'; }

function renderBadges(st){
  var list = st.badges||[]; var sec=$('secBadges');
  if(!list.length){ sec.style.display='none'; $('badges').innerHTML=''; return; }
  sec.style.display='';
  $('badgesSub').textContent = list.length===1 ? '' : list.length+' items';
  $('badges').innerHTML = list.map(function(b, i){
    var t=b.tray||{}; var k=JSON.stringify(b.key);
    var what = (t.name && t.name!==t.type) ? (t.type?t.type+' · ':'')+t.name : (t.type||'filament');
    var head='<div><h3>'+esc(b.dev_name)+' · slot '+esc(b.slot)+': '+esc(what)+'</h3>';
    var body='', btns='';
    if(b.kind==='decision'){
      var cands=b.candidates||[];
      if(cands.length===0) body='<div class="muted">AMS detected a new filament. Add it to Filament Manager?</div>';
      else if(cands.length===1) body='<div class="muted">A similar filament was found in Filament Manager and matched automatically. Link this spool to it, or add it as a new one.</div>';
      else body='<div class="muted">Multiple similar filaments were found in Filament Manager. Pick the one this spool is, or add it as a new one.</div>';
      if(cands.length) body+='<div class="cands">'+cands.map(function(c,j){ return '<label><input type="radio" name="cand'+i+'" value="'+esc(c.id)+'"'+(j===0?' checked':'')+'><span class="sw" style="background:'+colorOf(c)+'"></span><span>'+spoolLine(c)+'</span></label>'; }).join('')+'</div>';
      btns='<div class="btns">'+(cands.length?'<button class="primary" data-act="badge" data-key=\''+esc(k)+'\' data-choice="link" data-i="'+i+'">Link to selected spool</button>':'')+'<button '+(cands.length?'':'class="primary" ')+'data-act="badge" data-key=\''+esc(k)+'\' data-choice="new">Add as new filament</button><button data-act="badge" data-key=\''+esc(k)+'\' data-choice="later">Later</button></div>';
    } else if(b.kind==='recorded'){
      body='<div class="muted">New filament information has been recorded in the library'+(b.spool?': '+spoolLine(b.spool):'')+'.</div>';
      btns='<div class="btns"><button class="primary" data-act="badge" data-key=\''+esc(k)+'\' data-choice="ok">OK</button></div>';
    } else if(b.kind==='failed'){
      body='<div class="muted">The cloud did not record this spool automatically'+(b.error?' ('+esc(b.error)+')':'')+'. Add it from the tray data instead.</div>';
      btns='<div class="btns"><button class="primary" data-act="badge" data-key=\''+esc(k)+'\' data-choice="add">Add to library</button><button data-act="badge" data-key=\''+esc(k)+'\' data-choice="later">Later</button></div>';
    } else {
      body='<div class="muted">Sent to the cloud, waiting for its answer…</div>';
    }
    return '<div class="badge '+esc(b.kind)+'">'+swatch(t.color, t.colors).replace('class="sw"','class="sw"')+head+body+btns+'</div></div>';
  }).join('');
}

function renderLibrary(st){
  var spools = (st.spools||[]).slice();
  var types = {}; spools.forEach(function(s){ if(s.filamentType) types[s.filamentType]=1; });
  var sel=$('filterType'); var cur=sel.value; sel.innerHTML='<option value="">All types</option>'+Object.keys(types).sort().map(function(t){ return '<option'+(t===cur?' selected':'')+'>'+esc(t)+'</option>'; }).join('');
  var q = $('search').value.trim().toLowerCase(); var ft=sel.value; var onlyM=$('onlyMounted').checked;
  spools = spools.filter(function(s){
    if(ft && s.filamentType!==ft) return false;
    if(onlyM && !s._location) return false;
    if(q){ var hay=[s.filamentName,s.displayName,s.filamentVendor,s.filamentType,s.note,s.filamentId,s._location].join(' ').toLowerCase(); if(hay.indexOf(q)<0) return false; }
    return true;
  });
  var k=S.sortKey, d=S.sortDir;
  spools.sort(function(a,b){ var x=a[k], y=b[k]; if(k==='location'){x=a._location||'';y=b._location||'';} if(k==='netWeight'){x=spoolRemaining(a).p; y=spoolRemaining(b).p; x=x==null?-1:x; y=y==null?-1:y;} if(x==null)x=''; if(y==null)y=''; if(typeof x==='string') x=x.toLowerCase(); if(typeof y==='string') y=y.toLowerCase(); return x<y?-d:(x>y?d:0); });
  $('librarySub').textContent = spools.length+' of '+(st.spools||[]).length+' spools';
  $('rows').innerHTML = spools.map(function(s){
    var r=spoolRemaining(s);
    var remHtml = r.p!=null ? '<div class="rem"><div class="bar"><i style="width:'+r.p+'%"></i></div><span>'+esc(r.n)+' / '+esc(r.t)+' g</span></div>' : '<span class="muted">'+(r.n!=null?esc(r.n)+' g':'?')+'</span>';
    var loc = s._location ? esc(s._location) + (s._live_here?'<span class="tag">now</span>':'') : '<span class="muted">—</span>';
    var nameHtml = s.displayName ? '<b>'+esc(s.displayName)+'</b><div class="muted" style="font-size:11px">'+esc(s.filamentName)+'</div>' : '<b>'+esc(s.filamentName)+'</b>';
    var tags = (s.RFID && /[^0]/.test(s.RFID)?'<span class="tag" title="'+esc(s.RFID)+'">RFID</span>':'') + (s.depleted?'<span class="tag" style="color:var(--bad)">empty</span>':'') + (s.status===1?'<span class="tag" style="color:var(--warn)">needs info</span>':'');
    return '<tr data-id="'+esc(s.id)+'"><td>'+swatch(s._color, s._colors)+'</td><td>'+nameHtml+tags+'</td><td>'+esc(s.filamentVendor)+'</td><td>'+esc(s.filamentType)+'</td><td>'+remHtml+'</td><td>'+loc+'</td><td class="muted">'+esc(s.note||'')+'</td><td class="muted">'+esc(fmtDate(s.updatedAt))+'</td>'+
      '<td class="actions"><button class="sm" data-act="edit" data-id="'+esc(s.id)+'">Edit</button><button class="sm danger" data-act="del" data-id="'+esc(s.id)+'">Delete</button></td></tr>';
  }).join('');
}

function render(){
  var st=S.state; if(!st) return;
  renderHeader(st); renderNotices(st); renderSlice(st); renderBadges(st); renderPrinters(st); renderLibrary(st);
  $('optPush').checked=!!st.settings.auto_push_remaining; $('optMount').checked=!!st.settings.auto_sync_mounts; $('optSoft').checked=!!st.settings.soft_match; $('optRegister').checked=!!st.settings.auto_register_rfid; $('optCheck').checked=!!st.settings.slice_check_notify;
  var diagPrinters = (st.printers||[]).map(function(p){
    var units = (p.units||[]).map(function(u){ var n = u.slots.filter(function(s){ return !s.empty; }).length; return u.label+': '+n+' trays'+(u.sn?' sn='+u.sn:''); });
    return {dev:p.dev_id, live:p.live, units:units};
  });
  $('diag').textContent = JSON.stringify({log:st.log, auth:{ok:st.auth.ok,host:st.auth.host,expires:st.auth.expires_at}, printers:diagPrinters, spools:(st.spools||[]).length, catalog:st.catalog_size, version:st.version}, null, 1);
}

function fillCatalog(st){
  var cat = st.catalog||{}; var fs = cat.filamentSettings||[]; var cats = cat.categories||[];
  var sel=$('fCatalog'); sel.innerHTML='<option value="">Custom / third party</option>'+fs.map(function(e,i){ return '<option value="'+i+'">'+esc(e.filamentVendor+' '+e.filamentName+' ('+e.filamentType+')')+'</option>'; }).join('');
  var ty=$('fType'); var seen={}; var opts=cats.slice(); (st.spools||[]).forEach(function(s){ if(s.filamentType && opts.indexOf(s.filamentType)<0) opts.push(s.filamentType); }); if(!opts.length) opts=['PLA','PETG','ABS','ASA','TPU','PA','PC','PET','PPS','Support'];
  ty.innerHTML = opts.map(function(t){ return '<option>'+esc(t)+'</option>'; }).join('');
  var vendors={}; fs.forEach(function(e){ vendors[e.filamentVendor]=1; }); (st.spools||[]).forEach(function(s){ if(s.filamentVendor) vendors[s.filamentVendor]=1; });
  $('vendors').innerHTML = Object.keys(vendors).sort().map(function(v){ return '<option value="'+esc(v)+'">'; }).join('');
}

function openDialog(spool, prefill){
  fillCatalog(S.state);
  S.editing = spool ? spool.id : null;
  $('dlgTitle').textContent = spool ? 'Edit spool' : 'Add spool';
  var src = spool || prefill || {};
  $('fCatalog').value=''; $('fVendor').value=src.filamentVendor||''; $('fName').value=src.filamentName||''; $('fDisplay').value=src.displayName||''; $('fId').value=src.filamentId||'';
  if(src.filamentType){ var ty=$('fType'); if([].slice.call(ty.options).every(function(o){return o.value!==src.filamentType;})){ var o=document.createElement('option'); o.textContent=src.filamentType; ty.appendChild(o);} ty.value=src.filamentType; }
  var col=(src.color||'#808080').slice(0,7); $('fColor').value=col; $('fColorHex').value=col;
  $('fTotal').value=src.totalNetWeight||1000; $('fNet').value=(src.netWeight!=null?src.netWeight:(src.totalNetWeight||1000)); $('fNote').value=src.note||'';
  var rfid = src.RFID && /[^0]/.test(src.RFID) ? src.RFID : '';
  $('fRfidRow').style.display = rfid?'':'none'; $('fRfid').value=rfid;
  $('fCatalog').disabled = !!spool;  // the catalogue entry is fixed once a spool exists
  $('modal').classList.add('open');
}
function closeDialog(){ $('modal').classList.remove('open'); S.editing=null; }

function saveDialog(){
  var body = {
    filamentVendor: $('fVendor').value.trim(), filamentType: $('fType').value, filamentName: $('fName').value.trim(),
    displayName: $('fDisplay').value.trim(), filamentId: $('fId').value.trim(), color: $('fColorHex').value.trim(), totalNetWeight: parseInt($('fTotal').value,10)||0,
    netWeight: parseInt($('fNet').value,10), note: $('fNote').value
  };
  if(!body.filamentName){ toast('Name is required'); return; }
  if(!/^#[0-9a-fA-F]{6}$/.test(body.color)){ toast('Colour must be #RRGGBB'); return; }
  if(isNaN(body.netWeight)) body.netWeight = body.totalNetWeight;
  if($('fRfid').value) body.RFID = $('fRfid').value;
  setBusy(true);
  if(S.editing!=null){ body.id = S.editing; post({action:'update', spool: body}); }
  else post({action:'create', spool: body});
}

document.addEventListener('click', function(ev){
  var b = ev.target.closest('button'); if(!b) return;
  var act=b.dataset.act;
  if(b.id==='btnRefresh'){ setBusy(true); post({action:'refresh'}); }
  else if(b.id==='btnAdd'){ openDialog(null); }
  else if(b.id==='btnSyncAll'){ setBusy(true); post({action:'sync_ams', dev_id:'', force:true}); }
  else if(b.id==='dlgCancel'){ closeDialog(); }
  else if(b.id==='dlgSave'){ saveDialog(); }
  else if(act==='edit'){ var sp=(S.state.spools||[]).filter(function(s){ return String(s.id)===b.dataset.id; })[0]; if(sp) openDialog(sp); }
  else if(act==='del'){ var sp2=(S.state.spools||[]).filter(function(s){ return String(s.id)===b.dataset.id; })[0]; if(sp2 && confirm('Delete "'+sp2.filamentName+'" from your cloud library?')){ setBusy(true); post({action:'delete', ids:[sp2.id]}); } }
  else if(act==='add_tray'){ setBusy(true); post({action:'add_from_tray', dev_id:b.dataset.dev, ams_id:b.dataset.ams, tray_id:b.dataset.tray}); }
  else if(act==='sync_dev'){ setBusy(true); post({action:'sync_ams', dev_id:b.dataset.dev, force:true}); }
  else if(act==='recheck'){ setBusy(true); post({action:'recheck'}); }
  else if(act==='badge'){ var key=JSON.parse(b.dataset.key); var target=0; if(b.dataset.choice==='link'){ var r=document.querySelector('input[name="cand'+b.dataset.i+'"]:checked'); target = r ? parseInt(r.value,10) : 0; } setBusy(true); post({action:'badge', key:key, choice:b.dataset.choice, target_id:target}); }
});
document.querySelectorAll('th[data-k]').forEach(function(th){ th.addEventListener('click', function(){ var k=th.dataset.k; if(!k) return; if(S.sortKey===k) S.sortDir=-S.sortDir; else { S.sortKey=k; S.sortDir=1; } render(); }); });
['search','filterType','onlyMounted'].forEach(function(id){ $(id).addEventListener('input', render); $(id).addEventListener('change', render); });
['optPush','optMount','optSoft','optRegister','optCheck'].forEach(function(id){ $(id).addEventListener('change', function(){ post({action:'settings', settings:{auto_push_remaining:$('optPush').checked, auto_sync_mounts:$('optMount').checked, soft_match:$('optSoft').checked, auto_register_rfid:$('optRegister').checked, slice_check_notify:$('optCheck').checked}}); }); });
$('fColor').addEventListener('input', function(){ $('fColorHex').value=$('fColor').value.toUpperCase(); });
$('fColorHex').addEventListener('input', function(){ if(/^#[0-9a-fA-F]{6}$/.test($('fColorHex').value)) $('fColor').value=$('fColorHex').value; });
$('fCatalog').addEventListener('change', function(){ var i=$('fCatalog').value; if(i==='') return; var e=(S.state.catalog.filamentSettings||[])[parseInt(i,10)]; if(!e) return; $('fVendor').value=e.filamentVendor; $('fName').value=e.filamentName; $('fId').value=e.filamentId; $('fType').value=e.filamentType; });
$('modal').addEventListener('click', function(ev){ if(ev.target===$('modal')) closeDialog(); });
document.addEventListener('keydown', function(ev){ if(ev.key==='Escape') closeDialog(); });

function onMessage(data){
  if(!data || typeof data!=='object') return;
  if(data.state){ S.state = data.state; render(); }
  if(data.action && data.action!=='state' && data.action!=='tick'){ setBusy(false); }
  if(data.ok===false && data.error){ toast('Error: '+data.error, 6000); }
  else if(data.message){ toast(data.message); }
  if(data.action==='create' || data.action==='update'){ if(data.ok!==false) closeDialog(); }
}
if(window.orca && window.orca.onMessage){ window.orca.onMessage(onMessage); }
window.__fm = {onMessage:onMessage, state:function(){return S.state;}};
post({action:'ready'});
setInterval(function(){ if(!S.busy) post({action:'state'}); }, 3000);
setInterval(function(){ if(!S.busy) post({action:'tick'}); }, 60000);
})();
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# Plugin core (independent of the orca module so the tests can drive it)
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {"auto_push_remaining": True, "auto_sync_mounts": True, "soft_match": True, "auto_register_rfid": True,
                    "slice_check_notify": True}


class FilamentManagerCore:
    def __init__(self, base_dir=None, client=None, state=None, start_tailer=True, host=None):
        self.base_dir = base_dir or data_dir()
        self.auth = Auth(self.base_dir).refresh()
        self.client = client or CloudClient(self.auth, ca_fallback=os.path.join(self.base_dir, "obn_cloud_ca.pem"))
        self.state = state or DeviceState()
        self.tailer = None
        self.host_feed = HostFeed(host, self.state) if HostFeed.available(host) else None
        if start_tailer and self.host_feed is None:
            self.start_tailer()
        self.settings = dict(DEFAULT_SETTINGS)
        self.spools = []
        self.catalog = {}
        self.devices = {}
        self.last_error = ""
        self.last_pull = 0.0
        self.last_push = {}            # RFID -> (time, grams)
        self.last_mount = {}           # RFID -> (dev_id, ams_id, slot_id)
        self.save_settings_fn = None
        self.notify_fn = None          # host notification: notify_fn(text, important: bool)
        self.auto_added = set()        # RFIDs already pushed to the cloud this session
        self.badges = {}               # (dev_id, ams_id, slot_id) -> pending "new filament" entry
        self.dismissed = set()         # RFIDs the user chose "later" for
        self.last_slice = None         # parsed G-code of the last slice (parse_slice_gcode)
        self.last_check = None         # check_slice() of it against the AMS

    # -- lifecycle ---------------------------------------------------------
    def start_tailer(self):
        if self.tailer is None or not self.tailer.is_alive():
            self.tailer = LogTailer(os.path.join(self.base_dir, "log"), self.state)
            self.tailer.start()

    def stop(self):
        if self.tailer:
            self.tailer.stop_event.set()

    def selected_machine(self):
        if self.host_feed:
            sel = self.host_feed.selected()
            if sel:
                return sel
        try:
            with open(os.path.join(self.base_dir, "obn.state.json"), "r", encoding="utf-8") as fh:
                return str(json.load(fh).get("selected_machine") or "")
        except Exception:  # noqa: BLE001
            return ""

    def device_names(self):
        """Cloud bind list names, overridden by what the host reports."""
        names = dict(self.devices)
        if self.host_feed:
            for dev_id, d in self.host_feed.names.items():
                merged = dict(names.get(dev_id, {}))
                merged.update({k: v for k, v in d.items() if v not in (None, "")})
                names[dev_id] = merged
        return names

    # -- cloud pulls -------------------------------------------------------
    def pull(self, with_catalog=True):
        self.auth.refresh()
        self.spools = self.client.list_spools()
        if with_catalog or not self.catalog:
            try:
                self.catalog = self.client.get_catalog() or {}
            except CloudError as e:
                _log("catalog: %s" % e)
            try:
                # keep only what the page needs; the bind list also carries access codes
                self.devices = {d["dev_id"]: {k: d.get(k) for k in ("dev_id", "name", "dev_product_name", "dev_model_name", "online")}
                                for d in self.client.list_devices() if d.get("dev_id")}
            except CloudError as e:
                _log("devices: %s" % e)
        self.last_pull = time.time()
        self.last_error = ""

    # -- state for the page --------------------------------------------------
    def tailer_status(self):
        printers, last_at, count = self.state.snapshot()
        age = int(time.time() - last_at) if last_at else None
        if self.host_feed:
            source, file_name, error = "host", "orca.host.device_status()", self.host_feed.error
        else:
            source = "log"
            file_name = os.path.basename(self.tailer.current) if self.tailer and self.tailer.current else ""
            error = self.tailer.error if self.tailer else "log tailer not running"
        return {
            "source": source,
            "file": file_name,
            "error": error,
            "payloads": count,
            "age_s": age,
            "live": bool(last_at) and (time.time() - last_at) < LOG_LIVE_WINDOW_S,
        }, printers

    def page_state(self):
        self.auth.refresh()
        log, printers = self.tailer_status()
        view = build_printers_view(printers, self.catalog, self.spools, self.device_names(), self.selected_machine(),
                                   soft_match=self.settings.get("soft_match", True))
        live_loc = {}
        for p in view:
            for u in p["units"]:
                for s in u["slots"]:
                    if s.get("match"):
                        live_loc[s["match"]["id"]] = "%s %s" % (p["name"], s["label"])
            if p.get("external") and p["external"].get("match"):
                live_loc[p["external"]["match"]["id"]] = "%s external" % p["name"]
        spools = []
        for s in self.spools:
            d = dict(s)
            d["_color"] = norm_hex(s.get("color")) or "#888888"
            d["_colors"] = [norm_hex(c) for c in (s.get("colors") or []) if norm_hex(c)]
            loc = live_loc.get(s.get("id"))
            d["_live_here"] = bool(loc)
            d["_location"] = loc or spool_location(s)
            spools.append(d)
        badge_slots = {(k[0], k[1], k[2]): b["kind"] for k, b in self.badges.items()}
        for p in view:
            for u in p["units"]:
                for s in u["slots"]:
                    kind = badge_slots.get((p["dev_id"], u["id"], s["id"]))
                    if kind:
                        s["badge"] = kind
        return {
            "version": PLUGIN_VERSION,
            "auth": self.auth.status(),
            "log": log,
            "printers": view,
            "badges": self.badges_view(self.device_names()),
            "spools": spools,
            "catalog": {"categories": self.catalog.get("categories") or [],
                        "filamentSettings": self.catalog.get("filamentSettings") or []},
            "catalog_size": len(self.catalog.get("filamentSettings") or []),
            "settings": self.settings,
            "last_error": self.last_error,
            "last_pull": self.last_pull,
            "slice_check": self.last_check,
        }

    # -- slice check -------------------------------------------------------
    def run_check(self, info=None):
        """Compare a slice (the latest by default) with the current AMS contents."""
        info = info or self.last_slice
        if not info:
            return ""
        _, printers = self.tailer_status()
        view = build_printers_view(printers, self.catalog, self.spools, self.device_names(), self.selected_machine(),
                                   soft_match=self.settings.get("soft_match", True))
        self.last_slice = info
        self.last_check = check_slice(info, view, self.selected_machine())
        return slice_check_message(self.last_check)

    def consume_slice(self):
        """Pick up a slice the pipeline step published; notify when it is a new one."""
        info = take_slice()
        if not info:
            return ""
        same = self.last_slice and self.last_slice.get("filaments") == info.get("filaments")
        msg = self.run_check(info)
        if same or not self.settings.get("slice_check_notify", True):
            return msg
        self._notify(msg.replace("Filament check: ", "", 1),
                     important=self.last_check["status"] in ("low", "missing", "backup"))
        return msg

    # -- helpers -----------------------------------------------------------
    def _find_tray(self, dev_id, ams_id, tray_id):
        printers, _, _ = self.state.snapshot()
        p = printers.get(dev_id)
        if not p:
            return None, None
        if str(ams_id) == "254":
            return p, p.get("vt_tray")
        unit = p["ams"].get(str(ams_id))
        if not unit:
            return p, None
        return p, unit["trays"].get(str(tray_id))

    def ams_serial(self, dev_id, printer, ams_id):
        """AMS unit serial: from the printer's get_version reply if seen, else
        from a cloud spool record that is mounted in that unit (Studio and
        Handy write amsSn there), else empty."""
        sn = (printer or {}).get("ams_sn", {}).get(str(ams_id), "")
        if sn:
            return sn
        want = _as_int(ams_id, -1)
        for s in self.spools:
            if str(s.get("devId") or "") == dev_id and _as_int(s.get("amsId"), -2) == want and s.get("amsSn"):
                return str(s["amsSn"])
        return ""

    def _spool_by_rfid(self, uuid):
        uuid = str(uuid or "").upper()
        for s in self.spools:
            if str(s.get("RFID") or "").upper() == uuid and _valid_uuid(uuid):
                return s
        return None

    def _spool_by_id(self, sid):
        for s in self.spools:
            if str(s.get("id")) == str(sid):
                return s
        return None

    # -- actions -----------------------------------------------------------
    def create_spool(self, form):
        body = {
            "createType": "ams" if form.get("RFID") else "manual",
            "filamentVendor": form.get("filamentVendor") or "Generic",
            "filamentType": form.get("filamentType") or "PLA",
            "filamentName": form.get("filamentName") or "Filament",
            "filamentId": form.get("filamentId") or "",
            "isSupport": False,
            "color": (norm_hex(form.get("color")) or "#808080"),
            "colorType": 0,
            "totalNetWeight": int(form.get("totalNetWeight") or 1000),
            "netWeight": int(form.get("netWeight") if form.get("netWeight") is not None else form.get("totalNetWeight") or 1000),
            "rolls": 1,
            "note": form.get("note") or "",
        }
        if form.get("displayName"):
            body["displayName"] = str(form["displayName"]).strip()
        if form.get("RFID"):
            body["RFID"] = str(form["RFID"]).upper()
        self.client.create_spool(body)
        self.pull(with_catalog=False)
        return "Added %s" % body["filamentName"]

    def update_spool(self, form):
        sid = form.get("id")
        cur = self._spool_by_id(sid) or {}
        body = {"id": int(sid), "filamentName": form.get("filamentName") or cur.get("filamentName") or "Filament"}
        for key in ("filamentVendor", "filamentType", "filamentId", "note", "displayName"):
            if key in form and (form[key] or "") != (cur.get(key) or ""):
                body[key] = form[key]
        if form.get("color") and norm_hex(form["color"]) != norm_hex(cur.get("color")):
            body["color"] = norm_hex(form["color"])
        for key in ("totalNetWeight", "netWeight"):
            if form.get(key) is not None and int(form[key]) != _as_int(cur.get(key)):
                body[key] = int(form[key])
        resp = self.client.update_spool(body)
        updated = (resp or {}).get("filamentV2")
        if isinstance(updated, dict):
            for i, s in enumerate(self.spools):
                if str(s.get("id")) == str(sid):
                    self.spools[i] = {**s, **updated}
        else:
            self.pull(with_catalog=False)
        return "Saved %s" % body["filamentName"]

    def delete_spools(self, ids):
        self.client.delete_spools(ids)
        self.spools = [s for s in self.spools if str(s.get("id")) not in {str(i) for i in ids}]
        return "Deleted %d spool%s" % (len(ids), "" if len(ids) == 1 else "s")

    def add_from_tray(self, dev_id, ams_id, tray_id):
        p, tray = self._find_tray(dev_id, ams_id, tray_id)
        if not tray or tray_is_empty(tray):
            raise CloudError(0, "That slot is empty or not known yet.")
        if _valid_uuid(tray.get("tray_uuid")) and self._spool_by_rfid(tray.get("tray_uuid")):
            raise CloudError(0, "That spool is already in the library.")
        body = build_create_from_tray(tray, self.catalog)
        self.client.create_spool(body)
        self.pull(with_catalog=False)
        # Record where it sits straight away so the library shows the slot.
        if _valid_uuid(tray.get("tray_uuid")) and self.settings.get("auto_sync_mounts", True):
            try:
                self.sync_ams(dev_id, force=True)
            except CloudError as e:
                _log("post-add sync: %s" % e)
        return "Added %s from slot %s%s" % (body["filamentName"], ams_unit_label(ams_id), _as_int(tray_id, 0) + 1)

    def sync_ams(self, dev_id="", force=False):
        """Push the mount state of every RFID-matched tray to the cloud.

        Mirrors Studio's path B (ams/sync) for mounted spools, and its
        slot-mappings unbind for cloud spools whose recorded slot is now empty
        or holds another spool. Returns a short summary.
        """
        printers, _, _ = self.state.snapshot()
        targets = [dev_id] if dev_id else [d for d, p in printers.items() if time.time() - p.get("last_seen", 0) < 300]
        pushed, unbound = 0, 0
        for dev in targets:
            p = printers.get(dev)
            if not p:
                continue
            present = {}
            per_unit = []           # one request per AMS unit: the server keys slots by amsSn+slotId,
            for uid, unit in p["ams"].items():   # and with an unknown serial two units would collide
                items = []
                utype = ams_type_from_info(unit.get("info"), uid)
                usn = self.ams_serial(dev, p, uid)
                for tid, tray in unit["trays"].items():
                    if tray_is_empty(tray, p.get("tray_exist_bits"), uid):
                        continue
                    uuid = str(tray.get("tray_uuid") or "").upper()
                    if not _valid_uuid(uuid):
                        continue
                    present[(uid, tid)] = uuid
                    spool = self._spool_by_rfid(uuid)
                    if not spool:
                        continue
                    mount = (dev, _as_int(uid, 0), str(tid))
                    if not force and self.last_mount.get(uuid) == mount:
                        continue
                    items.append(build_ams_sync_item(tray, uid, usn, utype, spool))
                if items:
                    per_unit.append(items)
            for items in per_unit:
                self.client.ams_sync(dev, items)
                for it in items:
                    self.last_mount[it["RFID"]] = (dev, it["amsId"], it["slotId"])
                pushed += len(items)
            # unbind: cloud says spool X is in (dev, ams, slot) but that slot is empty / different now
            mappings = []
            for s in self.spools:
                if str(s.get("devId") or "") != dev:
                    continue
                ams_id, slot = s.get("amsId"), s.get("slotId")
                if ams_id is None or slot in (None, ""):
                    continue
                key = (str(ams_id), str(slot))
                unit = p["ams"].get(str(ams_id))
                if not unit or key[1] not in unit["trays"]:
                    continue        # we have no data for that slot; leave it alone
                here = present.get(key)
                if here and here == str(s.get("RFID") or "").upper():
                    continue
                usn = self.ams_serial(dev, p, str(ams_id))
                if not usn:
                    continue        # slot-mappings needs the AMS serial
                mappings.append({"amsId": _as_int(ams_id, 0), "amsSn": usn,
                                 "amsType": ams_type_from_info(unit.get("info"), ams_id),
                                 "rfid": None, "slotId": str(slot), "spoolId": None})
            if mappings:
                self.client.slot_mappings_sync(dev, mappings)
                unbound += len(mappings)
        if pushed or unbound:
            self.pull(with_catalog=False)
        return "Synced %d slot%s%s" % (pushed, "" if pushed == 1 else "s", (", cleared %d" % unbound) if unbound else "")

    def push_remaining(self, force=False, only_rfid=None):
        """Studio's path A: PUT netWeight from the AMS remain estimate."""
        printers, _, _ = self.state.snapshot()
        now = time.time()
        updated = 0
        for dev, p in printers.items():
            busy = str(p.get("gcode_state") or "").upper() in BUSY_STATES
            trays = []
            for uid, unit in p["ams"].items():
                trays.extend(unit["trays"].values())
            if isinstance(p.get("vt_tray"), dict):
                trays.append(p["vt_tray"])
            for tray in trays:
                if tray_is_empty(tray):
                    continue
                uuid = str(tray.get("tray_uuid") or "").upper()
                if not _valid_uuid(uuid) or (only_rfid and uuid != only_rfid):
                    continue
                spool = self._spool_by_rfid(uuid)
                if not spool:
                    continue
                total = _as_int(spool.get("totalNetWeight"), 0) or 0
                grams = remain_grams(tray, total)
                if grams is None:
                    continue
                if not force and grams == _as_int(spool.get("netWeight")):
                    continue
                last = self.last_push.get(uuid)
                if not force and busy and last and now - last[0] < BUSY_PUSH_INTERVAL_S:
                    continue
                if not force and last and last[1] == grams:
                    continue
                resp = self.client.update_spool({"id": int(spool["id"]), "filamentName": spool.get("filamentName") or "Filament",
                                                 "netWeight": int(grams)})
                self.last_push[uuid] = (now, grams)
                upd = (resp or {}).get("filamentV2")
                for i, s in enumerate(self.spools):
                    if s is spool:
                        self.spools[i] = {**s, **(upd if isinstance(upd, dict) else {"netWeight": grams})}
                updated += 1
        return updated

    # -- new official RFID spools (Studio's "i" badge) -----------------------
    def _notify(self, text, important=False):
        if self.notify_fn:
            try:
                self.notify_fn(text, important)
            except Exception as e:  # noqa: BLE001
                _log("notify: %r" % e)

    def calibrate_badges(self, printers):
        """Drop badges whose slot is now empty or holds a different spool (Studio rule)."""
        for key in list(self.badges.keys()):
            dev, uid, tid = key
            p = printers.get(dev)
            tray = (p or {}).get("ams", {}).get(uid, {}).get("trays", {}).get(tid) if p else None
            if tray is None or tray_is_empty(tray, (p or {}).get("tray_exist_bits"), uid):
                continue                # no data for that slot right now; keep it
            uuid = str(tray.get("tray_uuid") or "").upper()
            if _valid_uuid(uuid) and uuid != self.badges[key]["uuid"]:
                del self.badges[key]

    def auto_register(self):
        """Push brand-new official RFID trays to the cloud and queue a badge for each."""
        if not self.settings.get("auto_register_rfid", True) or not self.last_pull:
            return
        printers, _, _ = self.state.snapshot()
        self.calibrate_badges(printers)
        new_keys = []
        for dev, p in printers.items():
            for uid, unit in p["ams"].items():
                utype = ams_type_from_info(unit.get("info"), uid)
                if utype == 2:          # AMS Lite has no RFID reader
                    continue
                usn = self.ams_serial(dev, p, uid)
                for tid, tray in unit["trays"].items():
                    if tray_is_empty(tray, p.get("tray_exist_bits"), uid) or not is_official_rfid_tray(tray):
                        continue
                    uuid = str(tray["tray_uuid"]).upper()
                    if self._spool_by_rfid(uuid):
                        self.auto_added.discard(uuid)       # delete + reinsert re-triggers, as in Studio
                        continue
                    if uuid in self.auto_added or uuid in self.dismissed:
                        continue
                    self.auto_added.add(uuid)
                    item = build_register_item(tray, uid, usn, utype, self.catalog)
                    key = (dev, uid, tid)
                    self.badges[key] = {"uuid": uuid, "kind": "pending", "created": time.time(), "ams_sn": usn,
                                        "tray": build_tray_view(tray, uid, p, self.catalog, {}, {}, False),
                                        "hit": None, "candidates": [], "spool": None, "notified": False}
                    try:
                        self.client.ams_sync(dev, [item])
                        _log("registered new RFID spool %s from %s %s%s" % (uuid, dev, ams_unit_label(uid), _as_int(tid, 0) + 1))
                    except CloudError as e:
                        _log("auto register %s: %s" % (uuid, e))
                        self.badges[key]["error"] = str(e)
                    new_keys.append(key)
        if new_keys:
            self.pull(with_catalog=False)
            self.check_badges()

    def check_badges(self):
        """Ask the soft-match queue what the cloud did with each registered spool."""
        cache = {}
        for key, b in self.badges.items():
            if b["kind"] != "pending":
                continue
            dev = key[0]
            q = (dev, b.get("ams_sn") or "")
            if q not in cache:
                try:
                    cache[q] = self.client.soft_match_pending(dev, q[1]) or {}
                except CloudError as e:
                    _log("soft-match pending: %s" % e)
                    cache[q] = None
            resp = cache[q]
            if resp is None:
                continue
            hit = next((h for h in (resp.get("hits") or []) if isinstance(h, dict)
                        and str(h.get("RFID") or "").upper() == b["uuid"]), None)
            if hit:
                cands = []
                for entry in resp.get("candidates") or []:
                    if isinstance(entry, dict) and entry.get("pendingId") == hit.get("id"):
                        cands = [c for c in (entry.get("candidates") or []) if isinstance(c, dict)]
                        break
                b.update({"kind": "decision", "hit": hit, "candidates": cands})
                if not b["notified"]:
                    b["notified"] = True
                    self._notify("AMS detected a new filament (%s %s). Open the Filament Manager tab to add or link it."
                                 % (b["tray"].get("type", ""), b["tray"].get("name", "")), important=True)
            else:
                spool = self._spool_by_rfid(b["uuid"])
                if spool:
                    b.update({"kind": "recorded", "spool": spool})
                    if not b["notified"]:
                        b["notified"] = True
                        self._notify("New filament recorded in Filament Manager: %s %s"
                                     % (spool.get("filamentType", ""), spool.get("filamentName", "")))
                elif b.get("error") or time.time() - b["created"] > 120:
                    b["kind"] = "failed"

    def resolve_badge(self, key, choice, target_id=0):
        b = self.badges.get(tuple(key))
        if not b:
            raise CloudError(0, "That prompt is no longer current.")
        msg = ""
        if choice == "later":
            self.dismissed.add(b["uuid"])
        elif choice == "add":                       # manual fallback when the cloud did not record it
            p, tray = self._find_tray(*key)
            if not tray:
                raise CloudError(0, "That slot is not known any more.")
            self.client.create_spool(build_create_from_tray(tray, self.catalog))
            msg = "Added as a new spool"
        elif b["kind"] == "decision" and b["hit"]:
            hit_id = int(b["hit"].get("id") or 0)
            if choice == "new":
                self.client.soft_match_action("create_new", hit_id)
                msg = "Added as a new spool"
            elif choice == "link":
                target = int(target_id or 0)
                if target and target != hit_id:
                    self.client.soft_match_action("link_other", hit_id, target)
                else:
                    self.client.soft_match_action("accept", hit_id)
                msg = "Linked to the existing spool"
            else:
                raise CloudError(0, "Unknown choice %r" % choice)
        elif choice in ("ok", "new", "link"):
            msg = ""
        else:
            raise CloudError(0, "Unknown choice %r" % choice)
        self.badges.pop(tuple(key), None)
        if msg:
            self.pull(with_catalog=False)
            if self.settings.get("auto_sync_mounts", True):
                try:
                    self.sync_ams(key[0], force=True)
                except CloudError as e:
                    _log("post-resolve sync: %s" % e)
        return msg

    def badges_view(self, device_names):
        out = []
        for key, b in self.badges.items():
            dev, uid, tid = key
            name = (device_names.get(dev) or {}).get("name") or dev
            out.append({
                "key": list(key), "dev_id": dev, "dev_name": name,
                "slot": "%s%d" % (ams_unit_label(uid), _as_int(tid, 0) + 1),
                "uuid": b["uuid"], "kind": b["kind"], "error": b.get("error", ""),
                "tray": b["tray"], "hit": b["hit"], "candidates": b["candidates"],
                "spool": b["spool"],
            })
        out.sort(key=lambda x: (x["kind"] != "decision", x["dev_name"], x["slot"]))
        return out

    def tick(self):
        """Periodic work driven by the page's timer (runs on the UI thread)."""
        if not self.auth.refresh().token or not self.spools:
            return ""
        msgs = []
        if self.settings.get("auto_push_remaining", True):
            n = self.push_remaining()
            if n:
                msgs.append("updated remaining on %d spool%s" % (n, "" if n == 1 else "s"))
        if self.settings.get("auto_sync_mounts", True):
            try:
                r = self.sync_ams()
                if not r.startswith("Synced 0 slots"):
                    msgs.append(r)
            except CloudError as e:
                _log("auto sync: %s" % e)
        if time.time() - self.last_pull > 900:
            self.pull(with_catalog=False)
        if any(b["kind"] == "pending" for b in self.badges.values()):
            self.check_badges()
        return "; ".join(msgs)

    # -- message dispatch --------------------------------------------------
    def handle(self, data):
        action = str(data.get("action") or "")
        reply = {"action": action, "ok": True}
        if self.host_feed:
            self.host_feed.poll()       # UI thread: the host bindings marshal there anyway
        try:
            if action == "ready":
                if not self.spools and self.auth.refresh().token:
                    self.pull()
            elif action == "refresh":
                self.pull()
                reply["message"] = "Loaded %d spools" % len(self.spools)
            elif action == "state":
                pass
            elif action == "tick":
                m = self.tick()
                if m:
                    reply["message"] = m
            elif action == "settings":
                for k, v in (data.get("settings") or {}).items():
                    if k in DEFAULT_SETTINGS:
                        self.settings[k] = bool(v)
                if self.save_settings_fn:
                    self.save_settings_fn(self.settings)
            elif action == "create":
                reply["message"] = self.create_spool(data.get("spool") or {})
            elif action == "update":
                reply["message"] = self.update_spool(data.get("spool") or {})
            elif action == "delete":
                reply["message"] = self.delete_spools(data.get("ids") or [])
            elif action == "add_from_tray":
                reply["message"] = self.add_from_tray(data.get("dev_id"), data.get("ams_id"), data.get("tray_id"))
            elif action == "sync_ams":
                n = self.push_remaining(force=False)
                reply["message"] = self.sync_ams(data.get("dev_id") or "", force=bool(data.get("force")))
                if n:
                    reply["message"] += ", updated remaining on %d" % n
            elif action == "badge":
                reply["message"] = self.resolve_badge(data.get("key") or [], data.get("choice") or "", data.get("target_id") or 0)
            elif action == "recheck":
                reply["message"] = self.run_check() or "No slice seen yet. Pick Filament check in Process settings > Others > Slicing Pipeline Plugin, then slice."
            else:
                reply["ok"] = False
                reply["error"] = "Unknown action %r" % action
        except CloudError as e:
            reply["ok"] = False
            reply["error"] = str(e)
            self.last_error = str(e)
            _log("%s: %s" % (action, e))
        except Exception as e:  # noqa: BLE001
            reply["ok"] = False
            reply["error"] = "%s: %s" % (type(e).__name__, e)
            self.last_error = reply["error"]
            _log("%s failed: %r" % (action, e))
        if action in ("ready", "state", "tick", "refresh") and self.auth.token:
            try:
                self.auto_register()
            except Exception as e:  # noqa: BLE001
                _log("auto_register: %r" % e)
        if action in ("ready", "state", "tick", "refresh"):
            try:
                self.consume_slice()
            except Exception as e:  # noqa: BLE001
                _log("slice check: %r" % e)
        reply["state"] = self.page_state()
        return reply


# ---------------------------------------------------------------------------
# OrcaSlicer bindings
# ---------------------------------------------------------------------------

if orca is not None:

    class FilamentManagerPage(orca.pages.PagesPluginCapabilityBase):
        core = None

        def get_name(self):
            return "Filament Manager"

        def get_icon(self):
            path = os.path.join(HERE, "filament_manager_icon.png")
            try:
                if not os.path.isfile(path):
                    with open(path, "wb") as fh:
                        fh.write(icon_png_bytes())
                return path
            except Exception as e:  # noqa: BLE001
                _log("icon: %r" % e)
                return ""

        def _core(self):
            if self.core is None:
                self.core = FilamentManagerCore(host=getattr(orca, "host", None))
                _log("printer feed: %s" % ("orca.host device bindings" if self.core.host_feed else "log tailer"))
                self.core.save_settings_fn = self._save_settings
                self.core.notify_fn = self._notify
                try:
                    stored = json.loads(self.get_config() or "{}")
                    for k in DEFAULT_SETTINGS:
                        if k in stored:
                            self.core.settings[k] = bool(stored[k])
                except Exception as e:  # noqa: BLE001
                    _log("config: %r" % e)
            return self.core

        def _save_settings(self, settings):
            try:
                self.save_config(json.dumps(settings))
            except Exception as e:  # noqa: BLE001
                _log("save_config: %r" % e)

        @staticmethod
        def _notify(text, important=False):
            """Plater notification (the slide-in box over the 3D view). Stands in for
            Studio's "i" badge, since a plugin cannot draw on the Device tab."""
            ui = getattr(orca.host, "ui", None)
            levels = getattr(ui, "NotificationLevel", None)
            if ui is None or levels is None:
                return
            level = levels.ImportantNotificationLevel if important else levels.RegularNotificationLevel
            ui.push_notification(level, "Filament Manager: " + text)

        def get_default_config(self):
            return dict(DEFAULT_SETTINGS)

        def on_load(self):
            try:
                self._core()
            except Exception as e:  # noqa: BLE001
                _log("on_load: %r" % e)

        def on_unload(self):
            if self.core:
                self.core.stop()

        def get_ui(self):
            self._core()
            return PAGE_HTML

        def on_message(self, message):
            try:
                data = message if isinstance(message, dict) else json.loads(message)
            except Exception:  # noqa: BLE001
                data = {"action": str(message)}
            if not isinstance(data, dict):
                data = {"action": str(data)}
            reply = self._core().handle(data)
            try:
                self.post_message(reply)
            except Exception as e:  # noqa: BLE001
                _log("post_message: %r" % e)

    class FilamentCheckStep(orca.slicing.SlicingPipelineCapabilityBase):
        """Slicing-pipeline step: runs right after every slice once it is picked in
        Process settings > Others > Slicing Pipeline Plugin. It runs on the slicing
        worker, so it only reads the G-code and hands the numbers over; the AMS
        comparison and the notification happen in the tab (UI thread), because
        orca.host.ui must not be used from here."""

        def get_name(self):
            return "Filament check"

        def execute(self, ctx):
            path = getattr(ctx, "gcode_path", "") or ""
            if not path:                      # geometry steps: nothing to do
                return orca.ExecutionResult.success()
            try:
                info = parse_slice_gcode(path)
                info["output_name"] = getattr(ctx, "output_name", "") or ""
                publish_slice(info)
                return orca.ExecutionResult.success("Filament check: %d filament(s), %.0f g" % (len(info["filaments"]), info["total_g"]))
            except Exception as e:  # noqa: BLE001
                _log("slice parse: %r" % e)
                return orca.ExecutionResult.success("Filament check skipped: %s" % e)

    @orca.plugin
    class FilamentManagerPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(FilamentManagerPage)
            orca.register_capability(FilamentCheckStep)
