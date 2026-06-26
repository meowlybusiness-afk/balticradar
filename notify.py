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

def send_email(sub, cars, extra=0):
    subject=f"{len(cars)} jauni auto pēc taviem kritērijiem | BalticRadar"
    lines="\n".join(f"- {c.get('make')} {c.get('model')} {c.get('year') or ''}, "
                    f"{c.get('last_price')} EUR  {c.get('source_url') or ''}" for c in cars)
    more=f"\n\n...un vēl {extra} sludinājumi vietnē: {SITE}" if extra>0 else ""
    body=(f"Sveiki{(' '+sub['name']) if sub.get('name') else ''},\n\n"
          f"BalticRadar atrada jaunākos sludinājumus, kas atbilst taviem kritērijiem:\n\n{lines}{more}\n\n"
          f"Skatīt visus: {SITE}\n\n"
          f"— BalticRadar\n(Lai atrakstītos, atbildi uz šo e-pastu.)")
    if not RESEND_KEY:
        print(f"\n[NO EMAIL PROVIDER] would email {sub['email']}:\nSUBJECT: {subject}\n{body}\n")
        return True
    r=requests.post("https://api.resend.com/emails",
        headers={"Authorization":f"Bearer {RESEND_KEY}","Content-Type":"application/json"},
        json={"from":FROM,"to":[sub["email"]],"subject":subject,"text":body},timeout=30)
    print("emailed",sub["email"],"->",r.status_code)
    return r.ok

def main():
    since=(datetime.datetime.utcnow()-datetime.timedelta(hours=LOOKBACK_H)).isoformat()+"Z"
    cars=get(f"cars?select=*&first_seen=gte.{since}&active=eq.true&limit=3000")
    subs=get("subscriptions?select=*")
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
