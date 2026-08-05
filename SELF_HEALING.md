# Autoplius mileage: the free "2b" route + self-healing pipeline

Fills autoplius mileage from the **results-page cards** (no per-car detail fetch), so it never trips
autoplius' per-IP detail-page VIEW LIMIT and needs **no proxy**. Added 2026-08-05.

## The three moving parts

1. **Parser** — `ap_list_rich()` in `balticradar.py` now reads mileage from each list card's own
   `<span>NNN km</span>` (anchored on the closing `</span>` so CO₂ "g/km" never matches). New/reposted
   autoplius cars get mileage automatically. Degrades **safely**: if the markup changes it returns
   NULL, never a wrong number.

2. **Backfill** — service **`br-ap-backfill`** (list-only autoplius year-sweep, `AP_FULL=1
   AP_LIST_ONLY=1 AP_PAGES=0 WD_DUMP=1`). Walks the whole ~42k catalogue by manufacture year (dodging
   autoplius' ~150-page flat-search cap) and, for any car whose stored `last_mileage` is NULL, patches
   it from the card — see the `if a["ad_id"] in exb:` branch in `ap_year_sweep`. Only ever writes when
   mileage is currently NULL, so it can't clobber good data. Zero detail fetches → no view-limit.
   Logs `mileage_backfilled N` per page. Drains the ~15.9k historical NULL backlog over hours.
   Tune rate in the drop-in `/etc/systemd/system/br-ap-backfill.service.d/rate.conf`.

3. **Watchdog** — service+timer **`br-parser-watchdog`** (every 20 min). Does NOT fetch autoplius
   (a cold request gets CF-challenged). Instead the backfill drops its freshest good list page at
   `health/ap_last_list.html`; the watchdog runs the **real** `ap_list_rich` on it and measures
   mileage-yield. Writes `health/parser_status.json` + appends `health/watchdog.log`, and on a not-OK
   status pushes a **Telegram** alert (bot `BalticRadar24Bot`; falls back to Resend email if `TG_*`
   unset). The Telegram creds live ONLY in the root-only VPS drop-in
   `/etc/systemd/system/br-parser-watchdog.service.d/telegram.conf` (`TG_BOT_TOKEN`,`TG_CHAT_ID`) —
   NEVER commit them. The unit sets `SuccessExitStatus=2` so a breakage-detected run (exit 2) is not
   flagged "failed". Status values:
   - `OK` — yield ≥ 0.55 (healthy pages run ~0.72–0.95).
   - `PARSER_BROKEN` — page + ads fine but mileage vanished ⇒ **autoplius changed the markup, fix the regex.**
   - `FETCH_STALE` — no fresh dump (backfill down / CF / IP) ⇒ **not a parser bug.**
   - `NO_ADS` — page had too few ads (transient).

## Self-healing: detect → notice → fix

Detection, evidence-capture and alerting are **fully automated** (the watchdog). The actual code fix is
**deliberately human/Claude-supervised**, because when autoplius changes their markup the correct fix
requires *looking at the new HTML* — a blind auto-rewrite could store WRONG mileage, which is worse than
NULL. So the pipeline hands a fixer everything it needs and stops there.

**On `PARSER_BROKEN`, the fixer procedure (human or a Claude Code session):**
```bash
# 1. read the diagnosis
ssh -i ~/.ssh/balticradar_vps root@95.216.164.221 'cat /opt/balticradar/health/parser_status.json'
# 2. pull the captured page (the exact evidence)
scp -i ~/.ssh/balticradar_vps root@95.216.164.221:/opt/balticradar/health/ap_list_sample_PARSER_BROKEN.html /tmp/
# 3. find the new mileage markup in that file, update the MILE regex in ap_list_rich (balticradar.py),
#    re-run the sample test until yield is back ~0.9, then deploy:
scp -i ~/.ssh/balticradar_vps ~/balticradar/balticradar.py root@…:/opt/balticradar/ && ssh … 'systemctl restart br-autoplius br-ap-backfill'
```

**Optional full autonomy:** a scheduled Claude Code routine can poll `parser_status.json` and, on
`PARSER_BROKEN`, auto-spawn a fixer subagent that does steps 2–3 and opens a PR for approval (apply is
still gated). Ask to wire it — it's a recurring cloud agent, so it's opt-in.
