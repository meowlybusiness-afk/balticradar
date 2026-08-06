#!/usr/bin/env python3
"""Merge REPOSTS of the same physical car into one, combining their price history.

ss.lv/autoplius dealers relist the same car as a NEW ad (new ad_id, same photos) at a lower price
instead of editing the old one. Our dedup never merged them (strong_signal_match checks photo_hash,
which is never populated), so the same car shows up several times and its price DROP is scattered
across separate 1-point cars instead of one price-history chart.

Signal for "same physical car": two active cars share a photo IMAGE-ID *and* the same make/model/year.
That is high-precision (identical photos == same car); genuinely-different fleet cars have different
photos and are left alone.

Merge = keep the NEWEST listing (max first_seen = the currently-live ad, usually the lowest/current
price) as canonical, RE-POINT every other listing's price_history rows onto it (so the chart shows the
full 11650 -> 10885 timeline), then hide the older duplicates. Price history is never deleted.

Run DRY (default) -> prints exactly what it would do, writes nothing. MERGE=1 -> executes.
Safety: only merges within same make/model/year + shared photo-id; per-run cap; skips groups >8
(a shared stock photo across many genuinely-different cars would be a false merge).
"""
import os, re, json, urllib.request, urllib.parse, collections

KEY = open("/opt/balticradar/balticradar_key.txt").read().strip()
U   = "https://wrilvoukvyubgpomuoyn.supabase.co/rest/v1"
DRY = os.environ.get("MERGE", "0") != "1"
CAP = int(os.environ.get("MERGE_CAP", "500"))       # max groups to act on per run
MAXGROUP = int(os.environ.get("MERGE_MAXGROUP", "8"))  # skip suspiciously large groups (shared stock photo)
HDR = {"apikey": KEY, "authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

def req(method, path, body=None, prefer=None):
    h = dict(HDR)
    if prefer: h["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{U}/{path}", data=data, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=60) as resp:
        t = resp.read().decode()
        return json.loads(t) if t.strip() else []

def get(path): return req("GET", path)

def photo_id(photos):
    if not photos: return None
    m = re.search(r"(\d{6,})", photos[0].split("/")[-1])
    return m.group(1) if m else None

# --- pass 1: all active cars, tiny select, group by fingerprint (photos come later) ---
print("loading active cars (car_id, fingerprint, mmy)...", flush=True)
rows, off = [], 0
while True:
    chunk = get(f"cars?select=car_id,fingerprint,make,model,year&active=eq.true&order=car_id&offset={off}&limit=1000")
    rows += chunk
    if len(chunk) < 1000: break
    off += 1000
print(f"  {len(rows)} active cars", flush=True)

byfp = collections.defaultdict(list)
for r in rows:
    if r.get("fingerprint"): byfp[r["fingerprint"]].append(r)
ambiguous = [r for g in byfp.values() if len(g) > 1 for r in g]   # only cars in multi-car fingerprint groups
print(f"  {sum(1 for g in byfp.values() if len(g)>1)} fingerprint groups >1, {len(ambiguous)} cars need photos", flush=True)

# --- pass 2: fetch photos + timing only for ambiguous cars ---
detail = {}
ids = [r["car_id"] for r in ambiguous]
for i in range(0, len(ids), 100):
    chunk = ids[i:i+100]
    q = "cars?select=car_id,make,model,year,photos,first_seen,last_seen,last_price&car_id=in.(" + ",".join(chunk) + ")"
    for r in get(q): detail[r["car_id"]] = r

# --- group ambiguous cars by (make,model,year, photo_id) ---
groups = collections.defaultdict(list)
for r in ambiguous:
    d = detail.get(r["car_id"]);  pid = photo_id(d.get("photos")) if d else None
    if not pid: continue
    key = (r.get("make"), r.get("model"), r.get("year"), pid)
    groups[key].append(d)

merge_groups = {k: v for k, v in groups.items() if len(v) > 1}
big = {k: v for k, v in merge_groups.items() if len(v) > MAXGROUP}
act = {k: v for k, v in merge_groups.items() if 2 <= len(v) <= MAXGROUP}
redundant = sum(len(v) - 1 for v in act.values())
print(f"\n== shared-photo repost groups: {len(merge_groups)} | skipped(>{MAXGROUP}): {len(big)} | to merge: {len(act)} | listings to hide: {redundant} ==")

# --- sample: show a few groups with their price timelines ---
for k, v in list(sorted(act.items(), key=lambda kv: -len(kv[1])))[:8]:
    v = sorted(v, key=lambda x: x.get("first_seen") or "")
    canon = max(v, key=lambda x: x.get("first_seen") or "")
    tl = " -> ".join(f"E{x['last_price']}@{(x.get('first_seen') or '')[:10]}" for x in v)
    print(f"  {k[0]} {k[1]} {k[2]} (photo {k[3]}): {tl}   [keep {canon['car_id']}]")

if DRY:
    print(f"\nDRY RUN — nothing written. Set MERGE=1 to execute (cap {CAP} groups/run).")
    raise SystemExit(0)

# --- execute: re-point price_history to canonical, hide the older reposts ---
done = 0
for k, v in list(act.items())[:CAP]:
    v = sorted(v, key=lambda x: x.get("first_seen") or "")
    canon = max(v, key=lambda x: x.get("first_seen") or "")
    cid = canon["car_id"]
    for other in v:
        if other["car_id"] == cid: continue
        oid = other["car_id"]
        try:
            req("PATCH", f"price_history?car_id=eq.{oid}", {"car_id": cid})           # move history to canonical
            req("PATCH", f"cars?car_id=eq.{oid}", {"active": False})                  # hide the repost
            req("PATCH", f"ads?car_id=eq.{oid}", {"active": False})
        except Exception as e:
            print("  merge err", oid, repr(e))
    done += 1
print(f"\nMERGED {done} groups, hid {sum(len(v)-1 for v in list(act.items())[:CAP] and [x[1] for x in list(act.items())[:CAP]])} reposts.")
