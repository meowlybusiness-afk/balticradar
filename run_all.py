"""BalticRadar - autoplius.lt detail-page parser (Stage 1 PARSE). Writes NOTHING."""
import re, hashlib

LABELS = {
    "reg_date":"Pirmā reģistrācija","mileage":"Nobraukums","engine":"Dzinējs",
    "fuel":"Degvielas tips","body":"Virsbūves tips","doors":"Durvju skaits",
    "drivetrain":"Piedziņa","gearbox":"Ātrumkārbas tips","color":"Krāsa",
    "inspection_until":"Tehniskā skate līdz","wheel_size":"Riteņu izmērs",
    "reg_country":"Pirmās reģistrācijas valsts","owner_code":"Īpašnieka deklarācijas kods",
}

def _meta(html, key, attr="name"):
    m = re.search(rf'<meta\s+{attr}=["\']{re.escape(key)}["\']\s+content=["\'](.*?)["\']\s*/?>',
                  html, re.IGNORECASE|re.DOTALL)
    return m.group(1).strip() if m else None

def _field_from_keywords(keywords, label):
    if not keywords:
        return None
    m = re.search(rf'{re.escape(label)}\s+(.*?)(?:,\s*[A-ZĀČĒĢĪĶĻŅŠŪŽ][a-zāčēģīķļņšūž]+\s|$|,\s*[A-Z][a-z])', keywords)
    return m.group(1).strip().rstrip(",").strip() if m else None

def _digits(s):
    return int(re.sub(r"\D","",s)) if s and re.search(r"\d",s) else None

def parse_listing(html, source_url=None):
    keywords = _meta(html,"keywords")
    title = _meta(html,"og:title",attr="property") or _meta(html,"title")

    m = re.search(r"\bA(\d{6,})\b", title or keywords or "")
    ad_id_display = ("A"+m.group(1)) if m else None
    ad_id_num = m.group(1) if m else None

    # make/model from the stable keywords segment (never contains the ad_id); fallback title
    make_model = ((keywords or title or "").split(",")[0]).strip() or None
    make = model = None
    if make_model:
        p = make_model.split(" ",1); make=p[0]; model=p[1] if len(p)>1 else None

    reg_date = _field_from_keywords(keywords, LABELS["reg_date"])
    year = int(reg_date[:4]) if reg_date and reg_date[:4].isdigit() else None

    engine_raw = _field_from_keywords(keywords, LABELS["engine"])
    em = re.search(r"(\d{3,5})", engine_raw) if engine_raw else None
    engine_cc = int(em.group(1)) if em else None

    mileage_km = _digits(_field_from_keywords(keywords, LABELS["mileage"]))

    price_eur = None
    pm = re.search(r"([\d][\d\s ]{2,})\s*€", html)
    if pm: price_eur = _digits(pm.group(1))

    vin_prefix = None
    for cand in re.findall(r"\bVIN\b[^<]{0,40}", html, re.IGNORECASE):
        tok = re.search(r"\b([A-HJ-NPR-Z0-9]{6,17})\b", cand)
        if tok and re.search(r"[A-Z]",tok.group(1)) and re.search(r"\d",tok.group(1)):
            vin_prefix = tok.group(1); break

    photos = sorted(set(re.findall(r"https://autoplius-img\.dgn\.lt/[^\s\"')]+\.jpg", html)))
    _ogimg = _meta(html,"og:image",attr="property")
    if _ogimg and _ogimg not in photos: photos.insert(0, _ogimg)

    # seller location + country (LV/LT/EE) from the seller block, default LT
    seg = html.split("Tapatība apstiprināta", 1)
    region_src = seg[1][:300] if len(seg) > 1 else html
    CW = {"Latvija":"LV","Latvia":"LV","Igaunija":"EE","Estija":"EE","Estonia":"EE",
          "Lietuva":"LT","Lithuania":"LT"}
    lm = re.search(r"([A-ZĀ-Ž][^\n,<>]{1,28}),\s*(Lietuva|Latvija|Igaunija|Estija|Estonia|Latvia|Lithuania)", region_src)
    location = f"{lm.group(1).strip()}, {lm.group(2)}" if lm else None
    country = CW.get(lm.group(2), "LT") if lm else "LT"


    return {
        "ad_id":ad_id_display,"ad_id_num":ad_id_num,"source_url":source_url,
        "make":make,"model":model,"year":year,"engine_cc":engine_cc,
        "fuel":_field_from_keywords(keywords,LABELS["fuel"]),
        "gearbox":_field_from_keywords(keywords,LABELS["gearbox"]),
        "body":_field_from_keywords(keywords,LABELS["body"]),
        "drivetrain":_field_from_keywords(keywords,LABELS["drivetrain"]),
        "owner_code":_field_from_keywords(keywords,LABELS["owner_code"]),
        "vin_prefix":vin_prefix,
        "price_eur":price_eur,"mileage_km":mileage_km,
        "reg_date":reg_date,
        "reg_country":_field_from_keywords(keywords,LABELS["reg_country"]),
        "color":_field_from_keywords(keywords,LABELS["color"]),
        "photos":photos,
        "location":location,
        "country":country,
    }

def fingerprint(f):
    parts=[(f.get("make") or "").lower().strip(),(f.get("model") or "").lower().strip(),
           str(f.get("year") or ""),str(f.get("engine_cc") or ""),
           (f.get("fuel") or "").lower().strip(),(f.get("gearbox") or "").lower().strip(),
           (f.get("body") or "").lower().strip(),(f.get("drivetrain") or "").lower().strip(),
           (f.get("owner_code") or "").lower().strip()]
    raw="|".join(parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:16], raw
