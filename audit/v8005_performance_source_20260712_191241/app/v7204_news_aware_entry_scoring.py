import json
import html
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
    "/pre-news-manager",
    "/entry-scoring",
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
    return _data("v7204_entry_scoring_config.json")


def _log_path():
    return _data("v7204_entry_scoring_log.jsonl")


def _default_cfg():
    return {
        "version": "V7204",
        "enabled": True,
        "apply_to_live": False,
        "observe_only_note": "apply_to_live=false means V7204 only calculates/logs score adjustments and does not change live execution.",
        "active_high_penalty": -100,
        "active_medium_penalty": -70,
        "high_15m_penalty": -50,
        "high_30m_penalty": -35,
        "high_45m_penalty": -25,
        "high_90m_penalty": -10,
        "medium_15m_penalty": -20,
        "medium_30m_penalty": -10,
        "medium_45m_penalty": -5,
        "active_high_confidence_cap": 0,
        "active_medium_confidence_cap": 20,
        "high_15m_confidence_cap": 40,
        "high_30m_confidence_cap": 55,
        "high_45m_confidence_cap": 65,
        "high_90m_confidence_cap": 80,
        "medium_15m_confidence_cap": 70,
        "medium_30m_confidence_cap": 82,
        "medium_45m_confidence_cap": 90,
        "note": "V7204 is scoring-only by default. It does not block or modify real orders unless apply_to_live is explicitly integrated later.",
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
            "load_error": str(exc),
        }


def _gate_risk_for_market(market):
    try:
        from app.v7201_event_gate_pack import _decision_risk_for_market
        return _decision_risk_for_market(market)
    except Exception:
        return {
            "market": market,
            "risk_level": _event_data().get("risk_level", "LOW"),
            "cooldown_active": _event_data().get("cooldown_active", False),
            "would_block": False,
            "reason": "V7201_GATE_HELPER_NOT_AVAILABLE",
            "market_risk": None,
            "affected_markets": [],
        }


def _num(x):
    try:
        if x is None or x == "":
            return None
        return float(str(x).replace(",", "."))
    except Exception:
        return None


def _rule_for_market_risk(market_risk, risk, cfg):
    mr = market_risk or {}
    status = str(mr.get("status") or "").upper()
    impact = str(mr.get("impact") or "").upper()

    try:
        mins = float(mr.get("minutes_to_event"))
    except Exception:
        mins = None

    penalty = 0
    cap = 100
    no_entry = False
    no_addon = False
    quality = "NORMAL"
    reason = "NO_EVENT_SCORE_ADJUSTMENT"
    recommendation = "Normal bewerten."

    if status == "ACTIVE_COOLDOWN" or bool(risk.get("would_block")):
        no_entry = True
        no_addon = True

        if impact == "HIGH":
            penalty = int(cfg.get("active_high_penalty", -100))
            cap = int(cfg.get("active_high_confidence_cap", 0))
            quality = "NO_ENTRY_ACTIVE_HIGH"
            reason = "ACTIVE_HIGH_IMPACT_COOLDOWN"
            recommendation = "Kein neuer Entry. Kein Add-on. Nur Management/Shadow."
        else:
            penalty = int(cfg.get("active_medium_penalty", -70))
            cap = int(cfg.get("active_medium_confidence_cap", 20))
            quality = "NO_ENTRY_ACTIVE_MEDIUM"
            reason = "ACTIVE_MEDIUM_IMPACT_COOLDOWN"
            recommendation = "Kein neuer Entry während aktivem Medium-Event-Cooldown."

    elif status == "UPCOMING":
        if impact == "HIGH":
            if mins is not None and mins <= 15:
                penalty = int(cfg.get("high_15m_penalty", -50))
                cap = int(cfg.get("high_15m_confidence_cap", 40))
                no_addon = True
                quality = "HIGH_NEWS_15M_RISK"
                reason = "HIGH_IMPACT_WITHIN_15M"
                recommendation = "Entry stark abwerten. Besser bis nach News warten."
            elif mins is not None and mins <= 30:
                penalty = int(cfg.get("high_30m_penalty", -35))
                cap = int(cfg.get("high_30m_confidence_cap", 55))
                no_addon = True
                quality = "HIGH_NEWS_30M_RISK"
                reason = "HIGH_IMPACT_WITHIN_30M"
                recommendation = "Entry deutlich abwerten. Add-ons vermeiden."
            elif mins is not None and mins <= 45:
                penalty = int(cfg.get("high_45m_penalty", -25))
                cap = int(cfg.get("high_45m_confidence_cap", 65))
                quality = "HIGH_NEWS_45M_CAUTION"
                reason = "HIGH_IMPACT_WITHIN_45M"
                recommendation = "Entry nur mit starkem Setup. Risiko reduzieren."
            elif mins is not None and mins <= 90:
                penalty = int(cfg.get("high_90m_penalty", -10))
                cap = int(cfg.get("high_90m_confidence_cap", 80))
                quality = "HIGH_NEWS_90M_AWARE"
                reason = "HIGH_IMPACT_WITHIN_90M"
                recommendation = "News-Risiko im Rating berücksichtigen."
        else:
            if mins is not None and mins <= 15:
                penalty = int(cfg.get("medium_15m_penalty", -20))
                cap = int(cfg.get("medium_15m_confidence_cap", 70))
                quality = "MEDIUM_NEWS_15M_CAUTION"
                reason = "MEDIUM_IMPACT_WITHIN_15M"
                recommendation = "Leichte bis mittlere Abwertung."
            elif mins is not None and mins <= 30:
                penalty = int(cfg.get("medium_30m_penalty", -10))
                cap = int(cfg.get("medium_30m_confidence_cap", 82))
                quality = "MEDIUM_NEWS_30M_AWARE"
                reason = "MEDIUM_IMPACT_WITHIN_30M"
                recommendation = "Leichte Abwertung."
            elif mins is not None and mins <= 45:
                penalty = int(cfg.get("medium_45m_penalty", -5))
                cap = int(cfg.get("medium_45m_confidence_cap", 90))
                quality = "MEDIUM_NEWS_45M_AWARE"
                reason = "MEDIUM_IMPACT_WITHIN_45M"
                recommendation = "Kleine News-Abwertung."

    return {
        "penalty": penalty,
        "confidence_cap": cap,
        "no_entry": no_entry,
        "no_addon": no_addon,
        "quality": quality,
        "reason": reason,
        "recommendation": recommendation,
    }


def _score_market(market, base_score=None, confidence=None):
    cfg = _cfg()
    market = str(market or "").upper().strip()

    risk = _gate_risk_for_market(market)
    mr = risk.get("market_risk") or {}

    rule = _rule_for_market_risk(mr, risk, cfg)

    base = _num(base_score)
    conf = _num(confidence)

    adjusted_score = None
    adjusted_confidence = None

    if base is not None:
        adjusted_score = max(0, min(100, round(base + rule.get("penalty", 0), 2)))

    if conf is not None:
        adjusted_confidence = max(0, min(conf, float(rule.get("confidence_cap", 100))))
        adjusted_confidence = round(adjusted_confidence, 2)

    allowed_by_score = not bool(rule.get("no_entry"))
    live_apply = bool(cfg.get("apply_to_live", False))

    return {
        "version": "V7204",
        "market": market,
        "base_score": base,
        "adjusted_score": adjusted_score,
        "score_penalty": rule.get("penalty"),
        "base_confidence": conf,
        "confidence_cap": rule.get("confidence_cap"),
        "adjusted_confidence": adjusted_confidence,
        "entry_allowed_by_scoring": allowed_by_score,
        "no_addon": bool(rule.get("no_addon")),
        "quality": rule.get("quality"),
        "reason": rule.get("reason"),
        "recommendation": rule.get("recommendation"),
        "apply_to_live": live_apply,
        "observe_only": not live_apply,
        "risk": risk,
        "event": mr,
        "now_utc": datetime.now(timezone.utc).isoformat(),
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
    reasons = Counter(str(r.get("reason", "-")) for r in rows)
    qualities = Counter(str(r.get("quality", "-")) for r in rows)
    markets = Counter(str(r.get("market", "-")) for r in rows)

    return {
        "version": "V7204",
        "mode": "NEWS_AWARE_ENTRY_SCORING_REPORT",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "rows_total": len(rows),
        "top_reasons": reasons.most_common(10),
        "top_qualities": qualities.most_common(10),
        "top_markets": markets.most_common(10),
        "latest_rows": rows[:100],
        "log_file": str(_log_path()),
    }


def _status():
    cfg = _cfg()
    samples = [_score_market(m, base_score=75, confidence=70) for m in DEFAULT_MARKETS]

    active_adjustments = sum(1 for s in samples if s.get("score_penalty") != 0)
    no_entry_count = sum(1 for s in samples if not s.get("entry_allowed_by_scoring"))

    return {
        "version": "V7204",
        "mode": "NEWS_AWARE_ENTRY_SCORING",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "config": cfg,
        "event_risk": _event_data(),
        "sample_base_score": 75,
        "sample_confidence": 70,
        "market_count": len(samples),
        "active_adjustments": active_adjustments,
        "no_entry_count": no_entry_count,
        "markets": samples,
        "note": "Observe/scoring only by default. Does not alter live trades unless explicitly integrated later.",
    }


def _candidate_request(request: Request):
    path = request.url.path.lower()
    method = request.method.upper()

    if method not in {"POST", "PUT", "PATCH"}:
        return False

    if any(x in path for x in ["heartbeat", "event-", "entry-scoring", "trade-protection", "pre-news", "calendar", "maintenance"]):
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
    cfg = s.get("config", {})
    token = request.query_params.get("token", "")

    adjustments = int(s.get("active_adjustments", 0))
    no_entry = int(s.get("no_entry_count", 0))

    bg = "#1f6f3d"
    label = "ENTRY SCORING OK"

    if no_entry > 0:
        bg = "#8a1f1f"
        label = "NO-ENTRY NEWS RISK"
    elif adjustments > 0:
        bg = "#8a6a1f"
        label = "NEWS SCORE ADJUST"

    return f"""
<div id="v7204_entry_scoring_card" style="
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
    V7204 {_esc(label)}
  </div>
  <div style="font-size:13px;color:#c8d6e4;line-height:1.45;">
    Active score adjustments: <b>{_esc(adjustments)}</b> |
    No-entry count: <b>{_esc(no_entry)}</b> |
    Apply to live: <b>{_esc(cfg.get("apply_to_live"))}</b>
    <br>
    Event risk: <b>{_esc(s.get("event_risk", {}).get("risk_level"))}</b> |
    Cooldown: <b>{_esc(s.get("event_risk", {}).get("cooldown_active"))}</b> |
    Upcoming: <b>{_esc(s.get("event_risk", {}).get("upcoming_count"))}</b>
    <br>
    <a style="color:#8cc8ff;" href="/entry-scoring?token={_esc(token)}">Entry Scoring</a> |
    <a style="color:#8cc8ff;" href="/entry-scoring-report?token={_esc(token)}">Report</a> |
    <a style="color:#8cc8ff;" href="/entry-scoring-config?token={_esc(token)}">Config</a>
  </div>
</div>
"""


def _inject(page, request):
    if "v7204_entry_scoring_card" in page:
        return page

    card = _card(request)

    for marker in ["v7203_pre_news_manager_card", "v7202_trade_protection_card", "v7200_9_calendar_status_card"]:
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
        out += f"""
<tr>
  <td>{_esc(r.get("market"))}</td>
  <td>{_esc(r.get("base_score"))}</td>
  <td>{_esc(r.get("score_penalty"))}</td>
  <td>{_esc(r.get("adjusted_score"))}</td>
  <td>{_esc(r.get("base_confidence"))}</td>
  <td>{_esc(r.get("confidence_cap"))}</td>
  <td>{_esc(r.get("adjusted_confidence"))}</td>
  <td>{_esc(r.get("entry_allowed_by_scoring"))}</td>
  <td>{_esc(r.get("quality"))}</td>
  <td>{_esc(r.get("reason"))}</td>
  <td>{_esc(ev.get("title", "-"))}<br>{_esc(ev.get("impact", "-"))} {_esc(ev.get("status", "-"))}<br>{_esc(ev.get("minutes_to_event", "-"))} min</td>
  <td>{_esc(r.get("recommendation"))}</td>
</tr>
"""

    if not out:
        out = "<tr><td colspan='12'>Keine Märkte.</td></tr>"

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
  <td>{_esc(r.get("score_penalty"))}</td>
  <td>{_esc(r.get("adjusted_score"))}</td>
  <td>{_esc(r.get("quality"))}</td>
  <td>{_esc(r.get("reason"))}</td>
</tr>
"""

    if not out:
        out = "<tr><td colspan='8'>Noch keine Logs.</td></tr>"

    return out


def install_v7204_news_aware_entry_scoring(app):
    if getattr(app.state, "v7204_entry_scoring_installed", False):
        return

    @app.middleware("http")
    async def v7204_middleware(request: Request, call_next):
        if _candidate_request(request):
            market = _market_from_request(request)
            if market:
                rec = _score_market(
                    market,
                    base_score=_score_from_request(request),
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

    @app.get("/entry-scoring", response_class=HTMLResponse)
    def entry_scoring_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        s = _status()
        token = request.query_params.get("token", "")

        page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7204 Entry Scoring</title>
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
  <h1>TradingBot V7204 — News-Aware Entry Scoring</h1>

  <div class="card">
    <div class="badge">V7204 OBSERVE SCORING</div>
    <p class="muted">
      Active adjustments: <b>{_esc(s.get("active_adjustments"))}</b> |
      No-entry count: <b>{_esc(s.get("no_entry_count"))}</b> |
      Apply to live: <b>{_esc(s.get("config", {}).get("apply_to_live"))}</b>
    </p>
    <p>
      <a href="/entry-scoring.json?token={_esc(token)}">JSON</a> ·
      <a href="/entry-scoring-config?token={_esc(token)}">Config</a> ·
      <a href="/entry-scoring-report?token={_esc(token)}">Report</a> ·
      <a href="/entry-scoring-evaluate.json?token={_esc(token)}&market=US100&score=75&confidence=70">Evaluate US100</a>
    </p>
    <p class="muted">{_esc(s.get("note"))}</p>
  </div>

  <div class="card">
    <h2>Market Scoring Preview</h2>
    <table>
      <tr>
        <th>Market</th><th>Base</th><th>Penalty</th><th>Adjusted</th>
        <th>Conf</th><th>Cap</th><th>Adj Conf</th><th>Allowed</th>
        <th>Quality</th><th>Reason</th><th>Event</th><th>Recommendation</th>
      </tr>
      {_market_rows(s.get("markets", []))}
    </table>
  </div>
</body>
</html>
"""
        return HTMLResponse(page)

    @app.get("/entry-scoring.json")
    def entry_scoring_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_status())

    @app.get("/entry-scoring-evaluate.json")
    def entry_scoring_evaluate_json(
        request: Request,
        market: str,
        score: Optional[float] = None,
        confidence: Optional[float] = None,
    ):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_score_market(market, base_score=score, confidence=confidence))

    @app.get("/entry-scoring-config", response_class=HTMLResponse)
    def entry_scoring_config_page(request: Request):
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
  <title>TradingBot V7204 Config</title>
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
  <h1>TradingBot V7204 — Config</h1>
  <div class="card">
    <p>Apply to live bleibt aus. V7204 ist erstmal Rating/Logging.</p>
    <form method="post" action="/entry-scoring-config/set?token={_esc(token)}&apply_to_live=0" style="display:inline;">
      <button type="submit">Apply Live OFF</button>
    </form>
    <form method="post" action="/entry-scoring-config/set?token={_esc(token)}&apply_to_live=1" style="display:inline;">
      <button type="submit">Apply Live ON</button>
    </form>
    <p>
      <a href="/entry-scoring?token={_esc(token)}">Entry Scoring</a> ·
      <a href="/entry-scoring-report?token={_esc(token)}">Report</a>
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

    @app.get("/entry-scoring-config.json")
    def entry_scoring_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    @app.post("/entry-scoring-config/set")
    def entry_scoring_config_set(request: Request, apply_to_live: Optional[int] = None, enabled: Optional[int] = None):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        c = _cfg()

        if apply_to_live is not None:
            c["apply_to_live"] = bool(int(apply_to_live))

        if enabled is not None:
            c["enabled"] = bool(int(enabled))

        c = _save_cfg(c)

        return JSONResponse({"version": "V7204", "updated": True, "config": c})

    @app.get("/entry-scoring-report", response_class=HTMLResponse)
    def entry_scoring_report_page(request: Request, limit: int = 500):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        r = _report(limit=limit)
        token = request.query_params.get("token", "")

        page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7204 Report</title>
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
  <h1>TradingBot V7204 — Entry Scoring Report</h1>
  <div class="card">
    <p class="muted">Rows: <b>{_esc(r.get("rows_total"))}</b></p>
    <p>
      <a href="/entry-scoring-report.json?token={_esc(token)}">JSON</a> ·
      <a href="/entry-scoring?token={_esc(token)}">Entry Scoring</a>
    </p>
  </div>
  <div class="card">
    <table>
      <tr><th>UTC</th><th>Path</th><th>Method</th><th>Market</th><th>Penalty</th><th>Adjusted</th><th>Quality</th><th>Reason</th></tr>
      {_log_rows(r.get("latest_rows", []))}
    </table>
  </div>
</body>
</html>
"""
        return HTMLResponse(page)

    @app.get("/entry-scoring-report.json")
    def entry_scoring_report_json(request: Request, limit: int = 500):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_report(limit=limit))

    app.state.v7204_entry_scoring_installed = True
    print("[V7204] News-Aware Entry Scoring installed")
