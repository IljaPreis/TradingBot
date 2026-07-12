import json, html, sqlite3
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, Counter
from typing import Optional
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

def _esc(x): return html.escape(str(x))

def _root():
    for p in [Path("/app"), Path("/opt/tradingbot_v6000"), Path.cwd()]:
        if (p / "data").exists():
            return p
    return Path.cwd()

def _data(name): return _root() / "data" / name

def _read_json(name, default=None):
    if default is None: default = {}
    try:
        p = _data(name)
        if p.exists():
            x = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(x, dict): return x
    except Exception as exc:
        return {"load_error": str(exc)}
    return default

def _write_json(name, obj):
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

def _cfg():
    c = {"version":"V7219-V7226","enabled":True,"observe_only":True,"min_samples":2,
         "note":"Performance pack is observe-only. No trades, no blocks, no auto actions."}
    c.update(_read_json("v7219_v7226_performance_config.json", {}))
    return c

def _token_ok(request: Request):
    try:
        from app.v7200_event_risk import _token_ok as real_token_ok
        return real_token_ok(request)
    except Exception:
        return bool(request.query_params.get("token", ""))

def _event_data():
    try:
        from app.v7200_event_risk import _event_data
        return _event_data()
    except Exception as exc:
        return {"risk_level":"ERROR","cooldown_active":False,"upcoming_count":0,"load_error":str(exc)}

def _control_status():
    try:
        from app.v7207_master_compact_control_center import _status
        return _status()
    except Exception as exc:
        return {"safe_state":False,"risky_on":True,"safety":{},"event_risk":_event_data(),"load_error":str(exc)}

def _parse_dt(x):
    if not x: return None
    s = str(x).strip()
    try:
        if s.endswith("Z"): s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None

def _session(dt):
    h = dt.hour if dt else datetime.now(timezone.utc).hour
    if 0 <= h < 6: return "ASIA"
    if 6 <= h < 12: return "LONDON"
    if 12 <= h < 17: return "NEW_YORK_OPEN"
    if 17 <= h < 21: return "NEW_YORK_LATE"
    return "AFTER_HOURS"

def _num(x, default=0.0):
    try:
        if x is None or x == "": return default
        return float(str(x).replace(",", "."))
    except Exception:
        return default

def _sqlite_files():
    d = _root() / "data"
    return sorted(d.glob("*.sqlite3")) if d.exists() else []

def _pick(low, names):
    for n in names:
        if n in low: return low[n]
    return None

def _scan_rows():
    rows = []
    for db in _sqlite_files():
        try:
            con = sqlite3.connect(str(db)); con.row_factory = sqlite3.Row
        except Exception:
            continue
        try:
            tables = con.execute("select name from sqlite_master where type='table'").fetchall()
            for tr in tables:
                table = tr[0]; tl = table.lower()
                try:
                    info = con.execute(f'pragma table_info("{table}")').fetchall()
                except Exception:
                    continue
                cols = [x[1] for x in info]
                low = {str(c).lower(): c for c in cols}
                market_col = _pick(low, ["market","symbol","instrument","ticker"])
                setup_col = _pick(low, ["setup_name","setup","strategy","signal_name","reason"])
                result_col = _pick(low, ["result","outcome","status"])
                pnl_col = _pick(low, ["pnl_r","r","rr","profit_r","pnl","profit"])
                side_col = _pick(low, ["direction","side","action"])
                time_col = _pick(low, ["closed_at","exit_time","created_at","timestamp","time","received_at"])
                id_col = _pick(low, ["id","trade_id","shadow_id","signal_id"])
                if not market_col: continue
                if not pnl_col and not result_col: continue
                if not any(x in tl for x in ["trade","outcome","shadow","learning","signal"]): continue
                order = f'order by "{time_col}" desc' if time_col else "order by rowid desc"
                try:
                    db_rows = con.execute(f'select * from "{table}" {order} limit 5000').fetchall()
                except Exception:
                    continue
                for rr in db_rows:
                    d = dict(rr)
                    market = str(d.get(market_col, "")).upper().strip()
                    if not market: continue
                    setup = str(d.get(setup_col, "") or "UNKNOWN_SETUP").strip()
                    result = str(d.get(result_col, "") or "").upper().strip()
                    pnl_r = _num(d.get(pnl_col), None) if pnl_col else None
                    if pnl_r is None:
                        pnl_r = 1.0 if result in {"WIN","WON","TP","PROFIT"} else (-1.0 if result in {"LOSS","LOST","SL"} else 0.0)
                    if not result:
                        result = "WIN" if pnl_r > 0 else ("LOSS" if pnl_r < 0 else "FLAT")
                    closed_raw = d.get(time_col) if time_col else None
                    dt = _parse_dt(closed_raw)
                    source_type = "SHADOW" if ("shadow" in tl or "SHADOW" in str(d.get(id_col, "")).upper()) else "LIVE_OR_LEARNING"
                    rows.append({
                        "source_db":db.name, "source_table":table, "source_type":source_type,
                        "id":d.get(id_col) if id_col else None, "market":market,
                        "direction":str(d.get(side_col, "") or "").upper(), "setup_name":setup,
                        "result":result, "pnl_r":round(float(pnl_r),4),
                        "closed_at":str(closed_raw) if closed_raw is not None else None,
                        "hour_utc":dt.hour if dt else None, "session":_session(dt),
                    })
        finally:
            try: con.close()
            except Exception: pass
    seen, clean = set(), []
    for r in rows:
        key = (r.get("source_db"), r.get("source_table"), str(r.get("id")), r.get("market"), r.get("setup_name"), r.get("closed_at"), r.get("pnl_r"))
        if key in seen: continue
        seen.add(key); clean.append(r)
    return clean

def _grade(count, winrate, total_r):
    if count <= 0: return "NO_DATA", "WAIT_FOR_DATA"
    if count >= 3 and total_r >= 1.5 and winrate >= 55: return "STRONG_EDGE", "PRIORITY_WATCH"
    if count >= 2 and (total_r <= -0.5 or winrate < 45): return "WEAK_EDGE", "DEPRIORITIZE"
    if total_r > 0: return "POSITIVE", "TRADEABLE_IF_CURRENT_SCORE_CONFIRMS"
    if total_r < 0: return "NEGATIVE", "CAUTION"
    return "NEUTRAL", "WAIT_FOR_MORE_DATA"

def _aggregate(rows, fields):
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r.get(f, "-") for f in fields)].append(r)
    out = []
    for key, items in groups.items():
        count = len(items)
        wins = sum(1 for x in items if float(x.get("pnl_r",0)) > 0)
        losses = sum(1 for x in items if float(x.get("pnl_r",0)) < 0)
        flats = count - wins - losses
        total_r = round(sum(float(x.get("pnl_r",0)) for x in items), 4)
        avg_r = round(total_r / count, 4) if count else 0
        winrate = round((wins / count) * 100, 2) if count else 0
        grade, action = _grade(count, winrate, total_r)
        row = {"count":count,"wins":wins,"losses":losses,"flats":flats,"winrate":winrate,
               "total_r":total_r,"avg_r":avg_r,"grade":grade,"action":action,"latest":items[:5]}
        for i, f in enumerate(fields): row[f] = key[i]
        out.append(row)
    out.sort(key=lambda x:(x.get("total_r",0),x.get("winrate",0),x.get("count",0)), reverse=True)
    return out

def _setup_performance():
    rows = _scan_rows(); agg = _aggregate(rows, ["setup_name"])
    return {"version":"V7219","mode":"SETUP_PERFORMANCE","now_utc":datetime.now(timezone.utc).isoformat(),
            "rows_total":len(rows),"setups":agg,"best_setups":agg[:10],"weak_setups":list(reversed(agg[-10:])),
            "observe_only":True}

def _market_session_performance():
    rows = _scan_rows()
    return {"version":"V7220","mode":"MARKET_SESSION_PERFORMANCE","now_utc":datetime.now(timezone.utc).isoformat(),
            "rows_total":len(rows),"by_market":_aggregate(rows,["market"]),"by_session":_aggregate(rows,["session"]),
            "by_market_session":_aggregate(rows,["market","session"]),"observe_only":True}

def _news_performance():
    rows = _scan_rows(); ev = _event_data(); tagged = []
    for r in rows:
        txt = json.dumps(r, ensure_ascii=False).lower()
        if any(x in txt for x in ["event","news","fomc","cpi","nfp","rate","claims","employment"]): tagged.append(r)
    return {"version":"V7221","mode":"NEWS_EVENT_PERFORMANCE_VIEW","now_utc":datetime.now(timezone.utc).isoformat(),
            "current_event_risk":{"risk_level":ev.get("risk_level"),"cooldown_active":ev.get("cooldown_active"),"upcoming_count":ev.get("upcoming_count")},
            "rows_total":len(rows),"event_tagged_rows":len(tagged),"untagged_rows":len(rows)-len(tagged),
            "event_tagged_setup_performance":_aggregate(tagged,["setup_name"]) if tagged else [],
            "note":"More exact news performance needs event tags at close time.","observe_only":True}

def _shadow_edge():
    rows = [r for r in _scan_rows() if r.get("source_type") == "SHADOW"]
    by_setup = _aggregate(rows, ["setup_name"]); by_market = _aggregate(rows, ["market"]); by_market_setup = _aggregate(rows, ["market","setup_name"])
    for r in by_setup + by_market + by_market_setup:
        score = 50 + (r.get("total_r",0)*8) + ((r.get("winrate",0)-50)*0.5)
        if r.get("count",0) < 3: score -= 10
        r["shadow_edge_score"] = max(0, min(100, round(score, 2)))
    by_market_setup.sort(key=lambda x:x.get("shadow_edge_score",0), reverse=True)
    return {"version":"V7222","mode":"SHADOW_EDGE_SCORE","now_utc":datetime.now(timezone.utc).isoformat(),
            "shadow_rows_total":len(rows),"by_setup":by_setup,"by_market":by_market,"by_market_setup":by_market_setup,
            "best_shadow_edges":by_market_setup[:10],"weak_shadow_edges":list(reversed(by_market_setup[-10:])),
            "observe_only":True}

def _best_times():
    rows = _scan_rows(); hour_rows = [r for r in rows if r.get("hour_utc") is not None]
    return {"version":"V7223","mode":"BEST_TRADING_TIMES","now_utc":datetime.now(timezone.utc).isoformat(),
            "rows_total":len(rows),"by_hour_utc":_aggregate(hour_rows,["hour_utc"]),"by_session":_aggregate(rows,["session"]),
            "by_market_hour":_aggregate(hour_rows,["market","hour_utc"]),"observe_only":True}

def _weak_setups():
    s = _setup_performance(); sh = _shadow_edge()
    weak = [x for x in s.get("setups", []) if x.get("grade") == "WEAK_EDGE" or x.get("total_r",0) < 0]
    weak_shadow = [x for x in sh.get("by_market_setup", []) if x.get("grade") == "WEAK_EDGE" or x.get("shadow_edge_score",100) < 45]
    return {"version":"V7224","mode":"WEAK_SETUP_DETECTOR","now_utc":datetime.now(timezone.utc).isoformat(),
            "weak_setups":weak[:20],"weak_shadow_edges":weak_shadow[:20],
            "recommendations":["Weak setups depriorisieren.","Nicht automatisch deaktivieren.","Starkes Setup + positive Shadow Edge + aktuelle Readiness bevorzugen."],
            "observe_only":True}

def _candidate_inbox():
    p = _data("v7217_trade_candidate_inbox.jsonl"); rows = []
    if p.exists():
        try:
            for line in reversed(p.read_text(encoding="utf-8", errors="ignore").splitlines()[-2000:]):
                if not line.strip(): continue
                try: rows.append(json.loads(line))
                except Exception: pass
        except Exception: pass
    return {"rows_total":len(rows),"latest":rows[:100],
            "top_markets":Counter(str(x.get("market","-")) for x in rows).most_common(10),
            "top_decisions":Counter(str(x.get("decision","-")) for x in rows).most_common(10)}

def _summary():
    setup = _setup_performance(); ms = _market_session_performance(); news = _news_performance()
    shadow = _shadow_edge(); times = _best_times(); weak = _weak_setups(); control = _control_status()
    return {"version":"V7225","mode":"LEARNING_SUMMARY_DASHBOARD","now_utc":datetime.now(timezone.utc).isoformat(),
            "safe_state":control.get("safe_state"),"risky_on":control.get("risky_on"),"rows_total":setup.get("rows_total"),
            "best_setups":setup.get("best_setups",[])[:8],"weak_setups":weak.get("weak_setups",[])[:8],
            "best_markets":ms.get("by_market",[])[:8],"best_sessions":times.get("by_session",[])[:8],
            "best_hours":times.get("by_hour_utc",[])[:8],"best_shadow_edges":shadow.get("best_shadow_edges",[])[:8],
            "news_event_view":{"current_event_risk":news.get("current_event_risk"),"event_tagged_rows":news.get("event_tagged_rows"),"untagged_rows":news.get("untagged_rows")},
            "candidate_inbox":_candidate_inbox(),"observe_only":True}

def _daily(write_file=False):
    s = _summary(); ev = _event_data()
    r = {"version":"V7226","mode":"DAILY_PERFORMANCE_REPORT","generated_utc":datetime.now(timezone.utc).isoformat(),
         "safe_state":s.get("safe_state"),"risky_on":s.get("risky_on"),
         "event_risk":{"risk_level":ev.get("risk_level"),"cooldown_active":ev.get("cooldown_active"),"upcoming_count":ev.get("upcoming_count")},
         "performance_summary":s,
         "headline":{"best_setups":s.get("best_setups",[])[:3],"weak_setups":s.get("weak_setups",[])[:3],
                     "best_shadow_edges":s.get("best_shadow_edges",[])[:3],"best_sessions":s.get("best_sessions",[])[:3]},
         "observe_only":True}
    if write_file: _write_json("v7226_daily_performance_report_last.json", r)
    return r

def _links(token):
    links = [
        ("Performance","/performance-learning"),("Setups","/setup-performance"),("Market/Session","/market-session-performance"),
        ("News","/news-performance"),("Shadow Edge","/shadow-edge"),("Best Times","/best-times"),
        ("Weak Setups","/weak-setups"),("Daily Report","/daily-performance-report"),("Intel","/daily-intelligence"),
    ]
    return " · ".join(f'<a href="{url}?token={_esc(token)}">{_esc(label)}</a>' for label, url in links)

def _html_page(title, body, request):
    token = request.query_params.get("token", "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>
body{{background:#0b0f14;color:#e8eef5;font-family:Arial,sans-serif;margin:24px}}
a{{color:#8cc8ff;text-decoration:none}}.card{{background:#121923;border:1px solid #263447;border-radius:12px;padding:16px;margin-bottom:18px}}
.badge{{display:inline-block;padding:7px 11px;border-radius:999px;color:white;font-weight:bold;background:#1f6f3d}}
.warn{{background:#8a6a1f}}table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}}
th,td{{border-bottom:1px solid #263447;padding:8px;text-align:left;vertical-align:top}}th{{color:#a9bfd6}}.muted{{color:#9fb0c0}}
</style></head><body><h1>{_esc(title)}</h1><div class="card">{_links(token)}</div>{body}</body></html>"""

def _perf_rows(rows, keys):
    out = ""
    for r in rows:
        name = " / ".join(str(r.get(k, "-")) for k in keys)
        out += f"""<tr><td>{_esc(name)}</td><td>{_esc(r.get("count"))}</td><td>{_esc(r.get("wins"))}</td>
<td>{_esc(r.get("losses"))}</td><td>{_esc(r.get("winrate"))}%</td><td>{_esc(r.get("avg_r"))}</td>
<td>{_esc(r.get("total_r"))}</td><td>{_esc(r.get("grade"))}</td><td>{_esc(r.get("action"))}</td></tr>"""
    return out or "<tr><td colspan='9'>Noch keine auswertbaren Daten.</td></tr>"

def _standard_table(title, rows, keys):
    return f"""<div class="card"><h2>{_esc(title)}</h2><table>
<tr><th>Name</th><th>N</th><th>Wins</th><th>Losses</th><th>Winrate</th><th>Avg R</th><th>Total R</th><th>Grade</th><th>Action</th></tr>
{_perf_rows(rows, keys)}</table></div>"""

def install_v7219_v7226_performance_learning_pack(app):
    if getattr(app.state, "v7219_v7226_performance_installed", False): return

    @app.get("/performance-learning", response_class=HTMLResponse)
    def performance_learning_page(request: Request):
        if not _token_ok(request): return HTMLResponse("unauthorized", status_code=401)
        s = _summary()
        body = f"""<div class="card"><span class="badge">V7219-V7226 PERFORMANCE</span>
<p class="muted">Safe: <b>{_esc(s.get("safe_state"))}</b> | Risky: <b>{_esc(s.get("risky_on"))}</b> | Rows: <b>{_esc(s.get("rows_total"))}</b></p>
<a href="/performance-learning.json?token={_esc(request.query_params.get("token",""))}">JSON</a></div>
{_standard_table("Best Setups", s.get("best_setups", []), ["setup_name"])}
{_standard_table("Weak Setups", s.get("weak_setups", []), ["setup_name"])}
{_standard_table("Best Shadow Edges", s.get("best_shadow_edges", []), ["market", "setup_name"])}
{_standard_table("Best Sessions", s.get("best_sessions", []), ["session"])}"""
        return HTMLResponse(_html_page("TradingBot V7219-V7226 - Performance Learning", body, request))

    @app.get("/performance-learning.json")
    def performance_learning_json(request: Request):
        if not _token_ok(request): return JSONResponse({"error":"unauthorized"}, status_code=401)
        return JSONResponse(_summary())

    @app.get("/performance-learning-config.json")
    def performance_learning_config_json(request: Request):
        if not _token_ok(request): return JSONResponse({"error":"unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    def html_route(request, title, badge, data, tables):
        if not _token_ok(request): return HTMLResponse("unauthorized", status_code=401)
        body = f'<div class="card"><span class="badge">{_esc(badge)}</span></div>' + "".join(_standard_table(t, rows, keys) for t, rows, keys in tables)
        return HTMLResponse(_html_page(title, body, request))

    @app.get("/setup-performance", response_class=HTMLResponse)
    def setup_page(request: Request):
        r = _setup_performance()
        return html_route(request, "TradingBot V7219 - Setup Performance", "V7219 SETUP PERFORMANCE", r, [("All Setups", r.get("setups", []), ["setup_name"])])

    @app.get("/setup-performance.json")
    def setup_json(request: Request):
        if not _token_ok(request): return JSONResponse({"error":"unauthorized"}, status_code=401)
        return JSONResponse(_setup_performance())

    @app.get("/market-session-performance", response_class=HTMLResponse)
    def market_session_page(request: Request):
        r = _market_session_performance()
        return html_route(request, "TradingBot V7220 - Market Session Performance", "V7220 MARKET SESSION", r,
                          [("By Market", r.get("by_market", []), ["market"]), ("By Session", r.get("by_session", []), ["session"]),
                           ("By Market + Session", r.get("by_market_session", []), ["market", "session"])])

    @app.get("/market-session-performance.json")
    def market_session_json(request: Request):
        if not _token_ok(request): return JSONResponse({"error":"unauthorized"}, status_code=401)
        return JSONResponse(_market_session_performance())

    @app.get("/news-performance", response_class=HTMLResponse)
    def news_page(request: Request):
        r = _news_performance()
        return html_route(request, "TradingBot V7221 - News Performance", "V7221 NEWS PERFORMANCE", r,
                          [("Event Tagged Setup Performance", r.get("event_tagged_setup_performance", []), ["setup_name"])])

    @app.get("/news-performance.json")
    def news_json(request: Request):
        if not _token_ok(request): return JSONResponse({"error":"unauthorized"}, status_code=401)
        return JSONResponse(_news_performance())

    @app.get("/shadow-edge", response_class=HTMLResponse)
    def shadow_page(request: Request):
        r = _shadow_edge()
        return html_route(request, "TradingBot V7222 - Shadow Edge", "V7222 SHADOW EDGE", r,
                          [("Best Shadow Edges", r.get("best_shadow_edges", []), ["market","setup_name"]),
                           ("By Setup", r.get("by_setup", []), ["setup_name"])])

    @app.get("/shadow-edge.json")
    def shadow_json(request: Request):
        if not _token_ok(request): return JSONResponse({"error":"unauthorized"}, status_code=401)
        return JSONResponse(_shadow_edge())

    @app.get("/best-times", response_class=HTMLResponse)
    def best_times_page(request: Request):
        r = _best_times()
        return html_route(request, "TradingBot V7223 - Best Times", "V7223 BEST TIMES", r,
                          [("Best Hours UTC", r.get("by_hour_utc", []), ["hour_utc"]),
                           ("Best Sessions", r.get("by_session", []), ["session"]),
                           ("Best Market + Hour", r.get("by_market_hour", []), ["market","hour_utc"])])

    @app.get("/best-times.json")
    def best_times_json(request: Request):
        if not _token_ok(request): return JSONResponse({"error":"unauthorized"}, status_code=401)
        return JSONResponse(_best_times())

    @app.get("/weak-setups", response_class=HTMLResponse)
    def weak_page(request: Request):
        r = _weak_setups()
        return html_route(request, "TradingBot V7224 - Weak Setups", "V7224 WEAK SETUPS", r,
                          [("Weak Setups", r.get("weak_setups", []), ["setup_name"]),
                           ("Weak Shadow Edges", r.get("weak_shadow_edges", []), ["market","setup_name"])])

    @app.get("/weak-setups.json")
    def weak_json(request: Request):
        if not _token_ok(request): return JSONResponse({"error":"unauthorized"}, status_code=401)
        return JSONResponse(_weak_setups())

    @app.get("/daily-performance-report", response_class=HTMLResponse)
    def daily_page(request: Request, write: Optional[int] = 1):
        if not _token_ok(request): return HTMLResponse("unauthorized", status_code=401)
        r = _daily(write_file=bool(int(write))); h = r.get("headline", {})
        body = f"""<div class="card"><span class="badge">V7226 DAILY PERFORMANCE REPORT</span>
<p class="muted">Safe: <b>{_esc(r.get("safe_state"))}</b> | Risky: <b>{_esc(r.get("risky_on"))}</b></p>
<a href="/daily-performance-report.json?token={_esc(request.query_params.get("token",""))}">JSON</a></div>
{_standard_table("Best Setups", h.get("best_setups", []), ["setup_name"])}
{_standard_table("Weak Setups", h.get("weak_setups", []), ["setup_name"])}
{_standard_table("Best Shadow Edges", h.get("best_shadow_edges", []), ["market","setup_name"])}"""
        return HTMLResponse(_html_page("TradingBot V7226 - Daily Performance Report", body, request))

    @app.get("/daily-performance-report.json")
    def daily_json(request: Request, write: Optional[int] = 1):
        if not _token_ok(request): return JSONResponse({"error":"unauthorized"}, status_code=401)
        return JSONResponse(_daily(write_file=bool(int(write))))

    app.state.v7219_v7226_performance_installed = True
    print("[V7219-V7226] Performance Learning Pack installed")
