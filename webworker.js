// Runs the planner's Python code (ui.Api) in the browser with Pyodide, for when ui.html is hosted as
// static files (e.g. GitHub Pages) instead of being served by ui.py. The page sends the same /api/...
// requests it would send to ui.py; the data files live in /data here and are mirrored by the page
// into localStorage, which it hands back on start.
const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/";
const PY_FILES = ["ui.py", "scheduler.py", "i18n.py", "xlsx_export.py", "errorlog.py", "example_config.json"];
const WRITES = ["/api/save", "/api/schedule", "/api/restore", "/api/delete-all"];  // requests that change /data

let py = null;
let ready = null;

async function start({ files, pendingSave }) {
  const { loadPyodide } = await import(PYODIDE + "pyodide.mjs");
  py = await loadPyodide({ indexURL: PYODIDE });
  py.FS.mkdirTree("/app");
  py.FS.mkdirTree("/data");
  await Promise.all(PY_FILES.map(async (name) => {
    const r = await fetch(name, { cache: "no-cache" });
    if (!r.ok) throw new Error(`${name}: ${r.status} ${r.statusText}`);
    py.FS.writeFile(`/app/${name}`, new Uint8Array(await r.arrayBuffer()));
  }));
  for (const [name, text] of Object.entries(files || {})) py.FS.writeFile(`/data/${name}`, text);
  py.runPython(`
import json, os, sys
os.environ["PLANNER_LOG_DIR"] = "/tmp/logs"
sys.path.insert(0, "/app")
import ui
API = ui.Api("/data/config.json")

def call(method, url, body, lang):
    status, out, ctype, headers = API.handle(method, url, json.loads(body) if body else None, lang)
    if not isinstance(out, bytes):
        out = json.dumps(out).encode()
    return status, out, ctype, headers

def data_files():
    return {n: open("/data/" + n, encoding="utf-8").read() for n in sorted(os.listdir("/data")) if n.endswith(".json")}
`);
  // edits made just before the page was last closed (the page cannot wait for Python while closing)
  if (pendingSave) call("POST", "/api/save", pendingSave, "en");
  return dataFiles();
}

function call(method, url, body, lang) {
  const res = py.globals.get("call")(method, url, body, lang);
  try {
    const [status, out, ctype, headers] = res.toJs({ dict_converter: Object.fromEntries });
    return { status, body: out, ctype, headers };
  } finally {
    res.destroy();
  }
}
function dataFiles() {
  const res = py.globals.get("data_files")();
  try { return res.toJs({ dict_converter: Object.fromEntries }); } finally { res.destroy(); }
}

self.onmessage = async ({ data: msg }) => {
  if (msg.type === "start") {
    ready = start(msg);
    ready.then((files) => postMessage({ type: "ready", files }),
               (e) => postMessage({ type: "failed", error: String(e && e.message || e) }));
    return;
  }
  try {
    await ready;
    const r = call(msg.method, msg.url, msg.body, msg.lang);
    const files = WRITES.includes(new URL(msg.url, "http://x").pathname) && msg.method === "POST" ? dataFiles() : null;
    postMessage({ id: msg.id, ...r, files }, [r.body.buffer]);
  } catch (e) {
    postMessage({ id: msg.id, status: 500, ctype: "application/json", headers: {},
                  body: new TextEncoder().encode(JSON.stringify({ error: String(e && e.message || e) })), files: null });
  }
};
