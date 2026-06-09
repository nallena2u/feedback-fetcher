#!/usr/bin/env python3
"""
Feedback Fetcher – local web frontend.

A self-contained (stdlib-only) web UI for feedback_fetcher.py. Run it and a
browser window opens with a form to pick apps, star ratings, country, date,
etc. Results are saved as a CSV in this folder and shown in a table.

Usage:
    python app.py            # opens http://127.0.0.1:8765 in your browser

No dependencies beyond what feedback_fetcher.py already needs.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs

# Where feedback_fetcher.py lives (alongside this file, or inside the PyInstaller
# bundle when frozen) and where CSVs should be written.
if getattr(sys, "frozen", False):
    # Running as a bundled .app: import from the bundle, but save CSVs somewhere
    # the user can actually find and the app can write to.
    MODULE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    BASE_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Feedback Fetcher Output")
    os.makedirs(BASE_DIR, exist_ok=True)
else:
    MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = MODULE_DIR

os.chdir(BASE_DIR)
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from feedback_fetcher import (
    scrape_apple_reviews,
    scrape_google_reviews,
    export_to_csv,
)

HOST = "127.0.0.1"
PREFERRED_PORTS = (8765, 8766, 8767)  # try these first, then any free port

# Version + update check (Option B: notify, don't auto-install).
# Bump VERSION on every release you build. To enable update notifications, host a
# small JSON file and put its public URL in DEFAULT_UPDATE_URL (or set the env var):
#   {"version": "1.1.0", "url": "https://.../Feedback Fetcher.zip", "notes": "What's new"}
# On launch the app fetches it; if the version is newer, the page shows a banner
# with a download link. No code is downloaded or run — only a version string + URL.
# Leave the URL empty to disable the check entirely (no network call, no banner).
VERSION = "1.0.0"
DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/nallena2u/feedback-fetcher/main/version.json"
UPDATE_URL = os.environ.get("FEEDBACK_FETCHER_UPDATE_URL", "").strip() or DEFAULT_UPDATE_URL
_update_info = None  # populated by the background check if a newer version exists

# Auto-shutdown: the open page sends a heartbeat to /ping. If the heartbeat
# stops (browser tab closed), the watchdog shuts the server down so it doesn't
# linger in the background — important for the double-click app where there's
# no Terminal to Ctrl+C.
HEARTBEAT_INTERVAL = 2   # seconds; how often the page pings (kept in sync with JS)
IDLE_TIMEOUT = 8         # seconds without a ping before we quit
_last_ping = None        # monotonic time of last heartbeat (None until first connect)

# Common storefronts for the dropdown. Value is the country code the stores use.
COUNTRIES = [
    ("us", "United States"),
    ("gb", "United Kingdom"),
    ("ca", "Canada"),
    ("au", "Australia"),
    ("de", "Germany"),
    ("fr", "France"),
    ("es", "Spain"),
    ("it", "Italy"),
    ("nl", "Netherlands"),
    ("br", "Brazil"),
    ("mx", "Mexico"),
    ("jp", "Japan"),
    ("in", "India"),
]


def detect_store(app_id: str) -> str:
    """Numeric id -> Apple, otherwise (package name with dots) -> Google."""
    return "apple" if app_id.strip().isdigit() else "google"


def _jsonable(value):
    """Make scraper rows JSON-serializable (dates -> ISO strings)."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def apple_app_exists(app_id: str, country: str):
    """True/False if the Apple app exists, or None if the check itself failed.
    Apple IDs must be numeric; anything else is invalid by definition."""
    if not str(app_id).strip().isdigit():
        return False
    url = f"https://itunes.apple.com/lookup?id={int(app_id)}&country={country.lower()}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("resultCount", 0) > 0
    except Exception:
        return None  # don't flag as invalid if we couldn't verify


def google_app_exists(app_id: str, language: str, country: str):
    """True/False if the Google Play app exists, or None if the check failed."""
    try:
        from google_play_scraper import app as gp_app
    except ImportError:
        return None
    try:
        gp_app(app_id, lang=language, country=country)
        return True
    except Exception:
        return False


def _relevance(title: str, query: str) -> int:
    """Score how well an app title matches the query, so the best name-match
    floats to the top of the combined (Apple + Google) list regardless of store."""
    t = (title or "").lower().strip()
    q = (query or "").lower().strip()
    if not t or not q:
        return 0
    if t == q:
        return 100
    if t.startswith(q):
        return 85
    if q in t:
        return 70
    q_words = [w for w in q.split() if w]
    t_words = set(t.split())
    if q_words and all(w in t_words for w in q_words):
        return 60                       # every query word appears in the title
    overlap = sum(1 for w in q_words if w in t_words)
    return 20 + overlap * 5 if overlap else 0


def search_apps(query: str, country: str = "us"):
    """Find apps by name across both stores. Returns
    {"results": [{store, id, title, developer}], "errors": [store names]}.
    Each store is queried independently (one failing still returns the other),
    and the combined list is ranked by title relevance so the most relevant
    matches — from either store — appear at the top."""
    query = (query or "").strip()[:100]
    if not query:
        return {"results": [], "errors": []}
    country = (country or "us").strip().lower() or "us"
    apple, google, errors = [], [], []

    # Apple — public iTunes Search API (verified TLS, same host as ID validation).
    try:
        from urllib.parse import urlencode
        qs = urlencode({"term": query, "entity": "software", "country": country, "limit": 20})
        with urllib.request.urlopen(f"https://itunes.apple.com/search?{qs}", timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for r in data.get("results", []):
            if r.get("trackId"):
                apple.append({"store": "apple", "id": str(r["trackId"]),
                              "title": r.get("trackName", ""), "developer": r.get("artistName", "")})
    except Exception:
        errors.append("Apple")

    # Google — bundled google_play_scraper.search().
    try:
        from google_play_scraper import search as gp_search
        for r in gp_search(query, lang="en", country=country, n_hits=20):
            if r.get("appId"):
                google.append({"store": "google", "id": r["appId"],
                               "title": r.get("title", ""), "developer": r.get("developer", "")})
    except Exception:
        errors.append("Google")

    # Rank by relevance; ties keep each store's own ranking (rank-within-store)
    # so the top hit from each store interleaves rather than clumping by store.
    scored = ([(-_relevance(r["title"], query), rank, r) for rank, r in enumerate(apple)]
              + [(-_relevance(r["title"], query), rank, r) for rank, r in enumerate(google)])
    scored.sort(key=lambda x: (x[0], x[1]))  # key avoids comparing the dicts
    return {"results": [r for _, _, r in scored], "errors": errors}


def run_scrape(params: dict) -> dict:
    """Run the scraper for one or more apps and return combined results."""
    raw_ids = params.get("app_ids", "")
    app_ids = [a.strip() for a in raw_ids.replace(",", "\n").splitlines() if a.strip()]
    if not app_ids:
        raise ValueError("Enter at least one app ID or package name.")

    store_choice = params.get("store", "auto")
    stars = [int(s) for s in params.get("stars", [1, 2, 3, 4, 5])]
    if not stars:
        raise ValueError("Select at least one star rating.")
    country = params.get("country", "us").strip() or "us"
    language = params.get("language", "en").strip() or "en"
    max_reviews = int(params.get("max_reviews", 500))
    max_reviews = max(1, min(max_reviews, 5000))  # clamp to sane bounds

    after_date = None
    after_raw = (params.get("after_date") or "").strip()
    if after_raw:
        try:
            after_date = datetime.fromisoformat(after_raw).date()
        except ValueError:
            raise ValueError("After-date must be in YYYY-MM-DD format.")

    all_reviews = []
    per_app = []
    for app_id in app_ids:
        store = detect_store(app_id) if store_choice == "auto" else store_choice
        try:
            if store == "apple":
                rows = scrape_apple_reviews(
                    app_id=app_id, country=country, star_ratings=stars,
                    max_reviews=max_reviews, after_date=after_date,
                )
            else:
                rows = scrape_google_reviews(
                    app_id=app_id, country=country, language=language,
                    star_ratings=stars, max_reviews=max_reviews,
                    after_date=after_date,
                )
            # Invalid IDs return an empty list rather than raising, so when we get
            # nothing back, verify the app actually exists and flag it if it doesn't.
            if not rows:
                exists = (apple_app_exists(app_id, country) if store == "apple"
                          else google_app_exists(app_id, language, country))
                if exists is False:
                    store_name = "Apple App Store" if store == "apple" else "Google Play"
                    hint = " (Apple IDs are numeric)" if store == "apple" and not str(app_id).isdigit() else ""
                    raise ValueError(f"not a valid {store_name} app ID{hint}")
            per_app.append({"app_id": app_id, "store": store, "count": len(rows), "error": None})
            all_reviews.extend(rows)
        except Exception as exc:  # keep going so one bad id doesn't kill the run
            per_app.append({"app_id": app_id, "store": store, "count": 0, "error": str(exc)})

    csv_name = None
    if all_reviews:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stars_str = "_".join(map(str, sorted(stars)))
        csv_name = f"reviews_{stars_str}_{timestamp}.csv"
        export_to_csv(all_reviews, os.path.join(BASE_DIR, csv_name))

    # Build a JSON-safe table (cap rows sent to the browser for responsiveness).
    table = [{k: _jsonable(v) for k, v in row.items()} for row in all_reviews]
    return {
        "ok": True,
        "total": len(all_reviews),
        "per_app": per_app,
        "csv": csv_name,
        "rows": table,
    }


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feedback Fetcher</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --line:#e1e4e8; --accent:#2563eb; --text:#1f2328; --muted:#656d76; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         background:var(--bg); color:var(--text); }
  header { background:var(--accent); color:#fff; padding:18px 24px; }
  header h1 { margin:0; font-size:20px; font-weight:600; }
  header p { margin:4px 0 0; font-size:13px; opacity:.9; }
  main { max-width:1100px; margin:24px auto; padding:0 16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:20px; margin-bottom:20px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; }
  label { display:block; font-size:13px; font-weight:600; margin-bottom:6px; }
  .hint { font-weight:400; color:var(--muted); font-size:12px; }
  input[type=text], input[type=number], input[type=date], select, textarea {
    width:100%; padding:9px 10px; border:1px solid var(--line); border-radius:7px; font-size:14px; font-family:inherit; background:#fff; }
  textarea { resize:vertical; min-height:80px; }
  .stars { display:flex; gap:8px; flex-wrap:wrap; }
  .star-btn { display:flex; align-items:center; gap:6px; border:1px solid var(--line); border-radius:9px;
              padding:11px 18px; cursor:pointer; font-size:16px; font-weight:600; user-select:none;
              background:#fff; color:var(--muted); transition:background .12s,border-color .12s,color .12s; }
  .star-btn .ic { color:#d0d7de; }
  .star-btn.active { background:#fffbeb; border-color:#f59e0b; color:#b45309; }
  .star-btn.active .ic { color:#f59e0b; }
  .presets { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
  .preset { font-size:12px; border:1px solid var(--line); background:#fff; color:var(--accent);
            border-radius:14px; padding:5px 13px; cursor:pointer; font-weight:600; }
  .preset:hover { background:#eff6ff; }
  details.adv { margin-top:6px; border-top:1px solid var(--line); padding-top:6px; }
  details.adv > summary { cursor:pointer; font-size:13px; font-weight:600; color:var(--accent); list-style:none; padding:8px 0; }
  details.adv > summary::-webkit-details-marker { display:none; }
  details.adv > summary::before { content:'\\25B8\\00a0'; }
  details.adv[open] > summary::before { content:'\\25BE\\00a0'; }
  button { background:var(--accent); color:#fff; border:none; border-radius:8px; padding:11px 22px;
           font-size:15px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.55; cursor:default; }
  .row-actions { display:flex; align-items:center; gap:14px; margin-top:18px; }
  #status { font-size:14px; color:var(--muted); }
  .err { color:#b91c1c; }
  .ok { color:#15803d; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th, td { border-bottom:1px solid var(--line); padding:8px 10px; text-align:left; vertical-align:top; }
  th { background:#f6f8fa; position:sticky; top:0; }
  .tablewrap { max-height:520px; overflow:auto; border:1px solid var(--line); border-radius:8px; }
  .summary { font-size:14px; margin-bottom:12px; }
  .callout { margin-top:16px; background:#fffbeb; border:1px solid #fde68a; border-radius:8px;
             padding:12px 14px; font-size:13px; line-height:1.5; color:#92400e; }
  .callout strong { color:#78350f; }
  .errbox { margin-bottom:14px; background:#fef2f2; border:1px solid #fecaca; border-radius:8px;
            padding:12px 14px; font-size:13px; line-height:1.5; color:#991b1b; }
  .errbox strong { color:#7f1d1d; }
  .errbox ul { margin:6px 0 0; padding-left:20px; }
  .errbox code { background:rgba(127,29,29,.1); padding:1px 5px; border-radius:3px; }
  .pill { display:inline-block; background:#eef2ff; color:#3730a3; border-radius:6px; padding:2px 8px; font-size:12px; margin-right:6px; }
  .download { display:inline-flex; align-items:center; gap:7px; background:var(--accent); color:#fff;
              font-weight:600; font-size:15px; text-decoration:none; padding:11px 22px; border-radius:8px;
              border:none; cursor:pointer; }
  .tip { position:relative; display:inline-flex; cursor:help; color:var(--muted); }
  .tip:hover, .tip:focus-within { color:var(--accent); }
  .tip svg { width:15px; height:15px; display:block; }
  .tip .bubble { position:absolute; left:0; top:24px; z-index:10; width:340px; display:none;
                 background:#1f2328; color:#fff; border-radius:8px; padding:12px 14px; font-size:12px;
                 font-weight:400; line-height:1.5; box-shadow:0 6px 20px rgba(0,0,0,.25); }
  .tip .bubble code { background:rgba(255,255,255,.14); padding:1px 4px; border-radius:3px; }
  .tip .bubble b { color:#ffd479; }
  .tip:hover .bubble, .tip:focus-within .bubble { display:block; }
  .search-row { display:flex; gap:8px; }
  .search-row input { flex:1; }
  .finder-results { display:none; margin-top:8px; position:relative; }
  .finder-results.show { display:block; }
  .search-hint { font-size:12px; color:var(--muted); line-height:1.5; margin-bottom:8px;
                 background:#f6f8fa; border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
  .search-results { border:1px solid var(--line); border-radius:8px; max-height:260px; overflow:auto; }
  /* On wide windows there's room to float the hint into the left margin, so the
     results sit full-width under the search bar. Narrower: hint stacks above. */
  @media (min-width:1600px) {
    .search-hint { position:absolute; top:0; right:100%; margin:0 36px 0 0; width:200px; }
  }
  .sr-item { display:flex; align-items:baseline; gap:8px; padding:9px 12px; cursor:pointer;
             border-bottom:1px solid var(--line); font-size:13px; }
  .sr-item:last-child { border-bottom:none; }
  .sr-item:hover { background:#f6f8fa; }
  .sr-title { font-weight:600; color:var(--text); }
  .sr-dev { color:var(--muted); }
  .sr-id { margin-left:auto; color:var(--muted); font-size:12px; font-family:ui-monospace,Menlo,monospace; }
  .sr-msg { padding:10px 12px; font-size:13px; color:var(--muted); }
  .update-banner { display:none; align-items:center; gap:14px; background:#eff6ff; border:1px solid #bfdbfe;
                   color:#1e40af; border-radius:10px; padding:12px 16px; margin-bottom:20px; font-size:14px; }
  .update-banner.show { display:flex; }
  .update-link { background:var(--accent); color:#fff; text-decoration:none; font-weight:600;
                 padding:7px 16px; border-radius:7px; font-size:13px; white-space:nowrap; }
  .update-x { margin-left:auto; background:none; border:none; color:#1e40af; font-size:20px;
              line-height:1; cursor:pointer; padding:0 4px; }
</style>
</head>
<body>
<header>
  <h1>Feedback Fetcher</h1>
  <p>Pull Apple App Store &amp; Google Play reviews into a CSV.</p>
</header>
<main>
  <div class="update-banner" id="update_banner">
    <span id="update_text"></span>
    <a id="update_link" class="update-link" target="_blank" rel="noopener">Download update</a>
    <button type="button" id="update_dismiss" class="update-x" title="Dismiss">&times;</button>
  </div>
  <div class="card">
    <div style="margin-bottom:18px">
      <label>Find an app by name <span class="hint">— searches Apple &amp; Google; click a result to add it below</span></label>
      <div class="search-row">
        <input type="text" id="search_q" placeholder="e.g. WhatsApp" autocomplete="off">
        <button type="button" id="search_btn">Search</button>
      </div>
      <div class="finder-results" id="finder_results">
        <div class="search-hint" id="search_hint">Don't see the app you expected? Apple and Google rank searches differently — try the developer's name or the app's exact store title.</div>
        <div class="search-results" id="search_results"></div>
      </div>
    </div>
    <div style="margin-bottom:18px">
      <label style="display:inline-flex; align-items:center; gap:6px">App IDs / package names
        <span class="tip" tabindex="0"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>
          <span class="bubble">
            <b>Apple:</b> open the app's App Store page — the ID is the number after <code>id</code> in the URL:<br>
            apps.apple.com/us/app/whatsapp/id<b>310633997</b><br><br>
            <b>Google:</b> open the app's Play Store page — the package name is the <code>id=</code> value:<br>
            play.google.com/store/apps/details?id=<b>com.whatsapp</b>
          </span>
        </span></label>
      <textarea id="app_ids" placeholder="310633997&#10;com.whatsapp"></textarea>
    </div>
    <div class="grid" style="margin-bottom:18px">
      <div>
        <label>Store</label>
        <select id="store">
          <option value="auto" selected>Auto-detect (recommended)</option>
          <option value="apple">Apple App Store</option>
          <option value="google">Google Play</option>
        </select>
      </div>
      <div>
        <label>Reviews on or after <span class="hint">(optional)</span></label>
        <input type="date" id="after_date">
      </div>
    </div>
    <div style="margin-bottom:6px">
      <label>Star ratings <span class="hint">— click a star to include or exclude it</span></label>
      <div class="stars" id="stars">
        <div class="star-btn active" data-v="1">1 <span class="ic">★</span></div>
        <div class="star-btn active" data-v="2">2 <span class="ic">★</span></div>
        <div class="star-btn active" data-v="3">3 <span class="ic">★</span></div>
        <div class="star-btn active" data-v="4">4 <span class="ic">★</span></div>
        <div class="star-btn active" data-v="5">5 <span class="ic">★</span></div>
      </div>
      <div class="presets">
        <button type="button" class="preset" data-set="1,2,3,4,5">All</button>
        <button type="button" class="preset" data-set="1,2">Negative (1–2)</button>
        <button type="button" class="preset" data-set="3">Neutral (3)</button>
        <button type="button" class="preset" data-set="4,5">Positive (4–5)</button>
      </div>
    </div>
    <details class="adv">
      <summary>Advanced options</summary>
      <div class="grid" style="margin-top:12px">
        <div>
          <label>Country / storefront</label>
          <select id="country">__COUNTRY_OPTIONS__</select>
        </div>
        <div>
          <label>Language <span class="hint">(Google Play only)</span></label>
          <input type="text" id="language" value="en">
        </div>
        <div>
          <label>Max reviews per app</label>
          <input type="number" id="max_reviews" value="500" min="1" max="5000">
        </div>
      </div>
    </details>
    <div class="row-actions">
      <button id="run">Scrape reviews</button>
      <span id="status"></span>
    </div>
  </div>

  <div class="card" id="results" style="display:none">
    <div class="summary" id="summary"></div>
    <div class="tablewrap"><table id="resultTable"></table></div>
    <div class="callout">
      <strong>Note:</strong> these results only include ratings where a customer also left a written review.
      Ratings without reviews are not included. If you're checking your own app, we recommend consulting
      App Store Connect or the Google Play Console for more detailed information.
    </div>
  </div>
</main>

<script>
const $ = id => document.getElementById(id);

function getStars() {
  return [...document.querySelectorAll('#stars .star-btn.active')].map(b => +b.dataset.v).sort();
}

document.querySelectorAll('#stars .star-btn').forEach(b =>
  b.addEventListener('click', () => b.classList.toggle('active')));

document.querySelectorAll('.preset').forEach(p =>
  p.addEventListener('click', () => {
    const set = new Set(p.dataset.set.split(',').map(Number));
    document.querySelectorAll('#stars .star-btn').forEach(b =>
      b.classList.toggle('active', set.has(+b.dataset.v)));
  }));

// --- Search apps by name ---
function setFinder(html, showHint) {
  $('search_results').innerHTML = html;
  $('search_hint').style.display = showHint ? '' : 'none';
  $('finder_results').classList.add('show');
}
function hideFinder() { $('finder_results').classList.remove('show'); }
function srMsg(text) { setFinder(`<div class="sr-msg">${esc(text)}</div>`, false); }

function addAppId(id) {
  const ta = $('app_ids');
  const ids = ta.value.split('\\n').map(s => s.trim()).filter(Boolean);
  if (!ids.includes(id)) {
    ta.value = (ta.value.trim() ? ta.value.replace(/\\s*$/, '') + '\\n' : '') + id;
  }
}

async function searchApps() {
  const q = $('search_q').value.trim();
  if (!q) { srMsg('Type an app name, then Search.'); return; }
  const country = $('country').value || 'us';
  srMsg('Searching…');
  try {
    const res = await fetch('/search?q=' + encodeURIComponent(q) + '&country=' + encodeURIComponent(country));
    const data = await res.json();
    if (!data.ok) { srMsg('Search error: ' + data.error); return; }
    const errs = data.errors || [];
    if (!data.results.length) {
      srMsg(errs.length ? `${errs.join(' and ')} search was unavailable — try again in a moment.` : 'No matches found.');
      return;
    }
    const note = errs.length
      ? `<div class="sr-msg">Showing ${esc(errs.length===1 && errs[0]==='Apple' ? 'Google' : 'Apple')} only — ${errs.join(' and ')} search was unavailable.</div>`
      : '';
    const items = note + data.results.map(r => {
      const store = r.store === 'apple' ? 'Apple' : 'Google';
      return `<div class="sr-item" data-id="${esc(r.id)}">`
        + `<span class="pill">${store}</span>`
        + `<span class="sr-title">${esc(r.title)}</span>`
        + `<span class="sr-dev">${esc(r.developer)}</span>`
        + `<span class="sr-id">${esc(r.id)}</span></div>`;
    }).join('');
    setFinder(items, true);
    $('search_results').querySelectorAll('.sr-item').forEach(el =>
      el.addEventListener('click', () => { addAppId(el.dataset.id); hideFinder(); $('search_q').value = ''; }));
  } catch (e) {
    srMsg("Couldn't reach the search service — is the app still running?");
  }
}

$('search_btn').addEventListener('click', searchApps);
$('search_q').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); searchApps(); } });

// --- Update check (notify only) ---
async function checkUpdate() {
  try {
    const d = await (await fetch('/update')).json();
    if (d.ok && d.update && d.update.url) {
      $('update_text').textContent = `Version ${d.update.latest} is available (you have ${d.current}).`
        + (d.update.notes ? ' ' + d.update.notes : '');
      $('update_link').href = d.update.url;
      $('update_banner').classList.add('show');
    }
  } catch (e) { /* offline or no check configured — ignore */ }
}
$('update_dismiss').addEventListener('click', () => $('update_banner').classList.remove('show'));
checkUpdate();

async function run() {
  const btn = $('run'), status = $('status');
  const payload = {
    app_ids: $('app_ids').value,
    store: $('store').value,
    country: $('country').value,
    language: $('language').value,
    max_reviews: +$('max_reviews').value,
    after_date: $('after_date').value,
    stars: getStars(),
  };
  if (!payload.app_ids.trim()) { status.className='err'; status.textContent='Enter at least one app ID.'; return; }
  if (!payload.stars.length) { status.className='err'; status.textContent='Select at least one star rating.'; return; }

  btn.disabled = true; status.className=''; status.textContent = 'Scraping… this can take a minute.';
  try {
    const res = await fetch('/run', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    const data = await res.json();
    if (!data.ok) { status.className='err'; status.textContent = 'Error: ' + data.error; return; }
    renderResults(data);
    const failed = data.per_app.filter(a => a.error).length;
    if (failed) {
      status.className='err';
      status.textContent = `Done — ${data.total} reviews. ${failed} app ID${failed>1?'s':''} couldn't be fetched (see below).`;
    } else {
      status.className='ok'; status.textContent = `Done — ${data.total} reviews.`;
    }
  } catch (e) {
    status.className='err';
    status.textContent = "Couldn't reach the scraper — is the server still running? "
      + "(Re-run app.py / the launcher, then reload this page.)";
  } finally {
    btn.disabled = false;
  }
}

const esc = s => (s ?? '').toString().replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function renderResults(data) {
  $('results').style.display = 'block';
  const failed = data.per_app.filter(a => a.error);
  const ok = data.per_app.filter(a => !a.error);

  let errbox = '';
  if (failed.length) {
    const items = failed.map(a => `<li><code>${esc(a.app_id)}</code> — ${esc(a.error)}</li>`).join('');
    errbox = `<div class="errbox"><strong>${failed.length} app ID${failed.length>1?'s':''} couldn't be fetched:</strong><ul>${items}</ul></div>`;
  }

  const perApp = ok.map(a => `<span class="pill">${esc(a.app_id)} (${a.store}): ${a.count}</span>`).join(' ');
  let dl = '';
  if (data.csv) dl = `<div style="margin-top:14px; display:flex; align-items:center; justify-content:flex-end; gap:14px"><span class="hint">saved to ${data.csv}</span><button type="button" class="download" id="reveal" data-csv="${data.csv}">Show in Finder</button></div>`;
  $('summary').innerHTML = `${errbox}<div><strong>${data.total} reviews</strong> ${perApp}</div>${dl}`;
  const reveal = $('reveal');
  if (reveal) reveal.addEventListener('click', async () => {
    try {
      const r = await fetch('/reveal?file=' + encodeURIComponent(reveal.dataset.csv));
      const d = await r.json();
      if (!d.ok) { reveal.insertAdjacentHTML('afterend', ` <span class="hint err">— ${d.error}</span>`); }
    } catch (e) { /* server gone; ignore */ }
  });

  const table = $('resultTable');
  if (!data.rows.length) { table.innerHTML = '<tr><td>No reviews matched.</td></tr>'; return; }
  const cols = ['store','app_id','rating','date','user','title','review','version','country'];
  const present = cols.filter(c => data.rows.some(r => c in r));
  let html = '<thead><tr>' + present.map(c => `<th>${c}</th>`).join('') + '</tr></thead><tbody>';
  for (const r of data.rows) {
    html += '<tr>' + present.map(c => `<td>${esc(r[c])}</td>`).join('') + '</tr>';
  }
  html += '</tbody>';
  table.innerHTML = html;
}

$('run').addEventListener('click', run);

// Heartbeat: tells the local server this tab is still open. When the tab is
// closed the pings stop and the server shuts itself down (no lingering process).
let beat = false;
async function ping() {
  try { await fetch('/ping'); beat = true; }
  catch (e) {
    // Once we've connected, a failed ping means the server has stopped.
    if (beat) {
      document.body.innerHTML = '<div style="max-width:520px;margin:80px auto;padding:24px;'
        + 'font-family:-apple-system,sans-serif;text-align:center;color:#656d76">'
        + '<h2 style="color:#1f2328">Feedback Fetcher has stopped</h2>'
        + '<p>You can close this tab. To use it again, start the app and reload.</p></div>';
    }
  }
}
ping();
setInterval(ping, 2000);
</script>
</body>
</html>"""


_LOOPBACK = ("127.0.0.1", "localhost")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet console

    def _is_local_request(self):
        """Reject cross-origin requests so a malicious web page open in the
        user's browser can't drive this local server (CSRF) or reach it via a
        rebound DNS name. The server only ever listens on loopback, so a genuine
        request has a loopback Host and (if present) a loopback Origin."""
        host = self.headers.get("Host", "")
        hostname = host.rsplit(":", 1)[0].strip("[]") if host else ""
        if hostname not in _LOOPBACK:
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            try:
                if urlparse(origin).hostname not in _LOOPBACK:
                    return False
            except Exception:
                return False
        return True

    def _send(self, code, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._is_local_request():
            self._send(403, {"ok": False, "error": "forbidden"})
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            options = "".join(
                f'<option value="{code}"{" selected" if code=="us" else ""}>{name}</option>'
                for code, name in COUNTRIES
            )
            self._send(200, PAGE.replace("__COUNTRY_OPTIONS__", options), "text/html; charset=utf-8")
        elif path == "/ping":
            global _last_ping
            _last_ping = time.monotonic()
            self._send(200, {"ok": True})
        elif path == "/reveal":
            qs = parse_qs(parsed.query)
            self._reveal(unquote((qs.get("file") or [""])[0]))
        elif path == "/search":
            qs = parse_qs(parsed.query)
            query = (qs.get("q") or [""])[0]
            country = (qs.get("country") or ["us"])[0]
            try:
                self._send(200, {"ok": True, **search_apps(query, country)})
            except Exception as exc:
                self._send(200, {"ok": False, "error": str(exc)})
        elif path == "/update":
            self._send(200, {"ok": True, "current": VERSION, "update": _update_info})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def _reveal(self, name):
        """Open Finder and select the saved CSV (it lives in BASE_DIR already)."""
        safe = os.path.basename(name)  # no path traversal
        full = os.path.join(BASE_DIR, safe)
        if not (safe.endswith(".csv") and os.path.isfile(full)):
            self._send(404, {"ok": False, "error": "file not found"})
            return
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", "-R", full], check=False)  # reveal & select
            elif sys.platform.startswith("win"):
                subprocess.run(["explorer", "/select,", full], check=False)
            else:
                subprocess.run(["xdg-open", BASE_DIR], check=False)  # open folder
            self._send(200, {"ok": True, "folder": BASE_DIR})
        except Exception as exc:
            self._send(200, {"ok": False, "error": str(exc)})

    def do_POST(self):
        if not self._is_local_request():
            self._send(403, {"ok": False, "error": "forbidden"})
            return
        if urlparse(self.path).path != "/run":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            params = json.loads(self.rfile.read(length) or b"{}")
            result = run_scrape(params)
            self._send(200, result)
        except Exception as exc:
            self._send(200, {"ok": False, "error": str(exc)})


def _version_tuple(v):
    """Parse a dotted version string into a 3-part tuple for comparison."""
    parts = (str(v).split(".") + ["0", "0", "0"])[:3]
    return tuple(int(p) if p.isdigit() else 0 for p in parts)


def _check_for_update():
    """Fetch the hosted version.json and record an update if it's newer. Fully
    fail-silent: offline, unreachable, or malformed → no banner, no error."""
    global _update_info
    if not UPDATE_URL:
        return
    try:
        with urllib.request.urlopen(UPDATE_URL, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = str(data.get("version", "")).strip()
        if latest and _version_tuple(latest) > _version_tuple(VERSION):
            _update_info = {"latest": latest, "url": str(data.get("url", "")),
                            "notes": str(data.get("notes", ""))}
    except Exception:
        pass


def _watchdog(server):
    """Quit once the browser stops sending heartbeats (i.e. the tab was closed)."""
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        # Don't quit before the page has ever connected (e.g. browser slow to open).
        if _last_ping is not None and time.monotonic() - _last_ping > IDLE_TIMEOUT:
            print("\nBrowser closed — shutting down.")
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


def make_server():
    """Bind a server, preferring the usual ports but falling back to any free
    one so a busy port never stops the app from launching."""
    env_port = os.environ.get("PORT")
    candidates = [int(env_port)] if env_port else list(PREFERRED_PORTS) + [0]  # 0 => OS picks
    last_err = None
    for port in candidates:
        try:
            return ThreadingHTTPServer((HOST, port), Handler)
        except OSError as exc:
            last_err = exc
    raise SystemExit(f"Could not bind a port: {last_err}")


def main():
    server = make_server()
    port = server.server_address[1]  # actual port (may differ from preferred)
    url = f"http://{HOST}:{port}/"
    print(f"\nFeedback Fetcher running at {url}")
    print("Close the browser tab (or press Ctrl+C) to stop.\n")
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_watchdog, args=(server,), daemon=True).start()
    threading.Thread(target=_check_for_update, daemon=True).start()  # non-blocking
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
        server.shutdown()
    finally:
        server.server_close()  # release the listening socket / port immediately
    print("Server stopped.")


if __name__ == "__main__":
    main()
