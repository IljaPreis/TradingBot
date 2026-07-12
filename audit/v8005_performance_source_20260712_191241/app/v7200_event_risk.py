import os
import re
import json
import html
from pathlib import Path
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()


def _root():
    here = Path(__file__).resolve()
    for p in [here.parent.parent, Path.cwd(), Path("/opt/tradingbot_v6000")]:
        if (p / "data").exists() or (p / "ops").exists():
            return p
    return here.parent.parent


def _parse_utc(ts):
    if not ts:
        return None
    try:
        ts = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _known_tokens():
    tokens = set()

    for name in ["DASHBOARD_TOKEN", "TRADINGBOT_TOKEN", "TOKEN", "ADMIN_TOKEN", "MASTER_TOKEN"]:
        val = str(os.environ.get(name, "") or "").strip()
        if val:
            tokens.add(val)

    root = _root()
    for rel in ["ops/v7000_check.sh", "ops/v7100_menu.sh"]:
        p = root / rel
        if p.exists():
            try:
                s = p.read_text(encoding="utf-8", errors="ignore")
                for m in re.findall(r"token=([A-Za-z0-9_\-]{20,})", s):
                    tokens.add(m)
                for m in re.findall(r"TOKEN=['\"]?([A-Za-z0-9_\-]{20,})", s):
                    tokens.add(m)
            except Exception:
                pass

    return tokens


def _token_ok(request: Request):
    tokens = _known_tokens()

    if not tokens:
        return True

    supplied = (
        request.query_params.get("token")
        or request.headers.get("X-Token")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
        or ""
    ).strip()

    return supplied in tokens


def _event_data():
    root = _root()
    path = os.environ.get("EVENT_RISK_FILE") or str(root / "data" / "event_risk.json")
    now = datetime.now(timezone.utc)

    raw = {
        "version": "V7200",
        "default_pre_minutes": 15,
        "default_post_minutes": 15,
        "events": []
    }

    load_error = None

    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                raw.update(loaded)
    except Exception as exc:
        load_error = str(exc)

    default_pre = int(raw.get("default_pre_minutes", 15) or 15)
    default_post = int(raw.get("default_post_minutes", 15) or 15)

    events = []
    active = []
    upcoming = []

    for ev in raw.get("events", []):
        if not isinstance(ev, dict):
            continue

        t = _parse_utc(ev.get("time_utc"))
        pre = int(ev.get("pre_minutes", default_pre) or default_pre)
        post = int(ev.get("post_minutes", default_post) or default_post)

        status = "unknown"
        minutes_to_event = None
        cooldown_start = None
        cooldown_end = None

        if t:
            cooldown_start = t - timedelta(minutes=pre)
            cooldown_end = t + timedelta(minutes=post)
            minutes_to_event = round((t - now).total_seconds() / 60, 1)

            if cooldown_start <= now <= cooldown_end:
                status = "ACTIVE_COOLDOWN"
            elif now < cooldown_start:
                status = "upcoming"
            else:
                status = "finished"

        row = {
            "title": ev.get("title", "Untitled Event"),
            "currency": ev.get("currency", "-"),
            "impact": ev.get("impact", "medium"),
            "time_utc": ev.get("time_utc", "-"),
            "minutes_to_event": minutes_to_event,
            "pre_minutes": pre,
            "post_minutes": post,
            "markets": ev.get("markets", []),
            "note": ev.get("note", ""),
            "status": status,
            "cooldown_start_utc": cooldown_start.isoformat() if cooldown_start else None,
            "cooldown_end_utc": cooldown_end.isoformat() if cooldown_end else None
        }

        events.append(row)

        if status == "ACTIVE_COOLDOWN":
            active.append(row)
        elif status == "upcoming":
            upcoming.append(row)

    events.sort(key=lambda x: 999999999 if x["minutes_to_event"] is None else x["minutes_to_event"])
    upcoming.sort(key=lambda x: 999999999 if x["minutes_to_event"] is None else x["minutes_to_event"])

    risk_level = "LOW"
    if active:
        risk_level = "MEDIUM"
        if any(str(x.get("impact", "")).lower() == "high" for x in active):
            risk_level = "HIGH"

    return {
        "version": "V7200",
        "now_utc": now.isoformat(),
        "risk_level": risk_level,
        "cooldown_active": bool(active),
        "active_count": len(active),
        "upcoming_count": len(upcoming),
        "active_events": active,
        "upcoming_events": upcoming[:10],
        "events": events,
        "source_file": path,
        "load_error": load_error
    }


def _esc(x):
    return html.escape(str(x))


def _row(ev):
    markets = ", ".join(ev.get("markets") or [])
    mins = ev.get("minutes_to_event")
    mins_txt = "-" if mins is None else str(mins)

    return f"""
    <tr>
      <td><b>{_esc(ev.get('status'))}</b></td>
      <td>{_esc(ev.get('impact'))}</td>
      <td>{_esc(ev.get('currency'))}</td>
      <td>{_esc(ev.get('title'))}</td>
      <td>{_esc(ev.get('time_utc'))}</td>
      <td>{_esc(mins_txt)}</td>
      <td>{_esc(ev.get('pre_minutes'))}/{_esc(ev.get('post_minutes'))} min</td>
      <td>{_esc(markets)}</td>
      <td>{_esc(ev.get('note', ''))}</td>
    </tr>
    """


@router.get("/event-risk", response_class=HTMLResponse)
def event_risk_page(request: Request):
    if not _token_ok(request):
        return HTMLResponse("unauthorized", status_code=401)

    d = _event_data()

    badge = "OK"
    if d["risk_level"] == "HIGH":
        badge = "HIGH RISK / COOLDOWN ACTIVE"
    elif d["risk_level"] == "MEDIUM":
        badge = "MEDIUM RISK / COOLDOWN ACTIVE"

    token = request.query_params.get("token", "")
    token_q = f"?token={_esc(token)}" if token else ""

    active_html = "".join(_row(x) for x in d["active_events"]) or "<tr><td colspan='9'>No active cooldown.</td></tr>"
    upcoming_html = "".join(_row(x) for x in d["upcoming_events"]) or "<tr><td colspan='9'>No upcoming events.</td></tr>"
    all_html = "".join(_row(x) for x in d["events"]) or "<tr><td colspan='9'>No events configured.</td></tr>"

    err = ""
    if d.get("load_error"):
        err = f"<div class='err'>JSON load error: {_esc(d.get('load_error'))}</div>"

    page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7200 Event Risk</title>
  <style>
    body {{ background:#0b0f14; color:#e8eef5; font-family:Arial,sans-serif; margin:24px; }}
    a {{ color:#8cc8ff; text-decoration:none; }}
    .card {{ background:#121923; border:1px solid #263447; border-radius:12px; padding:16px; margin-bottom:18px; }}
    .badge {{ display:inline-block; padding:8px 12px; border-radius:999px; font-weight:bold; }}
    .LOW {{ background:#1f6f3d; }}
    .MEDIUM {{ background:#8a6a1f; }}
    .HIGH {{ background:#8a1f1f; }}
    table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }}
    th,td {{ border-bottom:1px solid #263447; padding:8px; text-align:left; vertical-align:top; }}
    th {{ color:#a9bfd6; }}
    .muted {{ color:#9fb0c0; }}
    .err {{ background:#3b1515; border:1px solid #7a2d2d; padding:10px; border-radius:8px; margin-bottom:14px; }}
  </style>
</head>
<body>
  <h1>TradingBot V7200 — Event Risk / News Cooldown</h1>

  <div class="card">
    <div class="badge {_esc(d['risk_level'])}">{_esc(badge)}</div>
    <p class="muted">
      Now UTC: {_esc(d['now_utc'])}<br>
      Active events: {_esc(d['active_count'])} |
      Upcoming events: {_esc(d['upcoming_count'])}<br>
      Source: {_esc(d['source_file'])}
    </p>
    <p>
      <a href="/master{token_q}">Master</a> ·
      <a href="/blocked{token_q}">Blocked</a> ·
      <a href="/shadow-detail{token_q}">Shadow Detail</a> ·
      <a href="/markets{token_q}">Markets</a> ·
      <a href="/cluster-intel{token_q}">Cluster Intel</a> ·
      <a href="/session-intel{token_q}">Session Intel</a> ·
      <a href="/event-risk.json{token_q}">JSON</a>
    </p>
  </div>

  {err}

  <div class="card">
    <h2>Active Cooldown</h2>
    <table>
      <tr><th>Status</th><th>Impact</th><th>Currency</th><th>Title</th><th>Time UTC</th><th>Min to Event</th><th>Pre/Post</th><th>Markets</th><th>Note</th></tr>
      {active_html}
    </table>
  </div>

  <div class="card">
    <h2>Upcoming Events</h2>
    <table>
      <tr><th>Status</th><th>Impact</th><th>Currency</th><th>Title</th><th>Time UTC</th><th>Min to Event</th><th>Pre/Post</th><th>Markets</th><th>Note</th></tr>
      {upcoming_html}
    </table>
  </div>

  <div class="card">
    <h2>All Configured Events</h2>
    <table>
      <tr><th>Status</th><th>Impact</th><th>Currency</th><th>Title</th><th>Time UTC</th><th>Min to Event</th><th>Pre/Post</th><th>Markets</th><th>Note</th></tr>
      {all_html}
    </table>
  </div>
</body>
</html>
"""
    return HTMLResponse(page)


@router.get("/event-risk.json")
def event_risk_json(request: Request):
    if not _token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(_event_data())
