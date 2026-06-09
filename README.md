# Review Scraper

Pull Apple App Store & Google Play customer reviews into a CSV — runs locally on your Mac.

## Getting started

1. Double-click **Review Scraper.zip** to unzip it.
2. Drag the resulting **Review Scraper.app** into your Applications folder.
3. The first time you open the app on a new Mac, **right-click the app and select Open**, then click **Open** again in the box that appears. (This one-time step is needed because the app is from outside the App Store and unsigned. After the first launch, you can simply double-click it from your Applications folder like any other app.)
4. A page will open in your web browser (this is the app's user interface).
5. Enter one or more app IDs (one per line), pick your star ratings and any other options, then click **Scrape reviews**. (Or use **Find an app by name** to search for an app and click a result to fill in its ID.)
6. Results will appear in a table on the web page, and a spreadsheet (`.csv`) is also saved to **Documents/Review Scraper Output**.
7. When you're done, just close the browser tab, and the app will stop itself automatically.

> If your Mac says the app "can't be opened," make sure you followed step 3: **right-click → Open → Open** (in the pop-up that appears).

## How it works

When you open the app, it starts a local web server. That server listens only on your machine's private "localhost" address (127.0.0.1), so it isn't exposed to the internet or to anyone else on your network. The app then opens that local page in your default browser.

At launch, it also checks whether a newer version is available by looking at a JSON file hosted on GitHub. The file contains a version number, and the app never downloads or installs anything on its own. If there's an update, you'll see a banner with a link to get the latest version; otherwise you won't notice a thing.

When you search for an app by name, the app checks Apple's App Store and Google Play for matching apps so it can fill in the right ID for you. And when you enter app IDs and click Scrape reviews, it makes secure connections out to those same stores to download their publicly available customer reviews. It doesn't log into any service or send any data.

The app displays the results in a table on the page and saves them as a CSV (spreadsheet) file in your Documents/Review Scraper Output folder. (The Show in Finder button opens that folder for you.) The app runs as a self-contained program. It doesn't copy files into your system folders, add background services or startup items, or require any permissions. You shouldn't even need your Mac's password.

Once you close the tab in your browser, the app will shut itself down automatically; it never continues running in the background.

---

## Technical reference

A self-contained desktop tool: a Python backend serves a single-page HTML/JS UI to the local browser, which acts as the front end.

### Components
- **`app.py`** — the local server and the entire UI (HTML/CSS/JS embedded as a string). Uses only the Python standard library (`http.server.ThreadingHTTPServer`), bound to `127.0.0.1` on an auto-selected port (tries 8765/8766/8767, then any free port).
- **`review_scraper.py`** — the scraping logic, importable as a module and also runnable as a standalone CLI.
- **`requirements.txt`** — runtime dependencies.
- **`build_mac_app.sh`** — packages everything into `dist/Review Scraper.app` (and a `.zip`) with PyInstaller.
- **`Review Scraper.command`** — a double-click launcher for running from source (uses `.venv` if present).

### HTTP endpoints (all loopback-only)
| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Serves the page |
| `/search?q=&country=` | GET | Find apps by name across both stores |
| `/run` | POST | Scrape reviews for the submitted app IDs |
| `/reveal?file=` | GET | Open the saved CSV in Finder |
| `/update` | GET | Returns the result of the launch-time version check |
| `/ping` | GET | Heartbeat used for auto-shutdown |

### How the pieces work
- **Scraping** — Apple via the `app-store-web-scraper` package; Google via `google-play-scraper`. Results from one or more apps are combined and written to one CSV in `~/Documents/Review Scraper Output` (when bundled) or the app folder (when run from source).
- **App search** — Apple's public iTunes Search API + `google_play_scraper.search`, merged and ranked by how well each title matches the query so the best match surfaces first regardless of store.
- **Invalid-ID detection** — if a store returns no reviews, the app verifies the app exists (iTunes lookup API / `google_play_scraper.app`) and flags genuinely invalid IDs without false-flagging valid apps that simply have no written reviews.
- **Lifecycle** — the open page sends a heartbeat to `/ping`; a watchdog shuts the server down a few seconds after the heartbeat stops (tab closed), and the listening socket is released on exit. No lingering process or port.
- **Update check** — at launch, a background thread fetches a hosted `version.json`; if its version is newer than the bundled `VERSION`, the page shows a download banner. Notify-only — no code is ever downloaded or executed. Fully fail-silent (offline/missing/malformed → no banner, no error).

### Security
- **Loopback-only** bind (`127.0.0.1`) — never network-exposed.
- **CSRF / DNS-rebinding guard** — every request must have a loopback `Host`, and any `Origin` header must be loopback; otherwise `403`. Stops a malicious web page in the browser from driving the local server.
- **TLS verification stays on**, backed by the `certifi` CA bundle (works on bare/frozen macOS without disabling certificate checks).
- **CSV formula-injection** — cells beginning with `= + - @` (or control chars) are prefixed with `'` so spreadsheets treat them as text.
- **Output rendering** — all third-party text (reviews, titles, errors) is HTML-escaped before display.
- **Path-traversal guard** on `/reveal` (basename-only, must be an existing `.csv` in the output folder; opened via `subprocess` with no shell).
- **Input clamping** — `max_reviews` is bounded.
- **Dependencies pinned** to patched versions; run `pip-audit` to re-check.

### Dependencies
`app-store-web-scraper`, `google-play-scraper`, plus pinned transitive patches (`idna`, `urllib3`, `certifi`). See `requirements.txt`.

### Releasing an update
1. Make your changes and **bump `VERSION`** in `app.py`.
2. Run `./build_mac_app.sh` → produces `dist/Review Scraper.app` and `dist/Review Scraper.zip`.
3. Upload the new `.zip` (e.g., as a GitHub Release asset).
4. Update the hosted **`version.json`** to point at it (see below). Existing installs show the update banner on their next launch.

### `version.json` format
Host this file at the URL set in `DEFAULT_UPDATE_URL` in `app.py`, which is currently:
`https://raw.githubusercontent.com/nallena2u/feedback-fetcher/main/version.json`
(i.e., commit `version.json` to the root of the `main` branch of the `feedback-fetcher` repo).

```json
{
  "version": "1.1.0",
  "url": "https://github.com/nallena2u/feedback-fetcher/releases/download/v1.1.0/Review.Scraper.zip",
  "notes": "What's new in this version."
}
```

| Field | Required | Notes |
|---|---|---|
| `version` | yes | Latest version as dotted numbers (`major.minor.patch`). The banner shows only if this is **greater** than the app's `VERSION`. |
| `url` | yes | Download link for the new `.zip`. The banner only appears if this is present. |
| `notes` | no | Short "what's new" line shown in the banner. |
