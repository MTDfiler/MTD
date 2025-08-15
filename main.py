import os
import time
import json
import uuid
import pathlib
from typing import Optional
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import RedirectResponse, PlainTextResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles
from openpyxl import load_workbook

# -------------------------------------------------------------------
# Config / environment
# -------------------------------------------------------------------
load_dotenv()

HMRC_CLIENT_ID = os.getenv("HMRC_CLIENT_ID", "")
HMRC_CLIENT_SECRET = os.getenv("HMRC_CLIENT_SECRET", "")
HMRC_REDIRECT_URI = os.getenv("HMRC_REDIRECT_URI", "http://localhost:3000/oauth/hmrc/callback")
BASE_URL = os.getenv("BASE_URL", "https://test-api.service.hmrc.gov.uk")
SCOPE = "read:vat write:vat read:vat-returns"

SESSION_SECRET = os.getenv("SESSION_SECRET", "please_change_me_32chars")
APP_USER = os.getenv("APP_USER", "admin")
APP_PASS = os.getenv("APP_PASS", "admin123")

# add these lines near the top, before TOKEN_FILE is defined
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "."))
TOKEN_FILE = DATA_DIR / "tokens.json"
RECEIPTS_FILE = DATA_DIR / "receipts.json"

# -------------------------------------------------------------------
# Persistence
# -------------------------------------------------------------------
TOKEN_FILE = pathlib.Path("tokens.json")
RECEIPTS_FILE = pathlib.Path("receipts.json")

def load_tokens():
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text())
        except Exception:
            return None
    return None

def save_tokens(tokens: dict):
    TOKEN_FILE.write_text(json.dumps(tokens))

def append_receipt(vrn: str, period_key: Optional[str], receipt: dict):
    data = []
    if RECEIPTS_FILE.exists():
        try:
            data = json.loads(RECEIPTS_FILE.read_text())
        except Exception:
            data = []
    data.append({"vrn": vrn, "periodKey": period_key, **receipt})
    RECEIPTS_FILE.write_text(json.dumps(data, indent=2))

# -------------------------------------------------------------------
# App + static + session
# -------------------------------------------------------------------
app = FastAPI()

# Always create ./static so Starlette doesn't error if it is missing
STATIC_DIR = pathlib.Path("static")
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

STORE = {"tokens": load_tokens(), "state": None, "device_id": str(uuid.uuid4())}

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
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
    # refresh a minute early
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

# -------------------------------------------------------------------
# OAuth + JSON API
# -------------------------------------------------------------------
@app.get("/")
def home():
    return RedirectResponse("/portal")

@app.get("/portal", response_class=HTMLResponse)
def portal():
    html = """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>My VAT Filer – Portal</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
  <div class="max-w-5xl mx-auto px-4 py-10">
    <header class="mb-8">
      <h1 class="text-3xl font-semibold tracking-tight">My VAT Filer <span class="text-slate-400">(Sandbox)</span></h1>
      <p class="text-slate-600 mt-1">Choose how you want to work.</p>
    </header>

    <div class="grid md:grid-cols-2 gap-6">
      <a href="/business/dashboard" class="block rounded-xl bg-white border border-slate-200 shadow-sm p-6 hover:shadow transition">
        <h2 class="text-xl font-medium">Business portal</h2>
        <p class="text-slate-600 mt-2">Single business filing VAT returns.</p>
      </a>
      <a href="/agent/dashboard" class="block rounded-xl bg-white border border-slate-200 shadow-sm p-6 hover:shadow transition">
        <h2 class="text-xl font-medium">Agent portal</h2>
        <p class="text-slate-600 mt-2">Manage multiple clients and file their VAT returns.</p>
      </a>
    </div>

    <div class="mt-10">
      <a href="/connect" class="inline-flex items-center rounded-lg bg-blue-600 text-white px-4 py-2 hover:bg-blue-700">Connect to HMRC</a>
      <span class="ml-3 text-sm text-slate-600">Use your sandbox test organisation.</span>
    </div>
  </div>
</body></html>
"""
    return HTMLResponse(html)

@app.get("/connect")
def connect():
    state = str(uuid.uuid4())
    STORE["state"] = state
    return RedirectResponse(auth_url(state))

@app.get("/oauth/hmrc/callback")
async def oauth_callback(code: str, state: str):
    if state != STORE.get("state"):
        raise HTTPException(400, "state mismatch")

    tokens = await token_request(
        {
            "grant_type": "authorization_code",
            "client_id": HMRC_CLIENT_ID,
            "client_secret": HMRC_CLIENT_SECRET,
            "redirect_uri": HMRC_REDIRECT_URI,
            "code": code,
        }
    )
    tokens["obtained_at"] = time.time()
    STORE["tokens"] = tokens
    save_tokens(tokens)
    return PlainTextResponse(f"Connected. Access token received. Expires in {tokens.get('expires_in')} seconds.")

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
    headers = {
        **hmrc_headers(request),
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }
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
        raise HTTPException(r.status_code, r.text)
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
    if RECEIPTS_FILE.exists():
        try:
            return json.loads(RECEIPTS_FILE.read_text())
        except Exception:
            return []
    return []

# --- Fallback Excel preview (typed cell refs) --------------------------------
@app.post("/api/excel/preview")
async def excel_preview_api(
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
            try:
                v = ws[c].value
                return float(v) if v is not None else 0.0
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
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

# -------------------------------------------------------------------
# Auth (simple)
# -------------------------------------------------------------------
LOGIN_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Login – My VAT Filer</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
  <div class="min-h-screen flex items-center justify-center px-4">
    <form method="post" class="bg-white shadow rounded-xl p-6 w-full max-w-sm border border-slate-200">
      <h1 class="text-xl font-semibold mb-4">Sign in</h1>
      <div class="mb-3">
        <label class="block text-sm font-medium mb-1">Username</label>
        <input name="username" class="w-full rounded border-slate-300 focus:border-blue-500 focus:ring-blue-500"/>
      </div>
      <div class="mb-4">
        <label class="block text-sm font-medium mb-1">Password</label>
        <input name="password" type="password" class="w-full rounded border-slate-300 focus:border-blue-500 focus:ring-blue-500"/>
      </div>
      <button class="w-full rounded bg-blue-600 text-white py-2 hover:bg-blue-700">Login</button>
      <p class="text-xs text-slate-500 mt-3">Default: admin / admin123</p>
    </form>
  </div>
</body></html>
"""

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/portal", status_code=303)
    return HTMLResponse(LOGIN_HTML)

@app.post("/login")
async def login_post(request: Request):
    form = await request.form()
    if form.get("username") == APP_USER and form.get("password") == APP_PASS:
        request.session["user"] = APP_USER
        return RedirectResponse("/portal", status_code=303)
    return HTMLResponse(LOGIN_HTML.replace("Sign in", "Sign in <span class='text-red-600'>(Invalid)</span>"))

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

# -------------------------------------------------------------------
# Business dashboard
# -------------------------------------------------------------------
@app.get("/business/dashboard", response_class=HTMLResponse)
def business_dashboard(request: Request):
    redir = require_login(request)
    if redir: 
        return redir
    html = """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Business dashboard – My VAT Filer</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
  <div class="max-w-5xl mx-auto px-4 py-8">
    <div class="flex items-center justify-between mb-6">
      <h1 class="text-2xl font-semibold">Business dashboard</h1>
      <a class="text-sm text-blue-700" href="/logout">Logout</a>
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

    <p class="text-sm text-slate-600 mt-6"><a class="text-blue-700" href="/portal">← Back to portal</a></p>
  </div>

<script>
const $ = (id)=>document.getElementById(id);
$('load').onclick = async ()=>{
  const vrn = $('vrn').value.trim();
  const sc = $('scenario').value.trim();
  if(!vrn){ alert('Enter VRN'); return; }
  const url = sc ? `/api/obligations?vrn=${vrn}&scenario=${encodeURIComponent(sc)}` : `/api/obligations?vrn=${vrn}`;
  const r = await fetch(url);
  const data = await r.json();
  const obs = data.obligations || [];
  const list = $('list');
  list.innerHTML = '';
  if(!obs.length){ list.textContent = 'No open obligations.'; return; }
  obs.forEach(o=>{
    const a = document.createElement('a');
    a.className='block border rounded p-3 mb-2 hover:bg-slate-50';
    a.href=`/prepare?role=business&vrn=${vrn}&periodKey=${encodeURIComponent(o.periodKey)}`;
    a.textContent = `${o.periodKey} · ${o.start} → ${o.end} · due ${o.due} — Prepare from Excel`;
    list.appendChild(a);
  });
};
</script>
</body></html>
"""
    return HTMLResponse(html)

# -------------------------------------------------------------------
# Agent dashboard
# -------------------------------------------------------------------
@app.get("/agent/dashboard", response_class=HTMLResponse)
def agent_dashboard(request: Request):
    redir = require_login(request)
    if redir: 
        return redir
    html = """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Agent dashboard – My VAT Filer</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
  <div class="max-w-6xl mx-auto px-4 py-8">
    <div class="flex items-center justify-between mb-6">
      <h1 class="text-2xl font-semibold">Agent dashboard</h1>
      <a class="text-sm text-blue-700" href="/logout">Logout</a>
    </div>

    <div class="bg-white border border-slate-200 rounded-xl p-5 shadow">
      <div class="grid md:grid-cols-5 gap-3 items-end">
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
          <button id="add" class="w-full rounded bg-blue-600 text-white py-2 hover:bg-blue-700">Add client</button>
        </div>
      </div>

      <div id="clients" class="mt-5"></div>
    </div>

    <p class="text-sm text-slate-600 mt-6"><a class="text-blue-700" href="/portal">← Back to portal</a></p>
  </div>

<script>
const clients = [];
const $ = (id)=>document.getElementById(id);

$('add').onclick = async ()=>{
  const vrn = $('vrn').value.trim();
  const sc = $('scenario').value.trim();
  if(!vrn){ alert('Enter VRN'); return; }
  const url = sc ? `/api/obligations?vrn=${vrn}&scenario=${encodeURIComponent(sc)}` : `/api/obligations?vrn=${vrn}`;
  const r = await fetch(url);
  const data = await r.json();
  const obs = data.obligations || [];
  const host = document.createElement('div');
  host.className = 'border rounded p-3 mb-4';
  host.innerHTML = `<div class="font-medium mb-2">Client VRN ${vrn}</div>`;
  if(!obs.length){
    host.innerHTML += `<div class="text-slate-600">No open obligations.</div>`;
  } else {
    obs.forEach(o=>{
      const a = document.createElement('a');
      a.className='block border rounded p-2 mb-2 hover:bg-slate-50';
      a.href=`/prepare?role=agent&vrn=${vrn}&periodKey=${encodeURIComponent(o.periodKey)}`;
      a.textContent = `${o.periodKey} · ${o.start} → ${o.end} · due ${o.due} — Prepare from Excel`;
      host.appendChild(a);
    });
  }
  $('clients').appendChild(host);
};
</script>
</body></html>
"""
    return HTMLResponse(html)

# -------------------------------------------------------------------
# Prepare (spreadsheet viewer + pick)
# -------------------------------------------------------------------
@app.get("/prepare", response_class=HTMLResponse)
def prepare():
    html = """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Prepare from Excel – My VAT Filer</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  #grid table { border-collapse: collapse; width: 100%; }
  #grid td, #grid th { border: 1px solid #e5e7eb; padding: 4px 6px; font-size: 12px; }
  #grid td.sel { outline: 2px solid #2563eb; outline-offset: -2px; }
</style>
</head><body class="bg-slate-50">
  <div class="max-w-7xl mx-auto px-4 py-8">
    <a class="text-sm text-blue-700" href="javascript:history.back()">← Back</a>
    <h1 class="text-2xl font-semibold mt-2">Prepare return</h1>
    <p class="text-slate-600" id="meta"></p>

    <div class="grid md:grid-cols-3 gap-6 mt-4">
      <!-- Left pane -->
      <section class="rounded-xl bg-white border border-slate-200 shadow-sm p-5">
        <h2 class="font-medium text-slate-800 mb-4">Pick cells</h2>

        <div class="mb-3">
          <label class="block text-sm font-medium mb-1">Excel file (.xlsx)</label>
          <input type="file" id="file" accept=".xlsx"/>
        </div>

        <div class="mb-3">
          <label class="block text-sm font-medium mb-1">Sheet</label>
          <select id="sheet" class="w-full rounded border-slate-300"></select>
        </div>

        <p class="text-sm text-slate-600 mb-3">Click a cell on the right preview, then press a “Pick” button below.</p>

        <div id="choices" class="space-y-3">
          <div class="flex items-center justify-between">
            <div>Box 1 — VAT due on sales</div>
            <div>
              <span class="text-xs text-slate-500 mr-2" id="s_box1">Selected: —</span>
              <button data-box="box1" class="pick rounded border px-2 py-1">Pick</button>
            </div>
          </div>
          <div class="flex items-center justify-between">
            <div>Box 2 — VAT due on acquisitions (EU)</div>
            <div>
              <span class="text-xs text-slate-500 mr-2" id="s_box2">Selected: —</span>
              <button data-box="box2" class="pick rounded border px-2 py-1">Pick</button>
            </div>
          </div>
          <div class="flex items-center justify-between">
            <div>Box 4 — VAT reclaimed on purchases</div>
            <div>
              <span class="text-xs text-slate-500 mr-2" id="s_box4">Selected: —</span>
              <button data-box="box4" class="pick rounded border px-2 py-1">Pick</button>
            </div>
          </div>
          <div class="flex items-center justify-between">
            <div>Box 6 — Total value of sales (ex VAT)</div>
            <div>
              <span class="text-xs text-slate-500 mr-2" id="s_box6">Selected: —</span>
              <button data-box="box6" class="pick rounded border px-2 py-1">Pick</button>
            </div>
          </div>
          <div class="flex items-center justify-between">
            <div>Box 7 — Total value of purchases (ex VAT)</div>
            <div>
              <span class="text-xs text-slate-500 mr-2" id="s_box7">Selected: —</span>
              <button data-box="box7" class="pick rounded border px-2 py-1">Pick</button>
            </div>
          </div>
          <div class="flex items-center justify-between">
            <div>Box 8 — Supplies to EU (ex VAT)</div>
            <div>
              <span class="text-xs text-slate-500 mr-2" id="s_box8">Selected: —</span>
              <button data-box="box8" class="pick rounded border px-2 py-1">Pick</button>
            </div>
          </div>
          <div class="flex items-center justify-between">
            <div>Box 9 — Acquisitions from EU (ex VAT)</div>
            <div>
              <span class="text-xs text-slate-500 mr-2" id="s_box9">Selected: —</span>
              <button data-box="box9" class="pick rounded border px-2 py-1">Pick</button>
            </div>
          </div>
        </div>

        <button id="preview" class="mt-5 w-full rounded bg-blue-600 text-white py-2 hover:bg-blue-700">Preview</button>
      </section>

      <!-- Right pane (grid) -->
      <section class="md:col-span-2 rounded-xl bg-white border border-slate-200 shadow-sm p-5">
        <div id="gridMsg" class="text-sm text-slate-600 mb-2">Loading spreadsheet viewer…</div>
        <div id="grid" class="overflow-auto h-[70vh] border border-slate-200 rounded"></div>
      </section>
    </div>
  </div>

<script>
// Load SheetJS with CDN then local fallback
(function loadXLSX(){
  const msg = document.getElementById('gridMsg');
  function fail(){
    msg.innerHTML = "Couldn't load the spreadsheet viewer library (XLSX). Please allow CDN scripts <em>or</em> save <code>xlsx.full.min.js</code> to <code>./static</code>.";
  }
  const s = document.createElement('script');
  s.src = "https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js";
  s.onload = ()=>window._xlsx_ready=true;
  s.onerror = ()=>{
    const s2 = document.createElement('script');
    s2.src = "/static/xlsx.full.min.js";
    s2.onload = ()=>window._xlsx_ready=true;
    s2.onerror = fail;
    document.head.appendChild(s2);
  };
  document.head.appendChild(s);
})();

const $ = (id)=>document.getElementById(id);
const params = new URLSearchParams(location.search);
const vrn = params.get('vrn') || '';
const periodKey = params.get('periodKey') || '';
const role = params.get('role') || 'business';
$('meta').textContent = `VRN ${vrn || '—'}, period ${periodKey || '—'} (${role})`;

let wb = null;
let sheetName = null;
let lastAddr = null;
let lastValue = null;

const picks = {
  box1:null, box2:null, box4:null, box6:null, box7:null, box8:null, box9:null
};

function renderSheet(name){
  sheetName = name;
  const grid = $('grid');
  const ws = wb.Sheets[name];
  const aoa = XLSX.utils.sheet_to_json(ws, {header:1, raw:true});
  // build table
  let html = '<table><thead><tr><th></th>';
  const cols = Math.max(...aoa.map(r=>r.length));
  for(let c=0;c<cols;c++){
    html += `<th>${colName(c+1)}</th>`;
  }
  html += '</tr></thead><tbody>';
  for(let r=0;r<aoa.length;r++){
    html += `<tr><th>${r+1}</th>`;
    for(let c=0;c<cols;c++){
      const v = (aoa[r] && aoa[r][c] != null) ? aoa[r][c] : '';
      const addr = XLSX.utils.encode_cell({r, c});
      html += `<td data-addr="${addr}">${escapeHtml(v)}</td>`;
    }
    html += '</tr>';
  }
  html += '</tbody></table>';
  grid.innerHTML = html;

  // clicking a cell
  grid.querySelectorAll('td').forEach(td=>{
    td.addEventListener('click', ()=>{
      grid.querySelectorAll('td.sel').forEach(x=>x.classList.remove('sel'));
      td.classList.add('sel');
      lastAddr = td.getAttribute('data-addr');
      lastValue = parseFloat(td.textContent.replace(/[,\\s]/g,'')) || 0;
      $('gridMsg').textContent = `Selected ${lastAddr} → "${td.textContent}"`;
    });
  });
}

function colName(n){ // 1->A
  let s=""; while(n>0){ let m=(n-1)%26; s=String.fromCharCode(65+m)+s; n=Math.floor((n-m)/26); }
  return s;
}
function escapeHtml(x){
  if (x==null) return '';
  return String(x).replace(/[&<>]/g, m=>({ '&':'&amp;','<':'&lt;','>':'&gt;' }[m]));
}

$('file').addEventListener('change', async (e)=>{
  const f = e.target.files[0];
  if(!f){ return; }
  const buf = await f.arrayBuffer();
  const r = new Uint8Array(buf);
  const wait = ()=>new Promise(res=>{
    const id = setInterval(()=>{ if(window._xlsx_ready){ clearInterval(id); res(); } }, 50);
  });
  await wait();
  wb = XLSX.read(r, {type:'array'});
  const sel = $('sheet');
  sel.innerHTML = '';
  wb.SheetNames.forEach(n=>{
    const o = document.createElement('option');
    o.value = n; o.textContent = n;
    sel.appendChild(o);
  });
  renderSheet(wb.SheetNames[0]);
  $('gridMsg').textContent = 'Click a cell to select it, then press a “Pick” button.';
});
$('sheet').addEventListener('change', e=>{
  if(wb) renderSheet(e.target.value);
});

document.querySelectorAll('button.pick').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    if(!lastAddr){ alert('Click a cell first'); return; }
    const box = btn.getAttribute('data-box');
    picks[box] = { addr:lastAddr, value:lastValue };
    document.getElementById('s_'+box).textContent = `Selected ${lastAddr} → "${lastValue}"`;
  });
});

// Compute and go to /preview
$('preview').addEventListener('click', ()=>{
  const values = {
    vatDueSales: (picks.box1?.value)||0,
    vatDueAcquisitions: (picks.box2?.value)||0,
    vatReclaimedCurrPeriod: (picks.box4?.value)||0,
    totalValueSalesExVAT: Math.round(picks.box6?.value||0),
    totalValuePurchasesExVAT: Math.round(picks.box7?.value||0),
    totalValueGoodsSuppliedExVAT: Math.round(picks.box8?.value||0),
    totalAcquisitionsExVAT: Math.round(picks.box9?.value||0),
  };
  values.totalVatDue = +(values.vatDueSales + values.vatDueAcquisitions).toFixed(2);
  values.netVatDue   = +(values.totalVatDue - values.vatReclaimedCurrPeriod).toFixed(2);

  const payload = {
    vrn, periodKey, role,
    values,
    cells: {
      box1: picks.box1?.addr || null,
      box2: picks.box2?.addr || null,
      box4: picks.box4?.addr || null,
      box6: picks.box6?.addr || null,
      box7: picks.box7?.addr || null,
      box8: picks.box8?.addr || null,
      box9: picks.box9?.addr || null,
    }
  };
  localStorage.setItem('previewPayload', JSON.stringify(payload));
  location.href = '/preview';
});
</script>
</body></html>
"""
    return HTMLResponse(html)

# -------------------------------------------------------------------
# Preview page (clean summary)
# -------------------------------------------------------------------
@app.get("/preview", response_class=HTMLResponse)
def preview():
    html = """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Preview – My VAT Filer</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
  <div class="max-w-5xl mx-auto px-4 py-8">
    <h1 class="text-2xl font-semibold">Preview VAT return</h1>
    <p class="text-slate-600" id="meta"></p>

    <div id="cards" class="grid md:grid-cols-3 gap-4 mt-6"></div>

    <div class="mt-8 flex gap-3">
      <button id="use" class="rounded bg-green-600 text-white px-4 py-2 hover:bg-green-700">Use these values → Go to /ui</button>
      <a href="javascript:history.back()" class="rounded border px-4 py-2">Back</a>
    </div>
  </div>

<script>
const pretty = n => (typeof n==='number' ? n.toLocaleString(undefined,{maximumFractionDigits:2}) : String(n));
const pf = JSON.parse(localStorage.getItem('previewPayload') || '{}');
if(!pf.values){ document.body.innerHTML = '<div class="p-8 text-center">Nothing to preview.</div>'; }
document.getElementById('meta').textContent = `VRN ${pf.vrn||'—'}, period ${pf.periodKey||'—'} (${pf.role||'business'})`;

const map = [
  ['Box 1 — VAT due on sales', pf.values?.vatDueSales],
  ['Box 2 — VAT due on acquisitions (EU)', pf.values?.vatDueAcquisitions],
  ['Box 3 — Total VAT due', pf.values?.totalVatDue],
  ['Box 4 — VAT reclaimed on purchases', pf.values?.vatReclaimedCurrPeriod],
  ['Box 5 — Net VAT to pay to HMRC or reclaim', pf.values?.netVatDue],
  ['Box 6 — Total value of sales (ex VAT)', pf.values?.totalValueSalesExVAT],
  ['Box 7 — Total value of purchases (ex VAT)', pf.values?.totalValuePurchasesExVAT],
  ['Box 8 — Supplies to EU (ex VAT)', pf.values?.totalValueGoodsSuppliedExVAT],
  ['Box 9 — Acquisitions from EU (ex VAT)', pf.values?.totalAcquisitionsExVAT],
];

const cards = document.getElementById('cards');
map.forEach(([label, val])=>{
  const d = document.createElement('div');
  d.className = 'rounded-xl bg-white border border-slate-200 shadow-sm p-4';
  d.innerHTML = `<div class="text-sm text-slate-600">${label}</div>
                 <div class="text-2xl font-semibold mt-1">${pretty(val)}</div>`;
  cards.appendChild(d);
});

document.getElementById('use').onclick = ()=>{
  // prepare payload for /ui
  const v = pf.values||{};
  localStorage.setItem('prefill', JSON.stringify({
    periodKey: pf.periodKey||'',
    vatDueSales: v.vatDueSales,
    vatDueAcquisitions: v.vatDueAcquisitions,
    totalVatDue: v.totalVatDue,
    vatReclaimedCurrPeriod: v.vatReclaimedCurrPeriod,
    netVatDue: v.netVatDue,
    totalValueSalesExVAT: v.totalValueSalesExVAT,
    totalValuePurchasesExVAT: v.totalValuePurchasesExVAT,
    totalValueGoodsSuppliedExVAT: v.totalValueGoodsSuppliedExVAT,
    totalAcquisitionsExVAT: v.totalAcquisitionsExVAT
  }));
  const vrn = encodeURIComponent(pf.vrn||'');
  location.href = '/ui?vrn='+vrn;
};
</script>
</body></html>
"""
    return HTMLResponse(html)

# -------------------------------------------------------------------
# Filing UI (original, with prefill)
# -------------------------------------------------------------------
@app.get("/ui", response_class=HTMLResponse)
def ui():
    html = """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>My VAT Filer — Sandbox</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config = { theme: { extend: { colors: { brand:'#2563eb' }}}}</script>
</head><body class="h-full bg-slate-50 text-slate-900">
  <div class="max-w-6xl mx-auto px-4 py-8">
    <header class="mb-8">
      <h1 class="text-3xl font-semibold tracking-tight">My VAT Filer <span class="text-slate-400">(Sandbox)</span></h1>
      <p class="text-slate-600 mt-1">Connect, pick an obligation period, complete Boxes 1–9, submit, and view the receipt.</p>
      <p class="text-sm mt-1"><a class="text-blue-700" href="/portal">Go to portal</a></p>
    </header>

    <div class="grid md:grid-cols-3 gap-6">
      <section class="md:col-span-1 rounded-xl bg-white border border-slate-200 shadow-sm p-5">
        <h2 class="font-medium text-slate-800 mb-3">1) Connect to HMRC</h2>
        <p class="text-sm text-slate-600 mb-4">Use your sandbox test organisation.</p>
        <button id="btnConnect" class="inline-flex items-center justify-center rounded-lg bg-brand px-4 py-2 text-white hover:bg-blue-600 active:bg-blue-700 transition">Connect</button>
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
      <p class="text-xs text-slate-500 mb-4">Boxes 2, 8 and 9 relate to EU movements (historic periods and/or Northern Ireland traders). In most cases set them to 0.</p>

      <div class="grid grid-cols-1 gap-y-5">
        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1">
            <span class="font-semibold">Box 1 — VAT due on sales and other outputs</span>
          </label>
          <input id="vatDueSales" value="0.00" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand" />
        </div>

        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1">
            <span class="font-semibold">Box 2 — VAT due on acquisitions from EU member states</span>
          </label>
          <input id="vatDueAcquisitions" value="0.00" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand" />
        </div>

        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1">
            <span class="font-semibold">Box 3 — Total VAT due</span>
          </label>
          <input id="totalVatDue" value="0.00" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand" />
        </div>

        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1">
            <span class="font-semibold">Box 4 — VAT reclaimed on purchases and other inputs</span>
          </label>
          <input id="vatReclaimedCurrPeriod" value="0.00" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand" />
        </div>

        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1">
            <span class="font-semibold">Box 5 — Net VAT to pay to HMRC or reclaim</span>
          </label>
          <input id="netVatDue" value="0.00" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand" />
        </div>

        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1">
            <span class="font-semibold">Box 6 — Total value of sales and all other outputs (ex VAT)</span>
          </label>
          <input id="totalValueSalesExVAT" value="0" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand" />
        </div>

        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1">
            <span class="font-semibold">Box 7 — Total value of purchases and all other inputs (ex VAT)</span>
          </label>
          <input id="totalValuePurchasesExVAT" value="0" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand" />
        </div>

        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1">
            <span class="font-semibold">Box 8 — Total value of supplies of goods to EU member states (ex VAT)</span>
          </label>
          <input id="totalValueGoodsSuppliedExVAT" value="0" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand" />
        </div>

        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1">
            <span class="font-semibold">Box 9 — Total value of acquisitions of goods from EU member states (ex VAT)</span>
          </label>
          <input id="totalAcquisitionsExVAT" value="0" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand" />
        </div>

        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1">
            <span class="font-semibold">Declaration</span>
          </label>
          <select id="finalised" class="w-full rounded-lg border-slate-300 focus:border-brand focus:ring-brand">
            <option>true</option>
            <option>false</option>
          </select>
        </div>
      </div>

      <div class="mt-5">
        <button id="btnSubmit" class="rounded-lg bg-brand text-white px-5 py-2 hover:bg-blue-600 active:bg-blue-700 transition">Submit return</button>
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

function notify(t){ console.log(t); }

function pretty(objOrText){
  try{ return JSON.stringify(typeof objOrText==='string' ? JSON.parse(objOrText) : objOrText, null, 2); }
  catch{ return String(objOrText); }
}

function recalcTotals(){
  const vds = parseFloat($('vatDueSales').value || 0);
  const vda = parseFloat($('vatDueAcquisitions').value || 0);
  const vrc = parseFloat($('vatReclaimedCurrPeriod').value || 0);
  $('totalVatDue').value = (vds + vda).toFixed(2);
  $('netVatDue').value   = (vds + vda - vrc).toFixed(2);
}
['vatDueSales','vatDueAcquisitions','vatReclaimedCurrPeriod'].forEach(id=>{
  $(id).addEventListener('input', recalcTotals);
});

// prefill from /preview
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
    recalcTotals();
    localStorage.removeItem('prefill');
  }
} catch(e) {}

try {
  const q = new URLSearchParams(location.search);
  const qvrn = q.get('vrn');
  if (qvrn) $('vrn').value = qvrn;
} catch(e) {}

$('btnConnect').onclick = () => { window.location = '/connect'; };

$('btnLoad').onclick = async ()=>{
  try{
    const vrn = $('vrn').value.trim();
    const scenario = $('scenario').value;
    if(!vrn){ out.textContent = 'Enter a VRN first'; return; }

    const url = scenario ? `/api/obligations?vrn=${vrn}&scenario=${encodeURIComponent(scenario)}`
                         : `/api/obligations?vrn=${vrn}`;
    const r = await fetch(url);
    const data = await r.json();

    const select = $('periodSelect');
    select.innerHTML = '';
    const obs = data.obligations || [];
    if(!obs.length){
      select.innerHTML = '<option value="">— no open obligations —</option>';
      $('periodKey').value = '';
      $('obligMeta').textContent = '';
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
  }catch(e){
    out.textContent = pretty(e.message);
  }
};

$('btnSubmit').onclick = async ()=>{
  try{
    const vrn = $('vrn').value.trim();
    const periodKey = $('periodKey').value.trim();
    if(!vrn || !periodKey){ out.textContent = 'VRN and periodKey required'; return; }

    const pre = await fetch(`/api/returns/view?vrn=${vrn}&periodKey=${encodeURIComponent(periodKey)}`);
    if (pre.ok) {
      const already = await pre.json();
      out.textContent = 'Already submitted. HMRC shows:\\n' + pretty(already);
      return;
    }

    recalcTotals();
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
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    const txt = await r.text();
    out.textContent = pretty(txt);
  }catch(e){
    out.textContent = pretty(e.message);
  }
};

$('btnCopy').onclick = async ()=>{ await navigator.clipboard.writeText(out.textContent || ''); };
$('btnClear').onclick = ()=>{ out.textContent = ''; };
</script>
</body></html>
"""
    return HTMLResponse(html)
