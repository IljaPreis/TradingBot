import json
import html
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse


def _esc(x):
    return html.escape(str(x))


def _root():
    for p in [Path("/app"), Path("/opt/tradingbot_v6000"), Path.cwd()]:
        if (p / "data").exists():
            return p
    return Path.cwd()


def _data(name):
    return _root() / "data" / name


def _read_json(name, default=None):
    if default is None:
        default = {}
    try:
        p = _data(name)
        if p.exists():
            x = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(x, dict):
                return x
    except Exception as exc:
        return {"load_error": str(exc), "file": name}
    return default


def _write_json(name, obj):
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _cfg():
    c = {
        "version": "V7209-V7212",
        "enabled": True,
        "observe_only": True,
        "log_candidate_requests": True,
        "excellent_score": 80,
        "good_score": 65,
        "caution_score": 45,
        "no_entry_blocks_readiness": True,
        "note": "Decision Suite is explainability/playbook/review/readiness only. It does not place, block, close, or modify trades.",
    }
    c.update(_read_json("v7209_v7212_decision_suite_config.json", {}))
    return c


def _log_path():
    return _data("v7209_v7212_decision_suite_log.jsonl")


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


def _control_status():
    try:
        from app.v7207_master_compact_control_center import _status
        return _status()
    except Exception as exc:
        return {
            "safe_state": False,
            "risky_on": True,
            "safety": {},
            "event_risk": _event_data(),
            "ranking": {},
            "load_error": str(exc),
        }


def _ranking_snapshot():
    try:
        from app.v7206_ranking_snapshot_compact_ui import _snapshot
        return _snapshot(write_file=True)
    except Exception:
        return _read_json("v7206_ranking_snapshot_last.json", {})


def _quality_eval(market, score=None, confidence=None):
    try:
        from app.v7205_signal_quality_dashboard import _evaluate_market
        return _evaluate_market(market, score=score, confidence=confidence)
    except Exception as exc:
        return {
            "market": market,
            "final_score": score if score is not None else 0,
            "grade": "UNKNOWN",
            "action": "CHECK_MANUALLY",
            "entry_allowed": False,
            "entry_scoring": {},
            "event": {},
            "shadow_stats": {},
            "load_error": str(exc),
        }


def _safe_mode():
    try:
        from app.v7208_single_header_mode import _set_safe_mode
        return _set_safe_mode()
    except Exception:
        try:
            from app.v7207_master_compact_control_center import _set_safe_mode
            return _set_safe_mode()
        except Exception as exc:
            return {
                "version": "V7209-V7212",
                "safe_mode": False,
                "error": str(exc),
                "now_utc": datetime.now(timezone.utc).isoformat(),
            }


def _decision_from_quality(q):
    grade = str(q.get("grade", "")).upper()
    action = str(q.get("action", "")).upper()
    allowed = bool(q.get("entry_allowed", False))
    final_score = float(q.get("final_score") or 0)

    if not allowed or grade == "NO_ENTRY":
        return "NO_ENTRY"
    if grade in {"AVOID", "WEAK"}:
        return "AVOID"
    if final_score >= 80 or grade == "EXCELLENT":
        return "HIGH_PRIORITY_WATCH"
    if final_score >= 65 or grade == "GOOD":
        return "TRADEABLE_IF_TECHNICALS_CONFIRM"
    if final_score >= 45 or grade == "CAUTION":
        return "WAIT_OR_SMALL_SIZE_ONLY"
    if "NO_ENTRY" in action:
        return "NO_ENTRY"
    return "LOW_PRIORITY"


def _readiness_from_explain(ex):
    q = ex.get("quality", {})
    safety = ex.get("safety", {})
    event = ex.get("event", {})
    cfg = _cfg()

    blockers = []
    warnings = []

    if safety.get("risky_on"):
        warnings.append("Risky toggle is ON. Use Safe Mode before relying on dashboard.")

    if not q.get("entry_allowed", False):
        blockers.append("Entry scoring says no-entry.")

    if str(q.get("grade")) == "NO_ENTRY":
        blockers.append("Signal quality grade is NO_ENTRY.")

    if str(q.get("grade")) in {"AVOID", "WEAK"}:
        warnings.append("Signal quality is weak/avoid.")

    if event.get("cooldown_active"):
        blockers.append("Event cooldown is active.")

    if q.get("entry_scoring", {}).get("score_penalty", 0):
        warnings.append(f"News score penalty: {q.get('entry_scoring', {}).get('score_penalty')}.")

    if bool(cfg.get("no_entry_blocks_readiness", True)) and blockers:
        ready = False
    else:
        ready = not blockers

    if ready and warnings:
        state = "READY_WITH_CAUTION"
    elif ready:
        state = "READY"
    else:
        state = "NOT_READY"

    return {
        "ready": ready,
        "state": state,
        "blockers": blockers,
        "warnings": warnings,
    }


def _explain_market(market="US100", score=None, confidence=None):
    market = str(market or "US100").upper().strip()
    q = _quality_eval(market, score=score, confidence=confidence)
    control = _control_status()
    ev = _event_data()

    entry = q.get("entry_scoring") or {}
    event = q.get("event") or {}
    shadow = q.get("shadow_stats") or {}

    reasons = []

    reasons.append(f"Signal Quality: {q.get('grade')} with final score {q.get('final_score')}.")
    reasons.append(f"Action: {q.get('action')}.")

    if q.get("entry_allowed"):
        reasons.append("Entry allowed by scoring layer.")
    else:
        reasons.append("Entry not allowed by scoring layer.")

    if event:
        title = event.get("title") or "-"
        impact = event.get("impact") or "-"
        status = event.get("status") or "-"
        mins = event.get("minutes_to_event")
        reasons.append(f"Event context: {impact} {status} {title}, minutes_to_event={mins}.")

    penalty = entry.get("score_penalty")
    if penalty not in [None, 0, "0"]:
        reasons.append(f"News-aware entry scoring applies penalty {penalty}.")
    else:
        reasons.append("No active news penalty on this market right now.")

    if shadow:
        reasons.append(
            f"Shadow stats: samples={shadow.get('samples')}, wins={shadow.get('wins')}, "
            f"losses={shadow.get('losses')}, winrate={shadow.get('winrate')}, total_r={shadow.get('total_r')}."
        )
    else:
        reasons.append("No strong market-specific shadow edge found yet.")

    safety = {
        "safe_state": control.get("safe_state"),
        "risky_on": control.get("risky_on"),
        **(control.get("safety") or {}),
    }

    if control.get("safe_state"):
        reasons.append("System safety state is clean: no risky live/enforce/auto toggles are active.")
    else:
        reasons.append("System safety state needs attention: at least one risky toggle is active.")

    decision = _decision_from_quality(q)

    ex = {
        "version": "V7209",
        "mode": "DECISION_EXPLAINABILITY",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "market": market,
        "decision": decision,
        "quality": q,
        "event": {
            "risk_level": ev.get("risk_level"),
            "cooldown_active": ev.get("cooldown_active"),
            "active_count": ev.get("active_count"),
            "upcoming_count": ev.get("upcoming_count"),
            "market_event": event,
        },
        "safety": safety,
        "reasons": reasons,
        "observe_only": True,
    }

    ex["readiness"] = _readiness_from_explain(ex)
    return ex


def _playbook():
    snap = _ranking_snapshot()
    ev = _event_data()
    best = snap.get("best_markets", []) or []
    worst = snap.get("worst_markets", []) or []
    no_entry = snap.get("no_entry_markets", []) or []
    avoid = snap.get("avoid_markets", []) or []

    rules = []

    if ev.get("cooldown_active"):
        rules.append("Active event cooldown: no new entries. Manage existing trades only.")
    elif ev.get("upcoming_count", 0):
        rules.append("Upcoming macro events exist: prefer clean technical confirmation and smaller size near event windows.")
    else:
        rules.append("No major event pressure detected by internal calendar.")

    if no_entry:
        rules.append("No-entry markets must be skipped until cooldown or scoring clears.")
    if avoid:
        rules.append("Avoid/weak markets should only be watched, not prioritized.")
    if best:
        rules.append("Best markets can be watched first, but only after chart confirmation.")

    return {
        "version": "V7210",
        "mode": "SIGNAL_PLAYBOOK",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "event_risk": {
            "risk_level": ev.get("risk_level"),
            "cooldown_active": ev.get("cooldown_active"),
            "active_count": ev.get("active_count"),
            "upcoming_count": ev.get("upcoming_count"),
        },
        "rules": rules,
        "best_markets": best[:8],
        "avoid_markets": avoid[:8],
        "no_entry_markets": no_entry[:8],
        "worst_markets": worst[:8],
        "note": "Playbook only. It does not execute or block orders.",
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


def _review(limit=500):
    rows = _read_log(limit)
    markets = Counter(str(r.get("market", "-")) for r in rows)
    decisions = Counter(str(r.get("decision", "-")) for r in rows)
    readiness = Counter(str((r.get("readiness") or {}).get("state", "-")) for r in rows)

    return {
        "version": "V7211",
        "mode": "DECISION_REVIEW_LOG",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "rows_total": len(rows),
        "top_markets": markets.most_common(10),
        "top_decisions": decisions.most_common(10),
        "top_readiness": readiness.most_common(10),
        "latest_rows": rows[:100],
        "log_file": str(_log_path()),
    }


def _suite():
    snap = _ranking_snapshot()
    playbook = _playbook()
    control = _control_status()

    best = snap.get("best_markets", []) or []
    worst = snap.get("worst_markets", []) or []
    no_entry = snap.get("no_entry_markets", []) or []

    explain_best = []
    for m in best[:5]:
        market = m.get("market")
        if market:
            explain_best.append(_explain_market(market))

    return {
        "version": "V7209-V7212",
        "mode": "DECISION_SUITE",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "safe_state": control.get("safe_state"),
        "risky_on": control.get("risky_on"),
        "safety": control.get("safety", {}),
        "event_risk": control.get("event_risk", {}),
        "ranking": {
            "market_count": snap.get("market_count"),
            "grade_counts": snap.get("grade_counts", {}),
            "best_markets": best[:8],
            "worst_markets": worst[:8],
            "no_entry_markets": no_entry[:8],
        },
        "playbook": playbook,
        "best_market_explanations": explain_best,
        "observe_only": True,
        "note": "V7209-V7212 is a decision explanation and readiness package only.",
    }


def _candidate_request(request: Request):
    path = request.url.path.lower()
    method = request.method.upper()

    if method not in {"POST", "PUT", "PATCH"}:
        return False

    ignore = [
        "heartbeat", "event-", "entry-scoring", "signal-quality",
        "trade-protection", "pre-news", "calendar", "maintenance",
        "decision-suite", "decision-explain", "decision-playbook",
        "decision-review", "single-header", "control-center",
    ]

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


def _links(token):
    return f"""
<a href="/decision-suite?token={_esc(token)}">Decision Suite</a> ·
<a href="/decision-explain?token={_esc(token)}&market=US100">Explain US100</a> ·
<a href="/decision-playbook?token={_esc(token)}">Playbook</a> ·
<a href="/decision-review?token={_esc(token)}">Review</a> ·
<a href="/single-header?token={_esc(token)}">Single Header</a> ·
<a href="/control-center?token={_esc(token)}">Control</a> ·
<a href="/ranking-snapshot?token={_esc(token)}">Ranking</a>
"""


def _html_page(title, body, request):
    token = request.query_params.get("token", "")
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{_esc(title)}</title>
  <style>
    body {{ background:#0b0f14; color:#e8eef5; font-family:Arial,sans-serif; margin:24px; }}
    a {{ color:#8cc8ff; text-decoration:none; }}
    .card {{ background:#121923; border:1px solid #263447; border-radius:12px; padding:16px; margin-bottom:18px; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(240px,1fr)); gap:12px; }}
    .mini {{ background:#0f1721; border:1px solid #263447; border-radius:10px; padding:12px; }}
    .badge {{ display:inline-block; padding:7px 11px; border-radius:999px; color:white; font-weight:bold; background:#1f6f3d; }}
    .warn {{ background:#8a6a1f; }}
    .danger {{ background:#8a1f1f; }}
    table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }}
    th,td {{ border-bottom:1px solid #263447; padding:8px; text-align:left; vertical-align:top; }}
    th {{ color:#a9bfd6; }}
    button {{ background:#123456; color:#e8eef5; border:1px solid #355273; border-radius:8px; padding:8px 11px; cursor:pointer; }}
    .muted {{ color:#9fb0c0; }}
    pre {{ white-space:pre-wrap; background:#0f1721; border:1px solid #263447; border-radius:10px; padding:12px; }}
  </style>
</head>
<body>
  <h1>{_esc(title)}</h1>
  <div class="card">{_links(token)}</div>
  {body}
</body>
</html>
"""


def _market_rows(rows):
    out = ""
    for r in rows:
        ev = r.get("event") or {}
        out += f"""
<tr>
  <td>{_esc(r.get("market"))}</td>
  <td>{_esc(r.get("final_score"))}</td>
  <td>{_esc(r.get("grade"))}</td>
  <td>{_esc(r.get("action"))}</td>
  <td>{_esc(r.get("entry_allowed"))}</td>
  <td>{_esc((r.get("entry_scoring") or {}).get("score_penalty"))}</td>
  <td>{_esc(ev.get("title", "-"))}</td>
  <td>{_esc(ev.get("impact", "-"))}</td>
  <td>{_esc(ev.get("status", "-"))}</td>
</tr>
"""
    if not out:
        out = "<tr><td colspan='9'>Keine Daten.</td></tr>"
    return out


def _explain_html(ex):
    reasons = "".join(f"<li>{_esc(x)}</li>" for x in ex.get("reasons", []))
    readiness = ex.get("readiness", {})
    blockers = "".join(f"<li>{_esc(x)}</li>" for x in readiness.get("blockers", []))
    warnings = "".join(f"<li>{_esc(x)}</li>" for x in readiness.get("warnings", []))

    return f"""
<div class="card">
  <span class="badge">{_esc(ex.get("decision"))}</span>
  <p class="muted">
    Market: <b>{_esc(ex.get("market"))}</b> |
    Readiness: <b>{_esc(readiness.get("state"))}</b> |
    Ready: <b>{_esc(readiness.get("ready"))}</b>
  </p>
  <h3>Warum?</h3>
  <ul>{reasons}</ul>
  <h3>Blocker</h3>
  <ul>{blockers or "<li>Keine</li>"}</ul>
  <h3>Warnungen</h3>
  <ul>{warnings or "<li>Keine</li>"}</ul>
</div>
"""


def _review_rows(rows):
    out = ""
    for r in rows:
        ready = r.get("readiness") or {}
        out += f"""
<tr>
  <td>{_esc(r.get("now_utc"))}</td>
  <td>{_esc(r.get("path"))}</td>
  <td>{_esc(r.get("method"))}</td>
  <td>{_esc(r.get("market"))}</td>
  <td>{_esc(r.get("decision"))}</td>
  <td>{_esc(ready.get("state"))}</td>
  <td>{_esc((r.get("quality") or {}).get("final_score"))}</td>
  <td>{_esc((r.get("quality") or {}).get("grade"))}</td>
</tr>
"""
    if not out:
        out = "<tr><td colspan='8'>Noch keine Decision Logs.</td></tr>"
    return out


def install_v7209_v7212_decision_suite(app):
    if getattr(app.state, "v7209_v7212_decision_suite_installed", False):
        return

    @app.middleware("http")
    async def v7209_v7212_middleware(request: Request, call_next):
        cfg = _cfg()

        if bool(cfg.get("enabled", True)) and bool(cfg.get("log_candidate_requests", True)) and _candidate_request(request):
            market = _market_from_request(request)
            if market:
                rec = _explain_market(
                    market,
                    score=_score_from_request(request),
                    confidence=_confidence_from_request(request),
                )
                rec["path"] = request.url.path
                rec["method"] = request.method
                _append_log(rec)

        response = await call_next(request)
        return response

    @app.get("/decision-suite", response_class=HTMLResponse)
    def decision_suite_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        s = _suite()
        ranking = s.get("ranking", {})
        badge_cls = "badge" if s.get("safe_state") else "badge danger"

        body = f"""
<div class="card">
  <span class="{badge_cls}">V7209-V7212 DECISION SUITE</span>
  <p class="muted">
    Safe State: <b>{_esc(s.get("safe_state"))}</b> |
    Risky On: <b>{_esc(s.get("risky_on"))}</b> |
    Market Count: <b>{_esc(ranking.get("market_count"))}</b> |
    Grades: <b>{_esc(ranking.get("grade_counts"))}</b>
  </p>
  <form method="post" action="/decision-suite/safe-mode?token={_esc(request.query_params.get("token", ""))}">
    <button type="submit">SAFE MODE: alle Risky Toggles OFF</button>
  </form>
</div>

<div class="card">
  <h2>Best Markets</h2>
  <table>
    <tr><th>Market</th><th>Score</th><th>Grade</th><th>Action</th><th>Allowed</th><th>Penalty</th><th>Event</th><th>Impact</th><th>Status</th></tr>
    {_market_rows(ranking.get("best_markets", []))}
  </table>
</div>

<div class="card">
  <h2>Weak / No Entry</h2>
  <table>
    <tr><th>Market</th><th>Score</th><th>Grade</th><th>Action</th><th>Allowed</th><th>Penalty</th><th>Event</th><th>Impact</th><th>Status</th></tr>
    {_market_rows((ranking.get("no_entry_markets", []) or []) + (ranking.get("worst_markets", []) or []))}
  </table>
</div>
"""
        return HTMLResponse(_html_page("TradingBot V7209-V7212 — Decision Suite", body, request))

    @app.get("/decision-suite.json")
    def decision_suite_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_suite())

    @app.get("/decision-suite-config.json")
    def decision_suite_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    @app.post("/decision-suite/safe-mode")
    def decision_suite_safe_mode(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_safe_mode())

    @app.get("/decision-explain", response_class=HTMLResponse)
    def decision_explain_page(request: Request, market: str = "US100", score: Optional[float] = None, confidence: Optional[float] = None):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        ex = _explain_market(market, score=score, confidence=confidence)
        body = _explain_html(ex)
        return HTMLResponse(_html_page(f"TradingBot V7209 — Explain {market.upper()}", body, request))

    @app.get("/decision-explain.json")
    def decision_explain_json(request: Request, market: str = "US100", score: Optional[float] = None, confidence: Optional[float] = None):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_explain_market(market, score=score, confidence=confidence))

    @app.get("/decision-playbook", response_class=HTMLResponse)
    def decision_playbook_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        pb = _playbook()
        rules = "".join(f"<li>{_esc(x)}</li>" for x in pb.get("rules", []))

        body = f"""
<div class="card">
  <span class="badge">V7210 PLAYBOOK</span>
  <p class="muted">
    Event Risk: <b>{_esc(pb.get("event_risk", {}).get("risk_level"))}</b> |
    Cooldown: <b>{_esc(pb.get("event_risk", {}).get("cooldown_active"))}</b> |
    Upcoming: <b>{_esc(pb.get("event_risk", {}).get("upcoming_count"))}</b>
  </p>
  <ul>{rules}</ul>
</div>

<div class="card">
  <h2>Best Markets</h2>
  <table>
    <tr><th>Market</th><th>Score</th><th>Grade</th><th>Action</th><th>Allowed</th><th>Penalty</th><th>Event</th><th>Impact</th><th>Status</th></tr>
    {_market_rows(pb.get("best_markets", []))}
  </table>
</div>
"""
        return HTMLResponse(_html_page("TradingBot V7210 — Signal Playbook", body, request))

    @app.get("/decision-playbook.json")
    def decision_playbook_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_playbook())

    @app.get("/decision-review", response_class=HTMLResponse)
    def decision_review_page(request: Request, limit: int = 500):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        r = _review(limit=limit)
        body = f"""
<div class="card">
  <span class="badge">V7211 REVIEW LOG</span>
  <p class="muted">
    Rows: <b>{_esc(r.get("rows_total"))}</b> |
    Top Decisions: <b>{_esc(r.get("top_decisions"))}</b> |
    Top Readiness: <b>{_esc(r.get("top_readiness"))}</b>
  </p>
</div>

<div class="card">
  <table>
    <tr><th>UTC</th><th>Path</th><th>Method</th><th>Market</th><th>Decision</th><th>Readiness</th><th>Score</th><th>Grade</th></tr>
    {_review_rows(r.get("latest_rows", []))}
  </table>
</div>
"""
        return HTMLResponse(_html_page("TradingBot V7211 — Decision Review Log", body, request))

    @app.get("/decision-review.json")
    def decision_review_json(request: Request, limit: int = 500):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_review(limit=limit))

    app.state.v7209_v7212_decision_suite_installed = True
    print("[V7209-V7212] Decision Suite Mega Pack installed")
