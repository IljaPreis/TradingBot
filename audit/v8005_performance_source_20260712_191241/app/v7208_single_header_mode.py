import json
import html
from pathlib import Path
from datetime import datetime, timezone
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse


TARGET_PATHS = {
    "/master",
    "/markets",
    "/event-risk",
    "/trade-protection",
    "/pre-news-manager",
    "/entry-scoring",
    "/signal-quality",
    "/ranking-snapshot",
    "/control-center",
    "/decision-suite",
    "/decision-explain",
    "/decision-playbook",
    "/decision-review",
    "/market-regime",
    "/session-bias",
    "/mtf-confirmation",
    "/rotation-board",
    "/candidate-inbox",
    "/daily-intelligence",
    "/performance-learning",
    "/setup-performance",
    "/market-session-performance",
    "/news-performance",
    "/shadow-edge",
    "/best-times",
    "/weak-setups",
    "/daily-performance-report",
    "/risk-management",
    "/position-overview",
    "/open-trade-risk",
    "/exit-readiness",
    "/sl-tp-review",
    "/cluster-exposure",
    "/open-trade-news-risk",
    "/trade-management-recommendations",
    "/trade-management-log",
    "/daily-risk-report",
    "/setup-optimizer",
    "/live-gate-review",
    "/weak-combo-report",
    "/entry-quality-report",
    "/event-leak-report",
    "/entry-bias-optimizer",
    "/bias-quality-report",
    "/entry-timing-review",
    "/soft-gate-backtest",
    "/soft-live-gate",
    "/soft-live-gate-log",
    "/master-integration",
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
        return {"load_error": str(exc)}
    return default


def _write_json(name, obj):
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _cfg():
    c = {
        "version": "V7208",
        "enabled": True,
        "hide_v7206_card": True,
        "hide_v7207_card": True,
        "hide_old_large_cards": True,
        "note": "V7208 replaces nested V7206/V7207 boxes with one compact static header.",
    }
    c.update(_read_json("v7208_single_header_config.json", {}))
    return c


def _save_cfg(c):
    c["updated_utc"] = datetime.now(timezone.utc).isoformat()
    _write_json("v7208_single_header_config.json", c)
    return c


def _token_ok(request: Request):
    try:
        from app.v7200_event_risk import _token_ok as real_token_ok
        return real_token_ok(request)
    except Exception:
        return bool(request.query_params.get("token", ""))


def _status():
    try:
        from app.v7207_master_compact_control_center import _status as v7207_status
        s = v7207_status()
    except Exception as exc:
        s = {
            "safe_state": False,
            "risky_on": True,
            "safety": {},
            "event_risk": {},
            "ranking": {},
            "load_error": str(exc),
        }

    s["version"] = "V7208"
    s["mode"] = "SINGLE_HEADER_MODE"
    s["v7208_config"] = _cfg()
    s["now_utc"] = datetime.now(timezone.utc).isoformat()
    return s


def _set_safe_mode():
    try:
        from app.v7207_master_compact_control_center import _set_safe_mode
        return _set_safe_mode()
    except Exception as exc:
        return {
            "version": "V7208",
            "safe_mode": False,
            "error": str(exc),
            "now_utc": datetime.now(timezone.utc).isoformat(),
        }


def _fmt_market(m):
    if not m:
        return "-"
    return f"{m.get('market')}:{m.get('grade')}:{m.get('final_score')}"


def _css():
    cfg = _cfg()

    hide = ""

    if bool(cfg.get("hide_v7206_card", True)):
        hide += """
#v7206_ranking_snapshot_card {
  display: none !important;
}
"""

    if bool(cfg.get("hide_v7207_card", True)):
        hide += """
#v7207_control_center_card {
  display: none !important;
}
"""

    if bool(cfg.get("hide_old_large_cards", True)):
        hide += """
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
<style id="v7208_single_header_css">
{hide}

#v7208_single_header_bar {{
  position: static !important;
  top: auto !important;
  left: auto !important;
  right: auto !important;
  bottom: auto !important;
  z-index: auto !important;
  max-width: 1120px !important;
  margin: 8px 0 12px 14px !important;
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

#v7208_single_header_bar * {{
  font-size: 12px !important;
  line-height: 1.35 !important;
}}

#v7208_single_header_bar a {{
  color: #8cc8ff !important;
  text-decoration: underline !important;
}}

#v7208_single_header_bar .v7208_badge {{
  display: inline-block !important;
  padding: 4px 8px !important;
  border-radius: 999px !important;
  color: white !important;
  font-weight: 700 !important;
  margin-right: 8px !important;
}}

#v7208_single_header_bar .v7208_line {{
  margin-top: 4px !important;
  color: #c8d6e4 !important;
}}
</style>
"""


def _bar(request: Request):
    s = _status()
    token = request.query_params.get("token", "")

    safety = s.get("safety", {}) or {}
    ev = s.get("event_risk", {}) or {}
    ranking = s.get("ranking", {}) or {}

    best = ", ".join(_fmt_market(x) for x in (ranking.get("best_markets", []) or [])[:4]) or "-"
    weak = ", ".join(_fmt_market(x) for x in (ranking.get("worst_markets", []) or [])[:3]) or "-"

    no_entry_count = len(ranking.get("no_entry_markets", []) or [])
    avoid_count = len(ranking.get("avoid_markets", []) or [])

    bg = "#1f6f3d"
    label = "ALL SAFE"

    if s.get("risky_on"):
        bg = "#8a1f1f"
        label = "RISKY TOGGLE ON"
    elif no_entry_count > 0:
        bg = "#8a6a1f"
        label = "NEWS NO-ENTRY"

    return f"""
<div id="v7208_single_header_bar">
  <span class="v7208_badge" style="background:{bg}!important;">V7208 {_esc(label)}</span>
  <b>Best:</b> {_esc(best)}
  <div class="v7208_line">
    <b>Weak:</b> {_esc(weak)} |
    <b>No Entry:</b> {_esc(no_entry_count)} |
    <b>Avoid:</b> {_esc(avoid_count)} |
    Event: <b>{_esc(ev.get("risk_level"))}</b> |
    Cooldown: <b>{_esc(ev.get("cooldown_active"))}</b> |
    Upcoming: <b>{_esc(ev.get("upcoming_count"))}</b>
  </div>
  <div class="v7208_line">
    HardBlock: <b>{_esc(safety.get("v7201_hard_block_enabled"))}</b> |
    Enforce: <b>{_esc(safety.get("v7202_enforce_blocks"))}</b> |
    AutoActions: <b>{_esc(safety.get("v7203_auto_actions_enabled"))}</b> |
    V7204Live: <b>{_esc(safety.get("v7204_apply_to_live"))}</b> |
    V7205Live: <b>{_esc(safety.get("v7205_apply_to_live"))}</b>
    &nbsp; | &nbsp;
    <a href="/control-center?token={_esc(token)}">Control</a> |
    <a href="/ranking-snapshot?token={_esc(token)}">Ranking</a> |
    <a href="/signal-quality?token={_esc(token)}">Quality</a> |
    <a href="/event-risk?token={_esc(token)}">Event</a> |
    <a href="/decision-suite?token={_esc(token)}">Decision</a> |\n    <a href="/daily-intelligence?token={_esc(token)}">Intel</a> |\n    <a href="/performance-learning?token={_esc(token)}">Performance</a> |\n    <a href="/risk-management?token={_esc(token)}">Risk</a> |\n    <a href="/master-integration?token={_esc(token)}">Master+</a> |\n    <a href="/setup-optimizer?token={_esc(token)}">Optimizer</a> |\n    <a href="/entry-bias-optimizer?token={_esc(token)}">Bias</a> |\n    <a href="/soft-live-gate?token={_esc(token)}">SoftGate</a> |\n    <a href="/pine-runtime?token={_esc(token)}">PineRun</a> |\n    <a href="/price-heartbeat-monitor?token={_esc(token)}">PriceHB</a> |\n    <a href="/pine-master?token={_esc(token)}">PineMaster</a> |\n    <a href="/single-header?token={_esc(token)}">V7208</a>
  </div>
</div>
"""


def _inject(page, request):
    cfg = _cfg()
    if not bool(cfg.get("enabled", True)):
        return page

    css = _css()

    if "v7208_single_header_css" not in page:
        lower = page.lower()
        head_pos = lower.find("</head>")
        if head_pos != -1:
            page = page[:head_pos] + css + page[head_pos:]
        else:
            page = css + page

    if 'id="v7208_single_header_bar"' in page or "id='v7208_single_header_bar'" in page:
        return page

    bar = _bar(request)

    lower = page.lower()
    body_pos = lower.find("<body")
    if body_pos != -1:
        end = page.find(">", body_pos)
        if end != -1:
            return page[:end + 1] + bar + page[end + 1:]

    return bar + page


def install_v7208_single_header_mode(app):
    if getattr(app.state, "v7208_single_header_installed", False):
        return

    @app.middleware("http")
    async def v7208_single_header_middleware(request: Request, call_next):
        response = await call_next(request)

        try:
            path = request.url.path
            if path in TARGET_PATHS and response.status_code == 200:
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

    @app.get("/single-header", response_class=HTMLResponse)
    def single_header_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        s = _status()
        token = request.query_params.get("token", "")
        cfg = _cfg()

        page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7208 Single Header</title>
  {_css()}
  <style>
    body {{ background:#0b0f14; color:#e8eef5; font-family:Arial,sans-serif; margin:24px; }}
    a {{ color:#8cc8ff; }}
    .card {{ background:#121923; border:1px solid #263447; border-radius:12px; padding:16px; margin-bottom:18px; }}
    table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }}
    th,td {{ border-bottom:1px solid #263447; padding:8px; text-align:left; vertical-align:top; }}
    th {{ color:#a9bfd6; }}
    button {{ background:#123456; color:#e8eef5; border:1px solid #355273; border-radius:8px; padding:8px 11px; cursor:pointer; }}
    .muted {{ color:#9fb0c0; }}
  </style>
</head>
<body>
  {_bar(request)}

  <h1>TradingBot V7208 — Single Header Mode</h1>

  <div class="card">
    <p class="muted">
      Safe State: <b>{_esc(s.get("safe_state"))}</b> |
      Risky On: <b>{_esc(s.get("risky_on"))}</b> |
      Enabled: <b>{_esc(cfg.get("enabled"))}</b>
    </p>
    <p>
      <a href="/single-header.json?token={_esc(token)}">JSON</a> ·
      <a href="/single-header-config.json?token={_esc(token)}">Config JSON</a> ·
      <a href="/master?token={_esc(token)}">Master</a> ·
      <a href="/control-center?token={_esc(token)}">Control Center</a> ·
      <a href="/ranking-snapshot?token={_esc(token)}">Ranking</a>
    </p>
    <form method="post" action="/single-header/safe-mode?token={_esc(token)}">
      <button type="submit">SAFE MODE: alle Risky Toggles OFF</button>
    </form>
  </div>
</body>
</html>
"""
        return HTMLResponse(page)

    @app.get("/single-header.json")
    def single_header_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_status())

    @app.get("/single-header-config.json")
    def single_header_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    @app.post("/single-header-config/set")
    def single_header_config_set(
        request: Request,
        enabled: int | None = None,
        hide_v7206_card: int | None = None,
        hide_v7207_card: int | None = None,
        hide_old_large_cards: int | None = None,
    ):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        c = _cfg()

        if enabled is not None:
            c["enabled"] = bool(int(enabled))
        if hide_v7206_card is not None:
            c["hide_v7206_card"] = bool(int(hide_v7206_card))
        if hide_v7207_card is not None:
            c["hide_v7207_card"] = bool(int(hide_v7207_card))
        if hide_old_large_cards is not None:
            c["hide_old_large_cards"] = bool(int(hide_old_large_cards))

        return JSONResponse({"version": "V7208", "updated": True, "config": _save_cfg(c)})

    @app.post("/single-header/safe-mode")
    def single_header_safe_mode(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_set_safe_mode())

    app.state.v7208_single_header_installed = True
    print("[V7208] Single Header Mode installed")
