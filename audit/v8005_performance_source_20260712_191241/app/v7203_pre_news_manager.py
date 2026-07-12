import json
import html
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse


TARGET_CARD_PATHS = {
    "/master",
    "/markets",
    "/event-risk",
    "/trade-protection",
    "/trade-protection-report",
    "/pre-news-manager",
}


def _esc(x):
    return html.escape(str(x))


def _root():
    for p in [Path("/app"), Path("/opt/tradingbot_v6000"), Path.cwd()]:
        if (p / "data").exists():
            return p
    return Path.cwd()


def _data(name):
    return _root() / "data" / name


def _cfg_path():
    return _data("v7203_pre_news_config.json")


def _log_path():
    return _data("v7203_pre_news_manager.jsonl")


def _default_cfg():
    return {
        "version": "V7203",
        "enabled": True,
        "auto_actions_enabled": False,
        "high_warning_minutes": 90,
        "high_manage_minutes": 45,
        "high_protect_minutes": 30,
        "high_reduce_minutes": 15,
        "medium_warning_minutes": 45,
        "medium_manage_minutes": 30,
        "medium_protect_minutes": 15,
        "protect_profit_r": 0.5,
        "partial_profit_r": 1.0,
        "danger_negative_r": -0.3,
        "note": "V7203 gives pre-news management recommendations only. It does not close trades or move SL/TP automatically.",
    }


def _cfg():
    c = _default_cfg()
    try:
        p = _cfg_path()
        if p.exists():
            x = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(x, dict):
                c.update(x)
    except Exception:
        pass
    return c


def _save_cfg(c):
    c["updated_utc"] = datetime.now(timezone.utc).isoformat()
    _cfg_path().parent.mkdir(parents=True, exist_ok=True)
    _cfg_path().write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
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
        return {
            "risk_level": "ERROR",
            "cooldown_active": False,
            "active_count": 0,
            "upcoming_count": 0,
            "active_events": [],
            "upcoming_events": [],
            "events": [],
            "load_error": str(exc),
        }


def _open_live_trades():
    try:
        from app.v7202_trade_protection import _open_live_trades
        return _open_live_trades()
    except Exception:
        return []


def _q(x):
    return '"' + str(x).replace('"', '""') + '"'


def _tables(db):
    try:
        con = sqlite3.connect(str(db))
        rows = con.execute("select name from sqlite_master where type='table'").fetchall()
        con.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _cols(con, table):
    try:
        return [r[1] for r in con.execute(f"pragma table_info({_q(table)})").fetchall()]
    except Exception:
        return []


def _num(x):
    try:
        if x is None or x == "":
            return None
        return float(str(x).replace(",", "."))
    except Exception:
        return None


def _latest_price(market):
    market = str(market or "").upper().strip()
    dbs = [
        _data("v7000_learning.sqlite3"),
        _data("v7000_news.sqlite3"),
        _data("trades.sqlite3"),
    ]

    for db in dbs:
        if not db.exists():
            continue

        try:
            con = sqlite3.connect(str(db))
            con.row_factory = sqlite3.Row
        except Exception:
            continue

        try:
            for table in _tables(db):
                cs = _cols(con, table)
                low = {c.lower(): c for c in cs}

                market_col = None
                for k in ["market", "symbol", "instrument", "ticker"]:
                    if k in low:
                        market_col = low[k]
                        break

                close_col = None
                for k in ["close", "price", "last", "last_price", "bid", "mid"]:
                    if k in low:
                        close_col = low[k]
                        break

                if not market_col or not close_col:
                    continue

                time_col = None
                for k in ["received_at", "updated_at", "created_at", "timestamp", "time"]:
                    if k in low:
                        time_col = low[k]
                        break

                order = f"order by {_q(time_col)} desc" if time_col else "order by rowid desc"

                try:
                    row = con.execute(
                        f"select * from {_q(table)} where upper(cast({_q(market_col)} as text))=? {order} limit 1",
                        (market,)
                    ).fetchone()
                except Exception:
                    row = None

                if row:
                    d = dict(row)
                    px = _num(d.get(close_col))
                    if px is not None:
                        return {
                            "price": px,
                            "source_db": str(db),
                            "source_table": table,
                            "time": d.get(time_col) if time_col else None,
                        }
        finally:
            try:
                con.close()
            except Exception:
                pass

    return None


def _trade_r(trade):
    market = trade.get("market")
    direction = str(trade.get("direction") or "").upper()
    entry = _num(trade.get("entry"))
    sl = _num(trade.get("sl"))
    px = _latest_price(market)

    if not px or entry is None or sl is None:
        return {
            "r_now": None,
            "close": px.get("price") if px else None,
            "price_source": px,
            "r_error": "missing price/entry/sl",
        }

    risk = abs(entry - sl)
    if risk <= 0:
        return {
            "r_now": None,
            "close": px.get("price"),
            "price_source": px,
            "r_error": "invalid risk distance",
        }

    close = px.get("price")

    if direction in {"SHORT", "SELL"}:
        r = (entry - close) / risk
    else:
        r = (close - entry) / risk

    return {
        "r_now": round(r, 3),
        "close": close,
        "price_source": px,
        "r_error": None,
    }


def _events_for_market(market):
    d = _event_data()
    market = str(market or "").upper().strip()

    evs = []

    for source_name, arr in [
        ("active_events", d.get("active_events", [])),
        ("upcoming_events", d.get("upcoming_events", [])),
    ]:
        for ev in arr or []:
            markets = [str(x).upper() for x in ev.get("markets", []) or []]
            if market in markets:
                e = dict(ev)
                e["_source"] = source_name
                evs.append(e)

    def score(ev):
        status = str(ev.get("status", "")).upper()
        impact = str(ev.get("impact", "")).upper()
        mins = ev.get("minutes_to_event")
        try:
            mins = float(mins)
        except Exception:
            mins = 999999

        active_score = 0 if status == "ACTIVE_COOLDOWN" else 1
        impact_score = 0 if impact == "HIGH" else 1
        return (active_score, impact_score, mins)

    evs.sort(key=score)
    return evs


def _recommend(trade, event, rdata, cfg):
    if not event:
        return {
            "level": "NORMAL",
            "action": "TRACK_ONLY",
            "score": 0,
            "recommendations": ["Kein relevantes Event für diesen Markt im aktuellen Fenster."],
        }

    impact = str(event.get("impact", "")).upper()
    status = str(event.get("status", "")).upper()

    try:
        mins = float(event.get("minutes_to_event"))
    except Exception:
        mins = None

    r = rdata.get("r_now")

    rec = []
    level = "EVENT_AWARE"
    action = "WATCH"
    score = 1

    if status == "ACTIVE_COOLDOWN":
        level = "ACTIVE_EVENT_PROTECTION"
        action = "MANAGEMENT_ONLY"
        score = 5
        rec += [
            "Aktiver News-Cooldown: keine neuen Entries.",
            "Kein Add-on / kein Nachkaufen.",
            "Nur Management: SL, TP, Exit, Reduce.",
            "Trade nicht automatisch schließen — Entscheidung manuell/regelbasiert.",
        ]
    else:
        if impact == "HIGH":
            if mins is not None and mins <= int(cfg.get("high_reduce_minutes", 15)):
                level = "HIGH_IMPACT_15MIN"
                action = "REDUCE_OR_PROTECT_CHECK"
                score = 4
                rec += ["High Impact in <=15 Min: Reduce/Flat/SL-Protect prüfen.", "Keine neuen Entries oder Add-ons."]
            elif mins is not None and mins <= int(cfg.get("high_protect_minutes", 30)):
                level = "HIGH_IMPACT_30MIN"
                action = "PROTECT_PROFIT_CHECK"
                score = 3
                rec += ["High Impact in <=30 Min: SL/TP/Risiko aktiv prüfen.", "Neue Entries vermeiden."]
            elif mins is not None and mins <= int(cfg.get("high_manage_minutes", 45)):
                level = "HIGH_IMPACT_45MIN"
                action = "PREPARE_MANAGEMENT"
                score = 2
                rec += ["High Impact in <=45 Min: Managementplan vorbereiten."]
            elif mins is not None and mins <= int(cfg.get("high_warning_minutes", 90)):
                level = "HIGH_IMPACT_90MIN"
                action = "WATCH_HIGH_EVENT"
                score = 1
                rec += ["High Impact in <=90 Min: Event im Blick behalten."]
            else:
                rec += ["High Impact Event später im Fenster."]
        else:
            if mins is not None and mins <= int(cfg.get("medium_protect_minutes", 15)):
                level = "MEDIUM_IMPACT_15MIN"
                action = "CAUTION_MANAGEMENT"
                score = 2
                rec += ["Medium Impact in <=15 Min: kein unnötiges Risiko erhöhen."]
            elif mins is not None and mins <= int(cfg.get("medium_manage_minutes", 30)):
                level = "MEDIUM_IMPACT_30MIN"
                action = "WATCH_MEDIUM_EVENT"
                score = 1
                rec += ["Medium Impact in <=30 Min: vorsichtig bleiben."]
            else:
                rec += ["Medium Impact Event im Fenster."]

    if r is not None:
        if r >= float(cfg.get("partial_profit_r", 1.0)):
            rec.append(f"Trade ist ca. +{r}R: Teilgewinn/SL in Profit prüfen.")
        elif r >= float(cfg.get("protect_profit_r", 0.5)):
            rec.append(f"Trade ist ca. +{r}R: Gewinnschutz prüfen.")
        elif r <= float(cfg.get("danger_negative_r", -0.3)):
            rec.append(f"Trade ist ca. {r}R: vor News nicht verschlechtern lassen.")
        else:
            rec.append(f"Trade ist ca. {r}R: neutraler Managementbereich.")
    else:
        rec.append("R_now nicht berechenbar: Entry/SL/Preis prüfen.")

    return {
        "level": level,
        "action": action,
        "score": score,
        "recommendations": rec,
    }


def _protected_trades():
    cfg = _cfg()
    trades = _open_live_trades()
    rows = []

    for t in trades:
        market = str(t.get("market", "")).upper()
        evs = _events_for_market(market)
        ev = evs[0] if evs else None
        rdata = _trade_r(t)
        rec = _recommend(t, ev, rdata, cfg)

        rows.append({
            "trade": t,
            "market": market,
            "event": ev,
            "all_events": evs[:5],
            "r": rdata,
            "level": rec.get("level"),
            "action": rec.get("action"),
            "score": rec.get("score"),
            "recommendations": rec.get("recommendations"),
        })

    rows.sort(key=lambda x: x.get("score", 0), reverse=True)
    return rows


def _next_events(limit=10):
    d = _event_data()
    evs = []
    for ev in d.get("active_events", []) or []:
        e = dict(ev)
        e["_source"] = "active_events"
        evs.append(e)
    for ev in d.get("upcoming_events", []) or []:
        e = dict(ev)
        e["_source"] = "upcoming_events"
        evs.append(e)

    def sort_key(e):
        impact = 0 if str(e.get("impact", "")).lower() == "high" else 1
        try:
            mins = float(e.get("minutes_to_event"))
        except Exception:
            mins = 999999
        active = 0 if str(e.get("status", "")).upper() == "ACTIVE_COOLDOWN" else 1
        return (active, mins, impact)

    evs.sort(key=sort_key)
    return evs[:limit]


def _status():
    rows = _protected_trades()
    cfg = _cfg()
    counts = Counter(r.get("level") for r in rows)

    return {
        "version": "V7203",
        "mode": "PRE_NEWS_POSITION_MANAGER",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "config": cfg,
        "event_risk": _event_data(),
        "open_trade_count": len(rows),
        "highest_score": max([r.get("score", 0) for r in rows], default=0),
        "counts": dict(counts),
        "protected_trades": rows,
        "next_events": _next_events(),
        "auto_actions_enabled": bool(cfg.get("auto_actions_enabled", False)),
        "note": "Recommendations only. No auto-close, no auto-SL/TP movement.",
    }


def _append_log(record):
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _read_log(limit=500):
    p = _log_path()
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []

    rows = []
    for line in reversed(lines[-5000:]):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
        if len(rows) >= limit:
            break
    return rows


def _report(limit=500):
    rows = _read_log(limit=limit)
    levels = Counter(str(r.get("level", "-")) for r in rows)
    markets = Counter(str(r.get("market", "-")) for r in rows)
    actions = Counter(str(r.get("action", "-")) for r in rows)

    return {
        "version": "V7203",
        "mode": "PRE_NEWS_MANAGER_REPORT",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "rows_total": len(rows),
        "top_levels": levels.most_common(10),
        "top_markets": markets.most_common(10),
        "top_actions": actions.most_common(10),
        "latest_rows": rows[:100],
        "log_file": str(_log_path()),
    }


def _candidate_request(request: Request):
    path = request.url.path.lower()
    method = request.method.upper()

    if method not in {"POST", "PUT", "PATCH"}:
        return False

    if any(x in path for x in ["heartbeat", "event-", "trade-protection", "pre-news", "calendar", "maintenance"]):
        return False

    return any(x in path for x in ["signal", "trade", "order", "decision", "webhook"])


def _market_from_request(request: Request):
    for k in ["market", "symbol", "ticker", "instrument"]:
        v = request.query_params.get(k)
        if v:
            return str(v).upper().strip()
    return None


def _card(request: Request):
    s = _status()
    cfg = s.get("config", {})
    token = request.query_params.get("token", "")

    score = int(s.get("highest_score", 0))
    open_count = int(s.get("open_trade_count", 0))

    bg = "#1f6f3d"
    label = "PRE-NEWS OK"

    if score >= 5:
        bg = "#8a1f1f"
        label = "ACTIVE NEWS PROTECTION"
    elif score >= 3:
        bg = "#8a6a1f"
        label = "PRE-NEWS MANAGE"
    elif score >= 1:
        bg = "#8a6a1f"
        label = "EVENT WATCH"

    return f"""
<div id="v7203_pre_news_manager_card" style="
  margin:12px 0 18px 0;
  padding:14px 16px;
  border-radius:12px;
  background:#101923;
  border:1px solid #263447;
  color:#e8eef5;
  font-family:Arial,sans-serif;
">
  <div style="
    display:inline-block;
    padding:7px 11px;
    border-radius:999px;
    background:{bg};
    color:white;
    font-weight:bold;
    margin-bottom:10px;
  ">
    V7203 {_esc(label)}
  </div>
  <div style="font-size:13px;color:#c8d6e4;line-height:1.45;">
    Open live trades: <b>{_esc(open_count)}</b> |
    Highest score: <b>{_esc(score)}</b> |
    Auto actions: <b>{_esc(cfg.get("auto_actions_enabled"))}</b>
    <br>
    Event risk: <b>{_esc(s.get("event_risk", {}).get("risk_level"))}</b> |
    Cooldown: <b>{_esc(s.get("event_risk", {}).get("cooldown_active"))}</b> |
    Upcoming: <b>{_esc(s.get("event_risk", {}).get("upcoming_count"))}</b>
    <br>
    <a style="color:#8cc8ff;" href="/pre-news-manager?token={_esc(token)}">Pre-News Manager</a> |
    <a style="color:#8cc8ff;" href="/pre-news-manager-report?token={_esc(token)}">Report</a> |
    <a style="color:#8cc8ff;" href="/pre-news-manager-config?token={_esc(token)}">Config</a>
  </div>
</div>
"""


def _inject(page, request):
    if "v7203_pre_news_manager_card" in page:
        return page

    card = _card(request)

    for marker in ["v7202_trade_protection_card", "v7200_9_calendar_status_card"]:
        if marker in page:
            pos = page.find("</div>", page.find(marker))
            if pos != -1:
                pos += len("</div>")
                return page[:pos] + card + page[pos:]

    lower = page.lower()
    body_pos = lower.find("<body")
    if body_pos != -1:
        end = page.find(">", body_pos)
        if end != -1:
            return page[:end + 1] + card + page[end + 1:]

    return card + page


def _trade_rows(rows):
    out = ""

    for r in rows:
        t = r.get("trade", {})
        ev = r.get("event") or {}
        rr = r.get("r") or {}
        rec = "<br>".join(_esc(x) for x in r.get("recommendations", []))

        out += f"""
<tr>
  <td>{_esc(t.get("market"))}</td>
  <td>{_esc(t.get("direction"))}</td>
  <td>{_esc(t.get("setup_name"))}</td>
  <td>{_esc(t.get("entry"))}</td>
  <td>{_esc(t.get("sl"))}</td>
  <td>{_esc(t.get("tp1"))}</td>
  <td>{_esc(rr.get("close"))}</td>
  <td>{_esc(rr.get("r_now"))}</td>
  <td>{_esc(r.get("level"))}</td>
  <td>{_esc(r.get("action"))}</td>
  <td>{_esc(ev.get("title", "-"))}<br>{_esc(ev.get("impact", "-"))} {_esc(ev.get("status", "-"))}<br>{_esc(ev.get("minutes_to_event", "-"))} min</td>
  <td>{rec}</td>
</tr>
"""

    if not out:
        out = "<tr><td colspan='12'>Keine offenen Live-Trades. V7203 wartet auf neue Positionen.</td></tr>"

    return out


def _event_rows(events):
    out = ""

    for ev in events:
        out += f"""
<tr>
  <td>{_esc(ev.get("time_utc"))}</td>
  <td>{_esc(ev.get("currency"))}</td>
  <td>{_esc(ev.get("impact"))}</td>
  <td>{_esc(ev.get("status"))}</td>
  <td>{_esc(ev.get("minutes_to_event"))}</td>
  <td>{_esc(ev.get("title"))}</td>
  <td>{_esc(", ".join(ev.get("markets") or []))}</td>
</tr>
"""

    if not out:
        out = "<tr><td colspan='7'>Keine Events im Fenster.</td></tr>"

    return out


def _log_rows(rows):
    out = ""

    for r in rows:
        out += f"""
<tr>
  <td>{_esc(r.get("ts_utc"))}</td>
  <td>{_esc(r.get("path"))}</td>
  <td>{_esc(r.get("market"))}</td>
  <td>{_esc(r.get("level"))}</td>
  <td>{_esc(r.get("action"))}</td>
  <td>{_esc(r.get("event_title"))}</td>
  <td>{_esc(r.get("r_now"))}</td>
</tr>
"""

    if not out:
        out = "<tr><td colspan='7'>Noch keine Logs.</td></tr>"

    return out


def install_v7203_pre_news_manager(app):
    if getattr(app.state, "v7203_pre_news_manager_installed", False):
        return

    @app.middleware("http")
    async def v7203_middleware(request: Request, call_next):
        if _candidate_request(request):
            market = _market_from_request(request)

            row = None
            if market:
                fake_trade = {"market": market, "direction": "-", "entry": None, "sl": None, "tp1": None}
                evs = _events_for_market(market)
                ev = evs[0] if evs else None
                rec = _recommend(fake_trade, ev, {"r_now": None}, _cfg())

                row = {
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                    "path": request.url.path,
                    "method": request.method,
                    "market": market,
                    "level": rec.get("level"),
                    "action": rec.get("action"),
                    "event_title": ev.get("title") if ev else None,
                    "event_status": ev.get("status") if ev else None,
                    "event_impact": ev.get("impact") if ev else None,
                    "r_now": None,
                }

                _append_log(row)

        response = await call_next(request)

        try:
            path = request.url.path
            if path in TARGET_CARD_PATHS and response.status_code == 200:
                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type or "charset" in content_type:
                    body = b""
                    async for chunk in response.body_iterator:
                        body += chunk
                    page = body.decode("utf-8", errors="ignore")
                    return HTMLResponse(_inject(page, request), status_code=response.status_code)
        except Exception:
            return response

        return response

    @app.get("/pre-news-manager", response_class=HTMLResponse)
    def pre_news_manager_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        s = _status()
        token = request.query_params.get("token", "")

        page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7203 Pre-News Manager</title>
  <style>
    body {{ background:#0b0f14; color:#e8eef5; font-family:Arial,sans-serif; margin:24px; }}
    a {{ color:#8cc8ff; text-decoration:none; }}
    .card {{ background:#121923; border:1px solid #263447; border-radius:12px; padding:16px; margin-bottom:18px; }}
    .badge {{ display:inline-block; padding:8px 12px; border-radius:999px; background:#1f6f3d; color:white; font-weight:bold; }}
    table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }}
    th,td {{ border-bottom:1px solid #263447; padding:8px; text-align:left; vertical-align:top; }}
    th {{ color:#a9bfd6; }}
    .muted {{ color:#9fb0c0; }}
  </style>
</head>
<body>
  <h1>TradingBot V7203 — Pre-News Position Manager</h1>

  <div class="card">
    <div class="badge">V7203 RECOMMENDATIONS ONLY</div>
    <p class="muted">
      Open live trades: <b>{_esc(s.get("open_trade_count"))}</b> |
      Highest score: <b>{_esc(s.get("highest_score"))}</b> |
      Auto actions: <b>{_esc(s.get("auto_actions_enabled"))}</b>
    </p>
    <p>
      <a href="/pre-news-manager.json?token={_esc(token)}">JSON</a> ·
      <a href="/pre-news-manager-config?token={_esc(token)}">Config</a> ·
      <a href="/pre-news-manager-report?token={_esc(token)}">Report</a> ·
      <a href="/trade-protection?token={_esc(token)}">Trade Protection</a> ·
      <a href="/event-risk?token={_esc(token)}">Event Risk</a>
    </p>
    <p class="muted">{_esc(s.get("note"))}</p>
  </div>

  <div class="card">
    <h2>Open Trade Pre-News Management</h2>
    <table>
      <tr>
        <th>Market</th><th>Side</th><th>Setup</th><th>Entry</th><th>SL</th><th>TP1</th>
        <th>Close</th><th>R now</th><th>Level</th><th>Action</th><th>Event</th><th>Recommendations</th>
      </tr>
      {_trade_rows(s.get("protected_trades", []))}
    </table>
  </div>

  <div class="card">
    <h2>Next Relevant Events</h2>
    <table>
      <tr><th>UTC</th><th>CCY</th><th>Impact</th><th>Status</th><th>Min</th><th>Title</th><th>Markets</th></tr>
      {_event_rows(s.get("next_events", []))}
    </table>
  </div>
</body>
</html>
"""
        return HTMLResponse(page)

    @app.get("/pre-news-manager.json")
    def pre_news_manager_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_status())

    @app.get("/pre-news-manager-config", response_class=HTMLResponse)
    def pre_news_config_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        c = _cfg()
        token = request.query_params.get("token", "")
        rows = "".join(f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in c.items())

        page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7203 Config</title>
  <style>
    body {{ background:#0b0f14; color:#e8eef5; font-family:Arial,sans-serif; margin:24px; }}
    a {{ color:#8cc8ff; text-decoration:none; }}
    .card {{ background:#121923; border:1px solid #263447; border-radius:12px; padding:16px; margin-bottom:18px; }}
    table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }}
    th,td {{ border-bottom:1px solid #263447; padding:8px; text-align:left; vertical-align:top; }}
    th {{ color:#a9bfd6; }}
    button {{ background:#123456; color:#e8eef5; border:1px solid #355273; border-radius:8px; padding:8px 11px; cursor:pointer; }}
  </style>
</head>
<body>
  <h1>TradingBot V7203 — Config</h1>
  <div class="card">
    <p>V7203 ist bewusst Recommendation-only. Auto Actions bleiben aus.</p>
    <form method="post" action="/pre-news-manager-config/set?token={_esc(token)}&enabled=1" style="display:inline;">
      <button type="submit">Enabled ON</button>
    </form>
    <form method="post" action="/pre-news-manager-config/set?token={_esc(token)}&enabled=0" style="display:inline;">
      <button type="submit">Enabled OFF</button>
    </form>
    <p>
      <a href="/pre-news-manager?token={_esc(token)}">Pre-News Manager</a> ·
      <a href="/pre-news-manager-report?token={_esc(token)}">Report</a>
    </p>
  </div>
  <div class="card">
    <table>
      <tr><th>Key</th><th>Value</th></tr>
      {rows}
    </table>
  </div>
</body>
</html>
"""
        return HTMLResponse(page)

    @app.get("/pre-news-manager-config.json")
    def pre_news_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    @app.post("/pre-news-manager-config/set")
    def pre_news_config_set(request: Request, enabled: Optional[int] = None):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        c = _cfg()
        if enabled is not None:
            c["enabled"] = bool(int(enabled))
        c = _save_cfg(c)

        return JSONResponse({"version": "V7203", "updated": True, "config": c})

    @app.get("/pre-news-manager-report", response_class=HTMLResponse)
    def pre_news_report_page(request: Request, limit: int = 500):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        r = _report(limit=limit)
        token = request.query_params.get("token", "")

        page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7203 Report</title>
  <style>
    body {{ background:#0b0f14; color:#e8eef5; font-family:Arial,sans-serif; margin:24px; }}
    a {{ color:#8cc8ff; text-decoration:none; }}
    .card {{ background:#121923; border:1px solid #263447; border-radius:12px; padding:16px; margin-bottom:18px; }}
    table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }}
    th,td {{ border-bottom:1px solid #263447; padding:8px; text-align:left; vertical-align:top; }}
    th {{ color:#a9bfd6; }}
    .muted {{ color:#9fb0c0; }}
  </style>
</head>
<body>
  <h1>TradingBot V7203 — Report</h1>
  <div class="card">
    <p class="muted">Rows: <b>{_esc(r.get("rows_total"))}</b></p>
    <p>
      <a href="/pre-news-manager-report.json?token={_esc(token)}">JSON</a> ·
      <a href="/pre-news-manager?token={_esc(token)}">Pre-News Manager</a>
    </p>
  </div>
  <div class="card">
    <table>
      <tr><th>UTC</th><th>Path</th><th>Market</th><th>Level</th><th>Action</th><th>Event</th><th>R</th></tr>
      {_log_rows(r.get("latest_rows", []))}
    </table>
  </div>
</body>
</html>
"""
        return HTMLResponse(page)

    @app.get("/pre-news-manager-report.json")
    def pre_news_report_json(request: Request, limit: int = 500):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_report(limit=limit))

    app.state.v7203_pre_news_manager_installed = True
    print("[V7203] Pre-News Position Manager installed")
