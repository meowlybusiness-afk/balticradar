"""
BalticRadar - ALL-IN-ONE LT/EE collector for your PC.
Run it with:  python balticradar.py
- Installs its own dependencies the first time.
- Asks for your Supabase key once, then remembers it (balticradar_key.txt next to this file).
- Collects autoplius.lt + auto24.ee from YOUR home IP (no proxy, no credits) every 30 min.
Leave the window open. Press Ctrl+C to stop. ss.lv keeps running in the cloud separately.
"""
# ============================================================ BOOTSTRAP (runs first)
import os, sys, subprocess, warnings
warnings.filterwarnings("ignore")   # keep the terminal clean (hide harmless deprecation notices)

def _bootstrap():
    try:
        import requests, bs4, playwright  # noqa: F401
    except ImportError:
        print("First run: installing Python packages (one minute)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet",
                               "requests", "beautifulsoup4", "playwright"])
    here = os.path.dirname(os.path.abspath(__file__))
    marker = os.path.join(here, ".chromium_installed")
    if not os.path.exists(marker):
        print("First run: installing the browser (Chromium)...")
        subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
        try: open(marker, "w").write("ok")
        except Exception: pass

_bootstrap()

# ---- configuration (do not edit) ----
os.environ.setdefault("SUPABASE_URL", "https://wrilvoukvyubgpomuoyn.supabase.co")
_HERE = os.path.dirname(os.path.abspath(__file__))
_KEYFILE = os.path.join(_HERE, "balticradar_key.txt")
if not os.environ.get("SUPABASE_KEY"):
    if os.path.exists(_KEYFILE):
        os.environ["SUPABASE_KEY"] = open(_KEYFILE, encoding="utf-8").read().strip()
    else:
        print("\n" + "=" * 60)
        print(" Supabase atslēga vajadzīga TIKAI pirmoreiz.")
        print(" Paņem: Supabase panelis -> Project Settings -> API")
        print("        -> service_role  (secret) -> kopē")
        print("=" * 60)
        _k = input("Ielīmē savu Supabase service_role atslēgu un spied Enter:\n> ").strip()
        os.environ["SUPABASE_KEY"] = _k
        try:
            open(_KEYFILE, "w", encoding="utf-8").write(_k)
            print("Atslēga saglabāta (balticradar_key.txt). Nākamreiz neprasīs.\n")
        except Exception:
            print("(Atslēgu nevarēja saglabāt failā, bet šim palaišanas reizei der.)\n")

# ===== BULK-FILL MODE (run on PC to fill the whole catalogue; Supabase Pro = no egress limit) =====
os.environ.setdefault("SS_PAGES", "10")       # ss.lv now ALSO fills on PC (direct requests, fast, no proxy)
os.environ.setdefault("SS_FULL", "1")         # full ss.lv brand-by-brand sweep
os.environ.setdefault("SS_BRANDS_PER_RUN", "6")  # catalogue full -> short maintenance sweep so date-backfill runs each cycle
os.environ.setdefault("SS_BRAND_PAGES", "80")    # deeper per brand (BMW alone has ~62 pages); repeat-detect stops at real end
os.environ.setdefault("SS_WORKERS", "5")         # detail-fetch threads (lower = less ss.lv throttle)
os.environ.setdefault("SS_PAGE_PAUSE", "0.35")   # pause between list pages (throttle avoidance)
os.environ.setdefault("SS_SWEEP_DET", "6000")    # more detail fetches/cycle (ss.lv is fast: requests, ~0.6s each)
os.environ.setdefault("AP_PAGES", "10")       # flat newest-pages crawl (autoplius caps a flat search at ~150 pages)
os.environ.setdefault("AP_FULL", "0")         # catalogue is full -> skip the by-YEAR re-sweep (it just finds 0 new and blocks the photo backfill). Set AP_FULL=1 only to re-collect the whole back-catalogue.
os.environ.setdefault("AP_YEARS_PER_RUN", "2")   # catalogue full -> short maintenance sweep so date-backfill runs each cycle
os.environ.setdefault("AP_YEAR_PAGES", "5")   # catalogue full -> short maintenance sweep (flat crawl catches new); date-backfill runs each cycle
os.environ.setdefault("AP_LIST_ONLY", "1")       # save straight off results page (no per-car detail fetch)
os.environ.setdefault("AP_PAGE_PAUSE", "1.0")
os.environ.setdefault("AP_DETAIL_CAP", "300") # per cycle = per sticky proxy IP; rotate the IP (new cycle) before autoplius limits it (~330/IP)
os.environ.setdefault("AP_PAUSE", "0")         # no throttle needed via rotating proxy IPs
os.environ.setdefault("A24_PAGES", "25"); os.environ.setdefault("NO_PROXY_BR", "1")      # catalogue full -> short maintenance sweep (pages 1,2 still catch new); date-backfill runs each cycle
os.environ.setdefault("DETAIL_CAP", "6000")   # MAX new cars detailed per cycle per source (raised for bulk fill)
os.environ.setdefault("BACKFILL", "400")      # catalogue is full -> backfill photos/dates by default (so the boot-time Startup script fills photos automatically when the PC is on).
os.environ.setdefault("BACKFILL_ALL", "1")    # (moot while BACKFILL=0) also date autoplius/auto24
os.environ.setdefault("LOOP_MINUTES", "3")    # near-continuous cycling for max throughput

if not os.environ.get("SUPABASE_KEY"):
    print("Nav Supabase atslēgas - nevar rakstīt datubāzē. Aizver un palaid vēlreiz.")
    sys.exit(1)

# ============================================================ COLLECTOR
import re, time, json, hashlib, datetime, requests
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
def strip_tags(s):
    if not s: return None
    s=re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>',' ',s)
    s=re.sub(r'(?i)<br\s*/?>','\n',s)
    s=re.sub(r'(?i)</(p|div|li)>','\n',s)
    s=re.sub(r'<[^>]+>',' ',s)
    s=(s.replace('&nbsp;',' ').replace('&amp;','&').replace('&quot;','"')
        .replace('&#039;',"'").replace('&lt;','<').replace('&gt;','>'))
    s=re.sub(r'[ \t]+',' ',s); s=re.sub(r'\n[ \t]*','\n',s); s=re.sub(r'\n{3,}','\n\n',s)
    s=re.sub(r'\s*<[^>]*$','',s)   # drop any trailing half-cut tag like "<div class=\""
    return s.strip()
def ap_desc(html):
    if not html: return None
    m=re.search(r'announcement-description[^>]*>(.*)$',html,re.S)
    if not m: return None
    chunk=m.group(1)[:5000]
    for mark in ['js-similar','similar-announcements','class="similar','announcement-actions','id="footer','class="footer','Susiję','Līdzīgi']:
        i=chunk.find(mark)
        if i>30: chunk=chunk[:i]; break
    t=strip_tags(chunk)
    return t[:2500] if t and len(t)>15 else None
def a24_desc(html):
    if not html: return None
    m=re.search(r'(?is)<(div|td|p|section)[^>]*class="[^"]*(?:vehicle-?desc|comment|lisainfo|adInfo|freetext|description)[^"]*"[^>]*>(.*?)</\1>',html)
    if m:
        t=strip_tags(m.group(2))
        if t and len(t)>15: return t[:2500]
    return None
import html as _html
# Makes whose name contains a space/special char; split-on-first-space would break them.
CANON={"lynk & co":"Lynk & Co","alfa romeo":"Alfa Romeo","aston martin":"Aston Martin",
  "great wall":"Great Wall","land rover":"Land Rover","mercedes-benz":"Mercedes-Benz",
  "rolls-royce":"Rolls-Royce","ds automobiles":"DS Automobiles"}
def _deent(s):
    try: return _html.unescape(s) if s else s
    except Exception: return s
def split_mm(title):
    """Return (make, model) from a listing title, decoding HTML entities and
    keeping multi-word makes like 'Lynk & Co' intact."""
    if not title: return None,None
    t=_deent(str(title)).strip()
    low=t.lower()
    for mk in CANON:
        if low.startswith(mk):
            rest=t[len(mk):].strip(" ,-")
            model=rest.split(",")[0].split()[0] if rest else None
            return CANON[mk], (model or None)
    p=t.replace(","," ").split()
    make=p[0] if p else None
    model=p[1] if len(p)>1 else None
    # guard against engine specs landing in make/model (e.g. "2.0", "18kW")
    if make and (re.fullmatch(r"\d+(\.\d+)?",make) or re.fullmatch(r"\d+\s*kW",make,re.I)): make=None
    if model and (re.fullmatch(r"\d+\.\d+",model) or re.fullmatch(r"\d+\s*kW",model,re.I)): model=None
    return make,model
def ap_detail(html, source_url=None):
    kw=meta(html,"keywords"); title=meta(html,"og:title","property") or meta(html,"title")
    m=re.search(r"\bA(\d{6,})\b", title or kw or "")
    ad_id=("A"+m.group(1)) if m else None
    mm=((kw or title or "").split(",")[0]).strip() or None
    make,model=split_mm(mm) if mm else (None,None)
    reg=_kw(kw,AP_LABELS["reg_date"]); year=int(reg[:4]) if reg and reg[:4].isdigit() else None
    er=_kw(kw,AP_LABELS["engine"]); em=re.search(r"(\d{3,5})",er) if er else None
    engine_cc=int(em.group(1)) if em else None
    price=None; pm=re.search(r"([\d][\d\s ]{2,})\s*€", html)
    if pm: price=_digits(pm.group(1))
    vin=None
    for cand in re.findall(r"\bVIN\b[^<]{0,40}", html, re.IGNORECASE):
        t=re.search(r"\b([A-HJ-NPR-Z0-9]{6,17})\b", cand)
        if t and re.search(r"[A-Z]",t.group(1)) and re.search(r"\d",t.group(1)): vin=t.group(1); break
    # ONLY the main-listing gallery. Autoplius detail pages embed a "similar / recommended cars"
    # carousel lower down whose thumbnails use the SAME autoplius-img.dgn.lt host (and even inherit
    # this page's slug) but point at OTHER cars' image IDs -> cut the page at that section first so
    # foreign photos never leak into this listing's gallery.
    _gal=html
    for _m in ('js-similar','similar-announcements','class="similar','data-similar','recommended','Susiję','Panašūs','Līdzīgi','Похожие','id="footer','class="footer'):
        _i=_gal.find(_m)
        if _i>800: _gal=_gal[:_i]; break
    photos=sorted(set(re.findall(r"https://autoplius-img\.dgn\.lt/[^\s\"')]+\.jpg", _gal)))
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
        "photos":photos,"location":location,"country":country,"posted":rel_posted(html),"description":ap_desc(html)}

# ============================================================ autoplius listing
AP_AD=re.compile(r'https://[a-z]{2}\.autoplius\.lt/(?:sludinajumi|skelbimai|ads|objavlenija)/[^\s"\')]+?-(\d+)\.html')
def ap_list(text, lang="lv"):
    seen={}
    for m in AP_AD.finditer(text):
        url,aid=m.group(0),m.group(1)
        if lang and f"://{lang}." not in url: continue
        seen.setdefault(aid,url)
    return {"ads":[{"ad_id":"A"+a,"url":u} for a,u in seen.items()]}

# --- fast list-only parser: pulls make/model/year/body/engine/fuel/price straight from the
#     rendered results page (no per-car detail fetch). ~20x fewer requests than detail crawling.
AP_ITEM=re.compile(r'<a\b[^>]*?href="(https://lv\.autoplius\.lt/sludinajumi/[a-z0-9-]+?-(\d+)\.html)"[^>]*class="[^"]*announcement-item')
AP_FUEL={"benzins":"Benzīns","dizelis":"Dīzelis","elektriba":"Elektrība","benzins-elektriba":"Hibrīds",
         "dizelis-elektriba":"Hibrīds","dujos":"Gāze","benzins-dujos":"Gāze","hibridas":"Hibrīds"}
def ap_list_rich(html):
    marks=[(m.start(),m.group(1),m.group(2)) for m in AP_ITEM.finditer(html)]
    ads=[]
    for i,(idx,href,aid) in enumerate(marks):
        end=marks[i+1][0] if i+1<len(marks) else idx+3000
        seg=html[idx:end]; slug=href.rsplit("/",1)[-1][:-5]
        tm=re.search(r'class="announcement-title">\s*([^<]+?)\s*<',seg)
        title=tm.group(1).strip() if tm else None
        pm=re.search(r'class="announcement-title-parameters"[^>]*>([\s\S]*?)</div>',seg)
        params=re.sub(r'\s+',' ',re.sub(r'<[^>]+>',' ',pm.group(1))).strip() if pm else ''
        prm=re.search(r'>\s*([\d\xa0  ]{2,})\s*(?:&euro;|€)',seg)
        price=int(re.sub(r'\D','',prm.group(1))) if prm and re.sub(r'\D','',prm.group(1)) else None
        ym=re.search(r'-((?:19|20)\d\d)-([a-z-]+?)-\d+$',slug)
        year=int(ym.group(1)) if ym else None
        fuelslug=ym.group(2) if ym else None
        engm=re.search(r'-(\d)-(\d)-l-',slug); engine_l=f"{engm.group(1)}.{engm.group(2)}" if engm else None
        bodym=re.match(r'\s*\d{4}-\d{2}\s+(.+)$',params); body=bodym.group(1).strip() if bodym else None
        if title and title.lower().replace("-","").strip() in ("kita kita","kita"): continue  # junk
        make,model=split_mm(title) if title else (None,None)
        ads.append({"ad_id":"A"+aid,"source_url":href,"make":make,"model":model,"year":year,
            "engine_l":engine_l,"fuel":AP_FUEL.get(fuelslug,fuelslug),"body":body,"price_eur":price,
            "country":"LT","gearbox":None,"engine_cc":None,"drivetrain":None,"owner_code":None,
            "vin_prefix":None,"mileage_km":None,"photos":[]})
    return {"ads":ads}
def page_url(base,n):
    return base if n==1 else f"{base}{'&' if '?' in base else '?'}page_nr={n}"

# --- ROBUST list-only parser (overrides the markup-based one above): builds a full car record
#     from the ad URL SLUG, which AP_AD reliably finds in the bot's HTML regardless of card
#     markup/attribute order. make/model/year/engine/fuel/body from slug; price best-effort.
AP_BODY=["visurgajejs-krosovers","kabriolets-rodsters","sedans","universalas","hecbeks","kupeja",
         "minivens","pikaps","minibuss","limuzins","vagons"]
AP_BODYLV={"sedans":"Sedans","universalas":"Universālis","hecbeks":"Hečbeks","kupeja":"Kupeja",
           "minivens":"Minivens","pikaps":"Pikaps","minibuss":"Mikroautobuss","limuzins":"Limuzīns",
           "vagons":"Vagons","visurgajejs-krosovers":"Apvidus","kabriolets-rodsters":"Kabriolets"}
AP_MAKEFIX=sorted({"mercedes-benz":"Mercedes-Benz","land-rover":"Land Rover","alfa-romeo":"Alfa Romeo",
  "aston-martin":"Aston Martin","great-wall":"Great Wall","rolls-royce":"Rolls-Royce",
  "ds-automobiles":"DS Automobiles","bmw":"BMW","mg":"MG","ds":"DS","gmc":"GMC","seat":"SEAT",
  "byd":"BYD","vaz":"VAZ","gaz":"GAZ","uaz":"UAZ","zaz":"ZAZ","mini":"MINI","ssangyong":"SsangYong",
  "smart":"Smart"}.items(), key=lambda x:-len(x[0]))
def ap_parse_slug(url):
    slug=url.rsplit("/",1)[-1]
    if slug.endswith(".html"): slug=slug[:-5]
    m=re.match(r'^(.*?)-((?:19|20)\d\d)-([a-z-]+)-(\d+)$',slug)
    if not m: return None
    head,year,fuelslug,aid=m.group(1),int(m.group(2)),m.group(3),m.group(4)
    eng=None; body=None; mm=head
    em=re.match(r'^(.*?)-(\d)-(\d)-l(?:-(.*))?$',head)
    if em:
        mm=em.group(1); eng=f"{em.group(2)}.{em.group(3)}"; body=em.group(4) or None
    else:
        for b in AP_BODY:
            if head.endswith("-"+b): body=b; mm=head[:-(len(b)+1)]; break
    if not mm: return None
    make=model=None
    for ms,mc in AP_MAKEFIX:
        if mm==ms or mm.startswith(ms+"-"):
            make=mc; rest=mm[len(ms):].strip("-"); model=(rest.split("-")[0].title() if rest else None); break
    if not make:
        make,model=split_mm(mm.replace("-"," ").title())
    if not make or make.lower() in ("kita","-kita-"): return None
    return {"ad_id":"A"+aid,"source_url":url,"make":make,"model":model,"year":year,
        "engine_l":eng,"fuel":AP_FUEL.get(fuelslug,fuelslug),"body":AP_BODYLV.get(body,body),
        "price_eur":None,"country":"LT","gearbox":None,"engine_cc":None,"drivetrain":None,
        "owner_code":None,"vin_prefix":None,"mileage_km":None,"photos":[]}
def ap_list_rich(html, lang="lv"):
    seen={}
    for m in AP_AD.finditer(html):
        url=m.group(0)
        if lang and f"://{lang}." not in url: continue
        seen.setdefault(url, m.start())
    items=sorted(seen.items(), key=lambda kv: kv[1])
    ads=[]
    for i,(url,pos) in enumerate(items):
        f=ap_parse_slug(url)
        if not f: continue
        end=items[i+1][1] if i+1<len(items) else pos+2200
        seg=html[pos:end]
        pm=re.search(r'>\s*([\d   ]{2,})\s*(?:&euro;|€)',seg)
        if pm:
            d=re.sub(r'\D','',pm.group(1))
            if d: f["price_eur"]=int(d)
        im=re.search(r'https://autoplius-img\.dgn\.lt/[^\s"\'<>]+?\.jpg',seg)
        if im: f["photos"]=[im.group(0)]
        po=rel_posted(seg)          # "Pirms X ..." badge -> posting date (only on freshly-bumped ads)
        if po: f["posted"]=po
        ads.append(f)
    return {"ads":ads}

# ============================================================ auto24
A24_AD=re.compile(r'(?:https://(?:eng|www)\.auto24\.ee)?/vehicles/(\d+)')
A24_BODY=("sedan","cabriolet","hatchback","coupe","caravan","limousine","minivan","minibus","pickup","van","suv","estate","wagon","convertible")
def a24_list(text):
    seen={}
    for m in A24_AD.finditer(text):
        a=m.group(1); seen.setdefault(a,f"https://eng.auto24.ee/vehicles/{a}")
    return {"ads":[{"ad_id":"EE"+a,"url":u} for a,u in seen.items()]}
def a24_posted(html):
    # auto24 keeps the exact date in a data attribute (data-changed='YYYY-MM-DD HH:MM:SS'),
    # present in the raw HTML (no JS needed). Fall back to the relative "Updated N ago".
    m=re.search(r"data-changed=['\"](\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})",html)
    if m: return f"{m.group(1)}T{m.group(2)}Z"
    return rel_posted(html)
def a24_detail(html, source_url=None):
    ogt=meta(html,"og:title","property") or ""
    ogd=(meta(html,"og:description","property") or "").replace("\xa0"," ")
    desc=(meta(html,"Description","name") or "").replace("\xa0"," ")
    ogurl=meta(html,"og:url","property") or source_url or ""
    img=meta(html,"og:image","property")
    am=re.search(r"/vehicles/(\d+)",ogurl); ad_id=("EE"+am.group(1)) if am else None
    make,model=split_mm(ogt)
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
        "engine_l":engine_l,"power_kw":power,"photos":photos,"location":"Estonia","country":"EE","posted":a24_posted(html),"description":a24_desc(html)}

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
    year=engine=price=mileage=None
    for c in cells:
        c=c.strip()
        if not year and re.fullmatch(r"(19|20)\d{2}",c): year=int(c)
        elif not engine and (re.fullmatch(r"\d\.\d[A-Za-z]?",c) or c.upper()=="E"): engine=c
        elif "€" in c: price=int(re.sub(r"[^\d]","",c) or 0) or None
        elif mileage is None and "tūkst" in c.lower():
            m=re.search(r"([\d ]+)",c); mileage=(int(re.sub(r"\D","",m.group(1)) or 0)*1000) or None
    el=re.match(r"(\d\.\d)",engine or ""); engine_l=el.group(1) if el else None
    return {"ad_id":ad_id,"source_url":url,"country":"LV","make":make,"model":model,"year":year,
        "engine_l":engine_l,"fuel":ss_fuel(engine),"gearbox":None,"body":None,"engine_cc":None,
        "drivetrain":None,"owner_code":None,"vin_prefix":None,"price_eur":price,"mileage_km":mileage,
        "photos":[photo] if photo else [],"title":title}
def ss_list(html):
    soup=BeautifulSoup(html,"html.parser"); ads=[]
    for row in soup.select("tr[id^='tr_']"):
        ad_id="LV"+row.get("id","").replace("tr_","")
        link=row.select_one("a.am")
        if not link: continue
        href=link.get("href"); url=("https://www.ss.lv"+href) if href and href.startswith("/") else href
        cells=[c.get_text(strip=True) for c in row.select("td")]
        dealcell=(cells[-1].lower() if cells else "")
        if ("pērk" in dealcell or "perk" in dealcell or "куп" in dealcell) and "€" not in dealcell: continue
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
def ss_detail_posted(html):
    m=re.search(r"Datums:\s*(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})",html)
    if not m: return None
    d,mo,y,H,Mi=m.groups(); return f"{y}-{mo}-{d}T{H}:{Mi}:00Z"
def rel_posted(html):
    # Latvian (autoplius LV): "Pirms N min/stundām/dienām"
    m=re.search(r"[Pp]irms\s+(\d+)\s+(min\w*|stund\w*|dien\w*)",html) or re.search(r"[Pp]irms\s+(\d+)\s*([dh])\b",html)
    if m:
        n=int(m.group(1)); u=m.group(2).lower()
        secs=60 if u.startswith("min") else 86400 if (u.startswith("dien") or u=="d") else 3600
        return (datetime.datetime.utcnow()-datetime.timedelta(seconds=n*secs)).isoformat()+"Z"
    # English (auto24): "Updated N min/h./d./month(s) ago"
    m=re.search(r"(\d+)\s*(min|h|d|month|mo)\w*\.?\s*ago",html,re.I)
    if m:
        n=int(m.group(1)); u=m.group(2).lower()
        secs=60 if u=="min" else 3600 if u=="h" else 2592000 if u.startswith("mo") else 86400
        return (datetime.datetime.utcnow()-datetime.timedelta(seconds=n*secs)).isoformat()+"Z"
    return None

# ============================================================ headless fetcher
from contextlib import contextmanager
@contextmanager
def browser_session(headless=True):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        launch={"headless":headless,"args":["--disable-blink-features=AutomationControlled","--no-sandbox"]}
        ps=os.environ.get("PROXY_SERVER","").strip(); pu=os.environ.get("PROXY_USER","").strip(); pw=os.environ.get("PROXY_PASS","").strip()
        NOPROX = os.environ.get("NO_PROXY_BR")=="1"
        if NOPROX:
            ps=""; print("NO_PROXY_BR=1 -> running on direct/home IP (no proxy)", flush=True)
        if not ps and not NOPROX:  # fall back to a local proxy.txt (host=/port=/user=/pass= lines) next to this script
            try:
                d={}
                for line in open(os.path.join(_HERE,"proxy.txt"),encoding="utf-8"):
                    if "=" in line: k,v=line.split("=",1); d[k.strip().lower()]=v.strip()
                if d.get("host") and d.get("port"):
                    ps=f"http://{d['host']}:{d['port']}"; pu=d.get("user",pu); pw=d.get("pass",pw)
            except Exception: pass
        if ps:
            # sticky session: hold ONE IP for this whole browser session (so JS challenges clear and stay cleared),
            # but a NEW random sessid each session rotates the IP across cycles -> avoids per-IP rate-limits.
            if pu and "sessid" not in pu.lower():
                import random
                sess=os.environ.get("PROXY_SESS","").strip() or f"br{random.randint(100000,999999)}"
                cc=os.environ.get("PROXY_CC","lt").strip()
                pu=f"{pu}__cr.{cc};sessid.{sess}" if cc else f"{pu}__sessid.{sess}"
            launch["proxy"]={"server":ps}
            if pu: launch["proxy"]["username"]=pu
            if pw: launch["proxy"]["password"]=pw
            print(f"PROXY: {ps} (sticky {pu.split('sessid.')[-1] if 'sessid.' in pu else '?'})")
        b=p.chromium.launch(**launch)
        ctx=b.new_context(locale="lv-LV",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            viewport={"width":1366,"height":900})
        if os.environ.get("BLOCK_IMAGES","1")!="0":
            # MINIMIZE PROXY BANDWIDTH (DataImpulse bills by GB): only the HTML document + the scripts/XHR
            # needed to render listings load through the proxy. Image/media/font/stylesheet bytes and
            # tracker/ad/analytics sub-requests are aborted. Photo URLs stay in the HTML (we store the URL
            # string; we just never download the image bytes). This cuts residential bandwidth ~80-95%.
            _BLOCK_RT={"image","media","font","stylesheet"}
            _BLOCK_HOST=("google-analytics","googletagmanager","doubleclick","googlesyndication",
                "adservice.google","facebook.net","facebook.com","fbcdn","connect.facebook",
                "hotjar","criteo","adnxs","adsystem","taboola","outbrain","scorecardresearch",
                "quantserve","gemius","mc.yandex","metrika","cookiebot","onesignal","cookielaw",
                "clarity.ms","pubmatic","rubiconproject","casalemedia","sentry","segment.io","amplitude")
            def _route(route):
                try:
                    req=route.request
                    if req.resource_type in _BLOCK_RT or any(h in req.url.lower() for h in _BLOCK_HOST):
                        return route.abort()
                    return route.continue_()
                except Exception:
                    try: return route.continue_()
                    except Exception: return
            ctx.route("**/*", _route)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page=ctx.new_page()
        try: yield page
        finally: b.close()
SCRAPER_KEY=os.environ.get("SCRAPER_KEY","").strip()
def scraper_get(url):
    import urllib.parse
    def call(render):
        api="http://api.scraperapi.com/?"+urllib.parse.urlencode(
            {"api_key":SCRAPER_KEY,"url":url,"render":"true" if render else "false","country_code":"eu"})
        return requests.get(api,timeout=80)
    r=call(False); h=r.text
    blocked=(r.status_code>=403 or "Just a moment" in h or "challenge-platform" in h
             or "Checking your browser" in h or "cf-browser-verification" in h or len(h)<800)
    if blocked: h=call(True).text
    return h
def make_fetch(page, wait_ms=None):
    if wait_ms is None: wait_ms=int(os.environ.get("WAIT_MS","1000"))  # lower = faster page loads (auto24/autoplius)
    def fetch(url):
        if SCRAPER_KEY:
            try: return scraper_get(url)
            except Exception as e: print("  scraperapi ERR",repr(e))
        page.goto(url,wait_until="domcontentloaded",timeout=45000)
        page.wait_for_timeout(wait_ms)
        html=page.content()
        def _challenged(h):
            l=h.lower()
            return ("just a moment" in l or "challenge-platform" in l or "cf-browser-verification" in l
                    or "checking your browser" in l or "uzgaidiet" in l or "prašau palaukite" in l or "palun oodake" in l
                    or "peržiūros limit" in l or "viršijote" in l)   # autoplius "view limit exceeded" block
        # JS challenge (incl. autoplius "Mazliet uzgaidiet") AND autoplius view-limit:
        # wait, RE-REQUEST the page (a view-limit page won't self-clear), retry with growing backoff
        for attempt in range(4):
            if not _challenged(html): break
            try: page.wait_for_load_state("networkidle", timeout=15000)
            except Exception: pass
            page.wait_for_timeout(6000*(attempt+1))          # 6s, 12s, 18s, 24s
            try: page.goto(url,wait_until="domcontentloaded",timeout=45000)   # actually re-fetch
            except Exception: pass
            html=page.content()
        return html
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
        def _do():
            r=_S.post(f"{SB_URL}/rest/v1/{table}",headers=h,json=rows,timeout=30)
            if r.status_code>=400: raise RuntimeError(f"POST {table} {r.status_code}: {r.text[:300]}")
            return r
        return _retry(_do)
    def _patch(table,params,body): _retry(lambda: _S.patch(f"{SB_URL}/rest/v1/{table}",params=params,json=body,timeout=30))
    def has_ad(ad_id): return bool(_get("ads",{"ad_id":f"eq.{ad_id}","select":"ad_id","limit":"1"}))
    def has_ads_batch(ad_ids):
        # one query instead of one-per-car: returns the set of ad_ids already in the DB
        ids=[a for a in ad_ids if a]
        if not ids: return set()
        try:
            rows=_get("ads",{"ad_id":f"in.({','.join(ids)})","select":"ad_id","limit":str(len(ids)+1)})
            return {r["ad_id"] for r in rows}
        except Exception: return set()
    def ad_info_batch(ad_ids):
        # like has_ads_batch, but also brings each ad's car_id + current stored price (via FK embed)
        # so we can detect a price change on the SAME listing over time (tirgusdati-style tracking).
        ids=[a for a in ad_ids if a]
        if not ids: return {}
        try:
            rows=_get("ads",{"ad_id":f"in.({','.join(ids)})","select":"ad_id,car_id,cars(last_price,last_mileage)","limit":str(len(ids)+1)})
            out={}
            for r in rows:
                c=r.get("cars") or {}
                out[r["ad_id"]]={"car_id":r.get("car_id"),"last_price":c.get("last_price"),"last_mileage":c.get("last_mileage")}
            return out
        except Exception: return {}
    def record_price_change(info, f):
        # same listing re-seen: if the price moved, append a history point + update the current price.
        cid=info.get("car_id"); np=f.get("price_eur")
        if not cid or np in (None,0): return False
        op=info.get("last_price")
        if op is None or np==op: return False   # no prior price or unchanged -> nothing to log
        try:
            _post("price_history",[{"car_id":cid,"ts":now_iso(),"price":np,"mileage":f.get("mileage_km")}])
            _patch("cars",{"car_id":f"eq.{cid}"},{"last_price":np,"last_mileage":f.get("mileage_km"),"last_seen":now_iso()})
            return True
        except Exception: return False
    def redate_first_point(cid, posted):
        # move a car's earliest price point back to the ad's real posting date (only if it's earlier)
        if not cid or not posted: return
        try:
            rows=_get("price_history",{"car_id":f"eq.{cid}","select":"id,ts","order":"ts.asc","limit":"1"})
            if rows and rows[0].get("ts") and str(posted)<str(rows[0]["ts"]):
                _patch("price_history",{"id":f"eq.{rows[0]['id']}"},{"ts":posted})
        except Exception: pass
    def _cands(f):
        # lean select: only fields matching needs. NEVER fetch photos/description (huge egress).
        p={"active":"eq.true","select":"car_id,make,model,year,engine_cc,fuel,gearbox,body,drivetrain,vin_prefix,owner_code,last_price,last_mileage","limit":"25"}
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
                    _post("price_history",[{"car_id":cid,"ts":f.get("posted") or now_iso(),"price":f.get("price_eur"),"mileage":f.get("mileage_km")}])
                _patch("cars",{"car_id":f"eq.{cid}"},{"active":True,"last_price":f.get("price_eur"),"last_mileage":f.get("mileage_km"),"last_seen":now_iso()})
                return "REPOST"
        # Only merge as a repost when a STRONG signal (VIN/owner/photo) confirmed it above.
        # specs_match alone is far too coarse for detail-less listings (every Focus 2010 looks
        # identical), so we must NOT shunt those to review — that silently drops real cars and
        # caps the catalogue at ~unique-fingerprint count. Default: save as a distinct car.
        if cands and os.environ.get("DEDUP_STRICT")=="1":
            _post("review_queue",[{"ad_id":f["ad_id"],"fingerprint":fingerprint(f)[0],"reason":"specs match, no signal","payload":f}],upsert=True); return "NEEDS_REVIEW"
        cid=f"car_{f['ad_id']}"
        _post("cars",[{"car_id":cid,"fingerprint":fingerprint(f)[0],"source":f.get("source"),"country":f.get("country"),
            "make":f.get("make"),"model":f.get("model"),"year":f.get("year"),"engine_cc":f.get("engine_cc"),
            "engine_l":f.get("engine_l"),"power_kw":f.get("power_kw"),"fuel":f.get("fuel"),"gearbox":f.get("gearbox"),
            "body":f.get("body"),"drivetrain":f.get("drivetrain"),"color":f.get("color"),"owner_code":f.get("owner_code"),
            "vin_prefix":f.get("vin_prefix"),"location":f.get("location"),"photos":f.get("photos") or [],
            "source_url":f.get("source_url"),"description":f.get("description"),"posted":f.get("posted"),"last_price":f.get("price_eur"),
            "last_mileage":f.get("mileage_km"),"active":True,"first_seen":now_iso(),"last_seen":now_iso()}],upsert=True)
        _post("ads",[{"ad_id":f["ad_id"],"car_id":cid,"source":f.get("source"),"source_url":f.get("source_url"),"active":True,"first_seen":now_iso(),"last_seen":now_iso()}],upsert=True)
        # first price point is dated to the ad's ORIGINAL posting date on the source site (falls back to now)
        _post("price_history",[{"car_id":cid,"ts":f.get("posted") or now_iso(),"price":f.get("price_eur"),"mileage":f.get("mileage_km")}])
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
        cut=(datetime.datetime.utcnow()-datetime.timedelta(days=days)).isoformat()+"Z"
        try:
            stale=_get("ads",{"active":"eq.true","last_seen":f"lt.{cut}","select":"ad_id,car_id","limit":"5000"})
            if not stale: print("deactivate: none"); return
            _patch("ads",{"active":"eq.true","last_seen":f"lt.{cut}"},{"active":False})
            cids=list({s["car_id"] for s in stale if s.get("car_id")})
            still=set()
            for i in range(0,len(cids),50):
                chunk=cids[i:i+50]
                rows=_get("ads",{"active":"eq.true","car_id":f"in.({','.join(chunk)})","select":"car_id","limit":"5000"})
                for r in (rows or []): still.add(r.get("car_id"))
            hide=[c for c in cids if c not in still]
            for i in range(0,len(hide),50):
                _patch("cars",{"car_id":f"in.({','.join(hide[i:i+50])})"},{"active":False})
            print(f"deactivate: {len(stale)} ads stale, {len(hide)} cars hidden (VIN + history kept)")
        except Exception as e: print("deactivate err",repr(e))
    def backfill_posted(limit=120):
        # Fill the ORIGINAL posting date for cars that don't have one yet, then re-date each car's
        # earliest price point to it. Each bot only backfills ITS OWN source (via ONLY) so the three
        # run in parallel with no conflict: ss.lv fast (requests), auto24 fast (browser, no limit),
        # autoplius slow (browser, rate-limited).
        ONLY=os.environ.get("ONLY","").strip().lower(); n=0
        if ONLY in ("","ss"):
            try: rows=_get("cars",{"posted":"is.null","source":"eq.ss.lv","select":"car_id,source_url","limit":str(limit)})
            except Exception as e: print("backfill ss query err",repr(e)); rows=[]
            for r in (rows or []):
                try:
                    d=_ss.get(r["source_url"],timeout=20).text
                    po=ss_detail_posted(d); patch={}
                    if po: patch["posted"]=po
                    ph=ss_detail_photos(d)
                    if ph: patch["photos"]=ph
                    if patch: _patch("cars",{"car_id":f"eq.{r['car_id']}"},patch)
                    if po: redate_first_point(r["car_id"], po); n+=1
                except Exception: pass
        if ONLY in ("","auto24"):
            try: rows=_get("cars",{"posted":"is.null","source":"eq.auto24","select":"car_id,source_url","limit":str(limit)})
            except Exception as e: print("backfill a24 query err",repr(e)); rows=[]
            for r in (rows or []):
                try:
                    d=requests.get(r["source_url"],headers=SS_H,timeout=20).text   # date is in raw HTML -> fast, no browser
                    po=a24_posted(d)
                    if po:
                        _patch("cars",{"car_id":f"eq.{r['car_id']}"},{"posted":po})
                        redate_first_point(r["car_id"], po); n+=1
                except Exception: pass
        if ONLY in ("","autoplius") and os.environ.get("BACKFILL_ALL")=="1":
            # autoplius detail pages are SERVER-RENDERED: every photo (autoplius-img.dgn.lt/*.jpg)
            # AND the date live in the raw HTML -> fetch with plain requests (no browser, ~15x faster,
            # same path/speed as ss.lv & auto24). Cars needing photos are exactly those with posted=null
            # (list-only save leaves posted empty + a single thumbnail). Any page that comes back blocked
            # or JS-only is deferred to a small browser pass as a safety net.
            try: rows=_get("cars",{"posted":"is.null","source":"eq.autoplius","select":"car_id,source_url","order":"last_seen.desc","limit":str(limit)})
            except Exception as e: print("backfill ap query err",repr(e)); rows=[]
            need_browser=[]
            for r in (rows or []):
                try:
                    h=_ss.get(r["source_url"],timeout=25).text
                    if "autoplius-img.dgn.lt" not in h:   # blocked / JS shell -> retry via browser later
                        need_browser.append(r); continue
                    det=ap_detail(h,r["source_url"]); patch={}
                    po=det.get("posted") or rel_posted(h)
                    if po: patch["posted"]=po
                    if det.get("photos"): patch["photos"]=det["photos"]
                    if patch:
                        _patch("cars",{"car_id":f"eq.{r['car_id']}"},patch)
                        if po: redate_first_point(r["car_id"], po)
                        n+=1
                    time.sleep(float(os.environ.get("AP_BF_SLEEP","0.4")))
                except Exception: need_browser.append(r)
            if need_browser and os.environ.get("AP_BF_BROWSER","1")=="1":
                try:
                    with browser_session() as page:
                        fetch=make_fetch(page)
                        for r in need_browser[:80]:
                            try:
                                h=fetch(r["source_url"]); det=ap_detail(h,r["source_url"]); patch={}
                                po=det.get("posted") or rel_posted(h)
                                if po: patch["posted"]=po
                                if det.get("photos"): patch["photos"]=det["photos"]
                                if patch:
                                    _patch("cars",{"car_id":f"eq.{r['car_id']}"},patch)
                                    if po: redate_first_point(r["car_id"], po)
                                    n+=1
                            except Exception: pass
                except Exception as e: print("ap bf browser fallback err",repr(e))
        print(f"backfill posted: dated {n} (ONLY={ONLY or 'all'})",flush=True)
    print("STORAGE: Supabase")
else:
    _MEM={"ads":{}}; _CARS={}
    def has_ad(ad_id): return ad_id in _MEM["ads"]
    def has_ads_batch(ad_ids): return {a for a in ad_ids if a in _MEM["ads"]}
    def ad_info_batch(ad_ids): return {}
    def record_price_change(info, f): return False
    def redate_first_point(cid, posted): pass
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
    def backfill_posted(limit=120): pass
    print("STORAGE: listings.json preview (set SUPABASE_* for full catalogue)")

# ============================================================ run
SS_PAGES=int(os.environ.get("SS_PAGES",5)); AP_PAGES=int(os.environ.get("AP_PAGES",3))
A24_PAGES=int(os.environ.get("A24_PAGES",1)); DETAIL_CAP=int(os.environ.get("DETAIL_CAP",200))
SS_DETAIL=int(os.environ.get("SS_DETAIL",1)); PAUSE=0.5
AP_BASE="https://lv.autoplius.lt/sludinajumi/lietotas-automasinas?order_by=1&order_direction=DESC"
A24_BASE="https://eng.auto24.ee/kasutatud/nimekiri.php?a=101102"   # cars+SUV ONLY (~14.7k); a=100 is ALL types incl. motos/trailers/trucks/machinery
SS_BASE="https://www.ss.lv/lv/transport/cars/today/"
SS_H={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36","Accept-Language":"lv,en;q=0.9"}
_ss=requests.Session(); _ss.headers.update(SS_H)
def ss_page(n): return SS_BASE if n==1 else f"{SS_BASE}page{n}.html"
SS_ALL="https://www.ss.lv/lv/transport/cars/"
def ss_brand_slugs():
    try:
        html=_ss.get(SS_ALL,timeout=25).text
        bad={"new","search","today","sell","buy","change","hand","mans","my","retro-cars","sport-cars","electric-cars"}
        out=[]
        for s in re.findall(r'/lv/transport/cars/([a-z0-9-]+)/"',html):
            if s not in bad and s not in out: out.append(s)
        return out
    except Exception as e:
        print("brand list err",repr(e)); return []
def ss_brand_page(slug,n): return f"{SS_ALL}{slug}/" if n==1 else f"{SS_ALL}{slug}/page{n}.html"

def run():
    if USE_SB and os.environ.get("REACTIVATE")=="1":
        try:
            _patch("cars",{"active":"eq.false"},{"active":True}); _patch("ads",{"active":"eq.false"},{"active":True})
            print("REACTIVATE: falsely-hidden cars set back to active")
        except Exception as e: print("reactivate err",repr(e))
    new=seen=0; seen_ids=set()
    ONLY=os.environ.get("ONLY","").strip().lower()  # ""=all; "ss"/"autoplius"/"auto24" -> run only that source (parallel bots)
    for n in (range(1,SS_PAGES+1) if ONLY in ("","ss") else []):
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
                    po=ss_detail_posted(d)
                    if po: f["posted"]=po
                except Exception: pass
            try: save(f); new+=1
            except Exception as e: print("  skip",f["ad_id"],repr(e))
        time.sleep(PAUSE)
    if os.environ.get("SS_FULL")=="1" and ONLY in ("","ss"):
        brands=ss_brand_slugs()
        if brands:
            per=int(os.environ.get("SS_BRANDS_PER_RUN",8)); bp=int(os.environ.get("SS_BRAND_PAGES",6))
            sdet=0; pchg=0; SSDET=int(os.environ.get("SS_SWEEP_DET",150))
            start=get_cursor("ss_brand")
            if not isinstance(start,int) or start>=len(brands): start=0
            print(f"ss.lv brand sweep START: cursor={start}, brands={len(brands)}, per={per}, bp={bp}",flush=True)
            for slug in brands[start:start+per]:
                prev_ids=None
                print(f"ss.lv brand '{slug}' begin",flush=True)
                for n in range(1,bp+1):
                    rows=[]
                    for attempt in range(4):
                        try: html=_ss.get(ss_brand_page(slug,n),timeout=25).text
                        except Exception: html=""
                        rows=ss_list(html)["ads"] if html else []
                        if rows: break
                        time.sleep(6+attempt*10)   # throttle backoff: 6s,16s,26s before giving up the page
                    if not rows:
                        print(f"ss.lv {slug} p{n}: empty after retries (throttle?) -> next brand",flush=True); break
                    ids=frozenset(f["ad_id"] for f in rows)
                    if ids==prev_ids: break   # ss.lv repeats last page beyond real max = genuine end of brand
                    prev_ids=ids
                    info=ad_info_batch([f["ad_id"] for f in rows])   # existing ads + their stored price
                    todo=[]
                    for f in rows:
                        seen+=1; seen_ids.add(f["ad_id"])
                        if f["ad_id"] in info:
                            if record_price_change(info[f["ad_id"]], f): pchg+=1   # price moved on same listing
                            continue
                        f["source"]="ss.lv"; todo.append(f)
                    print(f"ss.lv {slug} p{n}: rows={len(rows)} existing={len(info)} new={len(todo)}",flush=True)
                    def _ssdet(f):
                        try:
                            d=requests.get(f["source_url"],headers=SS_H,timeout=20).text   # thread-safe: own connection, not the shared session
                            ph=ss_detail_photos(d)
                            if ph: f["photos"]=ph
                            ml=ss_detail_mileage(d)
                            if ml is not None: f["mileage_km"]=ml
                            f["description"]=ss_detail_desc(d) or "-"
                            po=ss_detail_posted(d)
                            if po: f["posted"]=po
                        except Exception: pass
                    dset=todo[:max(0,SSDET-sdet)]   # detail-fetch budget for this page
                    if dset:
                        from concurrent.futures import ThreadPoolExecutor
                        with ThreadPoolExecutor(max_workers=int(os.environ.get("SS_WORKERS","6"))) as ex:
                            list(ex.map(_ssdet,dset))   # fetch details in parallel (ss.lv is direct/fast)
                        sdet+=len(dset)
                    saved=0
                    for f in todo:
                        try: save(f); new+=1; saved+=1
                        except Exception as e: print(f"  ss save FAIL {f.get('ad_id')} make={f.get('make')!r}: {e!r}",flush=True)
                    time.sleep(float(os.environ.get("SS_PAGE_PAUSE","0.35")))
            nxt=start+per
            set_cursor("ss_brand", 0 if nxt>=len(brands) else nxt)
            print(f"ss.lv full sweep: brands {start}..{start+per}/{len(brands)}, new total {new}, price changes {pchg}")
    details=0
    try:
        if os.environ.get("SKIP_LTEE")=="1" or ONLY=="ss": raise RuntimeError("skip-ltee")
        with browser_session() as page:
            fetch=make_fetch(page)
            def crawl(name, base, pages, plist, pdetail, country, paginate, cursor_key=None):
                nonlocal new,seen
                dcount=0
                start=get_cursor(cursor_key) if cursor_key else 1
                empty=False
                scan = [1,2] + [p for p in range(start,start+pages) if p>2] if cursor_key else list(range(start,start+pages))
                for n in scan:
                    url=paginate(base,n) if paginate else base
                    try: txt=fetch(url)
                    except Exception as e: print(name,"page",n,"fetch ERR",repr(e)); break
                    ads=plist(txt)["ads"]
                    if not ads:
                        empty=True
                        ti=re.search(r"<title[^>]*>(.*?)</title>",txt,re.I|re.S)
                        print(f"{name} page {n}: 0 ads | len={len(txt)} | title={(ti.group(1).strip()[:90] if ti else '?')} | head={txt[:200].replace(chr(10),' ')}")
                        break
                    existing=has_ads_batch([ad["ad_id"] for ad in ads])   # 1 query instead of ~50
                    for ad in ads:
                        seen+=1; seen_ids.add(ad["ad_id"])
                        if ad["ad_id"] in existing: continue
                        if dcount>=DETAIL_CAP:
                            print(name,"hit DETAIL_CAP")
                            if cursor_key: set_cursor(cursor_key,n)
                            return
                        dcount+=1
                        try:
                            f=pdetail(fetch(ad["url"]),source_url=ad["url"]); f["ad_id"]=ad["ad_id"]; f["source"]=name
                            if country: f.setdefault("country",country)
                            save(f); new+=1
                        except Exception as e: print("  skip",ad["ad_id"],repr(e))
                    print(f"{name} page {n} (cursor): new total {new}, details {dcount}"); time.sleep(PAUSE)
                if cursor_key: set_cursor(cursor_key, 1 if empty else start+pages)
            def ap_year_sweep():
                # autoplius flat search caps at ~150 pages (~3.7k). To reach the full ~42k catalogue,
                # split by manufacture YEAR (make_date_from=Y&make_date_to=Y); each year is well under the cap.
                nonlocal new,seen
                years=list(range(2026,1989,-1))
                per=int(os.environ.get("AP_YEARS_PER_RUN",2)); yp=int(os.environ.get("AP_YEAR_PAGES",150))
                ystart=get_cursor("ap_year")
                if not isinstance(ystart,int) or ystart>=len(years): ystart=0
                apcap=int(os.environ.get("AP_DETAIL_CAP", os.environ.get("DETAIL_CAP",4000)))
                apause=float(os.environ.get("AP_PAUSE","3"))  # throttle: pause after each NEW detail to stay under autoplius rate-limit
                det=0; yi=ystart
                while yi<min(ystart+per,len(years)):
                    y=years[yi]; yb=f"{AP_BASE}&make_date_from={y}&make_date_to={y}"
                    print(f"ap_year {y}: START (up to {yp} pages, detail cap {apcap})", flush=True)
                    for n in range(1,yp+1):
                        url=yb if n==1 else f"{yb}&page_nr={n}"
                        print(f"ap_year {y} p{n}: fetching list...", flush=True)
                        try: txt=fetch(url)
                        except Exception as e: print("ap_year",y,"p",n,"list ERR",repr(e),flush=True); break
                        ads=ap_list(txt,"lv")["ads"]
                        if not ads:
                            blocked = ("peržiūros limit" in txt or "viršijote" in txt)
                            if blocked:
                                got=False
                                for rl in range(2):   # extra recovery beyond fetch()'s own retries; keep going like auto24
                                    print(f"ap_year {y} p{n}: RATE-LIMITED - waiting {45*(rl+1)}s and retrying...",flush=True)
                                    time.sleep(45*(rl+1))   # 45s, then 90s
                                    try: txt=fetch(url)
                                    except Exception as e: print("ap_year",y,"p",n,"retry ERR",repr(e),flush=True); break
                                    ads=ap_list(txt,"lv")["ads"]
                                    if ads: got=True; break
                                if not got:
                                    print(f"ap_year {y} p{n}: still limited after retries -> next year",flush=True); break
                            else:
                                print(f"ap_year {y} p{n}: 0 ads (len={len(txt)}) -> year done",flush=True); break
                        print(f"ap_year {y} p{n}: {len(ads)} ads found", flush=True)
                        if os.environ.get("AP_LIST_ONLY","1")=="1":
                            # FAST PATH: parse make/model/year/price straight off the results page,
                            # save without a per-car detail fetch (~20x fewer requests). Details/photos
                            # can be backfilled later; priority is getting all ~42k listed quickly.
                            rich=ap_list_rich(txt)["ads"]
                            exb=ad_info_batch([a["ad_id"] for a in rich])
                            for a in rich:
                                seen+=1; seen_ids.add(a["ad_id"])
                                if a["ad_id"] in exb:
                                    record_price_change(exb[a["ad_id"]], a)   # log price move on same listing
                                    # already have the car; backfill its photo if it has none yet
                                    if a.get("photos") and os.environ.get("AP_PATCH_PHOTOS","1")=="1":
                                        try: _patch("cars",{"car_id":f"eq.car_{a['ad_id']}","photos":"eq.{}"},{"photos":a["photos"]})
                                        except Exception: pass
                                    continue
                                a["source"]="autoplius"
                                try: save(a); new+=1
                                except Exception as e: print("  ap save FAIL",a["ad_id"],repr(e),flush=True)
                            print(f"ap_year {y} p{n}: list-only {len(rich)} parsed, new total {new}",flush=True)
                            if len(ads) < 12:   # full pages are ~20; a short page = last real page or fallback -> year done
                                print(f"ap_year {y} p{n}: partial page ({len(ads)} ads) -> year done",flush=True); break
                            time.sleep(float(os.environ.get("AP_PAGE_PAUSE","1.0")))
                            continue
                        for ad in ads:
                            seen+=1; seen_ids.add(ad["ad_id"])
                            if has_ad(ad["ad_id"]): continue
                            if det>=apcap:
                                set_cursor("ap_year",yi); print(f"ap_year {y}: hit AP_DETAIL_CAP p{n}",flush=True); return
                            det+=1
                            try:
                                f=ap_detail(fetch(ad["url"]),source_url=ad["url"]); f["ad_id"]=ad["ad_id"]; f["source"]="autoplius"; save(f); new+=1
                            except Exception as e: print("  skip",ad["ad_id"],repr(e),flush=True)
                            time.sleep(apause)
                        print(f"ap_year {y} p{n} DONE: new total {new}, det {det}", flush=True); time.sleep(PAUSE)
                    print(f"ap_year {y}: done, new total {new}, det {det}"); yi+=1
                set_cursor("ap_year", 0 if yi>=len(years) else yi)
            if ONLY in ("","autoplius"):
                crawl("autoplius",AP_BASE,AP_PAGES,lambda t:ap_list(t,"lv"),ap_detail,None,page_url,"autoplius")
                if os.environ.get("AP_FULL")=="1": ap_year_sweep()
            # auto24 full used-vehicle list (~22k). Pages via ?ak=<offset of 50>; cursor walks deep across cycles.
            def a24_paginate(base,n):
                return base if n<=1 else base+("&" if "?" in base else "?")+f"ak={(n-1)*50}"
            if ONLY in ("","auto24"):
                # auto24 cars+SUV is ~14.7k (~300 pages of 50). If the cursor walked far past that, it's just
                # re-scanning duplicates -> reset to re-sweep from the start and pick up anything missed.
                _c=get_cursor("auto24")
                if isinstance(_c,int) and _c>int(os.environ.get("A24_MAXPAGE","300")):
                    set_cursor("auto24",1); print(f"auto24: cursor was {_c} (past catalogue) -> reset to 1")
                crawl("auto24",A24_BASE,A24_PAGES,a24_list,a24_detail,"EE",a24_paginate,"auto24")
    except Exception as e:
        if str(e)=="skip-ltee": print("SKIP_LTEE=1 -> autoplius/auto24 skipped this run")
        else: print("browser phase error:",repr(e))
    bump_seen(seen_ids)
    bf=int(os.environ.get("BACKFILL",0))
    if bf>0: backfill_posted(bf)
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
# BalticRadar all-in-one collector - end
