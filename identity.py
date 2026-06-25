"""
BalticRadar - auto24.ee parsers (separate platform from autoplius; AllePal/BCG).
listing page -> detail URLs ; detail page -> fields. Country is EE. Writes NOTHING.
Detail fields are read from meta tags (robust in raw HTML) + a VIN probe.
"""
import re
from autoplius_parse import fingerprint  # reuse identity fingerprint

AD_URL = re.compile(r'(?:https://(?:eng|www)\.auto24\.ee)?/vehicles/(\d+)')

def parse_listing_page(text):
    seen = {}
    for m in AD_URL.finditer(text):
        aid = m.group(1)
        seen.setdefault(aid, f"https://eng.auto24.ee/vehicles/{aid}")
    ads = [{"ad_id": "EE"+a, "ad_id_num": a, "url": u} for a, u in seen.items()]
    return {"ads": ads, "count": len(ads)}

def _meta(html, key, attr="property"):
    m = re.search(rf'<meta\s+{attr}=["\']{re.escape(key)}["\']\s+content=["\'](.*?)["\']',
                  html, re.IGNORECASE|re.DOTALL)
    return m.group(1).strip() if m else None

def _digits(s):
    return int(re.sub(r"\D","",s)) if s and re.search(r"\d",s) else None

BODY_WORDS = ("sedan","cabriolet","hatchback","coupe","caravan","limousine","minivan",
              "minibus","pickup","van","suv","estate","wagon","convertible")

def parse_listing(html, source_url=None):
    ogt  = _meta(html,"og:title") or ""                  # "BMW 645 4.4 245kW"
    ogd  = (_meta(html,"og:description") or "").replace("\xa0"," ")  # "2004, petrol, 279 000 km, EUR 13,500 ..."
    desc = (_meta(html,"Description",attr="name") or "").replace("\xa0"," ")
    ogurl= _meta(html,"og:url") or source_url or ""
    img  = _meta(html,"og:image")

    am = re.search(r"/vehicles/(\d+)", ogurl)
    ad_num = am.group(1) if am else None
    ad_id = ("EE"+ad_num) if ad_num else None

    toks = ogt.split()
    make = toks[0] if toks else None
    model = toks[1] if len(toks) > 1 else None

    ym = re.search(r"\b((?:19|20)\d{2})\b", ogd) or re.search(r"\b((?:19|20)\d{2})\b", desc)
    year = int(ym.group(1)) if ym else None

    fuel = None
    fm = re.search(r"\b(petrol|diesel|electric|hybrid|gas|petrol/gas|petrol/electric)\b", ogd, re.I)
    if fm: fuel = fm.group(1).lower()

    mil = re.search(r"([\d ]{3,})\s*km", ogd) or re.search(r"([\d ]{3,})\s*km", desc)
    mileage_km = _digits(mil.group(1)) if mil else None

    pm = re.search(r"EUR\D*?([\d,]+)", ogd)
    price_eur = int(pm.group(1).replace(",","")) if pm else None

    el = re.search(r"(\d\.\d)\b", ogt); engine_l = el.group(1) if el else None
    kwm = re.search(r"(\d+)\s*kW", ogt); power_kw = int(kwm.group(1)) if kwm else None
    ccm = re.search(r"(\d{3,5})\s*cm³", html); engine_cc = int(ccm.group(1)) if ccm else None

    body = None
    bm = re.search(r"\dkW\s+([A-Za-z/ -]+?)(?:\s*\(|\s+\d)", desc)
    if bm: body = bm.group(1).strip()
    if not body:
        for w in BODY_WORDS:
            if re.search(rf"\b{w}\b", desc, re.I): body = w; break

    gear = None
    gm = re.search(r"\b(automatic|manual)\b", html, re.I)
    if gm: gear = gm.group(1).lower()

    vin_prefix = None
    for m in re.finditer(r"VIN", html):
        win = html[m.end():m.end()+60]
        t = re.search(r"\b([A-HJ-NPR-Z0-9]{5,17})\b", win)
        if t and re.search(r"[A-Z]", t.group(1)) and re.search(r"\d", t.group(1)):
            vin_prefix = t.group(1); break

    photos = sorted(set(re.findall(r"https://img\d*\.img-bcg\.eu/[^\s\"')]+\.jpg", html)))
    photos = [p for p in photos if "/h30/" in p]   # per-size hash differs; keep only valid large variants
    if img and img not in photos: photos.insert(0, img)

    return {
        "ad_id":ad_id, "ad_id_num":ad_num, "source_url":ogurl or source_url,
        "make":make, "model":model, "year":year, "engine_cc":engine_cc,
        "fuel":fuel, "gearbox":gear, "body":body, "drivetrain":None,
        "owner_code":None, "vin_prefix":vin_prefix,
        "price_eur":price_eur, "mileage_km":mileage_km,
        "engine_l":engine_l, "power_kw":power_kw,
        "photos":photos, "location":"Estonia", "country":"EE",
    }
