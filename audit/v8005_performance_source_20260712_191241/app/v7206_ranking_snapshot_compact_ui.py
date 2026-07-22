import json
import html
from pathlib import Path
from datetime import datetime, timezone
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
    "/ranking-snapshot",
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
    return _data("v7206_ranking_snapshot_config.json")


def _snapshot_path():
    return _data("v7206_ranking_snapshot.json")


def _default_cfg():
    return {
        "version": "V7206",
        "enabled": True,
        "compact_ui_enabled": True,
        "hide_old_large_cards": True,
        "snapshot_limit_best": 8,
        "snapshot_limit_worst": 8,
        "note": "V7206 creates auto ranking snapshots and replaces large nested cards with one compact static summary bar.",
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


def _quality_status():
    try:
        from app.v7205_signal_quality_dashboard import _status
        return _status()
    except Exception as exc:
        return {
            "version": "V7205_UNAVAILABLE",
            "market_count": 0,
            "markets": [],
            "best_markets": [],
            "worst_markets": [],
            "grade_counts": {},
            "load_error": str(exc),
        }


def _snapshot(write_file=True):
    cfg = _cfg()
    q = _quality_status()
    ev = _event_data()

    markets = q.get("markets", []) or []
    best_limit = int(cfg.get("snapshot_limit_best", 8))
    worst_limit = int(cfg.get("snapshot_limit_worst", 8))

    best = list(markets[:best_limit])
    worst = list(reversed(markets[-worst_limit:])) if markets else []

    no_entry = [m for m in markets if str(m.get("grade")) == "NO_ENTRY"]
    avoid = [m for m in markets if str(m.get("grade")) in {"AVOID", "WEAK"}]
    good = [m for m in markets if str(m.get("grade")) in {"EXCELLENT", "GOOD"}]

    snap = {
        "version": "V7206",
        "mode": "AUTO_RANKING_SNAPSHOT",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "config": cfg,
        "event_risk": {
            "risk_level": ev.get("risk_level"),
            "cooldown_active": ev.get("cooldown_active"),
            "active_count": ev.get("active_count"),
            "upcoming_count": ev.get("upcoming_count"),
        },
        "market_count": len(markets),
        "grade_counts": q.get("grade_counts", {}),
        "best_markets": best,
        "good_markets": good[:best_limit],
        "worst_markets": worst,
        "no_entry_markets": no_entry,
        "avoid_markets": avoid[:worst_limit],
        "source": "V7205 signal quality + V7204 news scoring + V7201 event gate",
        "note": "Ranking snapshot only. No live execution changes.",
    }

    if write_file:
        try:
            _snapshot_path().parent.mkdir(parents=True, exist_ok=True)
            _snapshot_path().write_text(json.dumps(snap, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception as exc:
            snap["write_error"] = str(exc)

    return snap


def _compact_css():
    cfg = _cfg()

    hide_css = ""
    if bool(cfg.get("hide_old_large_cards", True)):
        hide_css = """
#v7200_9_calendar_status_card,
#v7200_5_observer_summary_card,
#v7200_observer_summary_card,
#v7202_trade_protection_card,
#v7203_pre_news_manager_card,
#v7204_entry_scoring_card,
#v7205_signal_quality_card {
  display: none !important;
}
"""

    return f"""
<style id="v7206_compact_dashboard_css">
{hide_css}

#v7206_ranking_snapshot_card {{
  position: static !important;
  top: auto !important;
  left: auto !important;
  right: auto !important;
  bottom: auto !important;
  z-index: auto !important;
  width: auto !important;
  max-width: 820px !important;
  min-height: 0 !important;
  margin: 8px 0 12px 18px !important;
  padding: 8px 10px !important;
  border-radius: 9px !important;
  background: #101923 !important;
  border: 1px solid #263447 !important;
  color: #e8eef5 !important;
  font-family: Arial, sans-serif !important;
  font-size: 12px !important;
  line-height: 1.35 !important;
  box-shadow: none !important;
}}

#v7206_ranking_snapshot_card * {{
  font-size: 12px !important;
  line-height: 1.35 !important;
}}

#v7206_ranking_snapshot_card .v7206_badge {{
  display: inline-block !important;
  padding: 4px 8px !important;
  border-radius: 999px !important;
  background: #1f6f3d !important;
  color: #fff !important;
  font-weight: 700 !important;
  margin-right: 8px !important;
}}

#v7206_ranking_snapshot_card a {{
  color: #8cc8ff !important;
  text-decoration: underline !important;
}}

#v7206_ranking_snapshot_card .v7206_line {{
  margin-top: 4px !important;
  color: #c8d6e4 !important;
}}
</style>
"""


def _fmt_market(m):
    if not m:
        return "-"
    return f"{m.get('market')}:{m.get('grade')}:{m.get('final_score')}"


def _card(request: Request):
    snap = _snapshot(write_file=False)
    token = request.query_params.get("token", "")

    best = ", ".join(_fmt_market(x) for x in snap.get("best_markets", [])[:4]) or "-"
    worst = ", ".join(_fmt_market(x) for x in snap.get("worst_markets", [])[:4]) or "-"
    no_entry_count = len(snap.get("no_entry_markets", []) or [])
    avoid_count = len(snap.get("avoid_markets", []) or [])

    ev = snap.get("event_risk", {})
    grade_counts = snap.get("grade_counts", {})

    badge_bg = "#1f6f3d"
    badge_text = "RANKING OK"

    if no_entry_count > 0:
        badge_bg = "#8a1f1f"
        badge_text = "NO-ENTRY ACTIVE"
    elif avoid_count > 0:
        badge_bg = "#8a6a1f"
        badge_text = "RANKING CAUTION"

    return f"""
<div id="v7206_ranking_snapshot_card">
  <span class="v7206_badge" style="background:{badge_bg}!important;">V7206 {_esc(badge_text)}</span>
  <b>Best:</b> {_esc(best)}
  <div class="v7206_line">
    <b>Weak/Avoid:</b> {_esc(worst)} |
    <b>No Entry:</b> {_esc(no_entry_count)} |
    <b>Avoid:</b> {_esc(avoid_count)} |
    <b>Grades:</b> {_esc(grade_counts)}
  </div>
  <div class="v7206_line">
    Event risk: <b>{_esc(ev.get("risk_level"))}</b> |
    Cooldown: <b>{_esc(ev.get("cooldown_active"))}</b> |
    Active: <b>{_esc(ev.get("active_count"))}</b> |
    Upcoming: <b>{_esc(ev.get("upcoming_count"))}</b>
    &nbsp; | &nbsp;
    <a href="/ranking-snapshot?token={_esc(token)}">Ranking</a> |
    <a href="/ranking-snapshot.json?token={_esc(token)}">JSON</a> |
    <a href="/signal-quality?token={_esc(token)}">Signal Quality</a> |
    <a href="/entry-scoring?token={_esc(token)}">Entry Scoring</a>
  </div>
</div>
"""


def _inject(page, request):
    cfg = _cfg()
    if not bool(cfg.get("compact_ui_enabled", True)):
        return page

    css = _compact_css()
    if "v7206_compact_dashboard_css" not in page:
        lower = page.lower()
        head_pos = lower.find("</head>")
        if head_pos != -1:
            page = page[:head_pos] + css + page[head_pos:]
        else:
            page = css + page

    if 'id="v7206_ranking_snapshot_card"' in page or "id='v7206_ranking_snapshot_card'" in page:
        return page

    card = _card(request)
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
  <td>{_esc(r.get("final_score"))}</td>
  <td>{_esc(r.get("grade"))}</td>
  <td>{_esc(r.get("action"))}</td>
  <td>{_esc(r.get("entry_allowed"))}</td>
  <td>{_esc(r.get("entry_scoring", {}).get("score_penalty"))}</td>
  <td>{_esc(ev.get("title", "-"))}</td>
  <td>{_esc(ev.get("impact", "-"))}</td>
  <td>{_esc(ev.get("status", "-"))}</td>
</tr>
"""
    if not out:
        out = "<tr><td colspan='9'>Keine Daten.</td></tr>"
    return out


def install_v7206_ranking_snapshot_compact_ui(app):
    if getattr(app.state, "v7206_ranking_snapshot_compact_ui_installed", False):
        return

    @app.middleware("http")
    async def v7206_compact_ui_middleware(request: Request, call_next):
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

    @app.get("/ranking-snapshot", response_class=HTMLResponse)
    def ranking_snapshot_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        snap = _snapshot(write_file=True)
        token = request.query_params.get("token", "")

        page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7206 Ranking Snapshot</title>
  {_compact_css()}
  <style>
    body {{ background:#0b0f14; color:#e8eef5; font-family:Arial,sans-serif; margin:24px; }}
    a {{ color:#8cc8ff; }}
    .card {{ background:#121923; border:1px solid #263447; border-radius:12px; padding:16px; margin-bottom:18px; }}
    table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }}
    th,td {{ border-bottom:1px solid #263447; padding:8px; text-align:left; vertical-align:top; }}
    th {{ color:#a9bfd6; }}
    .muted {{ color:#9fb0c0; }}
  </style>
</head>
<body>
  {_card(request)}

  <h1>TradingBot V7206 — Auto Ranking Snapshot</h1>

  <div class="card">
    <p class="muted">
      Generated: <b>{_esc(snap.get("generated_utc"))}</b> |
      Markets: <b>{_esc(snap.get("market_count"))}</b> |
      Event Risk: <b>{_esc(snap.get("event_risk", {}).get("risk_level"))}</b>
    </p>
    <p>
      <a href="/ranking-snapshot.json?token={_esc(token)}">JSON</a> ·
      <a href="/signal-quality?token={_esc(token)}">Signal Quality</a> ·
      <a href="/entry-scoring?token={_esc(token)}">Entry Scoring</a> ·
      <a href="/master?token={_esc(token)}">Master</a>
    </p>
  </div>

  <div class="card">
    <h2>Best Markets</h2>
    <table>
      <tr><th>Market</th><th>Score</th><th>Grade</th><th>Action</th><th>Allowed</th><th>Penalty</th><th>Event</th><th>Impact</th><th>Status</th></tr>
      {_market_rows(snap.get("best_markets", []))}
    </table>
  </div>

  <div class="card">
    <h2>Weak / Avoid Markets</h2>
    <table>
      <tr><th>Market</th><th>Score</th><th>Grade</th><th>Action</th><th>Allowed</th><th>Penalty</th><th>Event</th><th>Impact</th><th>Status</th></tr>
      {_market_rows(snap.get("worst_markets", []))}
    </table>
  </div>
</body>
</html>
"""
        return HTMLResponse(page)

    @app.get("/ranking-snapshot.json")
    def ranking_snapshot_json(request: Request, write: Optional[int] = 1):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_snapshot(write_file=bool(int(write))))

    @app.get("/ranking-snapshot-config.json")
    def ranking_snapshot_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    @app.post("/ranking-snapshot-config/set")
    def ranking_snapshot_config_set(
        request: Request,
        compact_ui_enabled: Optional[int] = None,
        hide_old_large_cards: Optional[int] = None,
    ):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        c = _cfg()
        if compact_ui_enabled is not None:
            c["compact_ui_enabled"] = bool(int(compact_ui_enabled))
        if hide_old_large_cards is not None:
            c["hide_old_large_cards"] = bool(int(hide_old_large_cards))

        return JSONResponse({"version": "V7206", "updated": True, "config": _save_cfg(c)})

    app.state.v7206_ranking_snapshot_compact_ui_installed = True
    print("[V7206] Ranking Snapshot + Compact UI installed")
