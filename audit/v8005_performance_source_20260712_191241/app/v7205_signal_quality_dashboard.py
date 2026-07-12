import json
import html
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse


TARGET_CARD_PATHS = {
    "/master",
    "/markets",
    "/event-risk",
    "/trade-protection",
    "/pre-news-manager",
    "/entry-scoring",
    "/signal-quality",
}

DEFAULT_MARKETS = [
    "US30", "US100", "US500", "XAUUSD",
    "EURUSD", "GBPUSD", "USDJPY", "USDCAD",
    "AUDUSD", "NZDUSD", "EURJPY", "GBPJPY",
    "AUDJPY", "NZDJPY", "CADJPY", "EURGBP",
    "GER40", "FRA40", "FTSE100", "ASX200",
    "UKOILSPOT"
]


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
    return _data("v7205_signal_quality_config.json")


def _log_path():
    return _data("v7205_signal_quality_log.jsonl")


def _default_cfg():
    return {
        "version": "V7205",
        "enabled": True,
        "apply_to_live": False,
        "base_score": 75,
        "excellent_threshold": 80,
        "good_threshold": 65,
        "caution_threshold": 45,
        "avoid_threshold": 25,
        "shadow_win_bonus": 8,
        "shadow_loss_penalty": -6,
        "shadow_min_samples": 2,
        "active_event_force_no_entry": True,
        "note": "V7205 is a dashboard and scoring aggregator only. It does not block or modify live trades.",
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
            "load_error": str(exc),
        }


def _entry_score(market, score=None, confidence=None):
    try:
        from app.v7204_news_aware_entry_scoring import _score_market
        return _score_market(market, base_score=score, confidence=confidence)
    except Exception as exc:
        return {
            "market": market,
            "base_score": score,
            "adjusted_score": score,
            "score_penalty": 0,
            "base_confidence": confidence,
            "adjusted_confidence": confidence,
            "entry_allowed_by_scoring": True,
            "quality": "ENTRY_SCORING_UNAVAILABLE",
            "reason": str(exc),
            "event": None,
            "risk": {},
        }


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


def _shadow_stats():
    dbs = [
        _data("v7000_learning.sqlite3"),
        _data("v7000_trades.sqlite3"),
        _data("trades.sqlite3"),
    ]

    rows = []

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
                result_col = None
                pnl_col = None
                time_col = None

                for k in ["market", "symbol", "instrument", "ticker"]:
                    if k in low:
                        market_col = low[k]
                        break

                for k in ["result", "outcome", "status"]:
                    if k in low:
                        result_col = low[k]
                        break

                for k in ["pnl_r", "r", "rr", "pnl"]:
                    if k in low:
                        pnl_col = low[k]
                        break

                for k in ["closed_at", "created_at", "timestamp", "time"]:
                    if k in low:
                        time_col = low[k]
                        break

                if not market_col or not result_col:
                    continue

                order = f"order by {_q(time_col)} desc" if time_col else "order by rowid desc"

                try:
                    q = f"select * from {_q(table)} {order} limit 500"
                    for r in con.execute(q).fetchall():
                        d = dict(r)
                        market = str(d.get(market_col, "")).upper().strip()
                        result = str(d.get(result_col, "")).upper().strip()

                        if not market or result not in {"WIN", "LOSS"}:
                            continue

                        rows.append({
                            "market": market,
                            "result": result,
                            "pnl_r": _num(d.get(pnl_col)) if pnl_col else None,
                            "time": d.get(time_col) if time_col else None,
                            "source_table": table,
                        })
                except Exception:
                    continue
        finally:
            try:
                con.close()
            except Exception:
                pass

    agg = defaultdict(lambda: {"wins": 0, "losses": 0, "samples": 0, "total_r": 0.0, "last": []})

    for r in rows:
        m = r["market"]
        agg[m]["samples"] += 1
        if r["result"] == "WIN":
            agg[m]["wins"] += 1
        elif r["result"] == "LOSS":
            agg[m]["losses"] += 1

        if r.get("pnl_r") is not None:
            agg[m]["total_r"] += float(r["pnl_r"])

        if len(agg[m]["last"]) < 10:
            agg[m]["last"].append(r)

    out = {}
    for m, v in agg.items():
        samples = int(v["samples"])
        wins = int(v["wins"])
        losses = int(v["losses"])
        winrate = round((wins / samples) * 100, 1) if samples else 0.0
        out[m] = {
            "market": m,
            "samples": samples,
            "wins": wins,
            "losses": losses,
            "winrate": winrate,
            "total_r": round(v["total_r"], 2),
            "last": v["last"],
        }

    return out


def _grade(score, allowed, cfg):
    if not allowed:
        return "NO_ENTRY"
    if score >= float(cfg.get("excellent_threshold", 80)):
        return "EXCELLENT"
    if score >= float(cfg.get("good_threshold", 65)):
        return "GOOD"
    if score >= float(cfg.get("caution_threshold", 45)):
        return "CAUTION"
    if score >= float(cfg.get("avoid_threshold", 25)):
        return "WEAK"
    return "AVOID"


def _action_for_grade(grade):
    if grade == "EXCELLENT":
        return "BEST_SETUP_WATCH"
    if grade == "GOOD":
        return "TRADEABLE_IF_TECHNICALS_CONFIRM"
    if grade == "CAUTION":
        return "SMALL_SIZE_OR_WAIT"
    if grade == "WEAK":
        return "LOW_PRIORITY"
    if grade == "AVOID":
        return "AVOID"
    if grade == "NO_ENTRY":
        return "NO_ENTRY_NEWS_RISK"
    return "WATCH"


def _evaluate_market(market, score=None, confidence=None):
    cfg = _cfg()
    market = str(market or "").upper().strip()

    base_score = _num(score)
    if base_score is None:
        base_score = float(cfg.get("base_score", 75))

    base_conf = _num(confidence)
    if base_conf is None:
        base_conf = 70.0

    es = _entry_score(market, score=base_score, confidence=base_conf)
    shadow = _shadow_stats().get(market)

    final = es.get("adjusted_score")
    if final is None:
        final = base_score

    notes = []
    adjustments = []

    penalty = _num(es.get("score_penalty")) or 0
    if penalty != 0:
        adjustments.append({"source": "V7204_NEWS", "value": penalty})
        notes.append(f"News adjustment {penalty}")

    allowed = bool(es.get("entry_allowed_by_scoring", True))

    event = es.get("event") or {}
    event_status = str(event.get("status", "")).upper()
    event_impact = str(event.get("impact", "")).upper()

    if bool(cfg.get("active_event_force_no_entry", True)) and event_status == "ACTIVE_COOLDOWN":
        allowed = False
        notes.append("Active event cooldown forces no-entry.")

    if shadow and int(shadow.get("samples", 0)) >= int(cfg.get("shadow_min_samples", 2)):
        if shadow.get("wins", 0) > shadow.get("losses", 0):
            b = float(cfg.get("shadow_win_bonus", 8))
            final += b
            adjustments.append({"source": "SHADOW_EDGE", "value": b})
            notes.append("Shadow edge positive.")
        elif shadow.get("losses", 0) > shadow.get("wins", 0):
            p = float(cfg.get("shadow_loss_penalty", -6))
            final += p
            adjustments.append({"source": "SHADOW_EDGE", "value": p})
            notes.append("Shadow edge negative.")

    final = max(0, min(100, round(final, 2)))

    grade = _grade(final, allowed, cfg)
    action = _action_for_grade(grade)

    return {
        "version": "V7205",
        "market": market,
        "base_score": base_score,
        "base_confidence": base_conf,
        "final_score": final,
        "grade": grade,
        "action": action,
        "entry_allowed": allowed,
        "adjustments": adjustments,
        "notes": notes,
        "entry_scoring": es,
        "event": event,
        "event_status": event_status,
        "event_impact": event_impact,
        "shadow_stats": shadow,
        "apply_to_live": bool(cfg.get("apply_to_live", False)),
        "observe_only": not bool(cfg.get("apply_to_live", False)),
        "now_utc": datetime.now(timezone.utc).isoformat(),
    }


def _status():
    cfg = _cfg()
    rows = [_evaluate_market(m) for m in DEFAULT_MARKETS]

    rows.sort(key=lambda x: x.get("final_score", 0), reverse=True)

    grade_counts = Counter(r.get("grade") for r in rows)

    best = rows[:5]
    worst = list(reversed(rows[-5:]))

    return {
        "version": "V7205",
        "mode": "SIGNAL_QUALITY_DASHBOARD",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "config": cfg,
        "event_risk": _event_data(),
        "market_count": len(rows),
        "grade_counts": dict(grade_counts),
        "best_markets": best,
        "worst_markets": worst,
        "markets": rows,
        "note": "Dashboard only. No live blocking or order modification.",
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
    grades = Counter(str(r.get("grade", "-")) for r in rows)
    markets = Counter(str(r.get("market", "-")) for r in rows)
    actions = Counter(str(r.get("action", "-")) for r in rows)

    return {
        "version": "V7205",
        "mode": "SIGNAL_QUALITY_REPORT",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "rows_total": len(rows),
        "top_grades": grades.most_common(10),
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

    ignore = ["heartbeat", "event-", "entry-scoring", "signal-quality", "trade-protection", "pre-news", "calendar", "maintenance"]
    if any(x in path for x in ignore):
        return False

    return any(x in path for x in ["signal", "trade", "order", "decision", "webhook"])


def _market_from_request(request: Request):
    for k in ["market", "symbol", "ticker", "instrument"]:
        v = request.query_params.get(k)
        if v:
            return str(v).upper().strip()
    return None


def _score_from_request(request: Request):
    for k in ["score", "base_score", "rating"]:
        v = request.query_params.get(k)
        if v is not None:
            return v
    return None


def _confidence_from_request(request: Request):
    for k in ["confidence", "conf", "base_confidence"]:
        v = request.query_params.get(k)
        if v is not None:
            return v
    return None


def _card(request: Request):
    s = _status()
    token = request.query_params.get("token", "")
    cfg = s.get("config", {})

    counts = s.get("grade_counts", {})
    no_entry = int(counts.get("NO_ENTRY", 0))
    excellent = int(counts.get("EXCELLENT", 0))
    good = int(counts.get("GOOD", 0))

    bg = "#1f6f3d"
    label = "SIGNAL QUALITY OK"

    if no_entry > 0:
        bg = "#8a1f1f"
        label = "NEWS NO-ENTRY ACTIVE"
    elif excellent + good > 0:
        bg = "#1f6f3d"
        label = "QUALITY BOARD ACTIVE"

    best_txt = ", ".join([str(x.get("market")) + ":" + str(x.get("grade")) for x in s.get("best_markets", [])[:3]])

    return f"""
<div id="v7205_signal_quality_card" style="
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
    V7205 {_esc(label)}
  </div>
  <div style="font-size:13px;color:#c8d6e4;line-height:1.45;">
    Excellent: <b>{_esc(excellent)}</b> |
    Good: <b>{_esc(good)}</b> |
    No Entry: <b>{_esc(no_entry)}</b> |
    Apply live: <b>{_esc(cfg.get("apply_to_live"))}</b>
    <br>
    Best: <b>{_esc(best_txt)}</b>
    <br>
    <a style="color:#8cc8ff;" href="/signal-quality?token={_esc(token)}">Signal Quality</a> |
    <a style="color:#8cc8ff;" href="/signal-quality-report?token={_esc(token)}">Report</a> |
    <a style="color:#8cc8ff;" href="/signal-quality-config?token={_esc(token)}">Config</a>
  </div>
</div>
"""


def _inject(page, request):
    if "v7205_signal_quality_card" in page:
        return page

    card = _card(request)

    for marker in ["v7204_entry_scoring_card", "v7203_pre_news_manager_card", "v7202_trade_protection_card", "v7200_9_calendar_status_card"]:
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


def _market_rows(rows):
    out = ""

    for r in rows:
        ev = r.get("event") or {}
        shadow = r.get("shadow_stats") or {}
        out += f"""
<tr>
  <td>{_esc(r.get("market"))}</td>
  <td>{_esc(r.get("final_score"))}</td>
  <td>{_esc(r.get("grade"))}</td>
  <td>{_esc(r.get("action"))}</td>
  <td>{_esc(r.get("entry_allowed"))}</td>
  <td>{_esc(ev.get("title", "-"))}<br>{_esc(ev.get("impact", "-"))} {_esc(ev.get("status", "-"))}</td>
  <td>{_esc(r.get("entry_scoring", {}).get("score_penalty"))}</td>
  <td>{_esc(shadow.get("samples", 0))}</td>
  <td>{_esc(shadow.get("winrate", "-"))}</td>
  <td>{_esc(shadow.get("total_r", "-"))}</td>
  <td>{_esc("; ".join(r.get("notes") or []))}</td>
</tr>
"""

    if not out:
        out = "<tr><td colspan='11'>Keine Märkte.</td></tr>"

    return out


def _log_rows(rows):
    out = ""

    for r in rows:
        out += f"""
<tr>
  <td>{_esc(r.get("now_utc"))}</td>
  <td>{_esc(r.get("path"))}</td>
  <td>{_esc(r.get("method"))}</td>
  <td>{_esc(r.get("market"))}</td>
  <td>{_esc(r.get("final_score"))}</td>
  <td>{_esc(r.get("grade"))}</td>
  <td>{_esc(r.get("action"))}</td>
  <td>{_esc(r.get("entry_allowed"))}</td>
</tr>
"""

    if not out:
        out = "<tr><td colspan='8'>Noch keine Logs.</td></tr>"

    return out


def install_v7205_signal_quality_dashboard(app):
    if getattr(app.state, "v7205_signal_quality_installed", False):
        return

    @app.middleware("http")
    async def v7205_middleware(request: Request, call_next):
        if _candidate_request(request):
            market = _market_from_request(request)
            if market:
                rec = _evaluate_market(
                    market,
                    score=_score_from_request(request),
                    confidence=_confidence_from_request(request),
                )
                rec["path"] = request.url.path
                rec["method"] = request.method
                _append_log(rec)

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

    @app.get("/signal-quality", response_class=HTMLResponse)
    def signal_quality_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        s = _status()
        token = request.query_params.get("token", "")

        page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7205 Signal Quality</title>
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
  <h1>TradingBot V7205 — Signal Quality Dashboard</h1>

  <div class="card">
    <div class="badge">V7205 QUALITY BOARD</div>
    <p class="muted">
      Markets: <b>{_esc(s.get("market_count"))}</b> |
      Apply live: <b>{_esc(s.get("config", {}).get("apply_to_live"))}</b> |
      Event risk: <b>{_esc(s.get("event_risk", {}).get("risk_level"))}</b>
    </p>
    <p>
      <a href="/signal-quality.json?token={_esc(token)}">JSON</a> ·
      <a href="/signal-quality-config?token={_esc(token)}">Config</a> ·
      <a href="/signal-quality-report?token={_esc(token)}">Report</a> ·
      <a href="/signal-quality-evaluate.json?token={_esc(token)}&market=US100&score=75&confidence=70">Evaluate US100</a>
    </p>
    <p class="muted">{_esc(s.get("note"))}</p>
  </div>

  <div class="card">
    <h2>Best Markets</h2>
    <table>
      <tr>
        <th>Market</th><th>Score</th><th>Grade</th><th>Action</th><th>Allowed</th>
        <th>Event</th><th>News Penalty</th><th>Shadow Samples</th><th>Shadow WR</th><th>Shadow R</th><th>Notes</th>
      </tr>
      {_market_rows(s.get("markets", []))}
    </table>
  </div>
</body>
</html>
"""
        return HTMLResponse(page)

    @app.get("/signal-quality.json")
    def signal_quality_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_status())

    @app.get("/signal-quality-evaluate.json")
    def signal_quality_evaluate_json(
        request: Request,
        market: str,
        score: Optional[float] = None,
        confidence: Optional[float] = None,
    ):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_evaluate_market(market, score=score, confidence=confidence))

    @app.get("/signal-quality-config", response_class=HTMLResponse)
    def signal_quality_config_page(request: Request):
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
  <title>TradingBot V7205 Config</title>
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
  <h1>TradingBot V7205 — Config</h1>
  <div class="card">
    <p>V7205 bleibt Dashboard/Scoring. Apply live ist standardmäßig OFF.</p>
    <form method="post" action="/signal-quality-config/set?token={_esc(token)}&apply_to_live=0" style="display:inline;">
      <button type="submit">Apply Live OFF</button>
    </form>
    <form method="post" action="/signal-quality-config/set?token={_esc(token)}&apply_to_live=1" style="display:inline;">
      <button type="submit">Apply Live ON</button>
    </form>
    <p>
      <a href="/signal-quality?token={_esc(token)}">Signal Quality</a> ·
      <a href="/signal-quality-report?token={_esc(token)}">Report</a>
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

    @app.get("/signal-quality-config.json")
    def signal_quality_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    @app.post("/signal-quality-config/set")
    def signal_quality_config_set(request: Request, apply_to_live: Optional[int] = None, enabled: Optional[int] = None):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        c = _cfg()

        if apply_to_live is not None:
            c["apply_to_live"] = bool(int(apply_to_live))

        if enabled is not None:
            c["enabled"] = bool(int(enabled))

        c = _save_cfg(c)

        return JSONResponse({"version": "V7205", "updated": True, "config": c})

    @app.get("/signal-quality-report", response_class=HTMLResponse)
    def signal_quality_report_page(request: Request, limit: int = 500):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        r = _report(limit=limit)
        token = request.query_params.get("token", "")

        page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7205 Report</title>
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
  <h1>TradingBot V7205 — Signal Quality Report</h1>
  <div class="card">
    <p class="muted">Rows: <b>{_esc(r.get("rows_total"))}</b></p>
    <p>
      <a href="/signal-quality-report.json?token={_esc(token)}">JSON</a> ·
      <a href="/signal-quality?token={_esc(token)}">Signal Quality</a>
    </p>
  </div>
  <div class="card">
    <table>
      <tr><th>UTC</th><th>Path</th><th>Method</th><th>Market</th><th>Score</th><th>Grade</th><th>Action</th><th>Allowed</th></tr>
      {_log_rows(r.get("latest_rows", []))}
    </table>
  </div>
</body>
</html>
"""
        return HTMLResponse(page)

    @app.get("/signal-quality-report.json")
    def signal_quality_report_json(request: Request, limit: int = 500):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_report(limit=limit))

    app.state.v7205_signal_quality_installed = True
    print("[V7205] Signal Quality Dashboard installed")
