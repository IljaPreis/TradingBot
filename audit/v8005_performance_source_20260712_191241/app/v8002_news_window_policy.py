from __future__ import annotations

import html
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

VERSION = "V8002-NEWS-WINDOW-POLICY"

TIER1_TERMS = (
    "interest rate", "rate decision", "fomc", "federal funds", "ecb rate",
    "boe rate", "boj rate", "rba rate", "rbnz rate", "boc rate", "snb rate",
    "nonfarm", "non-farm", "nfp", "employment situation", "cpi", "consumer price",
    "core pce", "pce price", "gdp", "gross domestic product", "unemployment rate",
    "average hourly earnings", "payrolls",
)
HIGH_TERMS = TIER1_TERMS + (
    "retail sales", "ism manufacturing", "ism services", "jobless claims",
    "employment change", "wage", "inflation", "pmi", "trade balance",
)
EXTREME_HEADLINE_TERMS = (
    "war", "attack", "missile", "invasion", "emergency", "default", "bank failure",
    "intervention", "capital controls", "rate cut", "rate hike", "unexpected decision",
)

MARKET_CURRENCIES: dict[str, set[str]] = {
    "US100": {"USD"}, "US500": {"USD"}, "US30": {"USD"},
    "GER40": {"EUR"}, "FRA40": {"EUR"}, "FTSE100": {"GBP"},
    "ASX200": {"AUD", "CNY"}, "JP225": {"JPY", "USD"},
    "XAUUSD": {"USD"}, "XAGUSD": {"USD"}, "USOIL": {"USD"}, "BRENT": {"USD", "GBP"},
    "BTCUSD": {"USD"}, "ETHUSD": {"USD"},
}


def _root() -> Path:
    for p in (Path("/app"), Path("/opt/tradingbot_v6000"), Path.cwd()):
        if (p / "data").exists():
            return p
    return Path.cwd()


def _data(name: str) -> Path:
    return _root() / "data" / name


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(obj, dict):
                return {**default, **obj}
    except Exception:
        pass
    return dict(default)


def _cfg() -> dict[str, Any]:
    default = {
        "version": VERSION,
        "enabled": True,
        "observe_only": True,
        "enforce": False,
        "calendar_authority": "economic_calendar_events",
        "actual_and_headline_authority": "financialjuice",
        "secondary_calendar_mode": "display_and_healthcheck_only",
        "timezone": "Europe/Berlin",
        "high_pre_minutes": 15,
        "high_post_minutes_confirmed": 15,
        "high_post_minutes_unconfirmed": 30,
        "tier1_post_minutes": 30,
        "medium_pre_minutes": 10,
        "medium_post_minutes": 10,
        "low_events_block": False,
        "medium_events_hard_block": False,
        "financialjuice_headline_window_minutes": 15,
        "financialjuice_headlines_hard_block": False,
    }
    return _read_json(_data("v8002_news_policy_config.json"), default)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_data("v7000_learning.sqlite3")))
    con.row_factory = sqlite3.Row
    return con


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def _pick_col(cols: Iterable[str], *names: str) -> str | None:
    low = {c.lower(): c for c in cols}
    for name in names:
        if name.lower() in low:
            return low[name.lower()]
    return None


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _parse_iso(value: Any) -> datetime | None:
    text = _as_text(value)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_event_dt(row: dict[str, Any], cfg: dict[str, Any]) -> datetime | None:
    for key in ("ts", "time_utc", "event_time", "datetime", "scheduled_at", "release_time"):
        dt = _parse_iso(row.get(key))
        if dt:
            return dt

    date_raw = _as_text(row.get("date_raw") or row.get("date"))
    time_raw = _as_text(row.get("time_raw") or row.get("time"))
    if not date_raw or not time_raw or time_raw.lower() in {"all day", "tentative", "-"}:
        return None

    formats = (
        "%m-%d-%Y %I:%M%p", "%d-%m-%Y %I:%M%p", "%Y-%m-%d %I:%M%p",
        "%m/%d/%Y %I:%M%p", "%d/%m/%Y %I:%M%p", "%Y/%m/%d %I:%M%p",
        "%m-%d-%Y %H:%M", "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M", "%Y/%m/%d %H:%M",
    )
    joined = f"{date_raw} {time_raw}".replace(" ", " ").strip()
    for fmt in formats:
        try:
            naive = datetime.strptime(joined, fmt)
            local = naive.replace(tzinfo=ZoneInfo(str(cfg.get("timezone") or "Europe/Berlin")))
            return local.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def _market_currencies(market: str) -> set[str]:
    m = _as_text(market).upper()
    if m in MARKET_CURRENCIES:
        return set(MARKET_CURRENCIES[m])
    if len(m) == 6 and m.isalpha():
        return {m[:3], m[3:]}
    out: set[str] = set()
    for code in ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY"):
        if code in m:
            out.add(code)
    return out


def _impact(row: dict[str, Any]) -> str:
    text = " ".join(_as_text(row.get(k)) for k in ("impact", "importance", "priority", "severity")).lower()
    title = _as_text(row.get("title") or row.get("event") or row.get("name")).lower()
    if any(term in title for term in HIGH_TERMS):
        return "high"
    if any(x in text for x in ("high", "3", "red", "major")):
        return "high"
    if any(x in text for x in ("medium", "2", "orange", "moderate")):
        return "medium"
    return "low"


def _is_tier1(title: str) -> bool:
    low = title.lower()
    return any(term in low for term in TIER1_TERMS)


def _actual_confirmed(row: dict[str, Any]) -> bool:
    actual = _as_text(row.get("actual"))
    return bool(actual and actual.lower() not in {"none", "null", "-", "n/a"})


def _calendar_rows(con: sqlite3.Connection, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    table = str(cfg.get("calendar_authority") or "economic_calendar_events")
    if table not in _tables(con):
        return []
    cols = _columns(con, table)
    order = _pick_col(cols, "ts", "event_time", "scheduled_at", "id") or cols[0]
    rows = con.execute(f'SELECT * FROM "{table}" ORDER BY "{order}" DESC LIMIT 1000').fetchall()
    return [dict(r) for r in rows]


def _headline_rows(con: sqlite3.Connection, minutes: int) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    for table in ("financialjuice_messages", "financialjuice_news"):
        if table not in _tables(con):
            continue
        cols = _columns(con, table)
        time_col = _pick_col(cols, "created_at", "published_at", "timestamp", "ts", "received_at")
        text_col = _pick_col(cols, "text", "message", "headline", "title", "content")
        if not time_col or not text_col:
            continue
        try:
            rows = con.execute(
                f'SELECT "{time_col}" AS t, "{text_col}" AS text FROM "{table}" ORDER BY "{time_col}" DESC LIMIT 100'
            ).fetchall()
        except Exception:
            continue
        for row in rows:
            dt = _parse_iso(row["t"])
            if not dt or now - dt > timedelta(minutes=minutes):
                continue
            text = _as_text(row["text"])
            if text:
                results.append({"table": table, "time": dt.isoformat(), "text": text})
    return results


@dataclass
class WindowHit:
    title: str
    currency: str
    impact: str
    event_time_utc: str
    minutes_to_event: float
    pre_minutes: int
    post_minutes: int
    actual_confirmed: bool
    hard_block: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def evaluate_news_window(market: str, now_utc: datetime | None = None) -> dict[str, Any]:
    cfg = _cfg()
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    if not cfg.get("enabled", True):
        return {"version": VERSION, "enabled": False, "hard_block": False, "risk": "OFF", "events": []}

    currencies = _market_currencies(market)
    hits: list[WindowHit] = []
    headline_warnings: list[dict[str, Any]] = []
    try:
        con = _connect()
        for row in _calendar_rows(con, cfg):
            currency = _as_text(row.get("currency") or row.get("country")).upper()
            if currencies and currency and currency not in currencies:
                continue
            event_dt = _parse_event_dt(row, cfg)
            if not event_dt:
                continue
            title = _as_text(row.get("title") or row.get("event") or row.get("name") or "Unknown event")
            impact = _impact(row)
            confirmed = _actual_confirmed(row)
            tier1 = _is_tier1(title)
            if impact == "high":
                pre = int(cfg.get("high_pre_minutes", 15))
                post = int(cfg.get("tier1_post_minutes", 30) if tier1 else (cfg.get("high_post_minutes_confirmed", 15) if confirmed else cfg.get("high_post_minutes_unconfirmed", 30)))
                hard = True
            elif impact == "medium":
                pre = int(cfg.get("medium_pre_minutes", 10))
                post = int(cfg.get("medium_post_minutes", 10))
                hard = bool(cfg.get("medium_events_hard_block", False))
            else:
                pre = post = 0
                hard = bool(cfg.get("low_events_block", False))
            delta_min = (event_dt - now).total_seconds() / 60.0
            active = (-post <= delta_min <= pre)
            if not active:
                continue
            reason = "scheduled_high_window" if impact == "high" else "scheduled_medium_window" if impact == "medium" else "scheduled_low_window"
            if impact == "high" and delta_min < 0 and not confirmed:
                reason += "_actual_unconfirmed"
            hits.append(WindowHit(title, currency, impact, event_dt.isoformat(), round(delta_min, 2), pre, post, confirmed, hard, reason))

        for item in _headline_rows(con, int(cfg.get("financialjuice_headline_window_minutes", 15))):
            text_low = item["text"].lower()
            market_match = not currencies or any(c.lower() in text_low for c in currencies)
            extreme = any(term in text_low for term in EXTREME_HEADLINE_TERMS)
            if market_match or extreme:
                headline_warnings.append({**item, "extreme_keyword": extreme, "hard_block": bool(extreme and cfg.get("financialjuice_headlines_hard_block", False))})
    except Exception as exc:
        return {
            "version": VERSION, "enabled": True, "hard_block": False, "risk": "ERROR",
            "market": market, "currencies": sorted(currencies), "events": [], "headline_warnings": [], "error": str(exc),
            "observe_only": bool(cfg.get("observe_only", True)), "enforce": bool(cfg.get("enforce", False)),
        }
    finally:
        try:
            con.close()
        except Exception:
            pass

    raw_hard = any(h.hard_block for h in hits) or any(x.get("hard_block") for x in headline_warnings)
    enforce = bool(cfg.get("enforce", False)) and not bool(cfg.get("observe_only", True))
    risk = "HARD_BLOCK" if raw_hard else "REVIEW" if hits or headline_warnings else "LOW"
    return {
        "version": VERSION,
        "enabled": True,
        "market": market,
        "currencies": sorted(currencies),
        "generated_utc": now.isoformat(),
        "risk": risk,
        "would_hard_block": raw_hard,
        "hard_block": bool(raw_hard and enforce),
        "observe_only": bool(cfg.get("observe_only", True)),
        "enforce": enforce,
        "events": [h.as_dict() for h in sorted(hits, key=lambda x: abs(x.minutes_to_event))],
        "headline_warnings": headline_warnings[:20],
        "policy": {
            "high_pre_minutes": cfg.get("high_pre_minutes"),
            "high_post_minutes_confirmed": cfg.get("high_post_minutes_confirmed"),
            "high_post_minutes_unconfirmed": cfg.get("high_post_minutes_unconfirmed"),
            "tier1_post_minutes": cfg.get("tier1_post_minutes"),
            "calendar_authority": cfg.get("calendar_authority"),
            "actual_and_headline_authority": cfg.get("actual_and_headline_authority"),
            "secondary_calendar_mode": cfg.get("secondary_calendar_mode"),
        },
    }


def _token_ok(request: Request) -> bool:
    try:
        from app.v7200_event_risk import _token_ok as real_token_ok
        return bool(real_token_ok(request))
    except Exception:
        return bool(request.query_params.get("token"))


def _esc(x: Any) -> str:
    return html.escape(str(x))


def _html(report: dict[str, Any]) -> str:
    events = report.get("events") or []
    heads = report.get("headline_warnings") or []
    trs = "".join(
        f"<tr><td>{_esc(e.get('event_time_utc'))}</td><td>{_esc(e.get('currency'))}</td><td>{_esc(e.get('title'))}</td><td>{_esc(e.get('impact'))}</td><td>{_esc(e.get('minutes_to_event'))}</td><td>{_esc(e.get('pre_minutes'))}/{_esc(e.get('post_minutes'))}</td><td>{_esc(e.get('actual_confirmed'))}</td><td>{_esc(e.get('hard_block'))}</td></tr>"
        for e in events
    ) or "<tr><td colspan='8'>Kein aktives Eventfenster.</td></tr>"
    htrs = "".join(
        f"<tr><td>{_esc(h.get('time'))}</td><td>{_esc(h.get('table'))}</td><td style='white-space:normal'>{_esc(h.get('text'))}</td><td>{_esc(h.get('extreme_keyword'))}</td></tr>"
        for h in heads
    ) or "<tr><td colspan='4'>Keine aktuellen FJ-Headline-Warnungen.</td></tr>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>V8002 News Policy</title><style>body{{background:#07111e;color:#eef6ff;font-family:Arial;margin:0}}.w{{max-width:1450px;margin:auto;padding:14px}}.c{{background:#101d2e;border:1px solid #273a52;border-radius:15px;padding:14px;margin:10px 0;overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{padding:7px;border-bottom:1px solid #273a52;text-align:left;white-space:nowrap}}th{{color:#a5d8ff}}.ok{{color:#22c55e}}.warn{{color:#facc15}}.bad{{color:#f87171}}a{{color:#93c5fd}}</style></head><body><div class='w'>
<h1>V8002 News Window Policy</h1><div class='c'><b>Observe only:</b> {_esc(report.get('observe_only'))} · <b>Enforce:</b> {_esc(report.get('enforce'))} · <b>Risk:</b> {_esc(report.get('risk'))} · <b>Would block:</b> {_esc(report.get('would_hard_block'))}</div>
<div class='c'><h2>Aktive Kalenderfenster</h2><table><tr><th>Zeit UTC</th><th>CCY</th><th>Event</th><th>Impact</th><th>Minuten</th><th>Pre/Post</th><th>Actual bestätigt</th><th>Hard</th></tr>{trs}</table></div>
<div class='c'><h2>FinancialJuice Headline-Warnungen</h2><table><tr><th>Zeit</th><th>Quelle</th><th>Headline</th><th>Extreme</th></tr>{htrs}</table></div>
<div class='c'><a href='/master'>Master</a> · <a href='/master-ai-review'>AI Review</a> · <a href='/v8001/pine-audit'>V8001 Pine Audit</a></div></div></body></html>"""


def install_v8002_news_window_policy(app) -> None:
    if getattr(app.state, "v8002_news_window_policy_installed", False):
        return

    @app.get("/v8002/news-policy", response_class=HTMLResponse)
    def v8002_news_policy_page(request: Request, market: str = "US100"):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        return HTMLResponse(_html(evaluate_news_window(market)))

    @app.get("/v8002/news-policy.json")
    def v8002_news_policy_json(request: Request, market: str = "US100"):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(evaluate_news_window(market))

    @app.get("/v8002/news-policy-config.json")
    def v8002_news_policy_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    app.state.v8002_news_window_policy_installed = True
    print("[V8002] News Window Policy installed (observe-only default)")
