"""
BalticRadar - alert notifier.

CORE PRODUCT: a user saves a filter (e.g. Citroen + Nissan). The moment a matching
car appears on ss.lv / auto24 / autoplius, they get an email. Timeliness IS the product.

How it works
------------
1. Pull cars INGESTED SINCE THE LAST RUN (first_seen >= now - LOOKBACK_MIN), not the catalogue.
2. Pull every saved_filter with notify_email = true whose owner is PREMIUM (alerts are paid).
3. Match each new car against each filter's criteria (multi-value: makes/models/fuels/...).
4. Email the owner, then record (filter_id, car_id) in filter_notifications so the same car
   is NEVER sent twice, even if runs overlap.
5. Legacy `subscriptions` (the old alert profile) are still honoured, deduped via `notifications`.

Env
---
SUPABASE_URL, SUPABASE_KEY (service key)   required
RESEND_API_KEY                             required to actually send; without it -> dry print
ALERT_FROM      sender, e.g. "BalticRadar <alerts@yourdomain.lv>"
                DEFAULT onboarding@resend.dev only delivers to the Resend account OWNER.
LOOKBACK_MIN    default 45 (cron runs every 20 min -> overlap is safe, dedupe absorbs it)
MAX_PER_EMAIL   default 12
MAX_EMAILS      default 80 (Resend free tier = 100/day)
TEST_TO         if set, EVERY email is redirected to this address (end-to-end test)
DRY_RUN         "1" -> match + render but send nothing
"""
import os, sys, json, datetime, requests

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_KEY", "")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

LOOKBACK_MIN = int(os.environ.get("LOOKBACK_MIN", 1560))   # 26 h: must cover the SLOWEST cadence
MAX_PER_EMAIL = int(os.environ.get("MAX_PER_EMAIL", 12))
MAX_EMAILS = int(os.environ.get("MAX_EMAILS", 80))

# ---------------------------------------------------------------- CADENCE (anti-spam)
# The job runs every 20 minutes. Before this, ANY new match sent an e-mail immediately, so a single
# saved filter could fire up to 72 times a day and a user with three filters over 200. That is how
# you get unsubscribed on day one and how Gmail learns to junk your domain.
#
# Now every filter has a cadence and we simply refuse to e-mail it again until its cooldown has
# expired. Nothing is lost: unsent cars stay unrecorded in filter_notifications and roll into the
# next batch, because the lookback window (26 h) is wider than the slowest cadence.
FREQ_COOLDOWN_MIN = {
    "instant":    60,     # PREMIUM. As fast as we will ever go: at most one e-mail an hour.
    "few_hours":  360,    # PREMIUM. Every ~6 h -> at most 4 a day.
    "daily":     1200,    # 20 h, so a "daily" digest never drifts past its slot. Max 1 a day.
}
DEFAULT_FREQ = "daily"          # the safe default for every NEW filter
FREE_FREQ = "daily"             # free accounts always get the digest; instant/hourly is the paywall
# Hard ceiling per USER per day, whatever their filters say. Anything over it rolls into tomorrow.
DAILY_CAP = int(os.environ.get("DAILY_CAP", 6))

# ---------------------------------------------------------------- GLOBAL PROVIDER BUDGET
# Resend's free tier is 100 e-mails A DAY across the whole account. Burn it and EVERY user - paying
# ones included - silently receives nothing for the rest of the day, with no error anywhere on the
# site. Refuse to spend past the reserve and say so loudly instead.
PROVIDER_DAILY_LIMIT = int(os.environ.get("PROVIDER_DAILY_LIMIT", 100))
PROVIDER_RESERVE = int(os.environ.get("PROVIDER_RESERVE", 10))     # never spend the last 10
GLOBAL_BUDGET = max(0, PROVIDER_DAILY_LIMIT - PROVIDER_RESERVE)    # -> 90/day
SITE = os.environ.get("SITE_URL", "https://balticradar.meowlybusiness.workers.dev")
RESEND_KEY = os.environ.get("RESEND_API_KEY")
# NOTE: an UNSET GitHub secret is passed through as an EMPTY STRING, not as "missing" - so
# os.environ.get(..., default) would return "" and Resend answers 422 "domain is invalid".
# Use `or` so an empty ALERT_FROM falls back to the shared sender.
FROM = (os.environ.get("ALERT_FROM") or "").strip() or "BalticRadar <onboarding@resend.dev>"
TEST_TO = (os.environ.get("TEST_TO") or "").strip()
ONLY_EMAIL = (os.environ.get("ONLY_EMAIL") or "").strip().lower()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# TEST-ONLY: skip the (filter_id, car_id) dedupe read AND skip recording the send, so a manual
# dispatch can re-render a real e-mail from cars that were already alerted on. Never set on cron.
IGNORE_SENT = os.environ.get("IGNORE_SENT") == "1"

# The legacy `subscriptions` table (the old pre-account alert form) is DEAD: openSub() has sent
# users to Mans profils > Mani filtri for a long time and nothing writes to it any more. What is
# left in it is 16 rows of test junk - probe_*@test.invalid, coltest@example.com - plus one row
# with EVERY criterion NULL, which therefore matched EVERY new car in the catalogue: that is the
# "92 cars in one e-mail" the owner was getting. Off by default; set LEGACY_SUBS=1 to resurrect it.
LEGACY_SUBS = os.environ.get("LEGACY_SUBS") == "1"

FLAG = {"LV": "\U0001F1F1\U0001F1FB", "LT": "\U0001F1F1\U0001F1F9", "EE": "\U0001F1EA\U0001F1EA"}


# ---------------------------------------------------------------- supabase
def get(path):
    try:
        r = requests.get(f"{URL}/rest/v1/{path}", headers=H, timeout=40)
        d = r.json()
        return d if isinstance(d, list) else []
    except Exception as e:
        print("GET error", path, e)
        return []


def post(path, rows, prefer=None):
    if not rows:
        return None
    h = dict(H)
    if prefer:
        h["Prefer"] = prefer
    try:
        return requests.post(f"{URL}/rest/v1/{path}", headers=h, json=rows, timeout=40)
    except Exception as e:
        print("POST error", path, e)
        return None


def patch(path, body):
    try:
        return requests.patch(f"{URL}/rest/v1/{path}", headers=H, json=body, timeout=30)
    except Exception as e:
        print("PATCH error", path, e)
        return None


# ---------------------------------------------------------------- cadence helpers
def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_ts(v):
    if not v:
        return None
    t = str(v).replace("Z", "+00:00")
    try:
        d = datetime.datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def freq_of(f, is_premium):
    """A free account is ALWAYS on the daily digest - instant/hourly alerts are the paid feature."""
    v = (f.get("notify_freq") or DEFAULT_FREQ).strip().lower()
    if v not in FREQ_COOLDOWN_MIN:
        v = DEFAULT_FREQ
    if not is_premium:
        return FREE_FREQ
    return v


def cooling_down(f, freq):
    """True -> this filter e-mailed too recently. Its new cars are simply left for the next batch."""
    last = _parse_ts(f.get("last_notified_at"))
    if not last:
        return False
    mins = (_now() - last).total_seconds() / 60.0
    need = FREQ_COOLDOWN_MIN[freq]
    if mins < need:
        print(f"  cooldown: last sent {mins:.0f} min ago, '{freq}' needs {need} -> hold")
        return True
    return False


def sends_today(user_id):
    """How many alert e-mails this user has already had since midnight UTC.
    Degrades gracefully: if alert_sends does not exist yet (db_alert_cadence.sql not run), we
    return 0 and rely on the per-filter cooldowns alone rather than blocking every alert."""
    if not user_id:
        return 0
    day = _now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        h = dict(H); h["Prefer"] = "count=exact"; h["Range"] = "0-0"
        r = requests.get(f"{URL}/rest/v1/alert_sends?select=id&user_id=eq.{user_id}"
                         f"&sent_at=gte.{day}", headers=h, timeout=30)
        if not r.ok:
            print("  !! alert_sends unreadable -> per-user daily cap NOT enforced. "
                  "Run db_alert_cadence.sql in Supabase.")
            return 0
        return int((r.headers.get("content-range") or "0/0").split("/")[-1])
    except Exception as e:
        print("  !! alert_sends error", e)
        return 0


def global_sends_today():
    """Every alert e-mail sent by the account since midnight UTC. Same graceful degradation as
    sends_today(): if the ledger is missing we cannot enforce the budget, and we shout about it."""
    day = _now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        h = dict(H); h["Prefer"] = "count=exact"; h["Range"] = "0-0"
        r = requests.get(f"{URL}/rest/v1/alert_sends?select=id&sent_at=gte.{day}", headers=h, timeout=30)
        if not r.ok:
            print("!! alert_sends unreadable -> GLOBAL PROVIDER BUDGET NOT ENFORCED. "
                  "Run db_alert_cadence.sql in Supabase NOW.")
            return None
        return int((r.headers.get("content-range") or "0/0").split("/")[-1])
    except Exception as e:
        print("!! alert_sends error", e)
        return None


def record_send(user_id, filter_id):
    """One row per e-mail actually sent. This ledger is what makes both the per-user cap and the
    GLOBAL provider budget enforceable. Legacy subscriptions have no user_id -> recorded as NULL,
    because they still spend a message from the same 100/day account budget."""
    post("alert_sends", [{"user_id": user_id, "filter_id": filter_id,
                          "sent_at": _now().isoformat()}])


# ---------------------------------------------------------------- normalisation
def _nb(v):
    s = (v or "").lower()
    if "sedan" in s: return "sedans"
    if "hatch" in s or "hečbek" in s or "hecbek" in s: return "hečbeks"
    if "coupe" in s or "kupej" in s: return "kupeja"
    if "cabrio" in s or "kabriolet" in s or "roadster" in s or "convertible" in s: return "kabriolets"
    if "pickup" in s or "pikap" in s: return "pikaps"
    if "suv" in s or "visurg" in s or "krosover" in s or "cross" in s or "apvidus" in s: return "apvidus"
    if "universal" in s or "estate" in s or "wagon" in s or "kombi" in s or "touring" in s or "caravan" in s: return "universālis"
    if "minibus" in s or "minivan" in s or "miniven" in s or "van" in s: return "minivens"
    if "limous" in s or "limuzin" in s: return "limuzīns"
    return s


_FUEL = {"petrol": "benzīns", "gasoline": "benzīns", "diesel": "dīzelis", "electric": "elektrība",
         "hybrid": "hibrīds", "gas": "gāze", "lpg": "gāze"}
_GEAR = {"automatic": "automātiskā", "manual": "mehāniskā"}
_DRIVE = {"fwd": "priekšējā", "front": "priekšējā", "rwd": "aizmugurējā", "rear": "aizmugurējā",
          "awd": "pilnpiedziņa", "4x4": "pilnpiedziņa", "4wd": "pilnpiedziņa"}


def _nf(v): s = (v or "").lower().strip(); return _FUEL.get(s, s)
def _ng(v): s = (v or "").lower().strip(); return _GEAR.get(s, s)
def _nd(v):
    s = (v or "").lower().strip()
    for k, x in _DRIVE.items():
        if k in s:
            return x
    return s


def model_series(make, model):
    """Mirror of the site's modelSeries(): '3. sērija' must match 320/318/330..."""
    m = (model or "").strip()
    mk = (make or "").lower()
    if not m:
        return m
    import re
    if mk == "bmw":
        x = re.match(r"^X\s?(\d)", m, re.I)
        if x: return "X" + x.group(1)
        z = re.match(r"^Z\s?(\d)", m, re.I)
        if z: return "Z" + z.group(1)
        d = re.match(r"^(\d)\d{2}", m)
        if d: return d.group(1) + ". sērija"
        return m
    if "mercedes" in mk:
        g = re.match(r"^(GLE|GLC|GLA|GLS|GLK|GLB|ML|CLA|CLS|SLK|SLC)", m, re.I)
        if g: return g.group(1).upper()
        c = re.match(r"^([A-Z])\s?\d", m, re.I)
        if c: return c.group(1).upper() + "-klase"
        return m
    if mk == "audi":
        a = re.match(r"^(RS\s?\d|S\s?\d|SQ\d|A\d|Q\d|TT|R8)", m, re.I)
        if a: return a.group(1).upper().replace(" ", "")
        return m
    return m


def arr(v):
    """criteria values may be a list (new multi-select) or a bare string (legacy)."""
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)]


def crit(c, *keys):
    """first non-empty of the given criteria keys (plural form first)"""
    for k in keys:
        a = arr(c.get(k))
        if a:
            return a
    return []


# ---------------------------------------------------------------- matching
def matches(car, c):
    """c = criteria dict. EMPTY LIST = no constraint (match all). Never invert that."""
    countries = crit(c, "countries", "country")
    if countries and car.get("country") not in countries:
        return False

    makes = [m.lower() for m in crit(c, "makes", "make")]
    if makes and (car.get("make") or "").lower() not in makes:
        return False

    # models_q holds "Make|Model" pairs -> match the PAIR, so picking Audi A4 + BMW 320 can never
    # let a BMW "A4" through. Fall back to bare model names for older/legacy filters.
    pairs = crit(c, "models_q")
    if pairs and any("|" in p for p in pairs):
        cmk = (car.get("make") or "").lower()
        cm = (car.get("model") or "").lower()
        cs = model_series(car.get("make"), car.get("model")).lower()
        ok = False
        for p in pairs:
            mk, _, md = p.partition("|")
            if mk.lower() == cmk and md.lower() in (cm, cs):
                ok = True
                break
        if not ok:
            return False
    else:
        models = crit(c, "models", "model")
        if models:
            cm = (car.get("model") or "").lower()
            cs = model_series(car.get("make"), car.get("model")).lower()
            if not any(m.lower() == cm or m.lower() == cs for m in models):
                return False

    fuels = [_nf(x) for x in crit(c, "fuels", "fuel")]
    if fuels and _nf(car.get("fuel")) not in fuels:
        return False

    gears = [_ng(x) for x in crit(c, "gearboxes", "gearbox")]
    if gears and _ng(car.get("gearbox")) not in gears:
        return False

    bodies = [_nb(x) for x in crit(c, "bodies", "body")]
    if bodies and _nb(car.get("body")) not in bodies:
        return False

    drives = [_nd(x) for x in crit(c, "drivetrains", "drivetrain")]
    if drives and _nd(car.get("drivetrain")) not in drives:
        return False

    p = car.get("last_price")
    if c.get("price_min") and (p is None or p < c["price_min"]): return False
    if c.get("price_max") and (p is None or p > c["price_max"]): return False

    y = car.get("year") or 0
    if c.get("year_min") and y < c["year_min"]: return False
    if c.get("year_max") and y > c["year_max"]: return False

    m = car.get("last_mileage")
    if c.get("km_min") and (m or 0) < c["km_min"]: return False
    if c.get("km_max") and (m is None or m > c["km_max"]): return False

    try:
        e = float(car.get("engine_l") or 0)
    except (TypeError, ValueError):
        e = 0
    if c.get("eng_min") and e < c["eng_min"]: return False
    if c.get("eng_max") and e and e > c["eng_max"]: return False
    return True


# ---------------------------------------------------------------- email
def eur(n):
    try:
        return f"{int(n):,}".replace(",", " ") + " &euro;"
    except (TypeError, ValueError):
        return ""


FONT = "'Manrope','Helvetica Neue',Helvetica,Arial,sans-serif"
ACC = "#16a06a"
INK = "#1d1d1f"
INK2 = "#5b6470"
MUT = "#8a929c"
LINE = "#e7e9ee"


def car_url(c):
    """The CTA opens the listing ON BALTICRADAR (/?car=<id>), not on ss.lv.
    The source-portal link still lives inside our own detail view."""
    return f"{SITE}/?car={c.get('car_id')}"


def specs_of(c):
    bits = []
    if c.get("last_mileage") is not None:
        bits.append(f"{int(c['last_mileage']):,}".replace(",", " ") + " km")
    if c.get("fuel"):
        bits.append(_nf(c["fuel"]).capitalize())
    if c.get("engine_l"):
        bits.append(f"{c['engine_l']} l")
    if c.get("gearbox"):
        bits.append(_ng(c["gearbox"]).capitalize())
    if c.get("body"):
        bits.append(_nb(c["body"]).capitalize())
    return " &middot; ".join(b for b in bits if b)


def car_row(c, drop=None):
    """One white card: table-based, fully inline, stacks on narrow screens."""
    img = (c.get("photos") or [None])[0]
    alt = f"{c.get('make') or ''} {c.get('model') or ''}".strip() or "Auto"
    thumb = (f'<img src="{img}" width="180" alt="{alt}" border="0" '
             f'style="width:180px;max-width:180px;height:126px;object-fit:cover;display:block;'
             f'border:0;border-radius:12px;background:#eef0f4">'
             if img else
             '<div style="width:180px;height:126px;background:#eef0f4;border-radius:12px"></div>')
    title = f"{c.get('make') or ''} {c.get('model') or ''}".strip()
    year = c.get("year") or ""
    flag = FLAG.get(c.get("country") or "", "")
    src = c.get("source") or ""
    url = car_url(c)

    drop_badge = (
        f'<div style="margin:0 0 7px">'
        f'<span style="display:inline-block;background:#e8f7f0;color:{ACC};'
        f'font:700 11px/1.6 {FONT};padding:2px 9px;border-radius:999px;white-space:nowrap">'
        f'&darr; Cena kritusi &nbsp;<span style="text-decoration:line-through;color:{MUT};font-weight:600">'
        f'{eur(drop)}</span></span></div>') if drop else ""

    return (
        f'<tr><td style="padding:0 0 14px">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="border-collapse:separate;background:#ffffff;border:1px solid {LINE};border-radius:16px">'
        f'<tr>'
        f'<td class="ph" width="180" style="width:180px;padding:14px 0 14px 14px;vertical-align:top">'
        f'<a href="{url}" target="_blank" style="text-decoration:none">{thumb}</a></td>'
        f'<td class="bd" style="padding:14px;vertical-align:top;font-family:{FONT}">'
        f'{drop_badge}'
        f'<a href="{url}" target="_blank" style="color:{INK};text-decoration:none;'
        f'font:800 17px/1.25 {FONT};letter-spacing:-.01em">{title} '
        f'<span style="color:{INK2};font-weight:600">{year}</span></a>'
        f'<div style="margin:6px 0 10px;color:{INK2};font:500 13px/1.5 {FONT}">{specs_of(c)}</div>'
        f'<div style="color:{INK};font:800 21px/1.2 {FONT};letter-spacing:-.02em">{eur(c.get("last_price"))}</div>'
        f'<div style="margin:6px 0 12px;color:{MUT};font:600 12px/1.5 {FONT}">{flag} {src}</div>'
        f'<a href="{url}" target="_blank" style="display:inline-block;background:{ACC};color:#ffffff;'
        f'text-decoration:none;font:800 13px/1 {FONT};padding:11px 18px;border-radius:10px">'
        f'Atvērt sludinājumu &rarr;</a>'
        f'</td></tr></table></td></tr>'
    )


MQ = (
    "@media only screen and (max-width:480px){"
    ".wrap{width:100% !important}"
    ".ph,.bd{display:block !important;width:100% !important;box-sizing:border-box}"
    ".ph{padding:14px 14px 0 !important}"
    ".bd{padding:12px 14px 16px !important}"
    ".ph img,.ph div{width:100% !important;max-width:100% !important;height:200px !important}"
    ".cta a{display:block !important}"
    "}"
)


FREQ_LABEL = {
    "instant":   "tūlītēji (ne biežāk kā reizi stundā)",
    "few_hours": "ik pēc dažām stundām",
    "daily":     "reizi dienā (kopsavilkums)",
}


def build_html(name, filter_name, cars, drops, extra, freq="daily"):
    rows = "".join(car_row(c, drops.get(c["car_id"])) for c in cars)
    n = len(cars)
    hello = f", {name}" if name else ""
    more = (f'<tr><td style="padding:2px 0 14px;text-align:center;font:600 13px/1.5 {FONT};color:{INK2}">'
            f'&hellip;un vēl <b style="color:{INK}">{extra}</b> jauni sludinājumi pēc šī filtra &mdash; '
            f'<a href="{SITE}" target="_blank" style="color:{ACC};text-decoration:none;font-weight:800">'
            f'skatīt visus</a></td></tr>') if extra > 0 else ""
    host = SITE.replace("https://", "").replace("http://", "")

    return (
        '<!DOCTYPE html><html lang="lv"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light only">'
        '<meta name="supported-color-schemes" content="light only">'
        '<title>Jauni auto | BalticRadar</title>'
        f'<style>{MQ}</style>'
        '</head>'
        '<body style="margin:0;padding:0;background:#f4f5f7;-webkit-font-smoothing:antialiased">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{n} jauns(-i) auto pēc filtra &bdquo;{filter_name}&ldquo; &mdash; tikko parādījās.</div>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f4f5f7">'
        '<tr><td align="center" style="padding:26px 12px">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" class="wrap" style="width:600px;max-width:600px">'

        '<tr><td style="padding:0 4px 18px">'
        f'<a href="{SITE}" target="_blank" style="text-decoration:none;font:800 24px/1 {FONT};color:{INK};letter-spacing:-.03em">'
        f'Baltic<span style="color:{ACC}">Radar</span></a></td></tr>'

        '<tr><td style="padding:0 0 16px">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#ffffff;border:1px solid {LINE};border-radius:16px">'
        '<tr><td style="padding:20px 18px">'
        f'<div style="font:800 19px/1.3 {FONT};color:{INK};letter-spacing:-.02em">Sveiki{hello} &mdash; {n} jauns(-i) auto tavam filtram</div>'
        f'<div style="margin-top:9px;font:600 13px/1.5 {FONT};color:{INK2}">Filtrs: '
        f'<span style="display:inline-block;background:#e8f7f0;color:{ACC};padding:3px 11px;border-radius:999px;font-weight:800">{filter_name}</span></div>'
        f'<div style="margin-top:11px;font:500 13px/1.6 {FONT};color:{MUT}">Šie sludinājumi tikko parādījās ss.lv / autoplius.lt / auto24.ee.</div>'
        '</td></tr></table></td></tr>'

        f'{rows}{more}'

        '<tr><td class="cta" align="center" style="padding:8px 0 26px">'
        f'<a href="{SITE}" target="_blank" style="display:inline-block;background:{INK};color:#ffffff;text-decoration:none;'
        f'font:800 14px/1 {FONT};padding:15px 30px;border-radius:12px">Skatīt visu katalogu &rarr;</a></td></tr>'

        f'<tr><td style="padding:18px 6px 6px;border-top:1px solid {LINE}">'
        f'<div style="font:600 12px/1.7 {FONT};color:{MUT}">'
        f'Šo e-pastu saņem, jo tev ir saglabāts filtrs ar ieslēgtiem paziņojumiem. '
        f'Biežums: <b style="color:{INK}">{FREQ_LABEL.get(freq, FREQ_LABEL["daily"])}</b>. '
        f'To vari mainīt vai izslēgt jebkurā brīdī.<br>'
        f'<a href="{SITE}/?p=filters" target="_blank" style="color:{ACC};text-decoration:none;font-weight:800">Mainīt biežumu / atteikties no paziņojumiem</a>'
        ' &nbsp;&middot;&nbsp; '
        f'<a href="mailto:meowlybusiness@gmail.com" style="color:{MUT};text-decoration:none">meowlybusiness@gmail.com</a></div>'
        f'<div style="margin-top:10px;font:500 11px/1.6 {FONT};color:#a8aeb7">'
        f'BalticRadar &middot; lietotu auto meklētājs Latvijā, Lietuvā un Igaunijā &middot; {host}</div>'
        '</td></tr>'

        '</table></td></tr></table></body></html>'
    )


def build_text(filter_name, cars, extra):
    L = [f'Jauni auto pēc filtra "{filter_name}" - BalticRadar', ""]
    for c in cars:
        price = f"{c.get('last_price')} EUR" if c.get("last_price") else "cena nav noradita"
        L.append(f"- {c.get('make') or ''} {c.get('model') or ''} {c.get('year') or ''} · {price}")
        L.append(f"  {car_url(c)}")
    if extra > 0:
        L.append(f"...un vēl {extra} sludinājumi.")
    L += ["", f"Skatīt visus: {SITE}",
          f"Mainīt paziņojumu biežumu vai atteikties: {SITE}/?p=filters"]
    return "\n".join(L)


SHARED_FROM = "BalticRadar <onboarding@resend.dev>"


def _resend(sender, to, subject, html, text):
    return requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
        json={"from": sender, "to": [to], "subject": subject, "html": html, "text": text,
              # Gmail and Yahoo penalise bulk senders that offer no unsubscribe header. Without
              # this, a burst of alerts is exactly the pattern that gets a domain filed as spam.
              "headers": {
                  "List-Unsubscribe": f"<mailto:meowlybusiness@gmail.com?subject=unsubscribe>, "
                                      f"<{SITE}/?p=filters>",
                  "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
              }},
        timeout=30,
    )


def send(to, subject, html, text):
    if TEST_TO:
        subject = f"[TEST -> {to}] {subject}"
        to = TEST_TO
    if DRY_RUN or not RESEND_KEY:
        print(f"  [DRY] would email {to}: {subject}")
        return True

    r = _resend(FROM, to, subject, html, text)
    if r.ok:
        print(f"  -> {to}: {r.status_code} (from {FROM})")
        return True

    body = r.text[:200]
    print(f"  -> {to}: {r.status_code} {body}")

    # The configured sender was rejected. 422 'domain is invalid' / 403 'not verified' both mean
    # the ALERT_FROM domain is not (yet) verified in Resend. Fall back to Resend's shared sender,
    # which STILL DELIVERS TO THE RESEND ACCOUNT OWNER - so the owner keeps getting alerts while
    # the real domain is being set up. Every other recipient will 403 here, and we say so loudly.
    if r.status_code in (403, 422) and FROM != SHARED_FROM:
        print(f"  !! sender '{FROM}' rejected -> retrying with {SHARED_FROM}")
        r2 = _resend(SHARED_FROM, to, subject, html, text)
        if r2.ok:
            print(f"  -> {to}: {r2.status_code} (delivered via shared sender; owner-only)")
            return True
        print(f"  -> {to}: {r2.status_code} {r2.text[:200]}")
        print("  !! BLOCKED: no verified sending domain. onboarding@resend.dev only delivers to the "
              "Resend ACCOUNT OWNER; every other subscriber is rejected. Verify a domain you own in "
              "Resend, add its DKIM/SPF records, then set the ALERT_FROM secret to alerts@<domain>.")
    return False


def price_drops(car_ids):
    """{car_id: previous_price} for cars whose latest price is lower than the one before."""
    out = {}
    if not car_ids:
        return out
    CH = 40
    for i in range(0, len(car_ids), CH):
        chunk = car_ids[i:i + CH]
        ids = ",".join(f'"{c}"' for c in chunk)
        rows = get(f"price_history?select=car_id,price,ts&car_id=in.({ids})&order=ts.asc&limit=5000")
        by = {}
        for r in rows:
            by.setdefault(r["car_id"], []).append(r)
        for cid, hist in by.items():
            if len(hist) >= 2:
                prev, last = hist[-2].get("price"), hist[-1].get("price")
                if prev and last and last < prev:
                    out[cid] = prev
    return out


# ---------------------------------------------------------------- main
def main():
    if not URL or not KEY:
        print("FATAL: SUPABASE_URL / SUPABASE_KEY missing")
        sys.exit(1)

    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(minutes=LOOKBACK_MIN)).isoformat().replace("+00:00", "Z")

    cars = []
    for off in range(0, 40000, 1000):                   # PostgREST hard-caps a response at 1000 rows
        h = dict(H); h["Range"] = f"{off}-{off + 999}"
        try:
            r = requests.get(f"{URL}/rest/v1/cars?select=*&active=eq.true"
                             f"&first_seen=gte.{since}&order=first_seen.desc", headers=h, timeout=40)
            page = r.json()
        except Exception as e:
            print("cars fetch error:", e); break
        if not isinstance(page, list) or not page:
            break
        cars.extend(page)
        if len(page) < 1000:
            break
    print(f"NEW cars since {since} ({LOOKBACK_MIN} min): {len(cars)}")
    if not cars:
        print("nothing new -> no alerts")
        return

    drops = price_drops([c["car_id"] for c in cars])

    filters = get("saved_filters?select=*&notify_email=eq.true")
    profs = {p["id"]: p for p in get("profiles?select=id,email,full_name,is_premium,plan")}
    print(f"saved filters with notifications ON: {len(filters)}")

    sent_emails = 0
    today = {}                       # user_id -> e-mails already sent to them today (incl. this run)

    spent = global_sends_today()     # None -> ledger missing, budget cannot be enforced
    if spent is None:
        budget_left = MAX_EMAILS     # fall back to the per-run cap only
    else:
        budget_left = GLOBAL_BUDGET - spent
        pct = (spent * 100 // max(1, PROVIDER_DAILY_LIMIT))
        print(f"PROVIDER BUDGET: {spent}/{PROVIDER_DAILY_LIMIT} spent today ({pct}%), "
              f"{budget_left} left before the {GLOBAL_BUDGET} safe ceiling "
              f"(reserve {PROVIDER_RESERVE})")
        if budget_left <= 0:
            print("!! DAILY E-MAIL BUDGET EXHAUSTED. Sending NOTHING this run so the reserve stays "
                  "intact. Every unsent car rolls into the next batch (nothing is lost). "
                  "If this keeps happening you have outgrown the Resend free tier - upgrade.")
            return
        if budget_left <= 20:
            print(f"!! WARNING: only {budget_left} e-mails left in today's budget.")

    for f in filters:
        uid = f.get("user_id")
        prof = profs.get(uid) or {}
        email = (prof.get("email") or "").strip()
        name = f.get("name") or "filtrs"
        if not email:
            print(f"filter {f['id']} '{name}': no email on profile -> skip")
            continue
        if ONLY_EMAIL and email.lower() != ONLY_EMAIL:
            continue

        premium = bool(prof.get("is_premium"))
        freq = freq_of(f, premium)   # free -> always the daily digest; instant/hourly is the paywall
        print(f"filter {f['id']} '{name}' ({email}) premium={premium} cadence={freq}")

        # 1) cadence: has this filter e-mailed too recently? (its cars roll into the next batch)
        if not (IGNORE_SENT or DRY_RUN) and cooling_down(f, freq):
            continue

        # 2) hard per-user daily ceiling, whatever the cadence says
        if uid not in today:
            today[uid] = sends_today(uid)
        if not (IGNORE_SENT or DRY_RUN) and today[uid] >= DAILY_CAP:
            print(f"  daily cap: {today[uid]}/{DAILY_CAP} e-mails already sent today -> hold")
            continue

        criteria = f.get("criteria") or {}
        # A filter with no make is not a filter, it is the whole catalogue. The UI makes make+model
        # mandatory, but a row created before that rule (or via the API) would e-mail ~1 500 cars a
        # day. Refuse rather than spam.
        if not crit(criteria, "makes", "make"):
            print("  no make in criteria -> this would match the ENTIRE catalogue. Skipped.")
            continue
        already = set() if IGNORE_SENT else {n["car_id"] for n in get(f"filter_notifications?select=car_id&filter_id=eq.{f['id']}")}
        hits = [c for c in cars if c["car_id"] not in already and matches(c, criteria)]
        if not hits:                 # 3) never send an empty e-mail
            print("  no new matches -> nothing sent")
            continue

        hits.sort(key=lambda c: (c.get("first_seen") or c.get("posted") or ""), reverse=True)
        shown = hits[:MAX_PER_EMAIL]
        extra = len(hits) - len(shown)

        if sent_emails >= MAX_EMAILS:
            print(f"MAX_EMAILS ({MAX_EMAILS}) reached -> stopping (per-run cap)")
            break
        if not IGNORE_SENT and sent_emails >= budget_left:
            print(f"!! GLOBAL BUDGET reached ({budget_left} left at the start of this run) -> "
                  f"stopping. The remaining filters roll into the next run.")
            break

        # 4) ONE e-mail containing every new match, never one per car
        subject = f"{len(hits)} jauns auto: {name} | BalticRadar" if len(hits) == 1 \
            else f"{len(hits)} jauni auto: {name} | BalticRadar"
        html = build_html(prof.get("full_name"), name, shown, drops, extra, freq)
        text = build_text(name, shown, extra)

        if send(email, subject, html, text):
            sent_emails += 1
            today[uid] = today.get(uid, 0) + 1
            # A DRY_RUN must not consume the backlog: send() returns True without sending anything,
            # so recording here would mark cars as "already alerted" and the user would NEVER get
            # them. Same for IGNORE_SENT. Neither may touch the dedupe tables.
            if IGNORE_SENT or DRY_RUN:
                print("  -> TEST send (DRY_RUN/IGNORE_SENT: nothing sent, nothing recorded)")
                continue
            # mark ALL hits (not just the shown ones) so the backlog is never re-sent later
            post("filter_notifications",
                 [{"filter_id": f["id"], "car_id": c["car_id"]} for c in hits],
                 prefer="resolution=ignore-duplicates")
            patch(f"saved_filters?id=eq.{f['id']}", {"last_notified_at": _now().isoformat()})
            record_send(uid, f["id"])
            print(f"  -> {email}: {len(hits)} new car(s) in ONE e-mail")

    # ---- legacy: the old "alerts profile" (subscriptions table)
    if not LEGACY_SUBS:
        print("legacy `subscriptions` path DISABLED (set LEGACY_SUBS=1 to re-enable). "
              "Nothing writes to that table any more and one of its rows has no criteria at all, "
              "so it matched every car in the catalogue.")
        print(f"DONE. emails sent this run: {sent_emails}."
              + (f" Today's total: {spent + sent_emails}/{PROVIDER_DAILY_LIMIT}." if spent is not None else ""))
        return
    subs = get("subscriptions?select=*")
    seen = {}
    for s in subs:
        e = (s.get("email") or "").lower().strip()
        if e:
            seen[e] = s
    for sub in seen.values():
        if sent_emails >= MAX_EMAILS:
            break
        if not IGNORE_SENT and sent_emails >= budget_left:
            print("!! GLOBAL BUDGET reached -> legacy subscriptions roll into the next run.")
            break
        # The legacy alert profile had NO cadence at all: it fired on every 20-minute run.
        # Put it on the same daily digest as a free account.
        if not IGNORE_SENT and cooling_down(sub, "daily"):
            print(f"legacy sub {sub['id']}: daily digest cooldown -> hold")
            continue
        already = {n["car_id"] for n in get(f"notifications?select=car_id&subscription_id=eq.{sub['id']}")}
        legacy_crit = {k: sub.get(k) for k in
                       ("country", "make", "model", "body", "fuel", "gearbox",
                        "price_min", "price_max", "year_min", "year_max")}
        legacy_crit["km_min"] = sub.get("mileage_min")
        legacy_crit["km_max"] = sub.get("mileage_max")
        legacy_crit["eng_min"] = sub.get("engine_min")
        legacy_crit["eng_max"] = sub.get("engine_max")
        hits = [c for c in cars if c["car_id"] not in already and matches(c, legacy_crit)]
        if not hits:
            continue
        hits.sort(key=lambda c: (c.get("first_seen") or ""), reverse=True)
        shown = hits[:MAX_PER_EMAIL]
        html = build_html(sub.get("name"), "Tavi kritēriji", shown, drops, len(hits) - len(shown), "daily")
        text = build_text("Tavi kritēriji", shown, len(hits) - len(shown))
        if send(sub["email"], f"{len(hits)} jauni auto | BalticRadar", html, text):
            sent_emails += 1
            if IGNORE_SENT or DRY_RUN:
                continue
            post("notifications", [{"subscription_id": sub["id"], "car_id": c["car_id"]} for c in hits],
                 prefer="resolution=ignore-duplicates")
            patch(f"subscriptions?id=eq.{sub['id']}", {"last_notified_at": _now().isoformat()})
            record_send(None, None)      # still spends one message from the provider budget

    if spent is not None:
        print(f"DONE. emails sent this run: {sent_emails}. "
              f"Today's total: {spent + sent_emails}/{PROVIDER_DAILY_LIMIT}.")
    else:
        print(f"DONE. emails sent this run: {sent_emails} (budget unenforced - run db_alert_cadence.sql)")


if __name__ == "__main__":
    main()
