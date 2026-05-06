#!/usr/bin/env python3
"""
auction-watcher/api/server.py

Lightweight HTTP API server that:
  - Reads auction listings from the shared RennAuktion inventory.db
  - Manages a watchlist table (star/unstar)
  - Serves JSON endpoints to the PWA frontend
  - Runs tight-loop polling for watched cars (60s in last 5h of auction)
  - Fires push alerts via existing push_server infrastructure

Usage:
    python3 api/server.py
    python3 api/server.py --port 7474
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT   = Path(__file__).parent.parent.resolve()
TRACKER_ROOT   = Path.home() / "porsche-tracker"
DB_PATH        = TRACKER_ROOT / "data" / "inventory.db"
WATCHLIST_DB   = PROJECT_ROOT / "data" / "watchlist.db"
LOG_DIR        = PROJECT_ROOT / "logs"
PUSH_SUBS_PATH = TRACKER_ROOT / "data" / "push_subscriptions.json"
SEEN_PATH      = PROJECT_ROOT / "data" / "seen_alerts_watchlist.json"

LOG_DIR.mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "api_server.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

AUCTION_DEALERS = frozenset({"Bring a Trailer", "Cars and Bids", "pcarmarket"})

ALLOWED_ORIGINS = {
    "https://ocx11.github.io",
    "http://localhost:8080",
    "http://localhost:3000",
    "null",
}

PORT = 7474


# ── Watchlist DB ──────────────────────────────────────────────────────────────

def init_watchlist_db():
    conn = sqlite3.connect(WATCHLIST_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id     INTEGER NOT NULL UNIQUE,
            dealer         TEXT NOT NULL,
            listing_url    TEXT NOT NULL,
            your_max_bid   INTEGER,
            notes          TEXT,
            starred_at     TEXT DEFAULT (datetime('now')),
            last_bid_price INTEGER,
            last_checked   TEXT,
            alert_outbid   INTEGER DEFAULT 1,
            alert_ending   INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()
    log.info("Watchlist DB ready at %s", WATCHLIST_DB)

def get_watchlist_conn():
    conn = sqlite3.connect(WATCHLIST_DB)
    conn.row_factory = sqlite3.Row
    return conn

def get_inventory_conn():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"inventory.db not found at {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

# ── Helpers ───────────────────────────────────────────────────────────────────

def _platform_key(dealer: str) -> str:
    return {"Bring a Trailer": "bat", "Cars and Bids": "cnb", "pcarmarket": "pcm"}.get(dealer, "other")

def _bid_status(current_price, your_max):
    if your_max is None or current_price is None:
        return "watching"
    return "winning" if current_price <= your_max else "outbid"

def _seconds_remaining(auction_ends_at):
    if not auction_ends_at:
        return None
    try:
        ends = datetime.fromisoformat(auction_ends_at.replace("Z", "+00:00"))
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
        return max(0, int((ends - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        return None

def _listing_to_dict(row, watchlist_row=None):
    d = dict(row)
    d["platform"] = _platform_key(d.get("dealer", ""))
    d["saved"] = watchlist_row is not None
    if watchlist_row:
        wr = dict(watchlist_row)
        d["your_max_bid"]   = wr.get("your_max_bid")
        d["notes"]          = wr.get("notes")
        d["starred_at"]     = wr.get("starred_at")
        d["last_bid_price"] = wr.get("last_bid_price")
        d["alert_outbid"]   = bool(wr.get("alert_outbid", 1))
        d["alert_ending"]   = bool(wr.get("alert_ending", 1))
        d["bid_status"]     = _bid_status(d.get("price"), wr.get("your_max_bid"))
    return d

# ── Feed ──────────────────────────────────────────────────────────────────────

def get_feed(platform_filter=None, page=1, per_page=50):
    try:
        inv = get_inventory_conn()
        wl  = get_watchlist_conn()
        wl_rows = {r["listing_id"]: r for r in wl.execute("SELECT * FROM watchlist").fetchall()}
        wl.close()
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        dealers = list(AUCTION_DEALERS)
        if platform_filter in ("bat", "cnb", "pcm"):
            dealers = [{"bat": "Bring a Trailer", "cnb": "Cars and Bids", "pcm": "pcarmarket"}[platform_filter]]
        ph = ",".join("?" * len(dealers))
        rows = inv.execute(f"""
            SELECT id, dealer, year, make, model, trim, price, mileage,
                   listing_url, image_url, image_url_cdn, auction_ends_at,
                   color, transmission, body_style, created_at, status
            FROM listings
            WHERE dealer IN ({ph}) AND status='active'
              AND (auction_ends_at IS NULL OR auction_ends_at > ?)
            ORDER BY CASE WHEN auction_ends_at IS NOT NULL THEN 0 ELSE 1 END,
                     auction_ends_at ASC, created_at DESC
            LIMIT ? OFFSET ?
        """, (*dealers, now_utc, per_page, (page-1)*per_page)).fetchall()
        total = inv.execute(f"SELECT COUNT(*) FROM listings WHERE dealer IN ({ph}) AND status='active' AND (auction_ends_at IS NULL OR auction_ends_at > ?)", (*dealers, now_utc)).fetchone()[0]
        inv.close()
        listings = []
        for row in rows:
            d = _listing_to_dict(row, wl_rows.get(row["id"]))
            d["seconds_remaining"] = _seconds_remaining(d.get("auction_ends_at"))
            listings.append(d)
        return {"listings": listings, "total": total, "page": page, "per_page": per_page}
    except FileNotFoundError as e:
        log.error("inventory.db not found: %s", e)
        return {"listings": [], "total": 0, "error": str(e)}

def get_watchlist():
    try:
        wl = get_watchlist_conn()
        wl_rows = wl.execute("SELECT * FROM watchlist ORDER BY starred_at DESC").fetchall()
        wl.close()
        if not wl_rows:
            return {"listings": []}
        inv = get_inventory_conn()
        ids = [r["listing_id"] for r in wl_rows]
        ph = ",".join("?" * len(ids))
        inv_rows = {r["id"]: r for r in inv.execute(
            f"SELECT id, dealer, year, make, model, trim, price, mileage, listing_url, image_url, image_url_cdn, auction_ends_at, color, transmission, body_style, created_at, status FROM listings WHERE id IN ({ph})", ids).fetchall()}
        inv.close()
        wl_map = {r["listing_id"]: r for r in wl_rows}
        listings = []
        for lid in ids:
            row = inv_rows.get(lid)
            if row is None: continue
            d = _listing_to_dict(row, wl_map[lid])
            d["seconds_remaining"] = _seconds_remaining(d.get("auction_ends_at"))
            listings.append(d)
        listings.sort(key=lambda x: (x.get("seconds_remaining") is None, x.get("seconds_remaining", 9999999)))
        return {"listings": listings}
    except FileNotFoundError as e:
        return {"listings": [], "error": str(e)}

def star_listing(listing_id, your_max_bid=None, notes=None):
    try:
        inv = get_inventory_conn()
        row = inv.execute("SELECT id, dealer, listing_url, price FROM listings WHERE id=?", (listing_id,)).fetchone()
        inv.close()
        if row is None: return {"ok": False, "error": "listing not found"}
        wl = get_watchlist_conn()
        wl.execute("INSERT OR IGNORE INTO watchlist (listing_id, dealer, listing_url, your_max_bid, notes, last_bid_price) VALUES (?,?,?,?,?,?)",
                   (listing_id, row["dealer"], row["listing_url"], your_max_bid, notes, row["price"]))
        wl.commit(); wl.close()
        log.info("Starred listing %d", listing_id)
        return {"ok": True, "listing_id": listing_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def unstar_listing(listing_id):
    wl = get_watchlist_conn()
    wl.execute("DELETE FROM watchlist WHERE listing_id=?", (listing_id,))
    wl.commit(); wl.close()
    log.info("Unstarred listing %d", listing_id)
    return {"ok": True}

def update_watchlist_entry(listing_id, your_max_bid=None, notes=None, alert_outbid=None, alert_ending=None):
    wl = get_watchlist_conn()
    fields, vals = [], []
    if your_max_bid  is not None: fields.append("your_max_bid=?");  vals.append(your_max_bid)
    if notes         is not None: fields.append("notes=?");         vals.append(notes)
    if alert_outbid  is not None: fields.append("alert_outbid=?");  vals.append(int(alert_outbid))
    if alert_ending  is not None: fields.append("alert_ending=?");  vals.append(int(alert_ending))
    if fields:
        wl.execute(f"UPDATE watchlist SET {', '.join(fields)} WHERE listing_id=?", (*vals, listing_id))
        wl.commit()
    wl.close()
    return {"ok": True}

# ── Poller ────────────────────────────────────────────────────────────────────

def _load_seen():
    if SEEN_PATH.exists():
        try: return set(json.loads(SEEN_PATH.read_text()))
        except: pass
    return set()

def _save_seen(seen):
    SEEN_PATH.write_text(json.dumps(list(seen)))

def _send_push(title, body, url=""):
    try:
        import subprocess
        push_script = TRACKER_ROOT / "rennmarkt" / "notify_push.py"
        if not push_script.exists():
            push_script = TRACKER_ROOT / "notify_push.py"
        if not push_script.exists():
            log.warning("push script not found"); return
        payload = json.dumps({"title": title, "body": body, "url": url})
        subprocess.run(["python3", str(push_script), "--raw", payload], capture_output=True, timeout=10)
        log.info("Push sent: %s — %s", title, body)
    except Exception as e:
        log.warning("Push failed: %s", e)

def watchlist_poller():
    log.info("Watchlist poller started")
    seen = _load_seen()
    while True:
        try:
            wl = get_watchlist_conn()
            wl_rows = wl.execute("SELECT * FROM watchlist").fetchall()
            wl.close()
            if not wl_rows:
                time.sleep(60); continue
            inv = get_inventory_conn()
            ids = [r["listing_id"] for r in wl_rows]
            ph = ",".join("?" * len(ids))
            inv_rows = {r["id"]: r for r in inv.execute(
                f"SELECT id, dealer, year, model, trim, price, listing_url, auction_ends_at, status FROM listings WHERE id IN ({ph})", ids).fetchall()}
            inv.close()
            wl_map = {r["listing_id"]: r for r in wl_rows}
            for listing_id, inv_row in inv_rows.items():
                wl_row      = wl_map[listing_id]
                cur_price   = inv_row["price"]
                your_max    = wl_row["your_max_bid"]
                last_price  = wl_row["last_bid_price"]
                secs        = _seconds_remaining(inv_row["auction_ends_at"])
                label       = f"{inv_row['year']} {inv_row['model']} {inv_row['trim'] or ''}".strip()
                url         = inv_row["listing_url"]
                if cur_price != last_price:
                    c = get_watchlist_conn()
                    c.execute("UPDATE watchlist SET last_bid_price=?, last_checked=datetime('now') WHERE listing_id=?", (cur_price, listing_id))
                    c.commit(); c.close()
                if your_max and cur_price and cur_price > your_max:
                    key = f"outbid:{listing_id}:{cur_price}"
                    if key not in seen and wl_row["alert_outbid"]:
                        _send_push(f"Outbid — {label}", f"Current: ${cur_price:,}  Your max: ${your_max:,}  (+${cur_price-your_max:,})", url)
                        seen.add(key); _save_seen(seen)
                if secs is not None:
                    for threshold, lbl in [(3600, "1 hour"), (900, "15 min")]:
                        key = f"ending:{listing_id}:{lbl}"
                        if secs <= threshold and key not in seen and wl_row["alert_ending"]:
                            status = _bid_status(cur_price, your_max)
                            st_str = "Winning" if status == "winning" else ("Outbid" if status == "outbid" else "Watching")
                            _send_push(f"Ending in {lbl} — {label}", f"{st_str}  Current: ${cur_price:,}" if cur_price else st_str, url)
                            seen.add(key); _save_seen(seen)
                if inv_row["status"] == "sold":
                    key = f"won:{listing_id}"
                    if key not in seen and your_max and cur_price and cur_price <= your_max:
                        _send_push(f"You won — {label}", f"Hammer: ${cur_price:,}", url)
                        seen.add(key); _save_seen(seen)
            has_urgent = any((_seconds_remaining(inv_rows[r["listing_id"]]["auction_ends_at"]) or 999999) < 18000
                             for r in wl_rows if r["listing_id"] in inv_rows)
            time.sleep(60 if has_urgent else 300)
        except Exception as e:
            log.error("Poller error: %s", e); time.sleep(60)

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _cors(self):
        origin = self.headers.get("Origin", "*")
        self.send_header("Access-Control-Allow-Origin", origin if origin in ALLOWED_ORIGINS else "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self._cors(); self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        path = parsed.path.rstrip("/")
        if path == "/feed":
            self._json(get_feed(qs.get("platform",[None])[0], int(qs.get("page",[1])[0])))
        elif path == "/watchlist":
            self._json(get_watchlist())
        elif path == "/health":
            self._json({"ok": True, "db": str(DB_PATH), "watchlist_db": str(WATCHLIST_DB)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        lid = body.get("listing_id")
        if path == "/watchlist/star":
            self._json(star_listing(int(lid), body.get("your_max_bid"), body.get("notes")) if lid else {"ok":False,"error":"listing_id required"})
        elif path == "/watchlist/unstar":
            self._json(unstar_listing(int(lid)) if lid else {"ok":False,"error":"listing_id required"})
        elif path == "/watchlist/update":
            self._json(update_watchlist_entry(int(lid), body.get("your_max_bid"), body.get("notes"), body.get("alert_outbid"), body.get("alert_ending")) if lid else {"ok":False,"error":"listing_id required"})
        else:
            self._json({"error": "not found"}, 404)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    init_watchlist_db()
    threading.Thread(target=watchlist_poller, daemon=True, name="poller").start()
    server = HTTPServer(("0.0.0.0", args.port), Handler)
    log.info("Auction Watcher API on http://127.0.0.1:%d", args.port)
    try: server.serve_forever()
    except KeyboardInterrupt: log.info("Shutting down")

if __name__ == "__main__":
    main()
