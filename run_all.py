"""
BalticRadar - SINGLE-FILE collector (ss.lv + autoplius.lt + auto24.ee -> Supabase).
Everything is inline (no local imports) so files can never get scrambled on upload.

Storage: if SUPABASE_URL + SUPABASE_KEY env are set -> Supabase; else -> listings.json preview.
Env knobs: SS_PAGES, AP_PAGES, A24_PAGES, DETAIL_CAP, LOOP_MINUTES, SS_DETAIL.
"""
import os, re, time, json, hashlib, datetime, requests
from bs4 import BeautifulSoup

# ============================================================ identity
PRICE_TOLERANCE = 0.35
def strong_signal_match(inc, car):
    if inc.get("vin_prefix") and inc["vin_prefix"] == car.get("vin_prefix"): return "vin"
    if inc.get("owner_code") and inc["owner_code"] == car.get("owner_code"): return "owner_code"
    if inc.get("photo_hash") and inc["photo_hash"] == car.get("photo_hash"): return "photo_hash"
    return None
def specs_match(inc, car):
    keys = ["make","model","year","engine_cc","fuel","gearbox","body","drivetrain"]
    return all((inc.get(k) or None) == (car.get(k) or None) for k in keys)
def price_sane(inc_price, car_price):
    if not inc_price or not car_price: return True
    return car_price*(1-PRICE_TOLERANCE) <= inc_price <= car_price*(1+PRICE_TOLERANCE)
def fingerprint(f):
    parts=[(f.get("make") or "").lower().strip(),(f.get("model") or "").lower().strip(),
           str(f.get("year") or ""),str(f.get("engine_cc") or ""),
           (f.get("fuel") or "").lower().strip(),(f.get("gearbox") or "").lower().strip(),
           (f.get("body") or "").lower().strip(),(f.get("drivetrain") or "").lower().strip(),
           (f.get("owner_code") or "").lower().strip()]
    raw="|".join(parts); return hashlib.sha1(raw.encode()).hexdigest()[:16], raw

# ============================================================ shared helpers
def meta(html, key, attr="name"):
    m = re.search(rf'<meta\s+{attr}=["\']{re.escape(key)}["\']\s+content=["\'](.*?)["\']\s*/?>',
                  html, re.IGNORECASE|re.DOTALL)
    return m.group(1).strip() if m else None
def _digits(s):
    return int(re.sub(r"\D","",s)) if s and re.search(r"\d",s) else None

# ============================================================ autoplius detail
AP_LABELS={"reg_date":"Pirmā reģistrācija","mileage":"Nobraukums","engine":"Dzinējs",
  "fuel":"Degvielas tips","body":"Virsbūves tips","drivetrain":"Piedziņa",
  "gearbox":"Ātrumkārbas tips","color":"Krāsa","reg_country":"Pirmās reģistrācijas valsts",
  "owner_code":"Īpašnieka deklarācijas kods"}
def _kw(kw, label):
    if not kw: return None
    m=re.search(rf'{re.escape(label)}\s+(.*?)(?:,\s*[A-ZĀČĒĢĪĶĻŅŠŪŽ][a-zāčēģīķļņšūž]+\s|$|,\s*[A-Z][a-z])', kw)
    return m.group(1).strip().rstrip(",").strip() if m else None
def ap_detail(html, source_url=None):
    kw=meta(html,"keywords"); title=meta(html,"og:title","property") or meta(html,"title")
    m=re.search(r"\bA(\d{6,})\b", title or kw or "")
    ad_id=("A"+m.group(1)) if m else None
    mm=((kw or title or "").split(",")[0]).strip() or None
    make=model=None
    if mm:
        p=mm.split(" ",1); make=p[0]; model=p[1] if len(p)>1 else None
    reg=_kw(kw,AP_LABELS["reg_date"]); year=int(reg[:4]) if reg and reg[:4].isdigit() else None
    er=_kw(kw,AP_LABELS["engine"]); em=re.search(r"(\d{3,5})",er) if er else None
    engine_cc=int(em.group(1)) if em else None
    price=None; pm=re.search(r"([\d][\d\s ]{2,})\s*€", html)
    if pm: price=_digits(pm.group(1))
    vin=None
    for cand in re.findall(r"\bVIN\b[^<]{0,40}", html, re.IGNORECASE):
        t=re.search(r"\b([A-HJ-NPR-Z0-9]{6,17})\b", cand)
        if t and re.search(r"[A-Z]",t.group(1)) and re.search(r"\d",t.group(1)): vin=t.group(1); break
    photos=sorted(set(re.findall(r"https://autoplius-img\.dgn\.lt/[^\s\"')]+\.jpg", html)))
    og=meta(html,"og:image","property")
    if og and og not in photos: photos.insert(0,og)
    seg=html.split("Tapatība apstiprināta",1); src=seg[1][:300] if len(seg)>1 else html
    CW={"Latvija":"LV","Latvia":"LV","Igaunija":"EE","Estija":"EE","Estonia":"EE","Lietuva":"LT","Lithuania":"LT"}
    lm=re.search(r"([A-ZĀ-Ž][^\n,<>]{1,28}),\s*(Lietuva|Latvija|Igaunija|Estija|Estonia|Latvia|Lithuania)", src)
    location=f"{lm.group(1).strip()}, {lm.group(2)}" if lm else None
    country=CW.get(lm.group(2),"LT") if lm else "LT"
    return {"ad_id":ad_id,"source_url":source_url,"make":make,"model":model,"year":year,
        "engine_cc":engine_cc,"fuel":_kw(kw,AP_LABELS["fuel"]),"gearbox":_kw(kw,AP_LABELS["gearbox"]),
        "body":_kw(kw,AP_LABELS["body"]),"drivetrain":_kw(kw,AP_LABELS["drivetrain"]),
        "owner_code":_kw(kw,AP_LABELS["owner_code"]),"vin_prefix":vin,"price_eur":price,
        "mileage_km":_digits(_kw(kw,AP_LABELS["mileage"])),"color":_kw(kw,AP_LABELS["color"]),
        "photos":photos,"location":location,"country":country}

# ============================================================ autoplius listing
AP_AD=re.compile(r'https://[a-z]{2}\.autoplius\.lt/(?:sludinajumi|skelbimai|ads|objavlenija)/[^\s"\')]+?-(\d+)\.html')
def ap_list(text, lang="lv"):
    seen={}
    for m in AP_AD.finditer(text):
        url,aid=m.group(0),m.group(1)
        if lang and f"://{lang}." not in url: continue
        seen.setdefault(aid,url)
    return {"ads":[{"ad_id":"A"+a,"url":u} for a,u in seen.items()]}
def page_url(base,n):
    return base if n==1 else f"{base}{'&' if '?' in base else '?'}page_nr={n}"

# ============================================================ auto24
A24_AD=re.compile(r'(?:https://(?:eng|www)\.auto24\.ee)?/vehicles/(\d+)')
A24_BODY=("sedan","cabriolet","hatchback","coupe","caravan","limousine","minivan","minibus","pickup","van","suv","estate","wagon","convertible")
def a24_list(text):
    seen={}
    for m in A24_AD.finditer(text):
        a=m.group(1); seen.setdefault(a,f"https://eng.auto24.ee/vehicles/{a}")
    return {"ads":[{"ad_id":"EE"+a,"url":u} for a,u in seen.items()]}
def a24_detail(html, source_url=None):
    ogt=meta(html,"og:title","property") or ""
    ogd=(meta(html,"og:description","property") or "").replace("\xa0"," ")
    desc=(meta(html,"Description","name") or "").replace("\xa0"," ")
    ogurl=meta(html,"og:url","property") or source_url or ""
    img=meta(html,"og:image","property")
    am=re.search(r"/vehicles/(\d+)",ogurl); ad_id=("EE"+am.group(1)) if am else None
    toks=ogt.split(); make=toks[0] if toks else None; model=toks[1] if len(toks)>1 else None
    ym=re.search(r"\b((?:19|20)\d{2})\b",ogd) or re.search(r"\b((?:19|20)\d{2})\b",desc)
    year=int(ym.group(1)) if ym else None
    fm=re.search(r"\b(petrol|diesel|electric|hybrid|gas)\b",ogd,re.I); fuel=fm.group(1).lower() if fm else None
    mil=re.search(r"([\d ]{3,})\s*km",ogd) or re.search(r"([\d ]{3,})\s*km",desc)
    mileage=_digits(mil.group(1)) if mil else None
    pm=re.search(r"EUR\D*?([\d,]+)",ogd); price=int(pm.group(1).replace(",","")) if pm else None
    el=re.search(r"(\d\.\d)\b",ogt); engine_l=el.group(1) if el else None
    kwm=re.search(r"(\d+)\s*kW",ogt); power=int(kwm.group(1)) if kwm else None
    ccm=re.search(r"(\d{3,5})\s*cm³",html); engine_cc=int(ccm.group(1)) if ccm else None
    body=None; bm=re.search(r"\dkW\s+([A-Za-z/ -]+?)(?:\s*\(|\s+\d)",desc)
    if bm: body=bm.group(1).strip()
    if not body:
        for w in A24_BODY:
            if re.search(rf"\b{w}\b",desc,re.I): body=w; break
    gm=re.search(r"\b(automatic|manual)\b",html,re.I); gear=gm.group(1).lower() if gm else None
    vin=None
    for mm in re.finditer(r"VIN",html):
        t=re.search(r"\b([A-HJ-NPR-Z0-9]{5,17})\b", html[mm.end():mm.end()+60])
        if t and re.search(r"[A-Z]",t.group(1)) and re.search(r"\d",t.group(1)): vin=t.group(1); break
    photos=sorted(set(re.findall(r"https://img\d*\.img-bcg\.eu/[^\s\"')]+\.jpg",html)))
    photos=[p for p in photos if "/h30/" in p]
    if img and img not in photos: photos.insert(0,img)
    return {"ad_id":ad_id,"source_url":ogurl or source_url,"make":make,"model":model,"year":year,
        "engine_cc":engine_cc,"fuel":fuel,"gearbox":gear,"body":body,"drivetrain":None,
        "owner_code":None,"vin_prefix":vin,"price_eur":price,"mileage_km":mileage,
        "engine_l":engine_l,"power_kw":power,"photos":photos,"location":"Estonia","country":"EE"}

# ============================================================ ss.lv
def ss_fuel(tok):
    if not tok: return None
    if tok.upper()=="E": return "Elektrība"
    if tok.upper().endswith("D"): return "Dīzelis"
    return "Benzīns"
def ss_row(ad_id,url,title,cells,photo=None):
    FIX={"bmw":"BMW","mg":"MG","ds":"DS","gmc":"GMC","seat":"SEAT","byd":"BYD","vaz":"VAZ","gaz":"GAZ"}
    mmm=re.search(r"/cars/([^/]+)/([^/]+)/",url or "")
    make=(FIX.get(mmm.group(1).lower(),mmm.group(1).replace("-"," ").title())) if mmm else None
    model=mmm.group(2).replace("-"," ").title() if mmm else None
    year=engine=price=None
    for c in cells:
        c=c.strip()
        if not year and re.fullmatch(r"(19|20)\d{2}",c): year=int(c)
        elif not engine and (re.fullmatch(r"\d\.\d[A-Za-z]?",c) or c.upper()=="E"): engine=c
        elif "€" in c: price=int(re.sub(r"[^\d]","",c) or 0) or None
    el=re.match(r"(\d\.\d)",engine or ""); engine_l=el.group(1) if el else None
    return {"ad_id":ad_id,"source_url":url,"country":"LV","make":make,"model":model,"year":year,
        "engine_l":engine_l,"fuel":ss_fuel(engine),"gearbox":None,"body":None,"engine_cc":None,
        "drivetrain":None,"owner_code":None,"vin_prefix":None,"price_eur":price,"mileage_km":None,
        "photos":[photo] if photo else [],"title":title}
def ss_list(html):
    soup=BeautifulSoup(html,"html.parser"); ads=[]
    for row in soup.select("tr[id^='tr_']"):
        ad_id="LV"+row.get("id","").replace("tr_","")
        link=row.select_one("a.am")
        if not link: continue
        href=link.get("href"); url=("https://www.ss.lv"+href) if href and href.startswith("/") else href
        cells=[c.get_text(strip=True) for c in row.select("td")]
        img=row.select_one("img"); photo=None
        if img:
            photo=img.get("src") or img.get("data-original")
            if photo and photo.startswith("//"): photo="https:"+photo
            if photo: photo=re.sub(r"\.(t|th|th2|th3|sm|400|1200|2000)\.jpg$",".800.jpg",photo)
        ads.append(ss_row(ad_id,url,link.get_text(strip=True),cells,photo))
    return {"ads":ads}
def ss_detail_photos(html):
    urls=re.findall(r"https://i\.ss\.(?:com|lv)/gallery/[^\s\"')]+?\.800\.jpg",html)
    seen=[]; [seen.append(u) for u in urls if u not in seen]; return seen
def ss_detail_mileage(html):
    m=re.search(r"Nobraukums[^\d]{0,40}?([\d][\d\s ]{2,}\d)",html)
    return int(re.sub(r"\D","",m.group(1))) if m else None
def ss_detail_desc(html):
    m=re.search(r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']',html,re.I|re.S)
    if not m: return None
    return re.sub(r"^Sludinājumi\..*?€\.?\s*","",m.group(1).strip()) or None

# ============================================================ headless fetcher
from contextlib import contextmanager
@contextmanager
def browser_session(headless=True):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b=p.chromium.launch(headless=headless,args=["--disable-blink-features=AutomationControlled","--no-sandbox"])
        ctx=b.new_context(locale="lv-LV",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            viewport={"width":1366,"height":900})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page=ctx.new_page()
        try: yield page
        finally: b.close()
def make_fetch(page, wait_ms=1500):
    def fetch(url):
        page.goto(url,wait_until="domcontentloaded",timeout=30000)
        page.wait_for_timeout(wait_ms); return page.content()
    return fetch

# ============================================================ identity decision (in-memory mode)
def decide(inc, db):
    if inc["ad_id"] in db["ads"]: return ("SAME_AD", db["ads"][inc["ad_id"]], "known ad")
    cands=[(cid,c) for cid,c in db["cars"].items() if specs_match(inc,c)]
    if not cands: return ("NEW_CAR",None,"no spec match")
    for cid,car in cands:
        sig=strong_signal_match(inc,car)
        if sig and price_sane(inc.get("price_eur"),car.get("last_price")):
            return ("REPOST",cid,f"signal:{sig}")
    return ("NEEDS_REVIEW",None,"specs match, no strong signal")

# ============================================================ storage
def now_iso(): return datetime.datetime.utcnow().isoformat()+"Z"
USE_SB = bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"))

if USE_SB:
    SB_URL=os.environ["SUPABASE_URL"].rstrip("/"); SB_KEY=os.environ["SUPABASE_KEY"]
    SBH={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}","Content-Type":"application/json"}
    _S=requests.Session(); _S.headers.update(SBH)
    def _retry(fn,tries=4):
        last=None
        for i in range(tries):
            try: return fn()
            except requests.exceptions.RequestException as e: last=e; time.sleep(1.5*(i+1))
        raise last
    def _ok(r): r.raise_for_status(); return r.json()
    def _get(table,params): return _retry(lambda: _ok(_S.get(f"{SB_URL}/rest/v1/{table}",params=params,timeout=30)))
    def _post(table,rows,upsert=False):
        h=dict(SBH)
        if upsert: h["Prefer"]="resolution=merge-duplicates"
        return _retry(lambda: _S.post(f"{SB_URL}/rest/v1/{table}",headers=h,json=rows,timeout=30))
    def _patch(table,params,body): _retry(lambda: _S.patch(f"{SB_URL}/rest/v1/{table}",params=params,json=body,timeout=30))
    def has_ad(ad_id): return bool(_get("ads",{"ad_id":f"eq.{ad_id}","select":"ad_id","limit":"1"}))
    def _cands(f):
        p={"active":"eq.true","select":"*","limit":"25"}
        for k in ("make","model","year","engine_cc"):
            if f.get(k) not in (None,""): p[k]=f"eq.{f[k]}"
        return _get("cars",p)
    def save(f):
        if has_ad(f["ad_id"]): return "SAME_AD"
        cands=[c for c in _cands(f) if specs_match(f,c)]
        for car in cands:
            if strong_signal_match(f,car) and price_sane(f.get("price_eur"),car.get("last_price")):
                cid=car["car_id"]
                _post("ads",[{"ad_id":f["ad_id"],"car_id":cid,"source":f.get("source"),"source_url":f.get("source_url"),"active":True,"first_seen":now_iso(),"last_seen":now_iso()}],upsert=True)
                if f.get("price_eur")!=car.get("last_price") or f.get("mileage_km")!=car.get("last_mileage"):
                    _post("price_history",[{"car_id":cid,"ts":now_iso(),"price":f.get("price_eur"),"mileage":f.get("mileage_km")}])
                _patch("cars",{"car_id":f"eq.{cid}"},{"active":True,"last_price":f.get("price_eur"),"last_mileage":f.get("mileage_km"),"last_seen":now_iso()})
                return "REPOST"
        if cands:
            _post("review_queue",[{"ad_id":f["ad_id"],"fingerprint":fingerprint(f)[0],"reason":"specs match, no signal","payload":f}],upsert=True); return "NEEDS_REVIEW"
        cid=f"car_{f['ad_id']}"
        _post("cars",[{"car_id":cid,"fingerprint":fingerprint(f)[0],"source":f.get("source"),"country":f.get("country"),
            "make":f.get("make"),"model":f.get("model"),"year":f.get("year"),"engine_cc":f.get("engine_cc"),
            "engine_l":f.get("engine_l"),"power_kw":f.get("power_kw"),"fuel":f.get("fuel"),"gearbox":f.get("gearbox"),
            "body":f.get("body"),"drivetrain":f.get("drivetrain"),"color":f.get("color"),"owner_code":f.get("owner_code"),
            "vin_prefix":f.get("vin_prefix"),"location":f.get("location"),"photos":f.get("photos") or [],
            "source_url":f.get("source_url"),"description":f.get("description"),"last_price":f.get("price_eur"),
            "last_mileage":f.get("mileage_km"),"active":True,"first_seen":now_iso(),"last_seen":now_iso()}],upsert=True)
        _post("ads",[{"ad_id":f["ad_id"],"car_id":cid,"source":f.get("source"),"source_url":f.get("source_url"),"active":True,"first_seen":now_iso(),"last_seen":now_iso()}],upsert=True)
        _post("price_history",[{"car_id":cid,"ts":now_iso(),"price":f.get("price_eur"),"mileage":f.get("mileage_km")}])
        return "NEW_CAR"
    def bump_seen(ad_ids):
        ids=[a for a in ad_ids if a]
        for i in range(0,len(ids),50):
            ch=ids[i:i+50]
            try: _patch("ads",{"ad_id":f"in.({','.join(ch)})"},{"last_seen":now_iso()})
            except Exception as e: print("bump_seen err",repr(e))
    def get_cursor(src):
        try:
            r=_get("crawl_state",{"source":f"eq.{src}","select":"next_page","limit":"1"})
            return r[0]["next_page"] if r else 1
        except Exception: return 1
    def set_cursor(src,page):
        try: _post("crawl_state",[{"source":src,"next_page":page}],upsert=True)
        except Exception as e: print("set_cursor err",repr(e))
    def deactivate(days=3):
        # mark ads not seen in `days` as inactive; row + VIN stay, site hides them
        cut=(datetime.datetime.utcnow()-datetime.timedelta(days=days)).isoformat()+"Z"
        try:
            stale=_get("ads",{"active":"eq.true","last_seen":f"lt.{cut}","select":"ad_id,car_id","limit":"5000"})
            if not stale: print("deactivate: none"); return
            _patch("ads",{"active":"eq.true","last_seen":f"lt.{cut}"},{"active":False})
            cids=list({s["car_id"] for s in stale if s.get("car_id")})
            for i in range(0,len(cids),50):
                _patch("cars",{"car_id":f"in.({','.join(cids[i:i+50])})"},{"active":False})
            print(f"deactivate: {len(stale)} ads hidden (VIN kept)")
        except Exception as e: print("deactivate err",repr(e))
    print("STORAGE: Supabase")
else:
    _MEM={"ads":{}}; _CARS={}
    def has_ad(ad_id): return ad_id in _MEM["ads"]
    def save(f):
        d,cid,_=decide(f,{"ads":_MEM["ads"],"cars":_CARS})
        if d=="NEW_CAR":
            cid=f"car_{f['ad_id']}"; f["car_id"]=cid; f["first_seen"]=now_iso(); f["last_price"]=f.get("price_eur"); f["active"]=True
            _CARS[cid]=f; _MEM["ads"][f["ad_id"]]=cid
        elif d in("SAME_AD","REPOST"): _MEM["ads"][f["ad_id"]]=cid
        return d
    def export_json(path="listings.json"):
        rows=sorted(_CARS.values(),key=lambda c:c.get("first_seen",""),reverse=True)
        json.dump({"updated":now_iso(),"cars":rows},open(path,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
    def bump_seen(ad_ids): pass
    def get_cursor(src): return 1
    def set_cursor(src,page): pass
    def deactivate(days=3): pass
    print("STORAGE: listings.json preview (set SUPABASE_* for full catalogue)")

# ============================================================ run
SS_PAGES=int(os.environ.get("SS_PAGES",5)); AP_PAGES=int(os.environ.get("AP_PAGES",3))
A24_PAGES=int(os.environ.get("A24_PAGES",1)); DETAIL_CAP=int(os.environ.get("DETAIL_CAP",200))
SS_DETAIL=int(os.environ.get("SS_DETAIL",1)); PAUSE=0.5
AP_BASE="https://lv.autoplius.lt/sludinajumi/lietotas-automasinas?order_by=1&order_direction=DESC"
A24_BASE="https://eng.auto24.ee/kasutatud/nimekiri.php?ad=7"
SS_BASE="https://www.ss.lv/lv/transport/cars/today/"
SS_H={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36","Accept-Language":"lv,en;q=0.9"}
_ss=requests.Session(); _ss.headers.update(SS_H)
def ss_page(n): return SS_BASE if n==1 else f"{SS_BASE}page{n}.html"

def run():
    new=seen=0; seen_ids=set()
    for n in range(1,SS_PAGES+1):
        try: r=_ss.get(ss_page(n),timeout=25); html=r.text
        except Exception as e: print("ss.lv page",n,"ERR",e); break
        rows=ss_list(html)["ads"]
        print(f"ss.lv page {n}: HTTP {r.status_code}, {len(rows)} rows")
        if not rows: break
        for f in rows:
            seen+=1; seen_ids.add(f["ad_id"])
            if has_ad(f["ad_id"]): continue
            f["source"]="ss.lv"
            if SS_DETAIL:
                try:
                    d=_ss.get(f["source_url"],timeout=20).text
                    ph=ss_detail_photos(d)
                    if ph: f["photos"]=ph
                    ml=ss_detail_mileage(d)
                    if ml: f["mileage_km"]=ml
                    de=ss_detail_desc(d)
                    if de: f["description"]=de
                except Exception: pass
            try: save(f); new+=1
            except Exception as e: print("  skip",f["ad_id"],repr(e))
        time.sleep(PAUSE)
    details=0
    try:
        with browser_session() as page:
            fetch=make_fetch(page)
            def crawl(name, base, pages, plist, pdetail, country, paginate, cursor_key=None):
                nonlocal new,seen,details
                start=get_cursor(cursor_key) if cursor_key else 1
                empty=False
                for n in range(start, start+pages):
                    url=paginate(base,n) if paginate else base
                    try: txt=fetch(url)
                    except Exception as e: print(name,"page",n,"fetch ERR",repr(e)); break
                    ads=plist(txt)["ads"]
                    if not ads: empty=True; print(f"{name} page {n}: 0 ads (end)"); break
                    for ad in ads:
                        seen+=1; seen_ids.add(ad["ad_id"])
                        if has_ad(ad["ad_id"]): continue
                        if details>=DETAIL_CAP:
                            print(name,"hit DETAIL_CAP")
                            if cursor_key: set_cursor(cursor_key,n)
                            return
                        details+=1
                        try:
                            f=pdetail(fetch(ad["url"]),source_url=ad["url"]); f["ad_id"]=ad["ad_id"]; f["source"]=name
                            if country: f.setdefault("country",country)
                            save(f); new+=1
                        except Exception as e: print("  skip",ad["ad_id"],repr(e))
                    print(f"{name} page {n} (cursor): new total {new}, details {details}"); time.sleep(PAUSE)
                if cursor_key: set_cursor(cursor_key, 1 if empty else start+pages)
            crawl("autoplius",AP_BASE,AP_PAGES,lambda t:ap_list(t,"lv"),ap_detail,None,page_url,"autoplius")
            crawl("auto24",A24_BASE,A24_PAGES,a24_list,a24_detail,"EE",None)
    except Exception as e:
        print("browser phase error:",repr(e))
    bump_seen(seen_ids)
    if os.environ.get("DEACTIVATE")=="1":
        deactivate(int(os.environ.get("DEACTIVATE_DAYS",3)))
    print(f"DONE. seen={seen}, new stored={new}")
    if not USE_SB: export_json()

if __name__=="__main__":
    loop=int(os.environ.get("LOOP_MINUTES",0))
    if loop>0:
        print(f"LOOP MODE every {loop} min. Ctrl+C to stop."); k=0
        while True:
            k+=1; print(f"\n===== cycle {k} =====")
            try: run()
            except Exception as e: print("cycle error:",repr(e))
            time.sleep(loop*60)
    else:
        run()
# BalticRadar single-file collector - end
