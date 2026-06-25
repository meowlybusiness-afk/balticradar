"""
BalticRadar - FULL catalogue collector (all cars, all 3 sources).
Storage:
  * If SUPABASE_URL + SUPABASE_KEY are set -> writes to Supabase (scales to 100k+).
  * Otherwise -> capped preview to listings.json (a few hundred) so you can see it.

ss.lv is card-level (fast, no per-car page). autoplius/auto24 discover ad ids from
listing pages, then fetch detail pages only for ad ids NOT already stored
(incremental backfill - run repeatedly / on a schedule to fill the catalogue).
"""
import os, time, requests
from listing_parse import parse_listing_page as ap_list, page_url
from autoplius_parse import parse_listing as ap_detail
from auto24_parse import parse_listing_page as a24_list, parse_listing as a24_detail
import ss_parse

USE_SUPABASE = bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"))

# ---- politeness / scope knobs ----
AP_PAGES  = int(os.environ.get("AP_PAGES", 3))     # autoplius listing pages per run
A24_PAGES = int(os.environ.get("A24_PAGES", 1))
SS_PAGES  = int(os.environ.get("SS_PAGES", 5))     # ss.lv pages (card-level, cheap)
DETAIL_CAP = int(os.environ.get("DETAIL_CAP", 200))  # max NEW detail fetches per run
SS_DETAIL = int(os.environ.get("SS_DETAIL", 1))   # 1=fetch ss.lv galleries (slower), 0=fast 1-photo
PAUSE = 0.6

AP_BASE  = "https://lv.autoplius.lt/sludinajumi/lietotas-automasinas?order_by=1&order_direction=DESC"
A24_BASE = "https://eng.auto24.ee/kasutatud/nimekiri.php?ad=7"
SS_BASE  = "https://www.ss.lv/lv/transport/cars/today/"   # verified working; + page{n}.html

SS_H = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36","Accept-Language":"lv,en;q=0.9"}

# ---- storage backends ----
if USE_SUPABASE:
    import supabase_store as store
    def has_ad(aid): return store.has_ad(aid)
    def save(fields): return store.ingest(fields)
    print("STORAGE: Supabase")
else:
    from collector import Store, _ingest, export_json
    _mem = Store()
    def has_ad(aid): return aid in _mem.ads
    def save(fields):
        st={"new":0,"repost":0,"same":0,"review":0,"seen":0}
        _ingest(fields,_mem,fields.get("source"),fields.get("country") or "LT",st)
        return next(k.upper() for k,v in st.items() if v and k!="seen")
    print("STORAGE: listings.json preview (set SUPABASE_* env vars for full catalogue)")

def ss_page_url(n): return SS_BASE if n==1 else f"{SS_BASE}page{n}.html"

def run():
    seen=new=0
    # ---- ss.lv: card-level, no detail fetch ----
    for n in range(1, SS_PAGES+1):
        try:
            r=_ss.get(ss_page_url(n),timeout=25); html=r.text
        except Exception as e: print("ss.lv page",n,"ERR",e); break
        rows=ss_parse.parse_listing_page(html)["ads"]
        print(f"ss.lv page {n}: HTTP {r.status_code}, {len(rows)} rows, {len(html)} bytes  url={ss_page_url(n)}")
        if not rows: break
        for f in rows:
            seen+=1
            if has_ad(f["ad_id"]): continue
            f["source"]="ss.lv"
            if SS_DETAIL:
                try:
                    d=_ss.get(f["source_url"],timeout=20).text
                    ph=ss_parse.detail_photos(d)
                    if ph: f["photos"]=ph                  # full gallery (8-9 photos)
                    ml=ss_parse.detail_mileage(d)
                    if ml: f["mileage_km"]=ml
                    de=ss_parse.detail_description(d)
                    if de: f["description"]=de
                except Exception: pass
            try: save(f); new+=1
            except Exception as e: print(f"  skip {f['ad_id']}: {e!r}")
        time.sleep(PAUSE)

    # ---- autoplius + auto24: discover ids from listing, detail-fetch NEW ones ----
    from fetcher_playwright import browser_session, make_fetch
    details=0
    with browser_session() as page:
        fetch=make_fetch(page)
        def crawl(name, base, pages, parse_list, parse_detail, country):
            nonlocal seen,new,details
            for n in range(1,pages+1):
                url = base if n==1 else page_url(base,n)
                for ad in parse_list(fetch(url))["ads"]:
                    seen+=1
                    if has_ad(ad["ad_id"]): continue
                    if details>=DETAIL_CAP: print(name,"hit DETAIL_CAP"); return
                    details+=1
                    f=parse_detail(fetch(ad["url"]), source_url=ad["url"])
                    f["ad_id"]=ad["ad_id"]; f["source"]=name
                    if country: f.setdefault("country",country)
                    try: save(f); new+=1
                    except Exception as e: print(f"  skip {ad['ad_id']}: {e!r}")
                print(f"{name} page {n}: new total {new}, details {details}")
                time.sleep(PAUSE)
        crawl("autoplius", AP_BASE, AP_PAGES, lambda p: ap_list(p,lang="lv"), ap_detail, None)
        crawl("auto24", A24_BASE, A24_PAGES, a24_list, a24_detail, "EE")

    print(f"\nDONE. seen={seen}, new stored={new}")
    if not USE_SUPABASE:
        export_json(_mem,"listings.json"); print("wrote listings.json (preview)")

if __name__=="__main__":
    loop=int(os.environ.get("LOOP_MINUTES",0))
    if loop>0:
        import time as _t
        print(f"LOOP MODE: collecting every {loop} min. Leave this window open (or minimize). Ctrl+C to stop.")
        n=0
        while True:
            n+=1; print(f"\n===== cycle {n} =====")
            try: run()
            except Exception as e: print("cycle error:", repr(e))
            print(f"--- sleeping {loop} min ---")
            _t.sleep(loop*60)
    else:
        run()
