# V7243.9 Stability Pack
# Read-only health/stability board for FJ, Calendar, Macro Actuals, Safe Live Gate, Heartbeats, Positions.

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

DB_CANDIDATES = [
    Path("/app/data/v7000_learning.sqlite3"),
    Path("/opt/tradingbot_v6000/data/v7000_learning.sqlite3"),
    Path("data/v7000_learning.sqlite3"),
]

def _db_path():
    for p in DB_CANDIDATES:
        if p.exists():
            return str(p)
    return "data/v7000_learning.sqlite3"

def _utc_now():
    return datetime.now(timezone.utc)

def _parse_dt(x):
    if not x:
        return None
    try:
        s = str(x).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None

def _age_minutes(x):
    dt = _parse_dt(x)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round((_utc_now() - dt.astimezone(timezone.utc)).total_seconds() / 60.0, 2)

def _q1(con, sql, params=(), default=None):
    try:
        r = con.execute(sql, params).fetchone()
        if not r:
            return default
        return list(r)[0]
    except Exception:
        return default

def _rows(con, sql, params=()):
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []

def _table_exists(con, name):
    return bool(_q1(con, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,), None))

def _trigger_exists(con, name):
    return bool(_q1(con, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,), None))

def _status(ok, warn=False):
    if ok:
        return "PASS"
    if warn:
        return "WARN"
    return "FAIL"

def v72439_stability_payload():
    db = _db_path()
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    out = {
        "ok": True,
        "version": "V7243.9-STABILITY-PACK",
        "generated_utc": _utc_now().isoformat(),
        "db": db,
        "modules": {},
        "summary": {},
        "latest": {},
    }

    # FinancialJuice
    fj_last_msg = _q1(con, "SELECT value FROM financialjuice_status WHERE key='last_message_at'", default=None) if _table_exists(con, "financialjuice_status") else None
    fj_connected = _q1(con, "SELECT value FROM financialjuice_status WHERE key='connected'", default="no") if _table_exists(con, "financialjuice_status") else "no"
    fj_age = _age_minutes(fj_last_msg)
    fj_news = _q1(con, "SELECT COUNT(*) FROM financialjuice_news", default=0) if _table_exists(con, "financialjuice_news") else 0
    fj_msgs = _q1(con, "SELECT COUNT(*) FROM financialjuice_messages", default=0) if _table_exists(con, "financialjuice_messages") else 0
    out["modules"]["financialjuice"] = {
        "status": _status(str(fj_connected).lower() == "yes" and fj_age is not None and fj_age < 30, warn=(fj_age is not None and fj_age < 90)),
        "connected": fj_connected,
        "last_message_at": fj_last_msg,
        "last_message_age_min": fj_age,
        "news_rows": fj_news,
        "message_rows": fj_msgs,
    }

    # Calendar / Macro Actual
    cal_count = _q1(con, "SELECT COUNT(*) FROM economic_calendar_events", default=0) if _table_exists(con, "economic_calendar_events") else 0
    actual_count = _q1(con, "SELECT COUNT(*) FROM economic_calendar_events WHERE COALESCE(actual,'')!=''", default=0) if _table_exists(con, "economic_calendar_events") else 0
    macro_updates = _q1(con, "SELECT COUNT(*) FROM v72436_macro_actual_updates", default=0) if _table_exists(con, "v72436_macro_actual_updates") else 0
    ehs = _rows(con, """
        SELECT country, title, actual, forecast, previous, impact, score, surprise_score
        FROM economic_calendar_events
        WHERE country='USD' AND title='Existing Home Sales'
        ORDER BY event_time_berlin DESC
        LIMIT 1
    """)
    ehs_clean = bool(ehs and str(ehs[0].get("actual")) == "4.09M" and str(ehs[0].get("score")) != "100.0")
    out["modules"]["calendar_macro"] = {
        "status": _status(cal_count > 0 and macro_updates > 0 and ehs_clean),
        "calendar_events": cal_count,
        "events_with_actual": actual_count,
        "macro_actual_updates": macro_updates,
        "existing_home_sales_clean": ehs_clean,
        "existing_home_sales": ehs[0] if ehs else None,
    }

    # DB Guards
    guards = {
        "no_pct_update": _trigger_exists(con, "v72436d_no_pct_existing_home_sales_update"),
        "no_pct_insert": _trigger_exists(con, "v72436d_no_pct_existing_home_sales_insert"),
        "auto_classify_insert": _trigger_exists(con, "v72436e_classify_existing_home_sales_pct_insert"),
        "auto_classify_update": _trigger_exists(con, "v72436e_classify_existing_home_sales_pct_update"),
        "duplicate_guard": _trigger_exists(con, "v72436f_ignore_duplicate_existing_home_sales_pct_insert"),
    }
    out["modules"]["macro_guards"] = {
        "status": _status(all(guards.values())),
        "guards": guards,
    }

    # Safe Live Permission
    v72437_count = _q1(con, "SELECT COUNT(*) FROM v72437_live_permission_log", default=0) if _table_exists(con, "v72437_live_permission_log") else 0
    good_recent = _q1(con, """
        SELECT COUNT(*)
        FROM v72437_live_permission_log
        WHERE market NOT LIKE 'V72437%'
          AND COALESCE(direction,'')!=''
          AND COALESCE(confidence,0) > 0
          AND id > 95
    """, default=0) if _table_exists(con, "v72437_live_permission_log") else 0
    latest_gate = _rows(con, """
        SELECT id, created_at, market, direction, confidence, news_score, new_action, proposed_state, substr(reasons_json,1,160) AS reasons
        FROM v72437_live_permission_log
        WHERE market NOT LIKE 'V72437%'
        ORDER BY id DESC
        LIMIT 8
    """)
    out["modules"]["safe_live_permission"] = {
        "status": _status(v72437_count > 0 and good_recent > 0),
        "log_rows": v72437_count,
        "valid_real_logs_after_fix": good_recent,
        "latest": latest_gate,
    }

    # Pine runtime
    pine_rows = _q1(con, "SELECT COUNT(*) FROM v7242_pine_runtime_log", default=0) if _table_exists(con, "v7242_pine_runtime_log") else 0
    latest_pine = _rows(con, """
        SELECT id, created_at, market, direction, setup_name, v7242_score, v7242_effect, final_gate_action
        FROM v7242_pine_runtime_log
        ORDER BY id DESC
        LIMIT 8
    """)
    out["modules"]["pine_runtime"] = {
        "status": _status(pine_rows > 0),
        "log_rows": pine_rows,
        "latest": latest_pine,
    }

    # Heartbeats
    hb_exists = _table_exists(con, "price_heartbeats")
    hb_count = _q1(con, "SELECT COUNT(*) FROM price_heartbeats", default=0) if hb_exists else 0
    hb_latest = _q1(con, "SELECT MAX(received_at) FROM price_heartbeats", default=None) if hb_exists else None
    hb_age = _age_minutes(hb_latest)
    latest_hb = _rows(con, """
        SELECT market, timeframe, ticker, close, received_at
        FROM price_heartbeats
        ORDER BY received_at DESC
        LIMIT 10
    """) if hb_exists else []
    out["modules"]["price_heartbeats"] = {
        "status": _status(hb_count > 0 and hb_age is not None and hb_age < 15, warn=(hb_age is not None and hb_age < 60)),
        "rows": hb_count,
        "latest_received_at": hb_latest,
        "latest_age_min": hb_age,
        "latest": latest_hb,
    }

    # Positions
    open_live = _q1(con, "SELECT COUNT(*) FROM open_trades WHERE UPPER(status)='OPEN'", default=0) if _table_exists(con, "open_trades") else 0
    open_shadow = _q1(con, "SELECT COUNT(*) FROM shadow_trades WHERE UPPER(status)='OPEN'", default=0) if _table_exists(con, "shadow_trades") else 0
    latest_live = _rows(con, """
        SELECT market, direction, setup_name, entry, sl, tp1, status, opened_at
        FROM open_trades
        WHERE UPPER(status)='OPEN'
        ORDER BY opened_at DESC
        LIMIT 10
    """) if _table_exists(con, "open_trades") else []
    out["modules"]["positions"] = {
        "status": "PASS",
        "open_live": open_live,
        "open_shadow": open_shadow,
        "latest_live": latest_live,
    }

    # DB size / backup hint
    try:
        db_size_mb = round(Path(db).stat().st_size / 1024 / 1024, 2)
    except Exception:
        db_size_mb = None
    out["modules"]["database"] = {
        "status": _status(db_size_mb is not None and db_size_mb < 500, warn=(db_size_mb is not None and db_size_mb < 1000)),
        "size_mb": db_size_mb,
    }

    # Overall
    states = [m.get("status") for m in out["modules"].values()]
    out["summary"] = {
        "pass": states.count("PASS"),
        "warn": states.count("WARN"),
        "fail": states.count("FAIL"),
        "overall": "FAIL" if "FAIL" in states else ("WARN" if "WARN" in states else "PASS"),
    }

    con.close()
    return out

def _esc(x):
    import html
    return html.escape(str(x if x is not None else ""))

def v72439_stability_html():
    d = v72439_stability_payload()
    modules = d.get("modules", {})

    def badge(s):
        c = {"PASS":"good", "WARN":"warn", "FAIL":"bad"}.get(s, "warn")
        return f'<span class="badge {c}">{_esc(s)}</span>'

    cards = []
    for name, m in modules.items():
        details = []
        for k, v in m.items():
            if k in ["latest", "latest_live", "guards", "existing_home_sales"]:
                continue
            details.append(f"<div><b>{_esc(k)}</b>: {_esc(v)}</div>")
        cards.append(f"""
        <div class="card">
          <h2>{_esc(name)}</h2>
          {badge(m.get("status"))}
          <div class="small">{''.join(details)}</div>
        </div>
        """)

    latest_gate_rows = "".join(
        f"<tr><td>{_esc(r.get('id'))}</td><td>{_esc(r.get('market'))}</td><td>{_esc(r.get('direction'))}</td><td>{_esc(r.get('confidence'))}</td><td>{_esc(r.get('news_score'))}</td><td>{_esc(r.get('new_action'))}</td><td>{_esc(r.get('reasons'))}</td></tr>"
        for r in modules.get("safe_live_permission", {}).get("latest", [])
    )

    latest_hb_rows = "".join(
        f"<tr><td>{_esc(r.get('market'))}</td><td>{_esc(r.get('timeframe'))}</td><td>{_esc(r.get('close'))}</td><td>{_esc(r.get('received_at'))}</td></tr>"
        for r in modules.get("price_heartbeats", {}).get("latest", [])
    )

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>V7243.9 Stability</title>
<style>
body{{margin:0;background:#07111e;color:#eef6ff;font-family:Arial,sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:14px}}
h1{{margin:8px 0 4px;font-size:32px}}
.sub{{color:#9caec4;margin-bottom:14px}}
.grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}}
.card,.section{{background:#101d2e;border:1px solid #273a52;border-radius:16px;padding:14px;margin:10px 0}}
.card h2{{margin:0 0 8px;font-size:22px}}
.badge{{display:inline-block;padding:4px 10px;border-radius:999px;font-weight:800;font-size:12px}}
.good{{background:#0f3b24;color:#86efac}}
.warn{{background:#3b2e0f;color:#facc15}}
.bad{{background:#3b1111;color:#fca5a5}}
.small{{font-size:13px;color:#b7c4d4;line-height:1.45;margin-top:8px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{border-bottom:1px solid #273a52;padding:7px;text-align:left;vertical-align:top}}
th{{color:#a5d8ff}}
a{{color:#93c5fd;text-decoration:none}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr}} h1{{font-size:28px}}}}
</style>
</head>
<body>
<div class="wrap">
<h1>🛡️ V7243.9 Stability Pack</h1>
<div class="sub">Generated UTC: {_esc(d.get("generated_utc"))} · Overall: {badge(d.get("summary",{}).get("overall"))}</div>

<div class="grid">
{''.join(cards)}
</div>

<div class="section">
<h2>Safe Live Permission Latest</h2>
<table>
<thead><tr><th>ID</th><th>Market</th><th>Dir</th><th>Conf</th><th>News</th><th>Action</th><th>Reasons</th></tr></thead>
<tbody>{latest_gate_rows or '<tr><td colspan="7">Keine Daten</td></tr>'}</tbody>
</table>
</div>

<div class="section">
<h2>Latest Price Heartbeats</h2>
<table>
<thead><tr><th>Market</th><th>TF</th><th>Close</th><th>Received</th></tr></thead>
<tbody>{latest_hb_rows or '<tr><td colspan="4">Keine Daten</td></tr>'}</tbody>
</table>
</div>

<div class="section">
<a href="/master">← Master</a> ·
<a href="/stability-v7243.json">JSON</a> ·
<a href="/calendar">Calendar</a> ·
<a href="/live-permission-v7243">Live Permission</a>
</div>

</div>
</body>
</html>"""
    return html

# === V7244B STABILITY INTEGRATION ===
try:
    if "_v72439_base_payload_for_v7244b" not in globals():
        _v72439_base_payload_for_v7244b = v72439_stability_payload

        def v72439_stability_payload():
            d = _v72439_base_payload_for_v7244b()
            try:
                from app.v7244_live_decision_quality import v7244_live_quality_payload

                q = v7244_live_quality_payload(limit=80)
                summary = q.get("summary", {}) or {}

                evaluated = int(summary.get("evaluated") or 0)
                live = int(summary.get("live_candidates") or 0)
                review = int(summary.get("review_candidates") or 0)
                counts = summary.get("counts") or {}
                shadow = int(counts.get("SHADOW_ONLY_RECOMMENDED") or 0)
                would_change = int(summary.get("would_change_vs_v72437") or 0)

                status = "PASS" if q.get("status") == "PASS" and evaluated > 0 else "WARN"

                d.setdefault("modules", {})["v7244_live_quality"] = {
                    "status": status,
                    "mode": q.get("mode"),
                    "evaluated": evaluated,
                    "live_candidates": live,
                    "review_candidates": review,
                    "shadow_only": shadow,
                    "would_change_vs_v72437": would_change,
                    "enforce": False,
                }
            except Exception as exc:
                d.setdefault("modules", {})["v7244_live_quality"] = {
                    "status": "FAIL",
                    "error": str(exc)[:240],
                }

            states = [m.get("status") for m in d.get("modules", {}).values()]
            d["summary"] = {
                "pass": states.count("PASS"),
                "warn": states.count("WARN"),
                "fail": states.count("FAIL"),
                "overall": "FAIL" if "FAIL" in states else ("WARN" if "WARN" in states else "PASS"),
            }
            d["version"] = str(d.get("version", "V7243.9")) + "+V7244B"
            return d

    print("[V7244B] Stability integration active.")
except Exception as exc:
    print("[V7244B] Stability integration failed:", exc)
# === END V7244B STABILITY INTEGRATION ===
