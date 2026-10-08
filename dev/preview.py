"""Build dev/preview.html: the plugin page with a fake window.orca bridge and
sample data, so the UI can be checked in a normal browser without OrcaSlicer.

    python dev/preview.py            -> writes dev/preview.html
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import glob as _glob
import importlib.util as _ilu
_plugin_file = sorted(_glob.glob(os.path.join(ROOT, "bambu_filament_manager*.py")))[0]
_spec = _ilu.spec_from_file_location("bambu_filament_manager", _plugin_file)
fm = _ilu.module_from_spec(_spec)
sys.modules["bambu_filament_manager"] = fm
_spec.loader.exec_module(fm)
import test_offline as fx            # noqa: E402

state = fm.DeviceState()
state.ingest("01P00A000000001", fx.PUSH_STATUS)
state.ingest("01P00A000000001", fx.GET_VERSION)
core = fm.FilamentManagerCore(base_dir=HERE, client=fx.FakeClient(), state=state, start_tailer=False)
core.auth.data = {"access_token": "x", "nick_name": "Elliott", "region": "GLOBAL", "expires_at": "2027-10-05T08:40:30Z"}
core.auth.error = ""
core.auth.refresh = lambda: core.auth          # no obn.auth.json here; keep the fake session
core.pull()
# a parked "new filament" decision and a recorded one, so the Action needed cards show
core.client.park = True
core.client.parked = {}
state.ingest("01P00A000000001", fx.NEW_TRAY_FRAME)
core.notify_fn = lambda *a: None
core.auto_register()
core.badges[("01P00A000000001", "0", "1")] = {"uuid": "D" * 32, "kind": "recorded", "created": 0, "ams_sn": "",
                                               "tray": {"type": "PETG", "name": "PETG HF", "color": "#000000", "colors": []},
                                               "hit": None, "candidates": [], "spool": fx.SPOOLS[2], "notified": True}
page_state = core.page_state()
page_state["log"] = {"file": "debug_Mon_Oct_05_10_46_21.log.0", "error": "", "payloads": 2, "age_s": 3, "live": True}

mock = """
<script>
window.__errors = []; window.__replies = 0;
window.onerror = function(m, src, line, col){ window.__errors.push(m+' @'+line+':'+col); document.title = 'JSERR ' + window.__errors.join(' | '); };
(function(){
  var handlers=[]; var STATE=%s;
  window.orca = {
    postMessage: function(msg){
      console.log('page -> plugin', JSON.stringify(msg));
      var reply = {action: msg.action, ok: true, state: STATE};
      if(msg.action==='refresh') reply.message='Loaded '+STATE.spools.length+' spools (mock)';
      if(msg.action==='create'){ STATE.spools.push(Object.assign({id:Date.now(),updatedAt:Date.now()/1000,_color:msg.spool.color}, msg.spool)); reply.message='Added (mock)'; }
      if(msg.action==='delete'){ STATE.spools = STATE.spools.filter(function(s){ return msg.ids.indexOf(s.id)<0; }); reply.message='Deleted (mock)'; }
      if(msg.action==='update'){ STATE.spools.forEach(function(s){ if(s.id===msg.spool.id) Object.assign(s, msg.spool, {_color: msg.spool.color}); }); reply.message='Saved (mock)'; }
      if(msg.action==='settings'){ Object.assign(STATE.settings, msg.settings); }
      setTimeout(function(){ handlers.forEach(function(h){ try { h(reply); } catch(e){ window.onerror(String(e && e.stack || e), '', 0, 0); } }); document.body.setAttribute('data-replied', String(++window.__replies));
        if(window.__replies===1 && (window.__OPEN_DIALOG || /[?&]dialog/.test(location.search))){ try{ document.getElementById('btnAdd').click(); document.getElementById('fCatalog').selectedIndex=1; document.getElementById('fCatalog').dispatchEvent(new Event('change')); }catch(e){ window.onerror(String(e), '', 0, 0); } }
      }, 30);
    },
    onMessage: function(cb){ handlers.push(cb); }
  };
})();
</script>
""" % json.dumps(page_state)

html = fm.PAGE_HTML.replace("<script>\n(function(){\n'use strict';", mock + "<script>\n(function(){\n'use strict';", 1)
assert mock in html, "could not inject the mock bridge"
out = os.path.join(HERE, "preview.html")
with open(out, "w", encoding="utf-8") as fh:
    fh.write(html)
print("wrote", out)
# same page with the Add dialog opened automatically (for screenshots)
out2 = os.path.join(HERE, "preview_dialog.html")
with open(out2, "w", encoding="utf-8") as fh:
    fh.write(html.replace("<script>\nwindow.__errors", "<script>window.__OPEN_DIALOG=true;</script>\n<script>\nwindow.__errors", 1))
print("wrote", out2)
