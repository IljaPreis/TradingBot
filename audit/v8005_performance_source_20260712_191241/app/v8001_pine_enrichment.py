from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

VERSION = "V8001-PINE-SERVER-ENRICHMENT"
SCHEMA = "V8001_DUAL_PLAYBOOK_ZONE_AUDIT"


def _root() -> Path:
    for p in (Path("/app"), Path("/opt/tradingbot_v6000"), Path.cwd()):
        if (p / "data").exists():
            return p
    return Path.cwd()


def _data(name: str) -> Path:
    return _root() / "data" / name


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_data("v7000_learning.sqlite3")), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _init_db(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS v8001_pine_enriched_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            signal_event_id TEXT NOT NULL,
            client_trade_id TEXT,
            schema_version TEXT,
            source TEXT,
            market TEXT,
            direction TEXT,
            setup_name TEXT,
            setup_family TEXT,
            playbook TEXT,
            timeframe TEXT,
            confidence REAL,
            zone_id TEXT,
            zone_type TEXT,
            zone_tf TEXT,
            zone_top REAL,
            zone_bottom REAL,
            zone_touch_count INTEGER,
            structure_event_id INTEGER,
            session TEXT,
            bias_5m TEXT,
            bias_15m TEXT,
            bias_30m TEXT,
            bias_1h TEXT,
            bias_4h TEXT,
            bias_1d TEXT,
            bias_1w TEXT,
            entry_layer_bias TEXT,
            tactical_layer_bias TEXT,
            htf_layer_bias TEXT,
            regime_bias TEXT,
            news_risk TEXT,
            news_would_hard_block INTEGER DEFAULT 0,
            news_hard_block INTEGER DEFAULT 0,
            raw_json TEXT NOT NULL,
            UNIQUE(signal_event_id)
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_v8001_received ON v8001_pine_enriched_signals(received_at)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_v8001_market ON v8001_pine_enriched_signals(market, direction, setup_name)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_v8001_playbook ON v8001_pine_enriched_signals(playbook, setup_family)")
    con.commit()


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "."))
    except Exception:
        return default


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _is_v8001_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    source = _text(payload.get("source")).lower()
    schema = _text(payload.get("schema_version")).upper()
    return source.startswith("tradingview_v8001") or schema.startswith("V8001")


def _news_report(market: str) -> dict[str, Any]:
    try:
        from app.v8002_news_window_policy import evaluate_news_window
        return evaluate_news_window(market)
    except Exception as exc:
        return {
            "version": "V8002-UNAVAILABLE",
            "risk": "ERROR",
            "would_hard_block": False,
            "hard_block": False,
            "observe_only": True,
            "error": str(exc),
        }


def _enrich(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    market = _text(out.get("market"))
    news = _news_report(market)
    now = datetime.now(timezone.utc).isoformat()

    out["v8001_server_enrichment"] = {
        "version": VERSION,
        "received_at": now,
        "payload_schema_recognized": True,
        "dual_playbook_recognized": bool(out.get("playbook")),
        "zone_schema_recognized": bool(out.get("zone_id") or out.get("zone_type")),
        "multi_tf_bias_recognized": all(k in out for k in ("bias_15m", "bias_1h", "bias_4h", "bias_1d")),
    }
    out["v8002_news_policy"] = news
    out["news_risk"] = news.get("risk", "LOW")
    out["news_would_hard_block"] = bool(news.get("would_hard_block", False))
    out["news_hard_block"] = bool(news.get("hard_block", False))

    # Default stays observe-only. Only an explicitly enabled V8002 policy can
    # annotate a real hard block for the existing downstream gates.
    if bool(news.get("hard_block", False)):
        out["event_risk"] = "HARD_BLOCK"
        out["force_shadow_only"] = True
        out["hard_block_reason"] = "V8002_SCHEDULED_NEWS_WINDOW"
    return out


def _store(payload: dict[str, Any]) -> None:
    signal_event_id = _text(payload.get("signal_event_id") or payload.get("client_trade_id"))
    if not signal_event_id:
        return
    news = payload.get("v8002_news_policy") if isinstance(payload.get("v8002_news_policy"), dict) else {}
    try:
        con = _connect()
        _init_db(con)
        con.execute(
            """
            INSERT INTO v8001_pine_enriched_signals (
                received_at, signal_event_id, client_trade_id, schema_version, source,
                market, direction, setup_name, setup_family, playbook, timeframe, confidence,
                zone_id, zone_type, zone_tf, zone_top, zone_bottom, zone_touch_count,
                structure_event_id, session, bias_5m, bias_15m, bias_30m, bias_1h,
                bias_4h, bias_1d, bias_1w, entry_layer_bias, tactical_layer_bias,
                htf_layer_bias, regime_bias, news_risk, news_would_hard_block,
                news_hard_block, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(signal_event_id) DO UPDATE SET
                received_at=excluded.received_at,
                client_trade_id=excluded.client_trade_id,
                confidence=excluded.confidence,
                news_risk=excluded.news_risk,
                news_would_hard_block=excluded.news_would_hard_block,
                news_hard_block=excluded.news_hard_block,
                raw_json=excluded.raw_json
            """,
            (
                datetime.now(timezone.utc).isoformat(), signal_event_id,
                _text(payload.get("client_trade_id")), _text(payload.get("schema_version")),
                _text(payload.get("source")), _text(payload.get("market")),
                _text(payload.get("direction") or payload.get("side")),
                _text(payload.get("setup_name") or payload.get("trigger")),
                _text(payload.get("setup_family")), _text(payload.get("playbook")),
                _text(payload.get("timeframe")), _num(payload.get("confidence")),
                _text(payload.get("zone_id")), _text(payload.get("zone_type")),
                _text(payload.get("zone_tf")), _num(payload.get("zone_top")),
                _num(payload.get("zone_bottom")), _int(payload.get("zone_touch_count")),
                _int(payload.get("structure_event_id")), _text(payload.get("session")),
                _text(payload.get("bias_5m")), _text(payload.get("bias_15m")),
                _text(payload.get("bias_30m")), _text(payload.get("bias_1h")),
                _text(payload.get("bias_4h")), _text(payload.get("bias_1d")),
                _text(payload.get("bias_1w")), _text(payload.get("entry_layer_bias")),
                _text(payload.get("tactical_layer_bias")), _text(payload.get("htf_layer_bias")),
                _text(payload.get("regime_bias")), _text(news.get("risk")),
                1 if news.get("would_hard_block") else 0, 1 if news.get("hard_block") else 0,
                json.dumps(payload, ensure_ascii=False, default=str),
            ),
        )
        con.commit()
    except Exception:
        pass
    finally:
        try:
            con.close()
        except Exception:
            pass


def _summary(limit: int = 100) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "version": VERSION,
        "mode": "observe_only_enrichment_by_default",
        "schema": SCHEMA,
        "rows": 0,
        "playbooks": {},
        "setups": {},
        "news_risk": {},
        "latest": [],
    }
    try:
        con = _connect()
        _init_db(con)
        result["rows"] = con.execute("SELECT COUNT(*) FROM v8001_pine_enriched_signals").fetchone()[0]
        for field in ("playbook", "setup_name", "news_risk"):
            rows = con.execute(
                f'SELECT COALESCE(NULLIF("{field}",\'\'),\'-\') AS k, COUNT(*) AS n '
                f'FROM v8001_pine_enriched_signals GROUP BY k ORDER BY n DESC LIMIT 30'
            ).fetchall()
            key = "setups" if field == "setup_name" else "playbooks" if field == "playbook" else field
            result[key] = {str(r["k"]): int(r["n"]) for r in rows}
        rows = con.execute(
            """
            SELECT received_at, signal_event_id, market, direction, setup_name, playbook,
                   confidence, zone_id, zone_tf, zone_touch_count, bias_15m, bias_1h,
                   bias_4h, bias_1d, news_risk, news_would_hard_block, news_hard_block
            FROM v8001_pine_enriched_signals ORDER BY id DESC LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        result["latest"] = [dict(r) for r in rows]
    except Exception as exc:
        result.update({"ok": False, "error": str(exc)})
    finally:
        try:
            con.close()
        except Exception:
            pass
    return result


def _token_ok(request: Request) -> bool:
    try:
        from app.v7200_event_risk import _token_ok as real_token_ok
        return bool(real_token_ok(request))
    except Exception:
        return bool(request.query_params.get("token"))


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _page(data: dict[str, Any]) -> str:
    trs = "".join(
        "<tr>" + "".join(
            f"<td>{_esc(row.get(k, ''))}</td>" for k in
            ("received_at", "market", "direction", "setup_name", "playbook", "confidence",
             "zone_id", "zone_tf", "zone_touch_count", "bias_15m", "bias_1h", "bias_4h",
             "bias_1d", "news_risk", "news_would_hard_block", "news_hard_block")
        ) + "</tr>"
        for row in data.get("latest", [])
    ) or "<tr><td colspan='16'>Noch keine V8001-Signale empfangen.</td></tr>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>V8001 Pine Audit</title><style>body{{background:#07111e;color:#eef6ff;font-family:Arial;margin:0}}.w{{max-width:1600px;margin:auto;padding:14px}}.c{{background:#101d2e;border:1px solid #273a52;border-radius:15px;padding:14px;margin:10px 0;overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:11px}}th,td{{padding:7px;border-bottom:1px solid #273a52;text-align:left;white-space:nowrap}}th{{color:#a5d8ff}}a{{color:#93c5fd}}</style></head><body><div class='w'>
<h1>V8001 Pine Server Audit</h1><div class='c'><b>Rows:</b> {_esc(data.get('rows'))} · <b>Mode:</b> {_esc(data.get('mode'))} · <b>Schema:</b> {_esc(data.get('schema'))}</div>
<div class='c'><b>Playbooks:</b> {_esc(data.get('playbooks'))}<br><b>Setups:</b> {_esc(data.get('setups'))}<br><b>News Risk:</b> {_esc(data.get('news_risk'))}</div>
<div class='c'><table><tr><th>Zeit</th><th>Markt</th><th>Dir</th><th>Setup</th><th>Playbook</th><th>Conf</th><th>Zone ID</th><th>TF</th><th>Touches</th><th>15m</th><th>1H</th><th>4H</th><th>1D</th><th>News</th><th>Would block</th><th>Hard block</th></tr>{trs}</table></div>
<div class='c'><a href='/master'>Master</a> · <a href='/master-ai-review'>AI Review</a> · <a href='/v8002/news-policy'>V8002 News Policy</a></div></div></body></html>"""


def _review_card(token: str) -> str:
    suffix = f"?token={quote(token)}" if token else ""
    return f"""<div id='v8001_v8002_review_card' style='margin:12px;padding:14px;border:1px solid #315273;border-radius:14px;background:#0e1d2d;color:#eef6ff'>
<b>V8001 Pine + V8002 News Policy</b><br><span style='opacity:.8'>Dual Playbook, Zone IDs, Multi-TF Bias und 15/30-Minuten News-Fenster. Standard: Observe-only.</span><br>
<a style='color:#93c5fd' href='/v8001/pine-audit{suffix}'>Pine Audit</a> · <a style='color:#93c5fd' href='/v8002/news-policy{suffix}'>News Policy</a></div>"""


class V8001EnrichmentMiddleware:
    """Pure ASGI middleware: enriches V8001 JSON and injects review links."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "GET").upper()
        path = str(scope.get("path") or "")

        if method == "POST":
            messages = []
            body = b""
            more = True
            while more:
                msg = await receive()
                messages.append(msg)
                if msg.get("type") == "http.request":
                    body += msg.get("body", b"")
                    more = bool(msg.get("more_body", False))
                else:
                    more = False

            new_body = body
            try:
                payload = json.loads(body.decode("utf-8"))
                if _is_v8001_payload(payload):
                    payload = _enrich(payload)
                    _store(payload)
                    new_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
            except Exception:
                pass

            replayed = False

            async def replay_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": new_body, "more_body": False}
                return {"type": "http.disconnect"}

            # Correct content length for downstream request parsers.
            headers = [(k, v) for (k, v) in scope.get("headers", []) if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(new_body)).encode("ascii")))
            new_scope = dict(scope)
            new_scope["headers"] = headers
            await self.app(new_scope, replay_receive, send)
            return

        if method == "GET" and path in {"/master", "/master-ai-review"}:
            start = None
            chunks: list[bytes] = []

            async def capture_send(message):
                nonlocal start
                if message["type"] == "http.response.start":
                    start = dict(message)
                elif message["type"] == "http.response.body":
                    chunks.append(message.get("body", b""))
                    if message.get("more_body", False):
                        return
                    if start is None:
                        return
                    body = b"".join(chunks)
                    headers = list(start.get("headers", []))
                    ctype = next((v.decode("latin1") for k, v in headers if k.lower() == b"content-type"), "")
                    if start.get("status") == 200 and "text/html" in ctype and b"v8001_v8002_review_card" not in body:
                        qs = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="ignore"))
                        token = (qs.get("token") or [""])[0]
                        text = body.decode("utf-8", errors="ignore")
                        card = _review_card(token)
                        text = text.replace("</body>", card + "</body>") if "</body>" in text else text + card
                        body = text.encode("utf-8")
                    headers = [(k, v) for k, v in headers if k.lower() not in {b"content-length", b"content-encoding"}]
                    headers.append((b"content-length", str(len(body)).encode("ascii")))
                    start["headers"] = headers
                    await send(start)
                    await send({"type": "http.response.body", "body": body, "more_body": False})

            await self.app(scope, receive, capture_send)
            return

        await self.app(scope, receive, send)


def install_v8001_pine_enrichment(app) -> None:
    if getattr(app.state, "v8001_pine_enrichment_installed", False):
        return

    app.add_middleware(V8001EnrichmentMiddleware)

    @app.get("/v8001/pine-audit", response_class=HTMLResponse)
    def v8001_pine_audit_page(request: Request, limit: int = 100):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        return HTMLResponse(_page(_summary(limit)))

    @app.get("/v8001/pine-audit.json")
    def v8001_pine_audit_json(request: Request, limit: int = 100):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_summary(limit))

    @app.get("/v8001/schema.json")
    def v8001_schema_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        schema_path = _data("v8001_payload_schema.json")
        try:
            return JSONResponse(json.loads(schema_path.read_text(encoding="utf-8")))
        except Exception as exc:
            return JSONResponse({"version": VERSION, "schema": SCHEMA, "error": str(exc)})

    app.state.v8001_pine_enrichment_installed = True
    print("[V8001] Pine enrichment installed (observe-only compatible)")
