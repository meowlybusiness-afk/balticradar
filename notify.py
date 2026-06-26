"""
BalticRadar - alert notifier. Run AFTER run_all.py (or on a schedule).
Finds cars added recently, matches them to each subscription's criteria, and
emails the subscriber. Idempotent: never emails the same car twice (notifications
table). Email sending is pluggable: set RESEND_API_KEY to actually send; without
it, it prints what it WOULD send (so you can test before choosing a provider).

Env: SUPABASE_URL, SUPABASE_KEY (service key). Optional: RESEND_API_KEY,
ALERT_FROM, LOOKBACK_H (default 24).
"""
import os, requests, datetime

URL=os.environ.get("SUPABASE_URL","").rstrip("/"); KEY=os.environ.get("SUPABASE_KEY","")
H={"apikey":KEY,"Authorization":f"Bearer {KEY}","Content-Type":"application/json"}
LOOKBACK_H=int(os.environ.get("LOOKBACK_H",24))
MAX_PER_EMAIL=int(os.environ.get("MAX_PER_EMAIL",20))
SITE=os.environ.get("SITE_URL","https://starlit-tulumba-bd46fb.netlify.app")
RESEND_KEY=os.environ.get("RESEND_API_KEY")
FROM=os.environ.get("ALERT_FROM","BalticRadar <onboarding@resend.dev>")

def get(path):
    try:
        r=requests.get(f"{URL}/rest/v1/{path}",headers=H,timeout=30).json()
        return r if isinstance(r,list) else []
    except Exception as e:
        print("get error:",e); return []
def post(path,rows): return requests.post(f"{URL}/rest/v1/{path}",headers=H,json=rows,timeout=30)

def matches(car, sub):
    if sub.get("country") and car.get("country")!=sub["country"]: return False
    if sub.get("make") and (car.get("make") or "").lower()!=sub["make"].lower(): return False
    if sub.get("model") and (car.get("model") or "").lower()!=sub["model"].lower(): return False
    if sub.get("price_max") and (car.get("last_price") or 10**12)>sub["price_max"]: return False
    if sub.get("year_min") and (car.get("year") or 0)<sub["year_min"]: return False
    return True

def car_row(c):
    img=(c.get("photos") or [None])[0]
    thumb=(f'<img src="{img}" width="120" height="80" style="border-radius:8px;object-fit:cover;display:block">'
           if img else '<div style="width:120px;height:80px;background:#e7eaf0;border-radius:8px"></div>')
    title=f"{c.get('make') or ''} {c.get('model') or ''} {c.get('year') or ''}".strip()
    specs=" · ".join(str(x) for x in [c.get('last_mileage') and f"{c.get('last_mileage')} km",
        c.get('fuel'), c.get('gearbox')] if x)
    price=f"{c.get('last_price')} &euro;" if c.get('last_price') else ''
    url=c.get("source_url") or SITE
    return (f'<tr><td style="padding:8px 8px 8px 0;vertical-align:top">{thumb}</td>'
            f'<td style="padding:8px 0;vertical-align:top;font-family:Arial,sans-serif">'
            f'<a href="{url}" style="color:#16202e;text-decoration:none;font-weight:700;font-size:15px">{title}</a><br>'
            f'<span style="color:#69748a;font-size:12px">{specs}</span><br>'
            f'<span style="color:#ff4605;font-weight:800;font-size:15px">{price}</span></td></tr>')

def send_email(sub, cars, extra=0):
    subject=f"{len(cars)} jauni auto pēc taviem kritērijiem | BalticRadar"
    rows="".join(car_row(c) for c in cars)
    more=f'<p style="color:#69748a;font-family:Arial">...un vēl {extra} sludinājumi vietnē.</p>' if extra>0 else ''
    html=(f'<div style="background:#f5f6f8;padding:20px"><div style="max-width:580px;margin:0 auto;background:#fff;border-radius:14px;padding:24px">'
          f'<div style="font-family:Arial,sans-serif;font-size:22px;font-weight:800;color:#16202e">Baltic<span style="color:#ff4605">Radar</span></div>'
          f'<p style="font-family:Arial,sans-serif;color:#16202e">Sveiki{(" "+sub["name"]) if sub.get("name") else ""}! Jaunākie auto pēc taviem kritērijiem:</p>'
          f'<table style="border-collapse:collapse;width:100%">{rows}</table>{more}'
          f'<p style="margin:22px 0"><a href="{SITE}" style="background:#ff4605;color:#fff;padding:13px 26px;border-radius:10px;text-decoration:none;font-weight:700;font-family:Arial,sans-serif">Skatīt visus BalticRadar &rarr;</a></p>'
          f'<p style="color:#9aa7b8;font-size:12px;font-family:Arial,sans-serif">Lai atrakstītos, atbildi uz šo e-pastu.</p></div></div>')
    text="\n".join(f"- {c.get('make')} {c.get('model')} {c.get('year') or ''}, {c.get('last_price')} EUR  {c.get('source_url') or ''}" for c in cars)
    text+=f"\n\nSkatīt visus: {SITE}"
    if not RESEND_KEY:
        print(f"\n[NO EMAIL PROVIDER] would email {sub['email']}: {subject} ({len(cars)} cars)\n")
        return True
    r=requests.post("https://api.resend.com/emails",
        headers={"Authorization":f"Bearer {RESEND_KEY}","Content-Type":"application/json"},
        json={"from":FROM,"to":[sub["email"]],"subject":subject,"html":html,"text":text},timeout=30)
    print("emailed",sub["email"],"->",r.status_code)
    return r.ok

def main():
    since=(datetime.datetime.utcnow()-datetime.timedelta(hours=LOOKBACK_H)).isoformat()+"Z"
    cars=get(f"cars?select=*&first_seen=gte.{since}&active=eq.true&limit=3000")
    subs=get("subscriptions?select=*&order=created.asc") or get("subscriptions?select=*")
    # de-dup by email: a person who re-registers/updates their profile keeps only their latest
    _seen={}
    for s in subs:
        e=(s.get("email") or "").lower().strip()
        if e: _seen[e]=s
    subs=list(_seen.values())
    print(f"{len(cars)} recent cars, {len(subs)} subscriptions, lookback {LOOKBACK_H}h")
    for sub in subs:
        already={n["car_id"] for n in get(f"notifications?select=car_id&subscription_id=eq.{sub['id']}")}
        hits=[c for c in cars if c["car_id"] not in already and matches(c,sub)]
        if not hits: continue
        # newest first; email only the newest MAX, but mark ALL as notified so the
        # older backlog isn't emailed later (subscribers only get the latest additions)
        hits.sort(key=lambda c:(c.get("posted") or c.get("first_seen") or ""),reverse=True)
        shown=hits[:MAX_PER_EMAIL]; extra=len(hits)-len(shown)
        if send_email(sub,shown,extra):
            post("notifications",[{"subscription_id":sub["id"],"car_id":c["car_id"]} for c in hits])

if __name__=="__main__":
    main()
