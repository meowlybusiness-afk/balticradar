"""
BalticRadar - multi-source collector.
collect() is source-agnostic: you inject the listing parser + detail parser.
Shared identity/dedup/price-history logic lives here. Writes to a Store
(swap for Supabase in prod) and exports listings.json for the frontend.
"""
import json, time, datetime
from listing_parse import parse_listing_page as _ap_list, page_url
from autoplius_parse import parse_listing as _ap_detail, fingerprint
from identity import decide

DEFAULT_POLL_SECONDS = 150   # poll each SOURCE politely; frontend refreshes fast off our DB

class Store:
    def __init__(self):
        self.ads = {}; self.cars = {}; self.price_history = []; self.review = []
    def newest(self, limit=200):
        return sorted(self.cars.values(), key=lambda c: c.get("first_seen",""), reverse=True)[:limit]

def _now():
    return datetime.datetime.utcnow().isoformat(timespec="seconds")+"Z"

def _ingest(fields, store, source, default_country, stats):
    fields["source"] = source
    fields.setdefault("country", default_country)   # parser's country wins if present
    d, cid, why = decide(fields, {"ads":store.ads, "cars":store.cars})
    if d == "NEW_CAR":
        cid = f"car_{fields['ad_id']}"
        fields.update({"car_id":cid,"active":True,"last_price":fields.get("price_eur"),
                       "first_seen":_now(),"fp":fingerprint(fields)[0]})
        store.cars[cid] = fields; store.ads[fields["ad_id"]] = cid
        store.price_history.append({"car_id":cid,"ts":_now(),
            "price":fields.get("price_eur"),"mileage":fields.get("mileage_km")})
        stats["new"] += 1
    elif d == "REPOST":
        store.ads[fields["ad_id"]] = cid; car = store.cars[cid]; car["active"] = True
        if fields.get("price_eur") != car.get("last_price") or fields.get("mileage_km") != car.get("mileage_km"):
            store.price_history.append({"car_id":cid,"ts":_now(),
                "price":fields.get("price_eur"),"mileage":fields.get("mileage_km")})
        car["last_price"] = fields.get("price_eur"); stats["repost"] += 1
    else:
        store.review.append({"ad_id":fields["ad_id"],"why":why}); stats["review"] += 1

def collect(fetch_fn, listing_urls, store, parse_list, parse_detail,
            source, country=None, max_details=None):
    stats = {"new":0,"repost":0,"same":0,"review":0,"seen":0}; details = 0
    for lu in listing_urls:
        for ad in parse_list(fetch_fn(lu))["ads"]:
            stats["seen"] += 1
            if ad["ad_id"] in store.ads: stats["same"] += 1; continue
            if max_details is not None and details >= max_details: break
            details += 1
            fields = parse_detail(fetch_fn(ad["url"]), source_url=ad["url"])
            fields["ad_id"] = ad["ad_id"]
            _ingest(fields, store, source, country or fields.get("country") or "LT", stats)
    return stats

def run_once(fetch_fn, base_listing_url, store, max_pages=1, lang="lv",
             country="LT", source="autoplius", max_details=None):
    urls = [base_listing_url] + [page_url(base_listing_url, n) for n in range(2, max_pages+1)]
    return collect(fetch_fn, urls, store, lambda p: _ap_list(p, lang=lang),
                   _ap_detail, source, country=None, max_details=max_details)


def collect_rows(rows, store, source, country):
    """For list-only sources (e.g. ss.lv) where the listing row already has all fields."""
    stats = {"new":0,"repost":0,"same":0,"review":0,"seen":0}
    for f in rows:
        stats["seen"] += 1
        if f.get("ad_id") in store.ads: stats["same"] += 1; continue
        _ingest(f, store, source, country, stats)
    return stats

def export_json(store, path):
    data = {"updated":_now(), "cars":store.newest()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path
