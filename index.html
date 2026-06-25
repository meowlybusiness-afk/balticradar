"""
BalticRadar - ss.lv (Latvia) parser. Works from the LISTING page alone (one
request for ~30 cars). make/model come from the ad URL; year/engine/price from
the row cells; thumbnail from the row image. Country = LV. Plain requests.
"""
import re
from bs4 import BeautifulSoup

BASE = "https://www.ss.lv"

def fuel_from_engine(tok):
    if not tok: return None
    if tok.upper() == "E": return "Elektrība"
    if tok.upper().endswith("D"): return "Dīzelis"
    if "/" in tok or "gāze" in tok.lower(): return "Benzīns / gāze"
    return "Benzīns"

def fields_from_row(ad_id, url, title, cells, photo=None):
    FIX = {"bmw":"BMW","mg":"MG","ds":"DS","gmc":"GMC","seat":"SEAT","byd":"BYD",
           "vaz":"VAZ","gaz":"GAZ","dfsk":"DFSK","gwm":"GWM","swm":"SWM"}
    mm = re.search(r"/cars/([^/]+)/([^/]+)/", url or "")
    make = (FIX.get(mm.group(1).lower(), mm.group(1).replace("-", " ").title())) if mm else None
    model = mm.group(2).replace("-", " ").title() if mm else None

    year = engine_tok = price = None
    for c in cells:
        c = c.strip()
        if not year and re.fullmatch(r"(19|20)\d{2}", c): year = int(c)
        elif not engine_tok and (re.fullmatch(r"\d\.\d[A-Za-z]?", c) or c.upper() == "E"): engine_tok = c
        elif "€" in c: price = int(re.sub(r"[^\d]", "", c) or 0) or None

    engine_l = None
    em = re.match(r"(\d\.\d)", engine_tok or "")
    if em: engine_l = em.group(1)

    return {
        "ad_id": ad_id, "source_url": url, "country": "LV",
        "make": make, "model": model, "year": year,
        "engine_l": engine_l, "fuel": fuel_from_engine(engine_tok),
        "gearbox": None, "body": None, "engine_cc": None, "drivetrain": None,
        "owner_code": None, "vin_prefix": None,
        "price_eur": price, "mileage_km": None,
        "photos": [photo] if photo else [], "title": title,
    }

def parse_listing_page(html):
    soup = BeautifulSoup(html, "html.parser")
    ads = []
    for row in soup.select("tr[id^='tr_']"):
        ad_id = "LV" + row.get("id", "").replace("tr_", "")
        link = row.select_one("a.am")
        if not link: continue
        href = link.get("href")
        url = (BASE + href) if href and href.startswith("/") else href
        cells = [c.get_text(strip=True) for c in row.select("td")]
        img = row.select_one("img")
        photo = None
        if img:
            photo = img.get("src") or img.get("data-original")
            if photo and photo.startswith("//"): photo = "https:" + photo
            if photo:  # upgrade thumbnail to full size
                photo = re.sub(r"\.(t|th|th2|th3|sm|400|1200|2000)\.jpg$", ".800.jpg", photo)
        ads.append(fields_from_row(ad_id, url, link.get_text(strip=True), cells, photo))
    return {"ads": ads, "count": len(ads)}


# ---- ss.lv DETAIL page enrichment (full gallery + mileage + description) ----
def detail_photos(html):
    urls = re.findall(r"https://i\.ss\.(?:com|lv)/gallery/[^\s\"')]+?\.800\.jpg", html)
    seen=[]; [seen.append(u) for u in urls if u not in seen]
    return seen

def detail_mileage(html):
    m = re.search(r"Nobraukums[^\d]{0,40}?([\d][\d\s ]{2,}\d)", html)
    return int(re.sub(r"\D","",m.group(1))) if m else None

def detail_description(html):
    m = re.search(r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', html, re.I|re.S)
    if not m: return None
    d = m.group(1).strip()
    # drop the boilerplate "Sludinājumi. ... Cena X €." prefix if present
    d = re.sub(r"^Sludinājumi\..*?€\.?\s*", "", d)
    return d or None
