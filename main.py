# main.py
import os
import time
import json
import uuid
import pathlib
import hashlib
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import RedirectResponse, PlainTextResponse, HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from openpyxl import load_workbook

# -----------------------------------------------------------------------------
# Config / env
# -----------------------------------------------------------------------------
load_dotenv()

HMRC_CLIENT_ID = os.getenv("HMRC_CLIENT_ID", "")
HMRC_CLIENT_SECRET = os.getenv("HMRC_CLIENT_SECRET", "")
HMRC_REDIRECT_URI = os.getenv("HMRC_REDIRECT_URI", "http://localhost:3000/oauth/hmrc/callback")
BASE_URL = os.getenv("BASE_URL", "https://test-api.service.hmrc.gov.uk")
SCOPE = "read:vat write:vat read:vat-returns"

SESSION_SECRET = os.getenv("SESSION_SECRET", "please_change_me_32chars")

# Persistence directory
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)

TOKEN_FILE = DATA_DIR / "tokens.json"
RECEIPTS_FILE = DATA_DIR / "receipts.json"
USERS_FILE = DATA_DIR / "users.json"  # email -> {email, role, salt, password_hash}

# -----------------------------------------------------------------------------
# Helpers: simple file persistence
# -----------------------------------------------------------------------------
def _read_json(p: pathlib.Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return default
    return default

def _write_json(p: pathlib.Path, obj):
    p.write_text(json.dumps(obj, indent=2))

def load_tokens():
    return _read_json(TOKEN_FILE, None)

def save_tokens(tokens: dict):
    _write_json(TOKEN_FILE, tokens)

def append_receipt(vrn: str, period_key: Optional[str], receipt: dict):
    data = _read_json(RECEIPTS_FILE, [])
    data.append({"vrn": vrn, "periodKey": period_key, **receipt})
    _write_json(RECEIPTS_FILE, data)

def load_users():
    return _read_json(USERS_FILE, {})

def save_users(users: dict):
    _write_json(USERS_FILE, users)

def password_hash(password: str, salt: str) -> str:
    return hashlib.sha256((salt + ":" + password).encode("utf-8")).hexdigest()

def create_user(email: str, password: str, role: str):
    email = email.strip().lower()
    users = load_users()
    if email in users:
        raise ValueError("User already exists")
    salt = uuid.uuid4().hex
    users[email] = {
        "email": email,
        "role": role,
        "salt": salt,
        "password_hash": password_hash(password, salt),
        "created_at": int(time.time()),
    }
    save_users(users)

def verify_user(email: str, password: str) -> Optional[dict]:
    users = load_users()
    u = users.get(email.strip().lower())
    if not u:
        return None
    if password_hash(password, u["salt"]) == u["password_hash"]:
        return u
    return None

# -----------------------------------------------------------------------------
# App + session
# -----------------------------------------------------------------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# serve static (xlsx viewer)
if not pathlib.Path("static").exists():
    pathlib.Path("static").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# -----------------------------------------------------------------------------
# React SPA (preview UI) at /app
# Build with Vite and copy dist/* into static/app
# -----------------------------------------------------------------------------
REACT_APP_DIR = pathlib.Path("static/app")
REACT_ASSETS_DIR = REACT_APP_DIR / "assets"

# Serve Vite assets at /app/assets (so index.html can load them)
if REACT_ASSETS_DIR.exists():
    app.mount("/app/assets", StaticFiles(directory=str(REACT_ASSETS_DIR)), name="app-assets")

# Serve the React SPA index for /app and any subpath (client-side routing)
@app.get("/app", response_class=HTMLResponse)
@app.get("/app/{_:path}", response_class=HTMLResponse)
def react_index():
    index_file = REACT_APP_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse(
            "<p>React app not built yet. Build your UI with Vite and copy dist/* to static/app.</p>",
            status_code=501,
        )
    return FileResponse(str(index_file))

STORE = {"tokens": load_tokens(), "state": None, "device_id": str(uuid.uuid4())}

# -----------------------------------------------------------------------------
# HMRC helpers
# -----------------------------------------------------------------------------
def hmrc_headers(request: Request) -> dict:
    ua = request.headers.get("user-agent", "my-vat-filer/1.0")
    client_ip = request.client.host if request.client else "203.0.113.10"
    return {
        "Accept": "application/vnd.hmrc.1.0+json",
        "User-Agent": "my-vat-filer/1.0",
        "Gov-Client-Device-Id": STORE["device_id"],
        "Gov-Client-Public-IP": client_ip,
        "Gov-Client-Local-IPs": "192.168.1.10",
        "Gov-Client-Timezone": "UTC+00:00",
        "Gov-Client-User-IDs": "os=user123",
        "Gov-Vendor-Version": "my-vat-filer=1.0.0",
        "Gov-Client-User-Agent": ua,
    }

def auth_url(state: str) -> str:
    from urllib.parse import urlencode
    q = {
        "response_type": "code",
        "client_id": HMRC_CLIENT_ID,
        "redirect_uri": HMRC_REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
    }
    return f"{BASE_URL}/oauth/authorize?{urlencode(q)}"

async def token_request(data: dict) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{BASE_URL}/oauth/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()

async def access_token() -> str:
    t = STORE["tokens"]
    if not t:
        raise HTTPException(401, "Not connected to HMRC yet.")
    obtained = t.get("obtained_at")
    expires_in = t.get("expires_in", 0)
    if obtained and (obtained + expires_in - 60) < time.time():
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{BASE_URL}/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": HMRC_CLIENT_ID,
                    "client_secret": HMRC_CLIENT_SECRET,
                    "refresh_token": t["refresh_token"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        t = r.json()
        t["obtained_at"] = time.time()
        STORE["tokens"] = t
        save_tokens(t)
    return t["access_token"]

def require_login(request: Request) -> Optional[RedirectResponse]:
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    return None

# -----------------------------------------------------------------------------
# Landing
# -----------------------------------------------------------------------------
from fastapi.responses import RedirectResponse

@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse(url="/app", status_code=307)


# -----------------------------------------------------------------------------
# Registration & Login
# -----------------------------------------------------------------------------
REGISTER_AGENT_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Register – Tax Agent</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
<div class="max-w-md mx-auto p-6">
  <h1 class="text-xl font-semibold mb-4">Register – Tax Agent</h1>
  <form method="post" class="bg-white border rounded p-5 space-y-3">
    <label class="block">Email <input name="email" class="w-full border rounded p-2"/></label>
    <label class="block">Password <input type="password" name="password" class="w-full border rounded p-2"/></label>
    <button class="rounded bg-green-600 text-white px-4 py-2">Register</button>
  </form>
  <p class="mt-3 text-sm"><a class="text-blue-700" href="/login">Already have an account? Log in</a></p>
</div>
</body></html>
"""

REGISTER_TAXPAYER_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Register – Taxpayer</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
<div class="max-w-md mx-auto p-6">
  <h1 class="text-xl font-semibold mb-4">Register – Taxpayer</h1>
  <form method="post" class="bg-white border rounded p-5 space-y-3">
    <label class="block">Email <input name="email" class="w-full border rounded p-2"/></label>
    <label class="block">Password <input type="password" name="password" class="w-full border rounded p-2"/></label>
    <button class="rounded bg-green-600 text-white px-4 py-2">Register</button>
  </form>
  <p class="mt-3 text-sm"><a class="text-blue-700" href="/login">Already have an account? Log in</a></p>
</div>
</body></html>
"""

LOGIN_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Login</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
<div class="max-w-md mx-auto p-6">
  <h1 class="text-xl font-semibold mb-4">Login</h1>
  <form method="post" class="bg-white border rounded p-5 space-y-3">
    <label class="block">Email <input name="email" class="w-full border rounded p-2"/></label>
    <label class="block">Password <input type="password" name="password" class="w-full border rounded p-2"/></label>
    <button class="rounded bg-blue-600 text-white px-4 py-2">Login</button>
  </form>
  <p class="mt-3 text-sm">
    <a class="text-blue-700" href="/register/agent">Register (Agent)</a> ·
    <a class="text-blue-700" href="/register/taxpayer">Register (Taxpayer)</a>
  </p>
</div>
</body></html>
"""

@app.get("/register/agent", response_class=HTMLResponse)
def register_agent_get():
    return HTMLResponse(REGISTER_AGENT_HTML)

@app.post("/register/agent")
async def register_agent_post(email: str = Form(...), password: str = Form(...)):
    try:
        create_user(email, password, "agent")
    except ValueError as e:
        return HTMLResponse(f"<p>Registration error: {e}</p><p><a href='/register/agent'>Back</a></p>")
    return RedirectResponse("/login", status_code=303)

@app.get("/register/taxpayer", response_class=HTMLResponse)
def register_taxpayer_get():
    return HTMLResponse(REGISTER_TAXPAYER_HTML)

@app.post("/register/taxpayer")
async def register_taxpayer_post(email: str = Form(...), password: str = Form(...)):
    try:
        create_user(email, password, "taxpayer")
    except ValueError as e:
        return HTMLResponse(f"<p>Registration error: {e}</p><p><a href='/register/taxpayer'>Back</a></p>")
    return RedirectResponse("/login", status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_get():
    return HTMLResponse(LOGIN_HTML)

@app.post("/login")
async def login_post(request: Request, email: str = Form(...), password: str = Form(...)):
    u = verify_user(email, password)
    if not u:
        return HTMLResponse("<p>Invalid credentials.</p><p><a href='/login'>Back</a></p>", status_code=401)
    request.session["user"] = u["email"]
    request.session["role"] = u["role"]
    return RedirectResponse("/dashboard", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

# -----------------------------------------------------------------------------
# Dashboard (JS embedded correctly)
# -----------------------------------------------------------------------------
DASHBOARD_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Dashboard – My VAT Filer</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
  <div class="max-w-4xl mx-auto px-4 py-8">
    <div class="flex items-center justify-between mb-6">
      <h1 class="text-2xl font-semibold">Dashboard</h1>
      <div class="space-x-3">
        <a class="text-sm text-blue-700" href="/ui">Classic UI</a>
        <a class="text-sm text-blue-700" href="/app">New React UI</a>
        <a class="text-sm text-blue-700" href="/logout">Logout</a>
      </div>
    </div>

    <div class="bg-white border border-slate-200 rounded-xl p-5 shadow">
      <div class="grid md:grid-cols-4 gap-3 items-end">
        <div class="md:col-span-2">
          <label class="block text-sm font-medium mb-1">VRN</label>
          <input id="vrn" class="w-full rounded border-slate-300 focus:border-blue-500 focus:ring-blue-500" placeholder="e.g. 458814905"/>
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Sandbox scenario</label>
          <select id="scenario" class="w-full rounded border-slate-300 focus:border-blue-500 focus:ring-blue-500">
            <option value="">(default)</option>
            <option>QUARTERLY_NONE_MET</option>
            <option>QUARTERLY_ONE_MET</option>
            <option>MULTIPLE_OBLIGATIONS</option>
          </select>
        </div>
        <div>
          <button id="load" class="w-full rounded bg-blue-600 text-white py-2 hover:bg-blue-700">Load</button>
        </div>
      </div>

      <div id="list" class="mt-5"></div>
    </div>
  </div>

<script>
const $ = (id)=>document.getElementById(id);

$('load').onclick = async ()=>{
  const vrn = $('vrn').value.trim();
  const sc  = $('scenario').value.trim();
  if(!vrn){ alert('Enter VRN'); return; }

  const url = sc
    ? `/api/obligations?vrn=${vrn}&scenario=${encodeURIComponent(sc)}`
    : `/api/obligations?vrn=${vrn}`;

  const r    = await fetch(url);
  const data = await r.json();
  const obs  = data.obligations || [];
  const list = $('list');
  list.innerHTML = '';

  if(!obs.length){ list.textContent = 'No open obligations.'; return; }

  obs.forEach(o=>{
    const a = document.createElement('a');
    a.className = 'block border rounded p-3 mb-2 bg-white hover:bg-slate-50';
    a.href = `/prepare?vrn=${vrn}&periodKey=${encodeURIComponent(o.periodKey)}`;
    a.textContent = `${o.periodKey} · ${o.start} → ${o.end} · due ${o.due}  —  Prepare from Excel`;
    list.appendChild(a);
  });
};
</script>
</body></html>
"""

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    redir = require_login(request)
    if redir:
        return redir
    return HTMLResponse(DASHBOARD_HTML)

# -----------------------------------------------------------------------------
# Prepare from Excel (dynamic page)
# -----------------------------------------------------------------------------
@app.get("/prepare", response_class=HTMLResponse)
def prepare(request: Request, vrn: str, periodKey: str):
    redir = require_login(request)
    if redir:
        return redir

    tpl = Template(r"""
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Prepare from Excel – ${vrn} / ${periodKey}</title>
<script src="https://cdn.tailwindcss.com"></script>
<!-- Use CDN to avoid missing local file issues on Render -->
<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
</head><body class="bg-slate-50">
  <div class="max-w-6xl mx-auto px-4 py-8">
    <a class="text-sm text-blue-700" href="/dashboard">← Back to dashboard</a>
    <h1 class="text-2xl font-semibold mt-2">Prepare return</h1>
    <p class="text-slate-600">VRN <b>${vrn}</b>, period <b>${periodKey}</b></p>

    <div class="grid md:grid-cols-3 gap-6 mt-4">
      <div class="md:col-span-1">
        <form id="f" class="bg-white border border-slate-200 rounded-xl p-5 space-y-3" enctype="multipart/form-data">
          <div>
            <label class="block text-sm font-medium mb-1">Excel file (.xlsx)</label>
            <input type="file" name="file" accept=".xlsx" required/>
          </div>

          <p class="text-sm text-slate-600">Pick cells for Boxes 1,2,4,6,7,8,9. Click a cell then press a “Pick” button.</p>

          <div class="space-y-3">
            <div class="flex items-center justify-between">
              <div>Box 1 — VAT due on sales</div><button class="px-2 py-1 border rounded" type="button" onclick="pick('box1')">Pick</button>
            </div><div id="sel_box1" class="text-sm text-slate-600"></div>

            <div class="flex items-center justify-between">
              <div>Box 2 — VAT due on acquisitions (EU)</div><button class="px-2 py-1 border rounded" type="button" onclick="pick('box2')">Pick</button>
            </div><div id="sel_box2" class="text-sm text-slate-600"></div>

            <div class="flex items-center justify-between">
              <div>Box 4 — VAT reclaimed on purchases</div><button class="px-2 py-1 border rounded" type="button" onclick="pick('box4')">Pick</button>
            </div><div id="sel_box4" class="text-sm text-slate-600"></div>

            <div class="flex items-center justify-between">
              <div>Box 6 — Total value of sales (ex VAT)</div><button class="px-2 py-1 border rounded" type="button" onclick="pick('box6')">Pick</button>
            </div><div id="sel_box6" class="text-sm text-slate-600"></div>

            <div class="flex items-center justify-between">
              <div>Box 7 — Total value of purchases (ex VAT)</div><button class="px-2 py-1 border rounded" type="button" onclick="pick('box7')">Pick</button>
            </div><div id="sel_box7" class="text-sm text-slate-600"></div>

            <div class="flex items-center justify-between">
              <div>Box 8 — Supplies to EU (ex VAT)</div><button class="px-2 py-1 border rounded" type="button" onclick="pick('box8')">Pick</button>
            </div><div id="sel_box8" class="text-sm text-slate-600"></div>

            <div class="flex items-center justify-between">
              <div>Box 9 — Acquisitions from EU (ex VAT)</div><button class="px-2 py-1 border rounded" type="button" onclick="pick('box9')">Pick</button>
            </div><div id="sel_box9" class="text-sm text-slate-600"></div>
          </div>

          <button class="rounded bg-blue-600 text-white py-2 px-4">Preview values</button>
        </form>

        <pre id="out" class="mt-4 text-sm bg-white border rounded p-3"></pre>
        <button id="use" class="hidden mt-3 rounded bg-green-600 text-white py-2 px-4">Use these values → Go to /ui</button>
      </div>

      <div class="md:col-span-2 bg-white border rounded-xl p-3">
        <div id="sheet" class="text-sm text-slate-700">Upload a file to view.</div>
      </div>
    </div>
  </div>

<script>
let _activeCell = null;

function renderWorkbookWB(wb){
  const wsname = wb.SheetNames[0];
  const ws = wb.Sheets[wsname];
  const html = XLSX.utils.sheet_to_html(ws, { editable:true });
  const host = document.getElementById('sheet');
  host.innerHTML = html;

  // track clicks
  host.querySelectorAll('td').forEach(td => {
    td.addEventListener('click', ()=>{
      host.querySelectorAll('td').forEach(x => x.style.outline = '');
      td.style.outline = '2px solid #2563eb';
      let v = td.getAttribute('data-address') || td.getAttribute('data-cell') || td.getAttribute('aria-label') || td.id || td.title || '';
      _activeCell = v;
    });
  });
}

function pick(box){
  if(!_activeCell){ alert('Click a cell first.'); return; }
  document.getElementById('sel_'+box).textContent = `Selected ${_activeCell}`;
  const inp = document.createElement('input');
  inp.type = 'hidden';
  inp.name = box;
  inp.value = _activeCell;
  document.getElementById('f').appendChild(inp);
}

document.getElementById('f').onchange = async (e)=>{
  const fileInput = e.target;
  if(fileInput.name !== 'file') return;
  const file = fileInput.files[0];
  if(!file) return;
  const buf = await file.arrayBuffer();
  const wb = XLSX.read(buf, { type:'array' });
  renderWorkbookWB(wb);
};

document.getElementById('f').onsubmit = async (e)=>{
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch('/api/excel/preview', { method:'POST', body: fd });
  const data = await r.json();
  document.getElementById('out').textContent = JSON.stringify(data, null, 2);
  if(r.ok) {
    const use = document.getElementById('use');
    use.classList.remove('hidden');
    data.periodKey = "${periodKey}";
    localStorage.setItem('prefill', JSON.stringify(data));
  }
};

document.getElementById('use').onclick = ()=>{
  window.location = '/ui?vrn=${vrn}';
};
</script>
</body></html>
""")
    return HTMLResponse(tpl.substitute(vrn=vrn, periodKey=periodKey))

# -----------------------------------------------------------------------------
# Classic UI page
# -----------------------------------------------------------------------------
@app.get("/ui", response_class=HTMLResponse)
def ui():
    return HTMLResponse("""
<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>My VAT Filer — UI</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config = { theme: { extend: { colors: { brand:'#2563eb' }}}}</script>
</head><body class="h-full bg-slate-50 text-slate-900">
  <div class="max-w-6xl mx-auto px-4 py-8">
    <header class="mb-8">
      <h1 class="text-3xl font-semibold tracking-tight">My VAT Filer <span class="text-slate-400">(Sandbox)</span></h1>
      <p class="text-slate-600 mt-1">Connect, load obligations, complete Boxes 1–9, submit, and view the receipt.</p>
      <p class="text-sm mt-1"><a class="text-blue-700" href="/dashboard">Go to dashboard</a> · <a class="text-blue-700" href="/app">New React UI</a></p>
    </header>

    <div id="toast" class="hidden fixed top-4 right-4 z-50 min-w-[280px] rounded-md border border-slate-200 bg-white shadow-lg p-3"></div>

    <div class="grid md:grid-cols-3 gap-6">
      <section class="md:col-span-1 rounded-xl bg-white border border-slate-200 shadow-sm p-5">
        <h2 class="font-medium text-slate-800 mb-3">1) Connect to HMRC</h2>
        <p class="text-sm text-slate-600 mb-4">Use your sandbox test organisation.</p>
        <button id="btnConnect" class="inline-flex items-center justify-center rounded-lg bg-brand px-4 py-2 text-white hover:bg-blue-600 active:bg-blue-700 transition">Connect</button>
        <div id="status" class="mt-3 text-sm text-slate-600">Status: <span class="font-medium">Unknown</span></div>
      </section>

      <section class="md:col-span-2 rounded-xl bg-white border border-slate-200 shadow-sm p-5">
        <h2 class="font-medium text-slate-800 mb-4">2) Pick an obligation</h2>

        <div class="grid md:grid-cols-4 gap-3 items-end">
          <div class="md:col-span-2">
            <label class="block text-sm font-medium text-slate-700 mb-1">VRN</label>
            <input id="vrn" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand" placeholder="e.g. 458814905" />
          </div>

          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Scenario (sandbox)</label>
            <select id="scenario" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand">
              <option value="">(default)</option>
              <option>QUARTERLY_NONE_MET</option>
              <option>QUARTERLY_ONE_MET</option>
              <option>MULTIPLE_OBLIGATIONS</option>
            </select>
          </div>

          <div class="flex gap-2">
            <button id="btnLoad" class="flex-1 rounded-lg border border-slate-300 hover:bg-slate-50 px-4 py-2 transition">Load obligations</button>
          </div>
        </div>

        <div class="grid md:grid-cols-3 gap-3 mt-4">
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Obligation (periodKey)</label>
            <select id="periodSelect" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand">
              <option value="">— none loaded —</option>
            </select>
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Auto-filled periodKey</label>
            <input id="periodKey" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand bg-slate-50" readonly />
          </div>
          <div class="text-sm text-slate-600 flex items-end" id="obligMeta"></div>
        </div>
      </section>
    </div>

    <section class="rounded-xl bg-white border border-slate-200 shadow-sm p-5 mt-6">
      <h2 class="font-medium text-slate-800 mb-2">3) Fill & Submit VAT return</h2>
      <p class="text-xs text-slate-500 mb-4">Boxes 2, 8 and 9 relate to EU movements. In most cases set them to 0.</p>

      <div class="grid grid-cols-1 gap-y-5">
        <div><label class="block text-sm font-medium text-slate-700 mb-1">Box 1</label><input id="vatDueSales" value="100.00" class="w-full rounded-lg border-slate-300"/></div>
        <div><label class="block text-sm font-medium text-slate-700 mb-1">Box 2</label><input id="vatDueAcquisitions" value="0.00" class="w-full rounded-lg border-slate-300"/></div>
        <div><label class="block text-sm font-medium text-slate-700 mb-1">Box 3</label><input id="totalVatDue" value="100.00" class="w-full rounded-lg border-slate-300"/></div>
        <div><label class="block text-sm font-medium text-slate-700 mb-1">Box 4</label><input id="vatReclaimedCurrPeriod" value="0.00" class="w-full rounded-lg border-slate-300"/></div>
        <div><label class="block text-sm font-medium text-slate-700 mb-1">Box 5</label><input id="netVatDue" value="100.00" class="w-full rounded-lg border-slate-300"/></div>
        <div><label class="block text-sm font-medium text-slate-700 mb-1">Box 6</label><input id="totalValueSalesExVAT" value="500" class="w-full rounded-lg border-slate-300"/></div>
        <div><label class="block text-sm font-medium text-slate-700 mb-1">Box 7</label><input id="totalValuePurchasesExVAT" value="0" class="w-full rounded-lg border-slate-300"/></div>
        <div><label class="block text-sm font-medium text-slate-700 mb-1">Box 8</label><input id="totalValueGoodsSuppliedExVAT" value="0" class="w-full rounded-lg border-slate-300"/></div>
        <div><label class="block text-sm font-medium text-slate-700 mb-1">Box 9</label><input id="totalAcquisitionsExVAT" value="0" class="w-full rounded-lg border-slate-300"/></div>
        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1">Declaration</label>
          <select id="finalised" class="w-full rounded-lg border-slate-300"><option>true</option><option>false</option></select>
        </div>
      </div>

      <div class="mt-5">
        <button id="btnSubmit" class="rounded-lg bg-brand text-white px-5 py-2 hover:bg-blue-600">Submit return</button>
      </div>
    </section>

    <section class="rounded-xl bg-white border border-slate-200 shadow-sm p-5 mt-6">
      <div class="flex items-center justify-between">
        <h2 class="font-medium text-slate-800">Result</h2>
        <div class="flex gap-2">
          <button id="btnCopy" class="rounded-lg border border-slate-300 px-3 py-1.5 hover:bg-slate-50">Copy</button>
          <button id="btnClear" class="rounded-lg border border-slate-300 px-3 py-1.5 hover:bg-slate-50">Clear</button>
        </div>
      </div>
      <pre id="out" class="mt-4 text-sm whitespace-pre-wrap bg-slate-50 border border-slate-200 rounded-lg p-3"></pre>
    </section>
  </div>

<script>
const $ = (id) => document.getElementById(id);
const out = $('out');
const toast = $('toast');

function notify(msg, type='info'){
  toast.className = "fixed top-4 right-4 z-50 min-w-[280px] rounded-md border p-3 shadow-lg " +
    (type==='error' ? "bg-red-50 border-red-200 text-red-700"
     : type==='success' ? "bg-green-50 border-green-200 text-green-700"
     : "bg-white border-slate-200 text-slate-800");
  toast.textContent = msg;
  toast.classList.remove('hidden');
  setTimeout(()=> toast.classList.add('hidden'), 3000);
}

function pretty(x){ try{ return JSON.stringify(typeof x==='string'? JSON.parse(x) : x, null, 2); }catch{ return String(x); } }

function recalc(){ 
  const vds = parseFloat($('vatDueSales').value||0);
  const vda = parseFloat($('vatDueAcquisitions').value||0);
  const vrc = parseFloat($('vatReclaimedCurrPeriod').value||0);
  $('totalVatDue').value = (vds+vda).toFixed(2);
  $('netVatDue').value   = (vds+vda-vrc).toFixed(2);
}
['vatDueSales','vatDueAcquisitions','vatReclaimedCurrPeriod'].forEach(id=>{
  $(id).addEventListener('input', recalc);
});

// prefill from /prepare
try {
  const pf = JSON.parse(localStorage.getItem('prefill') || '{}');
  if (pf && Object.keys(pf).length) {
    if (pf.periodKey) $('periodKey').value = pf.periodKey;
    if ('vatDueSales' in pf) $('vatDueSales').value = Number(pf.vatDueSales).toFixed(2);
    if ('vatDueAcquisitions' in pf) $('vatDueAcquisitions').value = Number(pf.vatDueAcquisitions).toFixed(2);
    if ('vatReclaimedCurrPeriod' in pf) $('vatReclaimedCurrPeriod').value = Number(pf.vatReclaimedCurrPeriod).toFixed(2);
    if ('totalValueSalesExVAT' in pf) $('totalValueSalesExVAT').value = pf.totalValueSalesExVAT;
    if ('totalValuePurchasesExVAT' in pf) $('totalValuePurchasesExVAT').value = pf.totalValuePurchasesExVAT;
    if ('totalValueGoodsSuppliedExVAT' in pf) $('totalValueGoodsSuppliedExVAT').value = pf.totalValueGoodsSuppliedExVAT;
    if ('totalAcquisitionsExVAT' in pf) $('totalAcquisitionsExVAT').value = pf.totalAcquisitionsExVAT;
    recalc();
    notify('Values loaded from Excel preview','success');
    localStorage.removeItem('prefill');
  }
} catch(e){}

$('btnConnect').onclick = () => { window.location = '/connect'; };

$('btnLoad').onclick = async ()=>{
  try{
    const vrn = $('vrn').value.trim();
    const scenario = $('scenario').value.trim();
    if(!vrn){ notify('Enter a VRN first','error'); return; }
    const url = scenario ? `/api/obligations?vrn=${vrn}&scenario=${encodeURIComponent(scenario)}` : `/api/obligations?vrn=${vrn}`;
    const data = await (await fetch(url)).json();

    const select = $('periodSelect');
    select.innerHTML = '';
    const obs = data.obligations || [];
    if(!obs.length){
      select.innerHTML = '<option value="">— no open obligations —</option>';
      $('periodKey').value = '';
      $('obligMeta').textContent = '';
      notify('No open obligations returned','info');
      return;
    }

    obs.forEach(o=>{
      const opt = document.createElement('option');
      opt.value = o.periodKey;
      opt.textContent = `${o.periodKey} · ${o.start} → ${o.end} · due ${o.due}`;
      opt.dataset.meta = JSON.stringify(o);
      select.appendChild(opt);
    });

    select.onchange = ()=>{
      const meta = JSON.parse(select.selectedOptions[0].dataset.meta);
      $('periodKey').value = meta.periodKey;
      $('obligMeta').textContent = `Selected: ${meta.start} → ${meta.end}, due ${meta.due}`;
    };
    select.dispatchEvent(new Event('change'));
    notify('Obligations loaded','success');
  }catch(e){
    out.textContent = pretty(e.message);
    notify('Failed to load obligations','error');
  }
};

$('btnSubmit').onclick = async ()=>{
  try{
    const vrn = $('vrn').value.trim();
    const periodKey = $('periodKey').value.trim();
    if(!vrn || !periodKey){ notify('VRN and periodKey required','error'); return; }

    const pre = await fetch(`/api/returns/view?vrn=${vrn}&periodKey=${encodeURIComponent(periodKey)}`);
    if (pre.ok) {
      const already = await pre.json();
      out.textContent = 'Already submitted. HMRC shows:\\n' + pretty(already);
      notify('Already submitted for this period','info');
      return;
    }

    recalc();
    const body = {
      periodKey,
      vatDueSales: $('vatDueSales').value.trim(),
      vatDueAcquisitions: $('vatDueAcquisitions').value.trim(),
      totalVatDue: $('totalVatDue').value.trim(),
      vatReclaimedCurrPeriod: $('vatReclaimedCurrPeriod').value.trim(),
      netVatDue: $('netVatDue').value.trim(),
      totalValueSalesExVAT: parseInt($('totalValueSalesExVAT').value,10) || 0,
      totalValuePurchasesExVAT: parseInt($('totalValuePurchasesExVAT').value,10) || 0,
      totalValueGoodsSuppliedExVAT: parseInt($('totalValueGoodsSuppliedExVAT').value,10) || 0,
      totalAcquisitionsExVAT: parseInt($('totalAcquisitionsExVAT').value,10) || 0,
      finalised: $('finalised').value === 'true'
    };

    const r = await fetch(`/api/returns?vrn=${vrn}`, {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    const txt = await r.text();
    out.textContent = pretty(txt);
    if(r.ok){ notify('Submitted successfully','success'); }
    else{ notify('Submission returned an error','error'); }
  }catch(e){
    out.textContent = pretty(e.message);
    notify('Submit failed','error');
  }
};

$('btnCopy').onclick = async ()=>{ await navigator.clipboard.writeText(out.textContent || ''); notify('Copied to clipboard','success'); };
$('btnClear').onclick = ()=>{ out.textContent = ''; };
</script>

</body></html>
""")

# -----------------------------------------------------------------------------
# OAuth routes
# -----------------------------------------------------------------------------
@app.get("/connect")
def connect():
    state = str(uuid.uuid4())
    STORE["state"] = state
    return RedirectResponse(auth_url(state))

@app.get("/oauth/hmrc/callback")
async def oauth_callback(code: str, state: str):
    if state != STORE.get("state"):
        raise HTTPException(400, "state mismatch")
    tokens = await token_request({
        "grant_type": "authorization_code",
        "client_id": HMRC_CLIENT_ID,
        "client_secret": HMRC_CLIENT_SECRET,
        "redirect_uri": HMRC_REDIRECT_URI,
        "code": code,
    })
    tokens["obtained_at"] = time.time()
    STORE["tokens"] = tokens
    save_tokens(tokens)
    return PlainTextResponse(f"Connected. Access token received. Expires in {tokens.get('expires_in')} seconds.")

# -----------------------------------------------------------------------------
# JSON APIs (obligations, returns, liabilities, payments, receipts)
# -----------------------------------------------------------------------------
@app.get("/api/obligations")
async def obligations(request: Request, vrn: str, status: str = "O", scenario: Optional[str] = None):
    tok = await access_token()
    url = f"{BASE_URL}/organisations/vat/{vrn}/obligations"
    headers = {**hmrc_headers(request), "Authorization": f"Bearer {tok}"}
    if scenario:
        headers["Gov-Test-Scenario"] = scenario
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params={"status": status}, headers=headers)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json() if r.text else {}

@app.post("/api/returns")
async def submit_return(request: Request, vrn: str, payload: dict):
    tok = await access_token()
    url = f"{BASE_URL}/organisations/vat/{vrn}/returns"
    headers = {**hmrc_headers(request), "Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    resp = r.json()
    append_receipt(vrn, payload.get("periodKey") if isinstance(payload, dict) else None, resp)
    return resp

@app.get("/api/returns/view")
async def view_return(request: Request, vrn: str, periodKey: str):
    tok = await access_token()
    url = f"{BASE_URL}/organisations/vat/{vrn}/returns/{periodKey}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers={**hmrc_headers(request), "Authorization": f"Bearer {tok}"})
    if r.status_code >= 400:
        return JSONResponse({"error": r.text}, status_code=r.status_code)
    return r.json()

@app.get("/api/liabilities")
async def liabilities(request: Request, vrn: str, from_: str, to: str, scenario: Optional[str] = None):
    tok = await access_token()
    url = f"{BASE_URL}/organisations/vat/{vrn}/liabilities"
    headers = {**hmrc_headers(request), "Authorization": f"Bearer {tok}"}
    if scenario:
        headers["Gov-Test-Scenario"] = scenario
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params={"from": from_, "to": to}, headers=headers)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.get("/api/payments")
async def payments(request: Request, vrn: str, from_: str, to: str, scenario: Optional[str] = None):
    tok = await access_token()
    url = f"{BASE_URL}/organisations/vat/{vrn}/payments"
    headers = {**hmrc_headers(request), "Authorization": f"Bearer {tok}"}
    if scenario:
        headers["Gov-Test-Scenario"] = scenario
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params={"from": from_, "to": to}, headers=headers)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.get("/api/receipts")
def receipts():
    return _read_json(RECEIPTS_FILE, [])

# -----------------------------------------------------------------------------
# Excel preview API
# -----------------------------------------------------------------------------
@app.post("/api/excel/preview")
async def excel_preview(
    file: UploadFile = File(...),
    box1: str = Form(...),
    box2: str = Form(...),
    box4: str = Form(...),
    box6: str = Form(...),
    box7: str = Form(...),
    box8: str = Form(...),
    box9: str = Form(...),
):
    content = await file.read()
    tmp = pathlib.Path("_upload.xlsx")
    tmp.write_bytes(content)
    try:
        wb = load_workbook(tmp, data_only=True)
        ws = wb.active

        def f(c):
            v = ws[c].value if c in ws else None
            try:
                return float(v)
            except Exception:
                try:
                    return float(str(v).replace(',',''))
                except Exception:
                    return 0.0

        vatDueSales = f(box1)
        vatDueAcquisitions = f(box2)
        vatReclaimedCurrPeriod = f(box4)
        totalValueSalesExVAT = f(box6)
        totalValuePurchasesExVAT = f(box7)
        totalValueGoodsSuppliedExVAT = f(box8)
        totalAcquisitionsExVAT = f(box9)

        totalVatDue = vatDueSales + vatDueAcquisitions
        netVatDue = totalVatDue - vatReclaimedCurrPeriod

        return {
            "vatDueSales": round(vatDueSales, 2),
            "vatDueAcquisitions": round(vatDueAcquisitions, 2),
            "totalVatDue": round(totalVatDue, 2),
            "vatReclaimedCurrPeriod": round(vatReclaimedCurrPeriod, 2),
            "netVatDue": round(netVatDue, 2),
            "totalValueSalesExVAT": int(totalValueSalesExVAT),
            "totalValuePurchasesExVAT": int(totalValuePurchasesExVAT),
            "totalValueGoodsSuppliedExVAT": int(totalValueGoodsSuppliedExVAT),
            "totalAcquisitionsExVAT": int(totalAcquisitionsExVAT),
        }
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

# -----------------------------------------------------------------------------
# Uvicorn entry (for local run)
# -----------------------------------------------------------------------------
# Run: uvicorn main:app --reload --port 3000
