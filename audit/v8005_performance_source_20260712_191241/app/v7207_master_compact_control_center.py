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
        "version": "V7207",
        "enabled": True,
        "inject_compact_link": True,
        "safe_mode_button_enabled": True,
        "note": "V7207 is a central read/control dashboard. It does not enable live blocking or auto actions by default.",
    }
    c.update(_read_json("v7207_control_center_config.json", {}))
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


def _ranking_snapshot():
    try:
        from app.v7206_ranking_snapshot_compact_ui import _snapshot
        return _snapshot(write_file=True)
    except Exception:
        return _read_json("v7206_ranking_snapshot_last.json", {})


def _safe_bool(d, key, default=False):
    try:
        return bool(d.get(key, default))
    except Exception:
        return default


def _status():
    v7201 = _read_json("v7201_event_gate_config.json", {})
    v7202 = _read_json("v7202_trade_protection_config.json", {})
    v7203 = _read_json("v7203_pre_news_manager_config.json", {})
    v7204 = _read_json("v7204_entry_scoring_config.json", {})
    v7205 = _read_json("v7205_signal_quality_config.json", {})
    v7206 = _read_json("v7206_ranking_snapshot_config.json", {})

    hard_block = _safe_bool(v7201, "hard_block_enabled", False)
    protection_enforce = _safe_bool(v7202, "enforce_blocks", False)
    pre_news_auto = _safe_bool(v7203, "auto_actions_enabled", False)
    entry_apply_live = _safe_bool(v7204, "apply_to_live", False)
    quality_apply_live = _safe_bool(v7205, "apply_to_live", False)

    risky_on = any([
        hard_block,
        protection_enforce,
        pre_news_auto,
        entry_apply_live,
        quality_apply_live,
    ])

    snap = _ranking_snapshot()
    ev = _event_data()

    return {
        "version": "V7207",
        "mode": "MASTER_COMPACT_CONTROL_CENTER",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "safe_state": not risky_on,
        "risky_on": risky_on,
        "safety": {
            "v7201_hard_block_enabled": hard_block,
            "v7202_enforce_blocks": protection_enforce,
            "v7203_auto_actions_enabled": pre_news_auto,
            "v7204_apply_to_live": entry_apply_live,
            "v7205_apply_to_live": quality_apply_live,
        },
        "configs": {
            "v7201": v7201,
            "v7202": v7202,
            "v7203": v7203,
            "v7204": v7204,
            "v7205": v7205,
            "v7206": v7206,
        },
        "event_risk": {
            "risk_level": ev.get("risk_level"),
            "cooldown_active": ev.get("cooldown_active"),
            "active_count": ev.get("active_count"),
            "upcoming_count": ev.get("upcoming_count"),
        },
        "ranking": {
            "generated_utc": snap.get("generated_utc"),
            "market_count": snap.get("market_count"),
            "grade_counts": snap.get("grade_counts", {}),
            "best_markets": snap.get("best_markets", [])[:8],
            "worst_markets": snap.get("worst_markets", [])[:8],
            "no_entry_markets": snap.get("no_entry_markets", [])[:8],
            "avoid_markets": snap.get("avoid_markets", [])[:8],
        },
        "note": "Safe by default. Safe Mode can only turn risky toggles OFF.",
    }


def _set_safe_mode():
    changed = []

    files = {
        "v7201_event_gate_config.json": {
            "hard_block_enabled": False
        },
        "v7202_trade_protection_config.json": {
            "enforce_blocks": False
        },
        "v7203_pre_news_manager_config.json": {
            "auto_actions_enabled": False
        },
        "v7204_entry_scoring_config.json": {
            "apply_to_live": False
        },
        "v7205_signal_quality_config.json": {
            "apply_to_live": False
        },
    }

    for fname, changes in files.items():
        d = _read_json(fname, {})
        if not isinstance(d, dict):
            d = {}

        before = dict(d)
        d.update(changes)
        d["safe_mode_set_utc"] = datetime.now(timezone.utc).isoformat()

        if d != before:
            _write_json(fname, d)
            changed.append(fname)

    return {
        "version": "V7207",
        "safe_mode": True,
        "changed_files": changed,
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "message": "All risky live/enforce/auto toggles set to OFF.",
    }


def _badge(status):
    if status:
        return '<span class="badge danger">RISKY ON</span>'
    return '<span class="badge ok">SAFE</span>'


def _fmt_market(m):
    if not m:
        return "-"
    return f"{m.get('market')} {m.get('grade')} {m.get('final_score')}"


def _rows(markets):
    out = ""
    for m in markets:
        ev = m.get("event") or {}
        out += f"""
<tr>
  <td>{_esc(m.get("market"))}</td>
  <td>{_esc(m.get("final_score"))}</td>
  <td>{_esc(m.get("grade"))}</td>
  <td>{_esc(m.get("action"))}</td>
  <td>{_esc(m.get("entry_allowed"))}</td>
  <td>{_esc(m.get("entry_scoring", {}).get("score_penalty"))}</td>
  <td>{_esc(ev.get("title", "-"))}</td>
  <td>{_esc(ev.get("impact", "-"))}</td>
  <td>{_esc(ev.get("status", "-"))}</td>
</tr>
"""
    if not out:
        out = "<tr><td colspan='9'>Keine Daten.</td></tr>"
    return out


def _compact_card(request: Request):
    s = _status()
    token = request.query_params.get("token", "")
    safety = s.get("safety", {})
    ranking = s.get("ranking", {})
    ev = s.get("event_risk", {})

    best = ", ".join(_fmt_market(x) for x in ranking.get("best_markets", [])[:3]) or "-"
    no_entry_count = len(ranking.get("no_entry_markets", []) or [])

    bg = "#1f6f3d"
    label = "CONTROL SAFE"

    if s.get("risky_on"):
        bg = "#8a1f1f"
        label = "RISKY TOGGLE ON"
    elif no_entry_count > 0:
        bg = "#8a6a1f"
        label = "NEWS CAUTION"

    return f"""
<div id="v7207_control_center_card" style="
  position: static !important;
  max-width: 980px !important;
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
">
  <span style="display:inline-block;padding:4px 8px;border-radius:999px;background:{bg};color:#fff;font-weight:700;margin-right:8px;">
    V7207 {_esc(label)}
  </span>
  <b>Best:</b> {_esc(best)}
  <div style="margin-top:4px;color:#c8d6e4;">
    HardBlock: <b>{_esc(safety.get("v7201_hard_block_enabled"))}</b> |
    Enforce: <b>{_esc(safety.get("v7202_enforce_blocks"))}</b> |
    AutoActions: <b>{_esc(safety.get("v7203_auto_actions_enabled"))}</b> |
    V7204Live: <b>{_esc(safety.get("v7204_apply_to_live"))}</b> |
    V7205Live: <b>{_esc(safety.get("v7205_apply_to_live"))}</b>
  </div>
  <div style="margin-top:4px;color:#c8d6e4;">
    Event: <b>{_esc(ev.get("risk_level"))}</b> |
    Cooldown: <b>{_esc(ev.get("cooldown_active"))}</b> |
    Upcoming: <b>{_esc(ev.get("upcoming_count"))}</b>
    &nbsp; | &nbsp;
    <a style="color:#8cc8ff;" href="/control-center?token={_esc(token)}">Control Center</a> |
    <a style="color:#8cc8ff;" href="/ranking-snapshot?token={_esc(token)}">Ranking</a> |
    <a style="color:#8cc8ff;" href="/signal-quality?token={_esc(token)}">Quality</a>
  </div>
</div>
"""


def _inject(page, request):
    cfg = _cfg()
    if not bool(cfg.get("inject_compact_link", True)):
        return page

    css = """
<style id="v7207_control_center_css">
#v7207_control_center_card {
  position: static !important;
  top: auto !important;
  left: auto !important;
  right: auto !important;
  bottom: auto !important;
  z-index: auto !important;
}
</style>
"""

    if "v7207_control_center_css" not in page:
        lower = page.lower()
        head_pos = lower.find("</head>")
        if head_pos != -1:
            page = page[:head_pos] + css + page[head_pos:]
        else:
            page = css + page

    if 'id="v7207_control_center_card"' in page or "id='v7207_control_center_card'" in page:
        return page

    card = _compact_card(request)

    if 'id="v7206_ranking_snapshot_card"' in page:
        pos = page.find("</div>", page.find('id="v7206_ranking_snapshot_card"'))
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


def install_v7207_master_compact_control_center(app):
    if getattr(app.state, "v7207_control_center_installed", False):
        return

    @app.middleware("http")
    async def v7207_control_center_middleware(request: Request, call_next):
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

    @app.get("/control-center", response_class=HTMLResponse)
    def control_center_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)

        s = _status()
        token = request.query_params.get("token", "")
        safety = s.get("safety", {})
        ranking = s.get("ranking", {})
        ev = s.get("event_risk", {})

        page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7207 Control Center</title>
  <style>
    body {{ background:#0b0f14; color:#e8eef5; font-family:Arial,sans-serif; margin:24px; }}
    a {{ color:#8cc8ff; text-decoration:none; }}
    .card {{ background:#121923; border:1px solid #263447; border-radius:12px; padding:16px; margin-bottom:18px; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(230px,1fr)); gap:12px; }}
    .mini {{ background:#0f1721; border:1px solid #263447; border-radius:10px; padding:12px; }}
    .badge {{ display:inline-block; padding:7px 11px; border-radius:999px; color:white; font-weight:bold; }}
    .ok {{ background:#1f6f3d; }}
    .warn {{ background:#8a6a1f; }}
    .danger {{ background:#8a1f1f; }}
    table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }}
    th,td {{ border-bottom:1px solid #263447; padding:8px; text-align:left; vertical-align:top; }}
    th {{ color:#a9bfd6; }}
    button {{ background:#123456; color:#e8eef5; border:1px solid #355273; border-radius:8px; padding:8px 11px; cursor:pointer; }}
    .muted {{ color:#9fb0c0; }}
  </style>
</head>
<body>
  <h1>TradingBot V7207 — Master Compact Control Center</h1>

  <div class="card">
    {_badge(s.get("risky_on"))}
    <p class="muted">
      Safe State: <b>{_esc(s.get("safe_state"))}</b> |
      Event Risk: <b>{_esc(ev.get("risk_level"))}</b> |
      Cooldown: <b>{_esc(ev.get("cooldown_active"))}</b> |
      Upcoming: <b>{_esc(ev.get("upcoming_count"))}</b> |
      Snapshot: <b>{_esc(ranking.get("generated_utc"))}</b>
    </p>
    <p>
      <a href="/control-center.json?token={_esc(token)}">JSON</a> ·
      <a href="/master?token={_esc(token)}">Master</a> ·
      <a href="/markets?token={_esc(token)}">Markets</a> ·
      <a href="/ranking-snapshot?token={_esc(token)}">Ranking</a> ·
      <a href="/signal-quality?token={_esc(token)}">Signal Quality</a> ·
      <a href="/entry-scoring?token={_esc(token)}">Entry Scoring</a> ·
      <a href="/event-risk?token={_esc(token)}">Event Risk</a> ·
      <a href="/trade-protection?token={_esc(token)}">Trade Protection</a> ·
      <a href="/pre-news-manager?token={_esc(token)}">Pre-News Manager</a>
    </p>
    <form method="post" action="/control-center/safe-mode?token={_esc(token)}">
      <button type="submit">SAFE MODE: alle Risky Toggles OFF</button>
    </form>
  </div>

  <div class="grid">
    <div class="mini"><b>V7201 Hard Block</b><br>{_esc(safety.get("v7201_hard_block_enabled"))}</div>
    <div class="mini"><b>V7202 Enforce Blocks</b><br>{_esc(safety.get("v7202_enforce_blocks"))}</div>
    <div class="mini"><b>V7203 Auto Actions</b><br>{_esc(safety.get("v7203_auto_actions_enabled"))}</div>
    <div class="mini"><b>V7204 Apply Live</b><br>{_esc(safety.get("v7204_apply_to_live"))}</div>
    <div class="mini"><b>V7205 Apply Live</b><br>{_esc(safety.get("v7205_apply_to_live"))}</div>
    <div class="mini"><b>Grade Counts</b><br>{_esc(ranking.get("grade_counts"))}</div>
  </div>

  <div class="card">
    <h2>Best Markets</h2>
    <table>
      <tr><th>Market</th><th>Score</th><th>Grade</th><th>Action</th><th>Allowed</th><th>Penalty</th><th>Event</th><th>Impact</th><th>Status</th></tr>
      {_rows(ranking.get("best_markets", []))}
    </table>
  </div>

  <div class="card">
    <h2>Weak / No-Entry Markets</h2>
    <table>
      <tr><th>Market</th><th>Score</th><th>Grade</th><th>Action</th><th>Allowed</th><th>Penalty</th><th>Event</th><th>Impact</th><th>Status</th></tr>
      {_rows((ranking.get("no_entry_markets", []) or []) + (ranking.get("worst_markets", []) or []))}
    </table>
  </div>
</body>
</html>
"""
        return HTMLResponse(page)

    @app.get("/control-center.json")
    def control_center_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_status())

    @app.get("/control-center-config.json")
    def control_center_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    @app.post("/control-center/safe-mode")
    def control_center_safe_mode(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_set_safe_mode())

    app.state.v7207_control_center_installed = True
    print("[V7207] Master Compact Control Center installed")
