/**
 * BalticRadar alert notifier — Cloudflare Worker.
 *
 * Faithful JS port of notify.py (the 865-line version deployed on GitHub Actions).
 * Purpose: a car appears on ss.lv / autoplius.lt / auto24.ee -> the owner of a matching
 * saved_filter gets ONE e-mail within ~5-15 min. Timeliness IS the product.
 *
 * WHY A WORKER: GitHub Actions' free scheduler silently throttles high-frequency cron
 * (measured 1-2.5 h between runs even with a 5-min cron); the live GitHub job is hourly (7 * * * *).
 * Cloudflare Cron Triggers fire ~every 5 min on the free plan. This Worker runs the same
 * match+send logic on a five-minute cron trigger.
 *
 * SHARES THE SAME DEDUPE TABLES/LOGIC as notify.py (filter_notifications, notifications,
 * alert_sends) so a car can never be e-mailed twice while both run in parallel.
 *
 * CLAIM-THEN-SEND (the one behavioural change vs the original ordering):
 *   The original notify.py SENT the e-mail first and INSERTED filter_notifications AFTER.
 *   With two notifiers overlapping, both read "not yet sent", both send, both then insert —
 *   the shared table stays clean but the paying user is e-mailed twice. This Worker (and the
 *   patched notify.py) instead CLAIM the (filter_id, car_id) rows first via
 *   INSERT ... ON CONFLICT DO NOTHING (PostgREST resolution=ignore-duplicates +
 *   return=representation). Only rows THIS runner actually inserted are "won"; we e-mail only
 *   those, and if the send fails we DELETE the claim so the car rolls into the next run.
 *   Requires a UNIQUE index on filter_notifications(filter_id, car_id) — see
 *   db_dedupe_unique.sql. Without it, ON CONFLICT is a no-op and dedupe silently breaks.
 *
 * Secrets (set in Cloudflare dashboard, encrypted): SUPABASE_URL, SUPABASE_KEY, RESEND_API_KEY.
 * Vars (plaintext, in wrangler.notifier.toml [vars] or dashboard): ALERT_FROM, SITE_URL, caps.
 *
 * Manual test endpoint (does NOT burn quota):
 *   GET /run?dry=1                      -> full pipeline, renders, sends NOTHING, returns JSON log
 *   GET /run?dry=1&lookback_min=10080   -> widen the window to find a recent car to render
 *   GET /run?token=<RUN_TOKEN>&send=1   -> real send (guarded); only if RUN_TOKEN secret is set
 *   GET /health                         -> liveness + resolved config (no secrets)
 */

// ------------------------------------------------------------------ config
function cfg(env) {
  const n = (v, d) => (v === undefined || v === null || v === "" ? d : parseInt(v, 10));
  const s = (v, d) => (v === undefined || v === null || v === "" ? d : String(v));
  return {
    URL: (env.SUPABASE_URL || "").replace(/\/+$/, ""),
    KEY: env.SUPABASE_KEY || "",
    RESEND_KEY: env.RESEND_API_KEY || "",

    LOOKBACK_MIN: n(env.LOOKBACK_MIN, 60),          // 1 h: 12 cron cycles of slack; claim-then-send dedupe covers gaps. (was 1560/26h -> exceededCpu)
    // Hard per-run SPIKE CAP. The free-plan CPU ceiling is ~10 ms/invocation; a normal run
    // (24-59 new cars) already sits at ~9.5 ms CPU, and ~1,393 cars/run is what blew the ceiling
    // (exceededCpu) on 2026-07-23. We therefore fetch at most this many cars per invocation,
    // ordered first_seen ASC (oldest first), so a big ingest batch drains across consecutive runs
    // with zero duplicates (claim-then-send dedupe) and zero drops. 200 = ~4x the proven-safe load
    // and realistic batch size, ~7x below the fatal ~1,393. Applied as a DB &limit= (NOT a JS slice)
    // so CPU is bounded no matter how many cars are inside the LOOKBACK window. Tune DOWN if any run
    // approaches the CPU ceiling; keep <= 1000 (PostgREST single-page cap).
    MAX_CARS_PER_RUN: n(env.MAX_CARS_PER_RUN, 200),
    MAX_PER_EMAIL: n(env.MAX_PER_EMAIL, 12),
    MAX_EMAILS: n(env.MAX_EMAILS, 80),              // per run
    POSTED_MAX_AGE_H: n(env.POSTED_MAX_AGE_H, 72),  // source publish-date guard (no backfill spam)
    MASS_INGEST_MAX: n(env.MASS_INGEST_MAX, 60),    // one filter matching > this = backfill -> hold

    DAILY_CAP: n(env.DAILY_CAP, 6),                 // per user per day
    PROVIDER_DAILY_LIMIT: n(env.PROVIDER_DAILY_LIMIT, 100),
    PROVIDER_RESERVE: n(env.PROVIDER_RESERVE, 10),

    // Links point at balticradar.com, never the source portal. (Override via SITE_URL var.)
    SITE: "https://balticradar.com",   // ALWAYS the public domain (magic-link redirect + all e-mail links must be balticradar.com, never workers.dev)
    FROM: (env.ALERT_FROM || "").trim() || "BalticRadar <alerts@balticradar.com>",

    TEST_TO: (env.TEST_TO || "").trim(),
    ONLY_EMAIL: (env.ONLY_EMAIL || "").trim().toLowerCase(),
    DRY_RUN: env.DRY_RUN === "1",
    IGNORE_SENT: env.IGNORE_SENT === "1",
    LEGACY_SUBS: env.LEGACY_SUBS === "1",
    RUN_TOKEN: env.RUN_TOKEN || "",

    // Resilience for the critical new-cars fetch (see fetchNewCars). A DB timeout / dropped
    // connection must be RETRIED and, if still failing, recorded as an ERROR — never silently
    // collapsed to "0 new cars". Tunable via env vars; defaults are safe for the free plan.
    NEW_CARS_ATTEMPTS: n(env.NEW_CARS_ATTEMPTS, 3),      // total tries before giving up
    NEW_CARS_TIMEOUT_MS: n(env.NEW_CARS_TIMEOUT_MS, 20000), // per-attempt hard timeout (was effectively none)
  };
}

const FREQ_COOLDOWN_MIN = { instant: 60, few_hours: 360, daily: 1200 };
const DEFAULT_FREQ = "daily";
const FREE_FREQ = "daily";
const SHARED_FROM = "BalticRadar <onboarding@resend.dev>";
const FLAG = { LV: "\u{1F1F1}\u{1F1FB}", LT: "\u{1F1F1}\u{1F1F9}", EE: "\u{1F1EA}\u{1F1EA}" };

// A tiny logger that both prints and accumulates lines for the HTTP test response.
function mkLog() {
  const lines = [];
  return { push: (...a) => { const m = a.join(" "); lines.push(m); console.log(m); }, lines };
}

// ------------------------------------------------------------------ supabase REST
function sbHeaders(C, extra) {
  return Object.assign(
    { apikey: C.KEY, Authorization: `Bearer ${C.KEY}`, "Content-Type": "application/json" },
    extra || {}
  );
}

async function sbGet(C, path, extraHeaders) {
  try {
    const r = await fetch(`${C.URL}/rest/v1/${path}`, { headers: sbHeaders(C, extraHeaders) });
    const d = await r.json();
    return Array.isArray(d) ? d : [];
  } catch (e) {
    console.log("GET error", path, e);
    return [];
  }
}

// count via Prefer: count=exact + Range 0-0 (matches notify.py's sends_today/global_sends_today)
async function sbCount(C, path) {
  try {
    const r = await fetch(`${C.URL}/rest/v1/${path}`, {
      headers: sbHeaders(C, { Prefer: "count=exact", Range: "0-0" }),
    });
    if (!r.ok) return null;
    const cr = r.headers.get("content-range") || "0/0";
    return parseInt(cr.split("/").pop(), 10) || 0;
  } catch (e) {
    console.log("COUNT error", path, e);
    return null;
  }
}

async function sbPost(C, path, rows, prefer) {
  if (!rows || !rows.length) return null;
  try {
    return await fetch(`${C.URL}/rest/v1/${path}`, {
      method: "POST",
      headers: sbHeaders(C, prefer ? { Prefer: prefer } : {}),
      body: JSON.stringify(rows),
    });
  } catch (e) {
    console.log("POST error", path, e);
    return null;
  }
}

async function sbPatch(C, path, body) {
  try {
    return await fetch(`${C.URL}/rest/v1/${path}`, {
      method: "PATCH",
      headers: sbHeaders(C),
      body: JSON.stringify(body),
    });
  } catch (e) {
    console.log("PATCH error", path, e);
    return null;
  }
}

async function sbDelete(C, path) {
  try {
    return await fetch(`${C.URL}/rest/v1/${path}`, { method: "DELETE", headers: sbHeaders(C) });
  } catch (e) {
    console.log("DELETE error", path, e);
    return null;
  }
}

// ------------------------------------------------------------------ resilient new-cars fetch
// The notifier's "new cars" query is the ONE call that must never silently return 0. Previously it
// did `const page = await r.json(); if (Array.isArray(page)) cars = page;` inside a try/catch that
// only logged — so a DB statement-timeout / dropped connection (PostgREST replies with a NON-array
// error object, or a non-2xx status) left `cars = []` and the run reported "nothing new" as a
// SILENT SUCCESS: 0 alerts sent despite hundreds of genuinely new cars.
//
// This helper fixes that: it RETRIES up to C.NEW_CARS_ATTEMPTS with a short linear backoff, applies
// a per-attempt AbortController timeout, and treats ALL of {non-OK HTTP status, timeout, non-array
// body} as a retryable ERROR. On success it returns the array (an empty array is a legitimate
// "genuinely 0 new cars" and is returned as-is). If every attempt fails it THROWS, so the caller
// aborts the run and the failure is recorded (Observability / non-2xx /run response) instead of
// masquerading as 0. Nothing about matching, dedupe, caps or cadence is touched.
async function fetchNewCars(C, since, cap, log) {
  const url = `${C.URL}/rest/v1/cars?select=*&active=eq.true&first_seen=gte.${since}` +
    `&order=first_seen.asc&limit=${cap}`;
  const attempts = Math.max(1, C.NEW_CARS_ATTEMPTS);
  const timeoutMs = Math.max(1000, C.NEW_CARS_TIMEOUT_MS);
  let lastErr = null;
  for (let i = 1; i <= attempts; i++) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const r = await fetch(url, { headers: sbHeaders(C), signal: ctl.signal });
      clearTimeout(timer);
      if (!r.ok) {
        const body = await r.text().catch(() => "");
        throw new Error(`HTTP ${r.status} ${body.slice(0, 200)}`);
      }
      const page = await r.json();
      if (!Array.isArray(page)) {
        // PostgREST returns {message,code,details,hint} on statement timeout / query error.
        throw new Error("non-array body (query error/timeout): " + JSON.stringify(page).slice(0, 200));
      }
      if (i > 1) log.push(`  new-cars fetch recovered on attempt ${i}/${attempts}`);
      return page;
    } catch (e) {
      clearTimeout(timer);
      lastErr = (e && e.name === "AbortError") ? new Error(`timeout after ${timeoutMs}ms`) : e;
      log.push(`  !! new-cars fetch attempt ${i}/${attempts} failed: ${lastErr.message || lastErr}`);
      if (i < attempts) await new Promise((res) => setTimeout(res, 300 * i)); // 300ms, 600ms backoff
    }
  }
  throw lastErr || new Error("unknown new-cars fetch failure");
}

// ------------------------------------------------------------------ time helpers
const now = () => new Date();
function parseTs(v) {
  if (!v) return null;
  const t = String(v).replace(" ", "T");
  const d = new Date(/[zZ]|[+\-]\d\d:?\d\d$/.test(t) ? t : t + "Z");
  return isNaN(d.getTime()) ? null : d;
}
function minutesBetween(a, b) { return (a.getTime() - b.getTime()) / 60000; }

// ------------------------------------------------------------------ posted / cadence guards
function freshlyPosted(C, c, ref) {
  const p = parseTs(c.posted);
  if (p === null) return true; // unknown -> fall back to first_seen
  return (ref.getTime() - p.getTime()) <= C.POSTED_MAX_AGE_H * 3600 * 1000;
}

function dropStalePosts(C, cars, log) {
  const ref = now();
  const fresh = cars.filter((c) => freshlyPosted(C, c, ref));
  const stale = cars.filter((c) => !freshlyPosted(C, c, ref));
  if (stale.length) {
    log.push(`POSTED GUARD: suppressed ${stale.length} of ${cars.length} recently-ingested car(s) ` +
      `whose source publication date is older than ${C.POSTED_MAX_AGE_H} h (re-scan/backfill).`);
  }
  return fresh;
}

function freqOf(f, isPremium) {
  let v = (f.notify_freq || DEFAULT_FREQ).trim().toLowerCase();
  if (!(v in FREQ_COOLDOWN_MIN)) v = DEFAULT_FREQ;
  return isPremium ? v : FREE_FREQ;
}

function coolingDown(f, freq, log) {
  const last = parseTs(f.last_notified_at);
  if (!last) return false;
  const mins = minutesBetween(now(), last);
  const need = FREQ_COOLDOWN_MIN[freq];
  if (mins < need) {
    log.push(`  cooldown: last sent ${mins.toFixed(0)} min ago, '${freq}' needs ${need} -> hold`);
    return true;
  }
  return false;
}

async function sendsToday(C, userId, log) {
  if (!userId) return 0;
  const day = new Date(Date.UTC(now().getUTCFullYear(), now().getUTCMonth(), now().getUTCDate()))
    .toISOString().replace(".000Z", "Z");
  const c = await sbCount(C, `alert_sends?select=id&user_id=eq.${userId}&sent_at=gte.${day}`);
  if (c === null) {
    log.push("  !! alert_sends unreadable -> per-user daily cap NOT enforced. Run db_alert_cadence.sql.");
    return 0;
  }
  return c;
}

async function globalSendsToday(C, log) {
  const day = new Date(Date.UTC(now().getUTCFullYear(), now().getUTCMonth(), now().getUTCDate()))
    .toISOString().replace(".000Z", "Z");
  const c = await sbCount(C, `alert_sends?select=id&sent_at=gte.${day}`);
  if (c === null) {
    log.push("!! alert_sends unreadable -> GLOBAL PROVIDER BUDGET NOT ENFORCED. Run db_alert_cadence.sql NOW.");
    return null;
  }
  return c;
}

async function recordSend(C, userId, filterId) {
  await sbPost(C, "alert_sends", [{ user_id: userId, filter_id: filterId, sent_at: now().toISOString() }]);
}

// ------------------------------------------------------------------ normalisation (mirror of notify.py)
function _nb(v) {
  const s = (v || "").toLowerCase();
  if (s.includes("sedan")) return "sedans";
  if (s.includes("hatch") || s.includes("hečbek") || s.includes("hecbek")) return "hečbeks";
  if (s.includes("coupe") || s.includes("kupej")) return "kupeja";
  if (s.includes("cabrio") || s.includes("kabriolet") || s.includes("roadster") || s.includes("convertible")) return "kabriolets";
  if (s.includes("pickup") || s.includes("pikap")) return "pikaps";
  if (s.includes("suv") || s.includes("visurg") || s.includes("krosover") || s.includes("cross") || s.includes("apvidus")) return "apvidus";
  if (s.includes("universal") || s.includes("estate") || s.includes("wagon") || s.includes("kombi") || s.includes("touring") || s.includes("caravan")) return "universālis";
  if (s.includes("minibus") || s.includes("minivan") || s.includes("miniven") || s.includes("van")) return "minivens";
  if (s.includes("limous") || s.includes("limuzin")) return "limuzīns";
  return s;
}
const _FUEL = { petrol: "benzīns", gasoline: "benzīns", diesel: "dīzelis", electric: "elektrība", hybrid: "hibrīds", gas: "gāze", lpg: "gāze" };
const _GEAR = { automatic: "automātiskā", manual: "mehāniskā" };
const _DRIVE = { fwd: "priekšējā", front: "priekšējā", rwd: "aizmugurējā", rear: "aizmugurējā", awd: "pilnpiedziņa", "4x4": "pilnpiedziņa", "4wd": "pilnpiedziņa" };
function _nf(v) { const s = (v || "").toLowerCase().trim(); return _FUEL[s] || s; }
function _ng(v) { const s = (v || "").toLowerCase().trim(); return _GEAR[s] || s; }
function _nd(v) { const s = (v || "").toLowerCase().trim(); for (const k in _DRIVE) if (s.includes(k)) return _DRIVE[k]; return s; }

function modelSeries(make, model) {
  const m = (model || "").trim();
  const mk = (make || "").toLowerCase();
  if (!m) return m;
  if (mk === "bmw") {
    let x = m.match(/^X\s?(\d)/i); if (x) return "X" + x[1];
    let z = m.match(/^Z\s?(\d)/i); if (z) return "Z" + z[1];
    let d = m.match(/^(\d)\d{2}/); if (d) return d[1] + ". sērija";
    return m;
  }
  if (mk.includes("mercedes")) {
    let g = m.match(/^(GLE|GLC|GLA|GLS|GLK|GLB|ML|CLA|CLS|SLK|SLC)/i); if (g) return g[1].toUpperCase();
    let c = m.match(/^([A-Z])\s?\d/i); if (c) return c[1].toUpperCase() + "-klase";
    return m;
  }
  if (mk === "audi") {
    let a = m.match(/^(RS\s?\d|S\s?\d|SQ\d|A\d|Q\d|TT|R8)/i); if (a) return a[1].toUpperCase().replace(/\s/g, "");
    return m;
  }
  return m;
}

function arr(v) {
  if (v === null || v === undefined || v === "") return [];
  if (Array.isArray(v)) return v.filter((x) => x !== null && x !== "").map(String);
  return [String(v)];
}
function crit(c, ...keys) {
  for (const k of keys) { const a = arr(c[k]); if (a.length) return a; }
  return [];
}

// ------------------------------------------------------------------ make normalisation (mirror of the scraper's _MAKE_CANON in balticradar.py)
// The scraper canonicalises brand names before writing them (e.g. "Mercedes" -> "Mercedes-Benz"),
// so a car row's make is the CANONICAL spelling. A saved filter, however, may store a legacy/display
// label ("Mercedes") captured before that normalisation existed. Exact make-equality then rejected
// every canonical car (e.g. "mercedes" !== "mercedes-benz") and the filter silently sent nothing.
// Fix: normalise BOTH sides through the SAME canonical mapping the scraper uses, keyed on the
// lowercased legacy variant and mapping to the lowercased canonical value, so "Mercedes" and
// "Mercedes-Benz" both collapse to "mercedes-benz" and match. Whole-value, case-insensitive; any
// make that is not a known variant is returned lowercased unchanged, so distinct brands never
// collide (this is NOT a loose substring match — it is the exact 18-pair canonical set only).
const MAKE_CANON = {
  mercedes: "mercedes-benz",
  land: "land rover",
  seat: "seat",
  mini: "mini",
  alfa: "alfa romeo",
  ssangyong: "ssangyong",
  ram: "ram",
  ds: "ds automobiles",
  man: "man",
  aston: "aston martin",
  gwm: "gwm",
  lynk: "lynk & co",
  uaz: "uaz",
  mclaren: "mclaren",
  zaz: "zaz",
  dfsk: "dfsk",
  kgm: "kgm",
  xev: "xev",
};
function normalizeMake(v) {
  const s = (v || "").toLowerCase().trim();
  return MAKE_CANON[s] || s;
}

// ------------------------------------------------------------------ matching (mirror of notify.py)
function matches(car, c) {
  const countries = crit(c, "countries", "country");
  if (countries.length && !countries.includes(car.country)) return false;

  const makes = crit(c, "makes", "make").map(normalizeMake);
  if (makes.length && !makes.includes(normalizeMake(car.make))) return false;

  const pairs = crit(c, "models_q");
  if (pairs.length && pairs.some((p) => p.includes("|"))) {
    const cmk = (car.make || "").toLowerCase();
    const cm = (car.model || "").toLowerCase();
    const cs = modelSeries(car.make, car.model).toLowerCase();
    let ok = false;
    for (const p of pairs) {
      const i = p.indexOf("|");
      const mk = (i < 0 ? p : p.slice(0, i)).toLowerCase();
      const md = (i < 0 ? "" : p.slice(i + 1)).toLowerCase();
      if (mk === cmk && (md === cm || md === cs)) { ok = true; break; }
    }
    if (!ok) return false;
  } else {
    const models = crit(c, "models", "model");
    if (models.length) {
      const cm = (car.model || "").toLowerCase();
      const cs = modelSeries(car.make, car.model).toLowerCase();
      if (!models.some((m) => m.toLowerCase() === cm || m.toLowerCase() === cs)) return false;
    }
  }

  const fuels = crit(c, "fuels", "fuel").map(_nf);
  if (fuels.length && !fuels.includes(_nf(car.fuel))) return false;

  const gears = crit(c, "gearboxes", "gearbox").map(_ng);
  if (gears.length && !gears.includes(_ng(car.gearbox))) return false;

  const bodies = crit(c, "bodies", "body").map(_nb);
  if (bodies.length && !bodies.includes(_nb(car.body))) return false;

  const drives = crit(c, "drivetrains", "drivetrain").map(_nd);
  if (drives.length && !drives.includes(_nd(car.drivetrain))) return false;

  const p = car.last_price;
  if (c.price_min && (p === null || p === undefined || p < c.price_min)) return false;
  if (c.price_max && (p === null || p === undefined || p > c.price_max)) return false;

  const y = car.year || 0;
  if (c.year_min && y < c.year_min) return false;
  if (c.year_max && y > c.year_max) return false;

  const m = car.last_mileage;
  if (c.km_min && (m || 0) < c.km_min) return false;
  if (c.km_max && (m === null || m === undefined || m > c.km_max)) return false;

  let e = parseFloat(car.engine_l || 0);
  if (isNaN(e)) e = 0;
  if (c.eng_min && e < c.eng_min) return false;
  if (c.eng_max && e && e > c.eng_max) return false;

  return true;
}

// ------------------------------------------------------------------ email rendering (mirror of notify.py)
const FONT = "'Manrope','Helvetica Neue',Helvetica,Arial,sans-serif";
const ACC = "#728C00", INK = "#1d1d1f", INK2 = "#5b6470", MUT = "#8a929c", LINE = "#e7e9ee";

function eur(nv) {
  const x = parseInt(nv, 10);
  if (isNaN(x)) return "";
  return x.toLocaleString("en-US").replace(/,/g, " ") + " &euro;";
}
// The car CTA opens the listing ON BALTICRADAR (/?car=<id>), never the source portal.
// When `magic` (a per-recipient single-use token_hash from generateMagicToken) is supplied, the
// link ALSO auto-logs the recipient into THEIR OWN account: the site callback runs
// verifyOtp({token_hash, type:'magiclink'}) and then opens ?car=<id>. `car` is a plain pass-through
// query param, independent of the token, so the SAME token can carry any car id. Without a token
// (share button, or generateLink failed) the link stays universal/public: a plain ?car=<id>.
function carUrl(C, c, magic) {
  if (magic) return `${C.SITE}/?token_hash=${encodeURIComponent(magic)}&type=magiclink&car=${encodeURIComponent(c.car_id)}`;
  return `${C.SITE}/?car=${encodeURIComponent(c.car_id)}`;
}

// Mint ONE magic-link token for a recipient via the GoTrue admin API (needs the service_role key,
// which is C.KEY). This does NOT send any e-mail - it only returns a token_hash we embed in the
// links, so it costs nothing against the Resend daily quota. Returns the hashed_token string, or
// null on any failure (caller then falls back to plain public ?car links - the alert still goes
// out, just without auto-login). One call per e-mail (per recipient), reused for every car in it.
async function generateMagicToken(C, email, log) {
  if (!C.URL || !C.KEY || !email) return null;
  try {
    const r = await fetch(`${C.URL}/auth/v1/admin/generate_link`, {
      method: "POST",
      headers: { apikey: C.KEY, Authorization: `Bearer ${C.KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({ type: "magiclink", email, options: { redirect_to: `${C.SITE}/` } }),
    });
    if (!r.ok) { if (log) log.push(`  magiclink: generate_link ${r.status} for ${email} -> plain ?car fallback`); return null; }
    const j = await r.json();
    // GoTrue returns hashed_token at the top level; supabase-js nests it under properties.
    const hash = (j && (j.hashed_token || (j.properties && j.properties.hashed_token))) || null;
    if (!hash && log) log.push(`  magiclink: no hashed_token in response for ${email} -> plain ?car fallback`);
    return hash;
  } catch (e) { if (log) log.push(`  magiclink: generate_link error for ${email}: ${e} -> plain ?car fallback`); return null; }
}

function specsOf(c) {
  const bits = [];
  if (c.last_mileage !== null && c.last_mileage !== undefined) bits.push(parseInt(c.last_mileage, 10).toLocaleString("en-US").replace(/,/g, " ") + " km");
  if (c.fuel) bits.push(cap(_nf(c.fuel)));
  if (c.engine_l) bits.push(`${c.engine_l} l`);
  if (c.gearbox) bits.push(cap(_ng(c.gearbox)));
  if (c.body) bits.push(cap(_nb(c.body)));
  return bits.filter(Boolean).join(" &middot; ");
}
function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

function carRow(C, c, drop, magic) {
  const img = (c.photos || [null])[0];
  const alt = `${c.make || ""} ${c.model || ""}`.trim() || "Auto";
  const thumb = img
    ? `<img src="${img}" width="180" alt="${alt}" border="0" style="width:180px;max-width:180px;height:126px;object-fit:cover;display:block;border:0;border-radius:12px;background:#eef0f4">`
    : `<div style="width:180px;height:126px;background:#eef0f4;border-radius:12px"></div>`;
  const title = `${c.make || ""} ${c.model || ""}`.trim();
  const year = c.year || "";
  const flag = FLAG[c.country || ""] || "";
  const src = c.source || "";
  const url = carUrl(C, c, magic);
  const dropBadge = drop
    ? `<div style="margin:0 0 7px"><span style="display:inline-block;background:#eef3df;color:${ACC};font:700 11px/1.6 ${FONT};padding:2px 9px;border-radius:999px;white-space:nowrap">&darr; Cena kritusi &nbsp;<span style="text-decoration:line-through;color:${MUT};font-weight:600">${eur(drop)}</span></span></div>`
    : "";
  return (
    `<tr><td style="padding:0 0 14px">` +
    `<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:separate;background:#ffffff;border:1px solid ${LINE};border-radius:16px">` +
    `<tr>` +
    `<td class="ph" width="180" style="width:180px;padding:14px 0 14px 14px;vertical-align:top">` +
    `<a href="${url}" target="_blank" style="text-decoration:none">${thumb}</a></td>` +
    `<td class="bd" style="padding:14px;vertical-align:top;font-family:${FONT}">` +
    `${dropBadge}` +
    `<a href="${url}" target="_blank" style="color:${INK};text-decoration:none;font:800 17px/1.25 ${FONT};letter-spacing:-.01em">${title} <span style="color:${INK2};font-weight:600">${year}</span></a>` +
    `<div style="margin:6px 0 10px;color:${INK2};font:500 13px/1.5 ${FONT}">${specsOf(c)}</div>` +
    `<div style="color:${INK};font:800 21px/1.2 ${FONT};letter-spacing:-.02em">${eur(c.last_price)}</div>` +
    `<div style="margin:6px 0 12px;color:${MUT};font:600 12px/1.5 ${FONT}">${flag} ${src}</div>` +
    `<a href="${url}" target="_blank" style="display:inline-block;background:${ACC};color:#ffffff;text-decoration:none;font:800 13px/1 ${FONT};padding:11px 18px;border-radius:10px">Atvērt sludinājumu &rarr;</a>` +
    `</td></tr></table></td></tr>`
  );
}

const MQ =
  "@media only screen and (max-width:480px){" +
  ".wrap{width:100% !important}" +
  ".ph,.bd{display:block !important;width:100% !important;box-sizing:border-box}" +
  ".ph{padding:14px 14px 0 !important}" +
  ".bd{padding:12px 14px 16px !important}" +
  ".ph img,.ph div{width:100% !important;max-width:100% !important;height:200px !important}" +
  ".cta a{display:block !important}" +
  "}";

const FREQ_LABEL = {
  instant: "tūlītēji (ne biežāk kā reizi stundā)",
  few_hours: "ik pēc dažām stundām",
  daily: "reizi dienā (kopsavilkums)",
};

function buildHtml(C, name, filterName, cars, drops, extra, freq, magic) {
  const rows = cars.map((c) => carRow(C, c, drops[c.car_id], magic)).join("");
  const n = cars.length;
  // Preheader = the notification/preview snippet. Concise "Make Model Year — price" list per car.
  const preview = cars.map((c) => (`${(c.make || "").trim()} ${(c.model || "").trim()} ${c.year || ""}`).trim() + (c.last_price ? ` — ${eur(c.last_price)}` : "")).join(" · ");
  const hello = name ? `, ${name}` : "";
  const more = extra > 0
    ? `<tr><td style="padding:2px 0 14px;text-align:center;font:600 13px/1.5 ${FONT};color:${INK2}">&hellip;un vēl <b style="color:${INK}">${extra}</b> jauni sludinājumi pēc šī filtra: <a href="${C.SITE}" target="_blank" style="color:${ACC};text-decoration:none;font-weight:800">skatīt visus</a></td></tr>`
    : "";
  const host = C.SITE.replace("https://", "").replace("http://", "");
  return (
    '<!DOCTYPE html><html lang="lv"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<meta name="color-scheme" content="light only"><meta name="supported-color-schemes" content="light only">' +
    '<title>Jauni auto | BalticRadar</title>' +
    `<style>${MQ}</style></head>` +
    '<body style="margin:0;padding:0;background:#f4f5f7;-webkit-font-smoothing:antialiased">' +
    `<div style="display:none;max-height:0;overflow:hidden;opacity:0">${preview}</div>` +
    '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f4f5f7">' +
    '<tr><td align="center" style="padding:26px 12px">' +
    '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" class="wrap" style="width:600px;max-width:600px">' +
    '<tr><td style="padding:0 4px 18px">' +
    `<a href="${C.SITE}" target="_blank" style="text-decoration:none;display:inline-block"><img src="${C.SITE}/br-icon-black.png" width="30" height="30" alt="BalticRadar" border="0" style="vertical-align:middle;border:0;margin-right:9px"><span style="font:800 24px/1 ${FONT};color:${INK};letter-spacing:-.03em;vertical-align:middle">BalticRadar</span></a></td></tr>` +
    '<tr><td style="padding:0 0 16px">' +
    `<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#ffffff;border:1px solid ${LINE};border-radius:16px">` +
    '<tr><td style="padding:20px 18px">' +
    `<div style="font:800 19px/1.3 ${FONT};color:${INK};letter-spacing:-.02em">Sveiki${hello}: ${n} jauns(-i) auto tavam filtram</div>` +
    `<div style="margin-top:9px;font:600 13px/1.5 ${FONT};color:${INK2}">Filtrs: <span style="display:inline-block;background:#eef3df;color:${ACC};padding:3px 11px;border-radius:999px;font-weight:800">${filterName}</span></div>` +
    `<div style="margin-top:11px;font:500 13px/1.6 ${FONT};color:${MUT}">Šie sludinājumi tikko parādījās ss.lv / autoplius.lt / auto24.ee.</div>` +
    '</td></tr></table></td></tr>' +
    `${rows}${more}` +
    '<tr><td class="cta" align="center" style="padding:8px 0 26px">' +
    `<a href="${C.SITE}" target="_blank" style="display:inline-block;background:${INK};color:#ffffff;text-decoration:none;font:800 14px/1 ${FONT};padding:15px 30px;border-radius:12px">Skatīt visu katalogu &rarr;</a></td></tr>` +
    `<tr><td style="padding:18px 6px 6px;border-top:1px solid ${LINE}">` +
    `<div style="font:500 11px/1.6 ${FONT};color:#a8aeb7">BalticRadar &middot; auto meklētājs Latvijā, Lietuvā un Igaunijā &middot; ${host}</div>` +
    '</td></tr></table></td></tr></table></body></html>'
  );
}

function buildText(C, filterName, cars, extra, magic) {
  const L = [`Jauni auto pēc filtra "${filterName}" - BalticRadar`, ""];
  for (const c of cars) {
    const price = c.last_price ? `${c.last_price} EUR` : "cena nav noradita";
    L.push(`- ${c.make || ""} ${c.model || ""} ${c.year || ""} · ${price}`);
    L.push(`  ${carUrl(C, c, magic)}`);
  }
  if (extra > 0) L.push(`...un vēl ${extra} sludinājumi.`);
  L.push("", `Skatīt visus: ${C.SITE}`, `Mainīt paziņojumu biežumu vai atteikties: ${C.SITE}/?p=filters`);
  return L.join("\n");
}

// ------------------------------------------------------------------ resend
async function resend(C, sender, to, subject, html, text) {
  return fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${C.RESEND_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      from: sender, to: [to], subject, html, text,
      headers: {
        "List-Unsubscribe": `<mailto:meowlybusiness@gmail.com?subject=unsubscribe>, <${C.SITE}/?p=filters>`,
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
      },
    }),
  });
}

async function send(C, to, subject, html, text, log) {
  if (C.TEST_TO) { subject = `[TEST -> ${to}] ${subject}`; to = C.TEST_TO; }
  if (C.DRY_RUN || !C.RESEND_KEY) { log.push(`  [DRY] would email ${to}: ${subject}`); return true; }
  let r = await resend(C, C.FROM, to, subject, html, text);
  if (r.ok) { log.push(`  -> ${to}: ${r.status} (from ${C.FROM})`); return true; }
  const body = (await r.text()).slice(0, 200);
  log.push(`  -> ${to}: ${r.status} ${body}`);
  if ((r.status === 403 || r.status === 422) && C.FROM !== SHARED_FROM) {
    log.push(`  !! sender '${C.FROM}' rejected -> retrying with ${SHARED_FROM}`);
    const r2 = await resend(C, SHARED_FROM, to, subject, html, text);
    if (r2.ok) { log.push(`  -> ${to}: ${r2.status} (delivered via shared sender; owner-only)`); return true; }
    log.push(`  -> ${to}: ${r2.status} ${(await r2.text()).slice(0, 200)}`);
    log.push("  !! BLOCKED: no verified sending domain. Verify balticradar.com in Resend + set ALERT_FROM.");
  }
  return false;
}

// ------------------------------------------------------------------ price drops
async function priceDrops(C, carIds) {
  const out = {};
  if (!carIds.length) return out;
  const CH = 40;
  for (let i = 0; i < carIds.length; i += CH) {
    const chunk = carIds.slice(i, i + CH);
    const ids = chunk.map((c) => `"${c}"`).join(",");
    const rows = await sbGet(C, `price_history?select=car_id,price,ts&car_id=in.(${ids})&order=ts.asc&limit=5000`);
    const by = {};
    for (const r of rows) (by[r.car_id] = by[r.car_id] || []).push(r);
    for (const cid in by) {
      const hist = by[cid];
      if (hist.length >= 2) {
        const prev = hist[hist.length - 2].price, last = hist[hist.length - 1].price;
        if (prev && last && last < prev) out[cid] = prev;
      }
    }
  }
  return out;
}

// ------------------------------------------------------------------ CLAIM-THEN-SEND primitives
// Insert candidate (filter_id, car_id) rows with ON CONFLICT DO NOTHING and return the rows that
// were ACTUALLY inserted. Requires UNIQUE(filter_id, car_id). Returns a Set of won car_ids, or
// null on transport error (in which case we must NOT send — dedupe cannot be guaranteed).
async function claimFilter(C, filterId, carIds) {
  if (!carIds.length) return new Set();
  const rows = carIds.map((id) => ({ filter_id: filterId, car_id: id }));
  const r = await sbPost(C, "filter_notifications", rows, "return=representation,resolution=ignore-duplicates");
  if (!r || !r.ok) { console.log("CLAIM failed", filterId, r && r.status); return null; }
  const data = await r.json().catch(() => []);
  return new Set((Array.isArray(data) ? data : []).map((x) => x.car_id));
}
async function unclaimFilter(C, filterId, carIds) {
  if (!carIds.length) return;
  const ids = carIds.map((c) => `"${c}"`).join(",");
  await sbDelete(C, `filter_notifications?filter_id=eq.${filterId}&car_id=in.(${ids})`);
}
// Legacy subscriptions dedupe: notifications(subscription_id, car_id).
async function claimSub(C, subId, carIds) {
  if (!carIds.length) return new Set();
  const rows = carIds.map((id) => ({ subscription_id: subId, car_id: id }));
  const r = await sbPost(C, "notifications", rows, "return=representation,resolution=ignore-duplicates");
  if (!r || !r.ok) { console.log("CLAIM(sub) failed", subId, r && r.status); return null; }
  const data = await r.json().catch(() => []);
  return new Set((Array.isArray(data) ? data : []).map((x) => x.car_id));
}
async function unclaimSub(C, subId, carIds) {
  if (!carIds.length) return;
  const ids = carIds.map((c) => `"${c}"`).join(",");
  await sbDelete(C, `notifications?subscription_id=eq.${subId}&car_id=in.(${ids})`);
}

// ------------------------------------------------------------------ main pipeline
async function run(env, overrides) {
  const C = cfg(Object.assign({}, env, overrides || {}));
  const log = mkLog();
  const t0 = Date.now();
  const result = { sent: 0, would_send: 0, spent: null, cars_new: 0, cars_eligible: 0, filters: 0, latency_samples: [], lines: log.lines };

  if (!C.URL || !C.KEY) { log.push("FATAL: SUPABASE_URL / SUPABASE_KEY missing"); result.error = "missing supabase creds"; return result; }

  const since = new Date(Date.now() - C.LOOKBACK_MIN * 60000).toISOString().replace(".000Z", "Z");

  // ----- PER-RUN CAP + OLDEST-FIRST PAGINATION (spike guard) -----
  // Fetch AT MOST MAX_CARS_PER_RUN cars, ordered first_seen ASC (oldest first), with the cap applied
  // as a DB-level &limit= (never a JS slice of an over-fetch). This bounds CPU regardless of how many
  // cars are inside the LOOKBACK window, so an ingest spike can no longer blow the ~10 ms free-plan
  // CPU ceiling (exceededCpu). Oldest-first means that when a batch exceeds the cap, THIS run handles
  // the oldest N and the NEXT 5-min cron run handles the following N; the existing claim-then-send
  // dedupe (filter_notifications unique index) skips anything already e-mailed, so a large batch
  // drains across consecutive runs with ZERO duplicates and ZERO drops.
  const cap = Math.max(1, C.MAX_CARS_PER_RUN);
  // Resilient fetch: retries + treats timeout/error/non-array as a FAILURE (never as 0 rows).
  // A genuine empty array still means "0 new cars" and proceeds normally below.
  let cars;
  try {
    cars = await fetchNewCars(C, since, cap, log);
  } catch (e) {
    const msg = (e && e.message) ? e.message : String(e);
    log.push(`FATAL: new-cars fetch failed after ${Math.max(1, C.NEW_CARS_ATTEMPTS)} attempt(s): ${msg}. ` +
      `NOT treating as 0 new cars — aborting so this run is recorded as an ERROR, not a silent success.`);
    result.error = "new-cars fetch failed: " + msg;
    result.ms = Date.now() - t0;
    return result;
  }
  log.push(`NEW cars since ${since} (${C.LOOKBACK_MIN} min), oldest-first, cap ${cap}: ${cars.length}`);
  result.cars_new = cars.length;
  result.cap = cap;

  // If we filled the cap there may be a backlog; count the window (cheap HEAD count) and report how
  // much rolls to the next run(s). Proves the drain behaviour on a spike without sending anything.
  result.capped = cars.length >= cap;
  if (result.capped) {
    const total = await sbCount(C, `cars?select=car_id&active=eq.true&first_seen=gte.${since}`);
    result.cars_in_window = total;
    result.cars_remaining_after_run = (total === null) ? null : Math.max(0, total - cars.length);
    log.push(`CAP HIT: processing oldest ${cars.length} of ${total === null ? "?" : total} new car(s) in the ` +
      `${C.LOOKBACK_MIN}-min window; ~${result.cars_remaining_after_run === null ? "?" : result.cars_remaining_after_run} ` +
      `remain and roll to the next cron run(s) (oldest-first + dedupe = no dupes, no drops).`);
  }
  if (!cars.length) { log.push("nothing new -> no alerts"); log.push(doneLine(C, 0, null)); result.ms = Date.now() - t0; return result; }

  cars = dropStalePosts(C, cars, log);
  log.push(`cars eligible to alert on (newly posted): ${cars.length}`);
  result.cars_eligible = cars.length;
  if (!cars.length) { log.push("nothing NEWLY POSTED -> no alerts"); log.push(doneLine(C, 0, null)); result.ms = Date.now() - t0; return result; }

  const drops = await priceDrops(C, cars.map((c) => c.car_id));
  const filters = await sbGet(C, "saved_filters?select=*&notify_email=eq.true");
  const profList = await sbGet(C, "profiles?select=id,email,full_name,is_premium,plan");
  const profs = {};
  for (const p of profList) profs[p.id] = p;
  log.push(`saved filters with notifications ON: ${filters.length}`);
  result.filters = filters.length;

  let sentEmails = 0;
  const today = {};

  let spent = await globalSendsToday(C, log);
  result.spent = spent;
  let budgetLeft;
  if (spent === null) {
    budgetLeft = C.MAX_EMAILS;
  } else {
    const GLOBAL_BUDGET = Math.max(0, C.PROVIDER_DAILY_LIMIT - C.PROVIDER_RESERVE);
    budgetLeft = GLOBAL_BUDGET - spent;
    const pct = Math.floor((spent * 100) / Math.max(1, C.PROVIDER_DAILY_LIMIT));
    log.push(`PROVIDER BUDGET: ${spent}/${C.PROVIDER_DAILY_LIMIT} spent today (${pct}%), ${budgetLeft} left before the ${GLOBAL_BUDGET} safe ceiling (reserve ${C.PROVIDER_RESERVE})`);
    if (budgetLeft <= 0) { log.push("!! DAILY E-MAIL BUDGET EXHAUSTED. Sending NOTHING this run."); log.push(doneLine(C, 0, spent)); result.ms = Date.now() - t0; return result; }
    if (budgetLeft <= 20) log.push(`!! WARNING: only ${budgetLeft} e-mails left in today's budget.`);
  }

  for (const f of filters) {
    const uid = f.user_id;
    const prof = profs[uid] || {};
    const email = (prof.email || "").trim();
    const name = f.name || "filtrs";
    if (!email) { log.push(`filter ${f.id} '${name}': no email on profile -> skip`); continue; }
    if (C.ONLY_EMAIL && email.toLowerCase() !== C.ONLY_EMAIL) continue;

    const premium = !!prof.is_premium;
    const freq = freqOf(f, premium);
    log.push(`filter ${f.id} '${name}' (${email}) premium=${premium} cadence=${freq}`);

    if (!(C.IGNORE_SENT || C.DRY_RUN) && coolingDown(f, freq, log)) continue;

    if (!(uid in today)) today[uid] = await sendsToday(C, uid, log);
    if (!(C.IGNORE_SENT || C.DRY_RUN) && today[uid] >= C.DAILY_CAP) {
      log.push(`  daily cap: ${today[uid]}/${C.DAILY_CAP} e-mails already sent today -> hold`); continue;
    }

    const criteria = f.criteria || {};
    if (!crit(criteria, "makes", "make").length) {
      log.push("  no make in criteria -> this would match the ENTIRE catalogue. Skipped."); continue;
    }

    const already = C.IGNORE_SENT ? new Set()
      : new Set((await sbGet(C, `filter_notifications?select=car_id&filter_id=eq.${f.id}`)).map((n) => n.car_id));
    let hits = cars.filter((c) => !already.has(c.car_id) && matches(c, criteria));
    if (!hits.length) { log.push("  no new matches -> nothing sent"); continue; }

    if (!C.IGNORE_SENT && hits.length > C.MASS_INGEST_MAX) {
      log.push(`  !! MASS-INGEST GUARD: ${hits.length} matches for this ONE filter (ceiling ${C.MASS_INGEST_MAX}). HOLDING: nothing sent, nothing recorded.`);
      continue;
    }

    hits.sort((a, b) => String(b.first_seen || b.posted || "").localeCompare(String(a.first_seen || a.posted || "")));

    if (sentEmails >= C.MAX_EMAILS) { log.push(`MAX_EMAILS (${C.MAX_EMAILS}) reached -> stopping (per-run cap)`); break; }
    if (!C.IGNORE_SENT && sentEmails >= budgetLeft) { log.push(`!! GLOBAL BUDGET reached (${budgetLeft}) -> stopping.`); break; }

    // ----- CLAIM-THEN-SEND -----
    // For a real run, claim ALL hits BEFORE sending so a parallel notifier (GitHub job / another
    // Worker tick) cannot also send them. Only e-mail rows we actually won. On send failure, undo.
    let claimedHits = hits;
    if (!(C.IGNORE_SENT || C.DRY_RUN)) {
      const won = await claimFilter(C, f.id, hits.map((c) => c.car_id));
      if (won === null) { log.push("  !! claim error -> NOT sending (dedupe unguaranteed); rolls into next run"); continue; }
      claimedHits = hits.filter((c) => won.has(c.car_id));
      if (!claimedHits.length) { log.push("  all matches already claimed by a parallel run -> nothing to send"); continue; }
    }

    const shown = claimedHits.slice(0, C.MAX_PER_EMAIL);
    const extra = claimedHits.length - shown.length;
    // Subject: a SINGLE new car shows "Make Model Year — price" so the phone notification title is
    // the car itself; multiple cars keep the count (the preheader then lists them).
    const _one = claimedHits[0];
    const _subjPrice = (v) => { const x = parseInt(v, 10); return isNaN(x) ? "" : x.toLocaleString("en-US").replace(/,/g, " ") + " €"; };
    const subject = claimedHits.length === 1
      ? ((`${(_one.make || "").trim()} ${(_one.model || "").trim()} ${_one.year || ""}`).trim() + (_one.last_price ? ` — ${_subjPrice(_one.last_price)}` : "")) || "1 jauna mašīna"
      : `${claimedHits.length} jaunas mašīnas`;
    // ONE magic-link token per recipient e-mail: every car button reuses it (the token logs the
    // recipient into THEIR account; the differing ?car=<id> is just the redirect target). Null on
    // failure -> carUrl() emits plain public ?car links and the alert still goes out.
    const magic = await generateMagicToken(C, email, log);
    if (C.DRY_RUN && shown.length) log.push(`  magiclink DRY: link[0] = ${carUrl(C, shown[0], magic)}${magic ? "" : "  (no token -> plain public ?car fallback)"}`);
    const html = buildHtml(C, prof.full_name, name, shown, drops, extra, freq, magic);
    const text = buildText(C, name, shown, extra, magic);

    // latency instrumentation: posted -> first_seen -> (now = emailed)
    for (const c of shown) {
      const p = parseTs(c.posted), fs = parseTs(c.first_seen);
      result.latency_samples.push({
        car_id: c.car_id, source: c.source,
        posted: c.posted, first_seen: c.first_seen, emailed_at: now().toISOString(),
        posted_to_email_min: p ? +minutesBetween(now(), p).toFixed(1) : null,
        firstseen_to_email_min: fs ? +minutesBetween(now(), fs).toFixed(1) : null,
      });
    }

    const ok = await send(C, email, subject, html, text, log);
    if (ok) {
      sentEmails++;
      result.would_send += (C.DRY_RUN || C.IGNORE_SENT) ? 1 : 0;
      today[uid] = (today[uid] || 0) + 1;
      if (C.IGNORE_SENT || C.DRY_RUN) { log.push("  -> TEST send (nothing sent, nothing recorded)"); continue; }
      // claim already recorded the dedupe rows; just stamp cadence + ledger
      await sbPatch(C, `saved_filters?id=eq.${f.id}`, { last_notified_at: now().toISOString() });
      await recordSend(C, uid, f.id);
      log.push(`  -> ${email}: ${claimedHits.length} new car(s) in ONE e-mail`);
    } else if (!(C.IGNORE_SENT || C.DRY_RUN)) {
      // send failed AFTER claiming -> roll the claim back so the cars retry next run
      await unclaimFilter(C, f.id, claimedHits.map((c) => c.car_id));
      log.push(`  !! send failed -> claim rolled back for ${claimedHits.length} car(s)`);
    }
  }

  result.sent = sentEmails;

  // ----- legacy subscriptions (disabled by default) -----
  if (C.LEGACY_SUBS) {
    const subs = await sbGet(C, "subscriptions?select=*");
    const seen = {};
    for (const s of subs) { const e = (s.email || "").toLowerCase().trim(); if (e) seen[e] = s; }
    for (const sub of Object.values(seen)) {
      if (sentEmails >= C.MAX_EMAILS) break;
      if (!C.IGNORE_SENT && sentEmails >= budgetLeft) { log.push("!! GLOBAL BUDGET reached -> legacy subs roll into next run."); break; }
      if (!C.IGNORE_SENT && coolingDown(sub, "daily", log)) { log.push(`legacy sub ${sub.id}: daily digest cooldown -> hold`); continue; }
      const already = new Set((await sbGet(C, `notifications?select=car_id&subscription_id=eq.${sub.id}`)).map((n) => n.car_id));
      const lc = {
        country: sub.country, make: sub.make, model: sub.model, body: sub.body, fuel: sub.fuel,
        gearbox: sub.gearbox, price_min: sub.price_min, price_max: sub.price_max,
        year_min: sub.year_min, year_max: sub.year_max,
        km_min: sub.mileage_min, km_max: sub.mileage_max, eng_min: sub.engine_min, eng_max: sub.engine_max,
      };
      let hits = cars.filter((c) => !already.has(c.car_id) && matches(c, lc));
      if (!hits.length) continue;
      hits.sort((a, b) => String(b.first_seen || "").localeCompare(String(a.first_seen || "")));
      let claimedHits = hits;
      if (!(C.IGNORE_SENT || C.DRY_RUN)) {
        const won = await claimSub(C, sub.id, hits.map((c) => c.car_id));
        if (won === null) { log.push("  !! claim(sub) error -> NOT sending"); continue; }
        claimedHits = hits.filter((c) => won.has(c.car_id));
        if (!claimedHits.length) continue;
      }
      const shown = claimedHits.slice(0, C.MAX_PER_EMAIL);
      const magic = await generateMagicToken(C, sub.email, log);
      const html = buildHtml(C, sub.name, "Tavi kritēriji", shown, drops, claimedHits.length - shown.length, "daily", magic);
      const text = buildText(C, "Tavi kritēriji", shown, claimedHits.length - shown.length, magic);
      const ok = await send(C, sub.email, claimedHits.length === 1 ? claimedHits.length + " jauna mašīna" : claimedHits.length + " jaunas mašīnas", html, text, log);
      if (ok && !(C.IGNORE_SENT || C.DRY_RUN)) {
        sentEmails++;
        await sbPatch(C, `subscriptions?id=eq.${sub.id}`, { last_notified_at: now().toISOString() });
        await recordSend(C, null, null);
      } else if (!ok && !(C.IGNORE_SENT || C.DRY_RUN)) {
        await unclaimSub(C, sub.id, claimedHits.map((c) => c.car_id));
      }
    }
    result.sent = sentEmails;
  } else {
    log.push("legacy `subscriptions` path DISABLED (set LEGACY_SUBS=1 to re-enable).");
  }

  log.push(doneLine(C, sentEmails, spent));
  result.ms = Date.now() - t0;
  return result;
}

function doneLine(C, n, spent) {
  if (C.DRY_RUN || C.IGNORE_SENT) {
    return `DONE (TEST RUN - nothing sent, nothing recorded, no quota spent). Emails that WOULD have been sent: ${n}.` +
      (spent !== null ? ` Real quota still at ${spent}/${C.PROVIDER_DAILY_LIMIT} today.` : "");
  }
  if (spent === null) return `DONE. emails sent this run: ${n} (budget unenforced - run db_alert_cadence.sql)`;
  return `DONE. emails sent this run: ${n}. Today's total: ${spent + n}/${C.PROVIDER_DAILY_LIMIT}.`;
}

// ------------------------------------------------------------------ Worker entrypoints
export default {
  // Cron Trigger "*/5 * * * *"
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env, {}).then((r) => {
      console.log(`[cron ${event.cron}] sent=${r.sent} new=${r.cars_new} eligible=${r.cars_eligible} ms=${r.ms}` +
        (r.error ? ` ERROR=${r.error}` : ""));
      // A failed new-cars fetch sets r.error. THROW so Cloudflare marks this cron invocation as
      // errored (visible in Observability) instead of a green run that silently sent nothing.
      if (r.error) throw new Error("[cron] notifier run failed: " + r.error);
    }));
  },

  // HTTP: manual test + health. Real sends require ?token=<RUN_TOKEN>&send=1.
  async fetch(request, env, ctx) {
    const u = new URL(request.url);
    const C0 = cfg(env);

    if (u.pathname === "/health") {
      return json({
        ok: true, now: new Date().toISOString(),
        site: C0.SITE, from: C0.FROM,
        has_supabase: !!(C0.URL && C0.KEY), has_resend: !!C0.RESEND_KEY,
        cron: "*/5 * * * *",
        config: {
          LOOKBACK_MIN: C0.LOOKBACK_MIN, MAX_CARS_PER_RUN: C0.MAX_CARS_PER_RUN,
          POSTED_MAX_AGE_H: C0.POSTED_MAX_AGE_H,
          MASS_INGEST_MAX: C0.MASS_INGEST_MAX, DAILY_CAP: C0.DAILY_CAP,
          PROVIDER_DAILY_LIMIT: C0.PROVIDER_DAILY_LIMIT, PROVIDER_RESERVE: C0.PROVIDER_RESERVE,
          MAX_PER_EMAIL: C0.MAX_PER_EMAIL, MAX_EMAILS: C0.MAX_EMAILS,
        },
      });
    }

    if (u.pathname === "/run") {
      const dry = u.searchParams.get("dry") !== "0"; // default DRY unless explicitly &dry=0
      const wantSend = u.searchParams.get("send") === "1";
      const token = u.searchParams.get("token") || "";
      // SECURITY: /run (even dry) exposes user e-mails + filters in its log, so gate the WHOLE
      // endpoint behind RUN_TOKEN. Without a valid token nobody can reach the run output at all.
      if (!C0.RUN_TOKEN || token !== C0.RUN_TOKEN) return json({ error: "forbidden: valid ?token required" }, 403);
      const overrides = {};
      const lm = u.searchParams.get("lookback_min");
      if (lm) overrides.LOOKBACK_MIN = lm;
      const only = u.searchParams.get("only_email");
      if (only) overrides.ONLY_EMAIL = only;
      const testTo = u.searchParams.get("test_to");
      if (testTo) overrides.TEST_TO = testTo;

      // Real send guard: must pass the RUN_TOKEN secret AND &send=1 AND &dry=0.
      if (wantSend && !dry) {
        if (!C0.RUN_TOKEN || token !== C0.RUN_TOKEN) return json({ error: "forbidden: valid ?token required for real send" }, 403);
        overrides.DRY_RUN = "0";
      } else {
        overrides.DRY_RUN = "1"; // force dry
      }
      const r = await run(env, overrides);
      // Non-2xx when the new-cars fetch failed, so a DB blip during a manual /run is unmistakable
      // (and never looks like a clean cars_new=0).
      return json(r, r.error ? 502 : 200);
    }

    return json({ ok: true, msg: "BalticRadar notifier. Try /health or /run?dry=1", cron: "*/5 * * * *" });
  },
};

function json(obj, status) {
  return new Response(JSON.stringify(obj, null, 2), {
    status: status || 200,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
