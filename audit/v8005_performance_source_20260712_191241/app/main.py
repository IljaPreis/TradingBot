import os
from datetime import datetime, timezone
from fastapi.responses import JSONResponse
from fastapi import Request
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional, Any, Dict, List
import sqlite3

from app.core.state import STATE, TRADES, NEWS, HISTORY, LEARNING, BACKTESTS
from app.core.config import WATCHLIST
from app.engines.scoring_engine import scan_all, decide
from app.engines.news_engine import add_news
from app.engines.learning_engine import upload_csv, backtest
from app.engines.risk_engine import risk_status
from app.engines.telegram_engine import send_telegram

# V7000 engines: News + Learning + Decision
from tradingbot_v7000.news_engine import NewsEngine
from tradingbot_v7000.decision_engine import DecisionEngine

app = FastAPI(title="TradingBot V6000 Institutional AI + V7000 Decision Layer")


# V7000 OPEN TRADE TRACKER HELPERS
V7000_LEARNING_DB = "data/v7000_learning.sqlite3"

def v7000_db():
    return sqlite3.connect(V7000_LEARNING_DB)

def v7000_init_open_trades():
    con = v7000_db()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS open_trades (
        client_trade_id TEXT PRIMARY KEY,
        decision_id INTEGER NOT NULL,
        market TEXT,
        direction TEXT,
        setup_name TEXT,
        entry REAL,
        sl REAL,
        tp1 REAL,
        status TEXT DEFAULT 'OPEN',
        opened_at TEXT,
        closed_at TEXT
    )
    """)
    con.commit()
    con.close()

def v7000_store_open_trade(client_trade_id, v7):
    if not client_trade_id:
        return False

    v7000_init_open_trades()
    con = v7000_db()
    cur = con.cursor()

    cur.execute("""
    INSERT OR REPLACE INTO open_trades
    (client_trade_id, decision_id, market, direction, setup_name, entry, sl, tp1, status, opened_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
    """, (
        str(client_trade_id),
        int(v7.get("decision_id")),
        v7.get("market"),
        v7.get("direction"),
        v7.get("setup_name"),
        float(v7.get("entry") or 0),
        float(v7.get("sl") or 0),
        float(v7.get("tp1") or 0),
        datetime.now(timezone.utc).isoformat()
    ))

    con.commit()
    con.close()
    return True

def v7000_find_decision_by_client_trade_id(client_trade_id):
    if not client_trade_id:
        return None

    v7000_init_open_trades()
    con = v7000_db()
    cur = con.cursor()

    row = cur.execute("""
    SELECT decision_id, market, direction, setup_name, entry, sl, tp1, status
    FROM open_trades
    WHERE client_trade_id = ?
    """, (str(client_trade_id),)).fetchone()

    con.close()

    if not row:
        return None

    return {
        "decision_id": row[0],
        "market": row[1],
        "direction": row[2],
        "setup_name": row[3],
        "entry": row[4],
        "sl": row[5],
        "tp1": row[6],
        "status": row[7],
    }

def v7000_close_open_trade(client_trade_id):
    if not client_trade_id:
        return False

    v7000_init_open_trades()
    con = v7000_db()
    cur = con.cursor()

    cur.execute("""
    UPDATE open_trades
    SET status='CLOSED', closed_at=?
    WHERE client_trade_id=?
    """, (datetime.now(timezone.utc).isoformat(), str(client_trade_id)))

    con.commit()
    con.close()
    return True




# V7000 DUPLICATE OUTCOME HTTP GUARD
@app.exception_handler(sqlite3.IntegrityError)
async def v7000_sqlite_integrity_handler(request: Request, exc: sqlite3.IntegrityError):
    msg = str(exc)

    if (
        "trade_outcomes" in msg
        or "ux_trade_outcomes_decision_id" in msg
        or "UNIQUE constraint failed: trade_outcomes.decision_id" in msg
    ):
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        return JSONResponse(
            status_code=200,
            content={
                "accepted": True,
                "type": "outcome",
                "stored": False,
                "duplicate": True,
                "decision_id": (
                    payload.get("decision_id")
                    or (
                        v7000_find_decision_by_client_trade_id(
                            payload.get("client_trade_id") or payload.get("trade_id") or payload.get("id")
                        ) or {}
                    ).get("decision_id")
                ),
                "client_trade_id": payload.get("client_trade_id") or payload.get("trade_id") or payload.get("id"),
                "result": payload.get("result"),
                "pnl_r": payload.get("pnl_r"),
                "message": "duplicate outcome ignored"
            },
        )

    return JSONResponse(
        status_code=409,
        content={"accepted": False, "error": msg},
    )



V7000_MARKETS = [
    "US100", "US500", "US30", "GER40", "FRA40", "FTSE100",
    "XAUUSD", "USOIL", "USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD",
]

V7000_NEWS = NewsEngine("config.v7000.yaml")
V7000_DECIDER = DecisionEngine("data/v7000_learning.sqlite3")


class Signal(BaseModel):
    # === V7000 V2.9 EXTRA FIELDS ===
    setup_quality: str | None = None
    trade_bias: str | None = None
    bias_gate: str | None = None
    htf_bias: str | None = None
    internal_structure_state: str | None = None
    liquidity_state: str | None = None
    bpr_state: str | None = None
    premium_discount_state: str | None = None
    delta_state: str | None = None
    cvd_state: str | None = None
    absorption_state: str | None = None
    reversal_state: str | None = None
    entry_state: str | None = None
    chase_state: str | None = None
    impulse_state: str | None = None
    wait_reason: str | None = None
    rel_volume: float | None = None
    zone_score: float | None = None
    htf_score: float | None = None
    technical_score: float | None = None

    client_trade_id: Optional[str] = None
    trade_id: Optional[str] = None
    id: Optional[str] = None
    # Normal TradingView entry alert fields.
    # They are optional so the same /webhook/tradingview endpoint can also accept
    # outcome/exit alerts without market/side/price.
    market: Optional[str] = None
    side: Optional[str] = None
    price: Optional[float] = None
    trigger: Optional[str] = None
    timeframe: Optional[str] = None
    trend_state: Optional[str] = None
    volume_state: Optional[str] = None
    vwap_state: Optional[str] = None
    structure_state: Optional[str] = None
    session: Optional[str] = None
    atr: Optional[float] = 50
    smc_score: Optional[float] = 0
    vp_score: Optional[float] = 0
    delta_score: Optional[float] = 0
    risk_score: Optional[float] = 50
    # V7000 optional fields. TradingView alerts can include these later.
    sl: Optional[float] = None
    tp1: Optional[float] = None
    setup_name: Optional[str] = None
    # V7000 outcome/exit alert fields.
    type: Optional[str] = None          # "outcome", "exit", "close", "result"
    decision_id: Optional[int] = None
    result: Optional[str] = None        # WIN / LOSS / BE / CANCELLED
    pnl_r: Optional[float] = None
    exit_price: Optional[float] = None
    notes: Optional[str] = ""
    force_update: Optional[bool] = False


class NewsIn(BaseModel):
    text: str


class OutcomeIn(BaseModel):
    decision_id: int
    result: str  # WIN / LOSS / BE / CANCELLED
    pnl_r: float
    exit_price: Optional[float] = None
    notes: Optional[str] = ""
    force_update: Optional[bool] = False


def _direction(side: str) -> str:
    s = (side or "").upper().strip()
    if s in {"SELL", "SHORT", "S"}:
        return "SHORT"
    return "LONG"


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _fallback_sl_tp(price: float, direction: str, atr: Optional[float], sl: Optional[float], tp1: Optional[float]) -> tuple[float, Optional[float]]:
    """Only used for V7000 logging if TradingView alert has no SL/TP yet."""
    p = _safe_float(price, 0.0)
    a = abs(_safe_float(atr, 50.0)) or 50.0
    if sl is None:
        sl = p - a if direction == "LONG" else p + a
    if tp1 is None:
        tp1 = p + a * 1.6 if direction == "LONG" else p - a * 1.6
    return _safe_float(sl, p), _safe_float(tp1, p)


def _v7000_news_snapshot(minutes: int = 90) -> Dict[str, Any]:
    # Cron updates the DB every 5 min. Here we only read the latest stored bias.
    return V7000_NEWS.market_bias(markets=V7000_MARKETS, minutes=minutes)



def _v7000_existing_outcome(decision_id: int) -> Optional[Dict[str, Any]]:
    """Return latest existing outcome for a decision_id, if already stored."""
    try:
        con = sqlite3.connect("data/v7000_learning.sqlite3")
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT outcome_id, decision_id, result, pnl_r, exit_price, notes, closed_at
            FROM trade_outcomes
            WHERE decision_id = ?
            ORDER BY outcome_id DESC
            LIMIT 1
            """,
            (int(decision_id),),
        ).fetchone()
        con.close()
        return dict(row) if row else None
    except Exception as exc:
        return {"error": str(exc)}


def _v7000_delete_outcomes(decision_id: int) -> int:
    """Delete existing outcomes for a decision_id. Used only with force_update=true."""
    con = sqlite3.connect("data/v7000_learning.sqlite3")
    cur = con.execute("DELETE FROM trade_outcomes WHERE decision_id = ?", (int(decision_id),))
    deleted = cur.rowcount or 0
    con.commit()
    con.close()
    return deleted


def _format_v7000_duplicate_outcome_telegram(decision_id: int, existing: Dict[str, Any], incoming_result: str, incoming_pnl_r: float) -> str:
    return (
        f"⚠️ V7000 DUPLICATE OUTCOME IGNORED\n"
        f"Decision ID: {decision_id}\n"
        f"Existing: {existing.get('result')} | R {existing.get('pnl_r')} | Outcome ID {existing.get('outcome_id')}\n"
        f"Incoming: {incoming_result} | R {incoming_pnl_r}\n"
        f"Use force_update:true only if you really want to replace it."
    )


def _v7000_decide_from_signal(s: Signal, old_decision: Dict[str, Any]) -> Dict[str, Any]:
    m = s.market.upper()
    direction = _direction(s.side)
    technical_score = _safe_float(old_decision.get("score"), 0.0)
    sl, tp1 = _fallback_sl_tp(s.price, direction, s.atr, s.sl, s.tp1)
    setup_name = s.setup_name or s.trigger or old_decision.get("trigger") or "V6000_WEBHOOK_SIGNAL"
    session = s.session or "UNKNOWN"
    timeframe = s.timeframe or "UNKNOWN"
    news_snapshot = _v7000_news_snapshot(minutes=90)
    return V7000_DECIDER.build_decision(
        market=m,
        direction=direction,
        setup_name=setup_name,
        session=session,
        timeframe=timeframe,
        entry=_safe_float(s.price),
        sl=sl,
        tp1=tp1,
        technical_score=technical_score,
        news_bias_snapshot=news_snapshot,
        features={
            "v6000_decision": old_decision,
            "trigger": s.trigger,
            "trend_state": s.trend_state,
            "volume_state": s.volume_state,
            "vwap_state": s.vwap_state,
            "structure_state": s.structure_state,
            "smc_score": s.smc_score,
            "vp_score": s.vp_score,
            "delta_score": s.delta_score,
            "risk_score": s.risk_score,
        },
    )


def _format_v7000_telegram(s: Signal, old_d: Dict[str, Any], v7: Dict[str, Any]) -> str:
    market_news = v7.get("news_market") or {}
    news_bias = market_news.get("bias", "neutral")
    event_risk = market_news.get("event_risk", "normal")
    learned = v7.get("learned") or {}
    return (
        f"🤖 V7000 SETUP\n"
        f"Market: {s.market.upper()} {v7.get('direction')}\n"
        f"Price: {s.price}\n"
        f"Trigger: {s.trigger}\n"
        f"V6000 Score: {old_d.get('score')}/100 {old_d.get('grade')}\n"
        f"V7000 Confidence: {v7.get('base_confidence')} → {learned.get('adjusted_confidence')}\n"
        f"News: {v7.get('news_score')} | {news_bias} | risk={event_risk}\n"
        f"Learning adj: {learned.get('adjustment')}\n"
        f"Risk R: {v7.get('risk_r')}\n"
        f"Decision: {'ALLOW' if v7.get('allow_trade') else 'BLOCK'}\n"
        f"Reason: {v7.get('reason')}"
    )


def _format_v7000_block_telegram(s: Signal, old_d: Dict[str, Any], v7: Dict[str, Any]) -> str:
    return (
        f"⛔ V7000 BLOCKED\n"
        f"Market: {s.market.upper()} {v7.get('direction')}\n"
        f"Price: {s.price}\n"
        f"Trigger: {s.trigger}\n"
        f"V6000 would allow: {old_d.get('score')}/100 {old_d.get('grade')}\n"
        f"News score: {v7.get('news_score')}\n"
        f"Confidence: {v7.get('base_confidence')} → {(v7.get('learned') or {}).get('adjusted_confidence')}\n"
        f"Reason: {v7.get('reason')}"
    )


def _format_v7000_outcome_telegram(decision_id: int, result: str, pnl_r: float, exit_price: Optional[float], notes: str, outcome_id: int) -> str:
    icon = "✅" if result == "WIN" else "❌" if result == "LOSS" else "⚪"
    return (
        f"{icon} V7000 OUTCOME STORED\n"
        f"Decision ID: {decision_id}\n"
        f"Result: {result}\n"
        f"PnL R: {pnl_r}\n"
        f"Exit: {exit_price}\n"
        f"Outcome ID: {outcome_id}\n"
        f"Notes: {notes or '-'}"
    )


@app.get("/health")
def health():
    return {"status": "online", "version": "TradingBot V6000 + V7000 Decision Layer", "markets": len(WATCHLIST), "trades": len(TRADES)}


@app.post("/webhook/tradingview")
async def webhook(s: Signal):
    # === V7000 V2.9 PINE COMPATIBILITY PATCH ===
    # Pine V2.9 sends rich states like trend_up / bos_bull / internal_mss_bear.
    # Server scoring expects compact states like trend / bos / mss.
    try:
        _d = s.model_dump() if hasattr(s, "model_dump") else s.dict()
        _side = str(_d.get("side") or "").upper().strip()
        _trend_raw = str(_d.get("trend_state") or "").lower().strip()
        _structure_raw = str(_d.get("structure_state") or "").lower().strip()
        _volume_raw = str(_d.get("volume_state") or "").lower().strip()
        _vwap_raw = str(_d.get("vwap_state") or "").lower().strip()

        if _trend_raw in ("trend_up", "trend_down", "trend"):
            _d["trend_state"] = "trend"
        elif _trend_raw.startswith("mixed"):
            _d["trend_state"] = "mixed"
        elif _trend_raw in ("range", "neutral", ""):
            _d["trend_state"] = "range"

        # === V7000 V2.9 SCORE BOOST PATCH ===
        # For the old V6000 scorer, MSS is a valid structure break.
        # We keep the original trigger name, but normalize structure_state to "bos"
        # so V2.9 reversal / MSS setups are not scored like weak mixed signals.
        if "mss" in _structure_raw or "bos" in _structure_raw or _structure_raw in ("bullish", "bearish", "internal_bullish", "internal_bearish"):
            _d["structure_state"] = "bos"

        if _volume_raw in ("spike", "above_avg"):
            _d["volume_state"] = "spike"
        elif _volume_raw in ("low", "normal"):
            _d["volume_state"] = _volume_raw

        if _vwap_raw not in ("above", "below"):
            if _side == "BUY" and _trend_raw == "trend_up":
                _d["vwap_state"] = "above"
            elif _side == "SELL" and _trend_raw == "trend_down":
                _d["vwap_state"] = "below"

        # === V7000 V2.9 SCORE BOOST PATCH ===
        # Pine V2.9 already filters entries with SMC, Entry-Zone, Countertrend-Gate,
        # No-Chase and Reversal logic. If such a setup reaches the server, the old
        # scorer must not kill it only because trend_state is "mixed".
        def _to_float(v, default=0.0):
            try:
                if v is None:
                    return default
                return float(v)
            except Exception:
                return default

        _trigger_raw = str(_d.get("trigger") or "").upper().strip()
        _vwap_now = str(_d.get("vwap_state") or "").lower().strip()
        _smc = _to_float(_d.get("smc_score"))
        _vp = _to_float(_d.get("vp_score"))
        _delta = _to_float(_d.get("delta_score"))

        _is_v29_setup = any(x in _trigger_raw for x in [
            "REVERSAL",
            "MSS",
            "BOS",
            "FVG_RETEST",
            "OB_RETEST",
            "BPR_RETEST",
            "SWEEP",
            "ABSORPTION"
        ])

        _is_quality_v29 = (
            _is_v29_setup
            and _smc >= 70.0
            and _vp >= 50.0
            and _delta >= 50.0
        )

        if _is_quality_v29:
            if _side == "BUY" and _vwap_now == "above":
                _d["trend_state"] = "trend"
            elif _side == "SELL" and _vwap_now == "below":
                _d["trend_state"] = "trend"

            if str(_d.get("volume_state") or "").lower().strip() in ("above_avg", "normal"):
                _d["volume_state"] = "spike"

        s = Signal(**_d)
    except Exception as _compat_e:
        try:
            print("V7000_V29_COMPAT_ERROR:", _compat_e)
        except Exception:
            pass

    # V7000 Outcome Router:
    # TradingView can send exit alerts to the SAME webhook.
    # Supported:
    # A) {"type":"outcome","decision_id":17,"result":"WIN","pnl_r":1.5}
    # B) {"type":"outcome","client_trade_id":"US100_1H_...","result":"WIN","pnl_r":1.5}
    alert_type = (s.type or "").lower().strip()
    if alert_type in {"outcome", "exit", "close", "result"}:
        client_trade_id = getattr(s, "client_trade_id", None) or getattr(s, "trade_id", None) or getattr(s, "id", None)

        # === V7000 SIGNAL AUDIT PATCH ===
        # Speichert jedes finale Webhook-Signal mit ALLOW/BLOCK und Grund.
        try:
            import sqlite3 as _sqlite3
            import json as _json
            from datetime import datetime as _dt, timezone as _tz
        
            _learned = v7.get("learned") or {}
            _confidence = _learned.get("adjusted_confidence", v7.get("base_confidence", 0))
            _nm = v7.get("news_market") or {}
            _event_risk = str(_nm.get("event_risk") or "normal")
            _reason = str(v7.get("reason") or "")
        
            _raw = {
                "signal": s.model_dump() if hasattr(s, "model_dump") else s.dict(),
                "v6000": old_d,
                "v7000": v7,
            }
        
            with _sqlite3.connect("data/v7000_learning.sqlite3") as _con:
                _con.execute("""
                CREATE TABLE IF NOT EXISTS signal_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    client_trade_id TEXT,
                    market TEXT,
                    direction TEXT,
                    trigger TEXT,
                    timeframe TEXT,
                    entry REAL,
                    technical_score REAL,
                    confidence REAL,
                    news_score REAL,
                    event_risk TEXT,
                    risk_r REAL,
                    allow_trade INTEGER,
                    reason TEXT,
                    bias_gate TEXT,
                    entry_state TEXT,
                    chase_state TEXT,
                    impulse_state TEXT,
                    raw_json TEXT
                )
                """)
                _con.execute("""
                INSERT INTO signal_audit (
                    created_at, client_trade_id, market, direction, trigger, timeframe,
                    entry, technical_score, confidence, news_score, event_risk, risk_r,
                    allow_trade, reason, bias_gate, entry_state, chase_state, impulse_state, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    _dt.now(_tz.utc).isoformat(),
                    client_trade_id,
                    m,
                    v7.get("direction") or str(getattr(s, "side", "")).upper(),
                    getattr(s, "trigger", None),
                    getattr(s, "timeframe", None),
                    getattr(s, "price", None),
                    v7.get("technical_score"),
                    _confidence,
                    v7.get("news_score"),
                    _event_risk,
                    v7.get("risk_r"),
                    1 if trade_allowed else 0,
                    _reason,
                    getattr(s, "bias_gate", None),
                    getattr(s, "entry_state", None),
                    getattr(s, "chase_state", None),
                    getattr(s, "impulse_state", None),
                    _json.dumps(_raw, ensure_ascii=False, default=str),
                ))
                _con.commit()
        except Exception as _audit_e:
            try:
                v7["signal_audit_error"] = str(_audit_e)
            except Exception:
                pass


        decision_id = s.decision_id
        if decision_id is None and client_trade_id:
            found_trade = v7000_find_decision_by_client_trade_id(client_trade_id)
            if found_trade:
                decision_id = int(found_trade["decision_id"])
            else:
                return {
                    "accepted": False,
                    "type": "outcome",
                    "stored": False,
                    "error": "unknown client_trade_id",
                    "client_trade_id": client_trade_id,
                }

        if decision_id is None:
            return {
                "accepted": False,
                "type": "outcome",
                "stored": False,
                "error": "missing decision_id or client_trade_id",
            }

        result = (s.result or "").upper().strip()
        if result not in {"WIN", "LOSS", "BE", "BREAKEVEN", "CANCELLED", "CANCELED"}:
            return {"accepted": False, "type": "outcome", "error": "result must be WIN, LOSS, BE, or CANCELLED"}

        if result == "BREAKEVEN":
            result = "BE"
        if result == "CANCELED":
            result = "CANCELLED"

        pnl_r = _safe_float(s.pnl_r, 0.0)

        existing = _v7000_existing_outcome(decision_id)
        if existing and existing.get("outcome_id") and not bool(s.force_update):
            await send_telegram(_format_v7000_duplicate_outcome_telegram(decision_id, existing, result, pnl_r))
            return {
                "accepted": True,
                "type": "outcome",
                "stored": False,
                "duplicate": True,
                "decision_id": decision_id,
                "client_trade_id": client_trade_id,
                "existing_outcome": existing,
                "incoming": {
                    "result": result,
                    "pnl_r": pnl_r,
                    "exit_price": s.exit_price,
                    "notes": s.notes or "",
                },
                "message": "duplicate outcome ignored",
            }

        replaced = 0
        if existing and existing.get("outcome_id") and bool(s.force_update):
            replaced = _v7000_delete_outcomes(decision_id)

        outcome_id = V7000_DECIDER.learning.remember_outcome(
            decision_id=decision_id,
            result=result,
            pnl_r=pnl_r,
            exit_price=s.exit_price,
            notes=s.notes or "TradingView outcome alert",
        )

        if client_trade_id:
            v7000_close_open_trade(client_trade_id)

        await send_telegram(_format_v7000_outcome_telegram(decision_id, result, pnl_r, s.exit_price, s.notes or "", outcome_id))

        return {
            "accepted": True,
            "type": "outcome",
            "stored": True,
            "duplicate": False,
            "replaced": replaced,
            "decision_id": decision_id,
            "client_trade_id": client_trade_id,
            "outcome_id": outcome_id,
            "result": result,
            "pnl_r": pnl_r,
        }

    # Entry alerts still require these fields.
    if not s.market or not s.side or s.price is None or not s.trigger:
        return {"accepted": False, "type": "entry", "error": "missing one of required fields: market, side, price, trigger"}

    m = s.market.upper()
    tech = 0
    reasons = []

    if s.trend_state and "trend" in s.trend_state:
        tech += 25
        reasons.append("Trend")
    if s.volume_state and "spike" in s.volume_state:
        tech += 15
        reasons.append("Volume")
    if s.vwap_state and "above" in s.vwap_state:
        tech += 15
        reasons.append("VWAP")
    if s.structure_state and s.structure_state.lower() in ["bos", "choch"]:
        tech += 20
        reasons.append("Struktur")
    if s.timeframe in ["15m", "1h"]:
        tech += 15
        reasons.append("Timeframe")

    STATE[m] = {
        **STATE.get(m, {"bias": s.side.upper()}),
        "price": s.price,
        "trigger": s.trigger,
        "tech_score": min(100, tech),
        "smc_score": s.smc_score,
        "vp_score": s.vp_score,
        "delta_score": s.delta_score,
        "risk_score": s.risk_score,
        "reasons": reasons,
    }

    old_d = decide(m)
    v7 = _v7000_decide_from_signal(s, old_d)

    # V7000 EVENT RISK HARD BLOCK PATCH
    # Wenn Kalender/News für diesen Markt event_risk=high meldet,
    # darf kein neuer Trade erlaubt werden, egal wie gut Technik/Score ist.
    try:
        _nm = v7.get("news_market") or {}
        _event_risk = str(_nm.get("event_risk") or "normal").lower().strip()

        # === V7000 NEWS FALSE POSITIVE GUARD PATCH ===
        # Verhindert, dass Börsen-/Business-Phrasen wie "war on AI disruption"
        # als echter Krieg/Geopolitik-Hardblock gewertet werden.
        try:
            _heads = _nm.get("top_headlines") or []
            _titles = " | ".join([str((h or {}).get("title") or "").lower() for h in _heads])
        
            _false_war_terms = [
                "war on ai",
                "war on disruption",
                "war on inflation",
                "price war",
                "bidding war",
                "streaming war",
                "console war",
                "talent war",
                "cola war",
                "ratings war",
                "winning the war on ai",
                "war for talent",
            ]
        
            _true_risk_terms = [
                "iran", "israel", "ukraine", "russia", "taiwan", "nato",
                "missile", "strike", "airstrike", "attack", "ceasefire",
                "nuclear", "sanction", "tariff", "trade war",
                "fed", "fomc", "powell", "cpi", "nfp", "jobs",
                "inflation", "rate cut", "rate hike", "ecb", "boe", "boj", "rba"
            ]
        
            _false_hit = any(x in _titles for x in _false_war_terms)
            _true_hit = any(x in _titles for x in _true_risk_terms)
        
            if _event_risk == "high" and _false_hit and not _true_hit:
                _event_risk = "normal"
                _nm["event_risk"] = "normal"
                v7["news_market"] = _nm
                v7["news_event_risk_downgraded"] = "false_positive_guard"
                v7["news_event_risk_original"] = "high"
        except Exception as _news_fp_e:
            v7["news_false_positive_guard_error"] = str(_news_fp_e)

        if _event_risk == "high":
            v7["allow_trade"] = False
            v7["event_risk_blocked"] = True
            v7["reason"] = "BLOCK: event_risk=high. Calendar/news risk active; no new trade allowed."
    except Exception as _e:
        v7["event_risk_guard_error"] = str(_e)

    # === V7000 V2.9 QUALITY ALLOW PATCH ===

    # Kontrollierte Freigabe für saubere Pine V2.9 Signale.

    # Nicht globaler Threshold-Drop, sondern nur wenn Pine-Filter sauber sind.

    try:

        def _flt(v, default=0.0):

            try:

                if v is None:

                    return default

                return float(v)

            except Exception:

                return default


        def _str(v):

            return str(v or "").lower().strip()


        _side = _str(getattr(s, "side", ""))

        _trigger = str(getattr(s, "trigger", "") or "").upper().strip()


        _entry_state = _str(getattr(s, "entry_state", ""))

        _chase_state = _str(getattr(s, "chase_state", ""))

        _impulse_state = _str(getattr(s, "impulse_state", ""))

        _bias_gate = _str(getattr(s, "bias_gate", ""))

        _vwap_state = _str(getattr(s, "vwap_state", ""))


        _smc = _flt(getattr(s, "smc_score", 0))

        _vp = _flt(getattr(s, "vp_score", 0))

        _delta = _flt(getattr(s, "delta_score", 0))


        _conf = _flt((v7.get("learned") or {}).get("adjusted_confidence", v7.get("base_confidence", 0)))

        _nm = v7.get("news_market") or {}

        _event_risk = _str(_nm.get("event_risk", "normal"))


        _is_v29_trigger = any(x in _trigger for x in [

            "REVERSAL",

            "MSS",

            "BOS",

            "FVG_RETEST",

            "OB_RETEST",

            "BPR_RETEST",

            "SWEEP",

            "ABSORPTION"

        ])


        _direction_ok = (

            (_side in ("buy", "long") and _vwap_state == "above") or

            (_side in ("sell", "short") and _vwap_state == "below")

        )


        _pine_filters_ok = (

            _entry_state == "entry_ok"

            and _chase_state == "ok"

            and _impulse_state == "ok"

            and "blocked" not in _bias_gate

        )


        _quality_ok = (

            _is_v29_trigger

            and _conf >= 50.0

            and _smc >= 75.0

            and _vp >= 50.0

            and _delta >= 50.0

            and _pine_filters_ok

            and _direction_ok

            and _event_risk != "high"

        )


        if (not bool(v7.get("allow_trade"))) and _quality_ok:

            v7["allow_trade"] = True

            v7["v29_quality_allow"] = True


            if _conf >= 60:

                v7["risk_r"] = max(_flt(v7.get("risk_r")), 0.75)

            elif _conf >= 55:

                v7["risk_r"] = max(_flt(v7.get("risk_r")), 0.50)

            else:

                v7["risk_r"] = max(_flt(v7.get("risk_r")), 0.25)


            v7["reason"] = (

                f"ALLOW V2.9 QUALITY: confidence {_conf:.1f} >= 50.0, "

                f"SMC {_smc:.1f}, VP {_vp:.1f}, Delta {_delta:.1f}, "

                f"entry={_entry_state}, chase={_chase_state}, impulse={_impulse_state}, "

                f"bias_gate={_bias_gate}, event_risk={_event_risk}."

            )

    except Exception as _e:

        v7["v29_quality_allow_error"] = str(_e)


    old_allowed = old_d.get("decision") == "TRADE_ALLOWED"
    v7_allowed = bool(v7.get("allow_trade"))
    # V7000 FINAL ROUTER:
    # V7000 entscheidet final. V6000 bleibt als Analyse/Score im Response erhalten,
    # blockiert aber nicht mehr, wenn V7000 das Setup erlaubt.
    trade_allowed = v7_allowed

    client_trade_id = getattr(s, "client_trade_id", None) or getattr(s, "trade_id", None) or getattr(s, "id", None)

    # === V7239 SOFT LIVE GATE HOOK ===
    try:
        from app.v7239_soft_live_gate_enforcement import v7239_apply_soft_gate
        v7 = v7239_apply_soft_gate(s, v7, client_trade_id=client_trade_id, old_d=old_d)
        v7_allowed = bool(v7.get("allow_trade"))
        trade_allowed = v7_allowed
    except Exception as _v7239_e:
        try:
            v7["v7239_soft_gate_error"] = str(_v7239_e)
        except Exception:
            pass
    # === END V7239 SOFT LIVE GATE HOOK ===

    # V8004_QUALITY_ROUTER_HOOK_BEGIN
    try:
        from app.v8004_quality_router import v8004_apply_quality_router
        v7 = v8004_apply_quality_router(
            s, v7, client_trade_id=client_trade_id, old_d=old_d
        )
        v7_allowed = bool(v7.get("allow_trade"))
        trade_allowed = v7_allowed
    except Exception as _v8004_e:
        try:
            v7["v8004_quality_router_error"] = str(_v8004_e)
        except Exception:
            pass
    # V8004_QUALITY_ROUTER_HOOK_END


    
    # === V7000 SHADOW TRADE HELPER PATCH ===
    def _v7000_store_shadow_trade(_client_trade_id, _market, _direction, _setup_name, _entry, _sl, _tp1, _reason, _confidence, _raw_json):
        try:
            import sqlite3 as _sh_sqlite3
            from datetime import datetime as _sh_dt, timezone as _sh_tz

            if not _client_trade_id:
                return False

            _shadow_id = "SHADOW_" + str(_client_trade_id)

            with _sh_sqlite3.connect("data/v7000_learning.sqlite3") as _sh_con:
                _sh_con.execute("""
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    shadow_id TEXT PRIMARY KEY,
                    client_trade_id TEXT,
                    market TEXT,
                    direction TEXT,
                    setup_name TEXT,
                    entry REAL,
                    sl REAL,
                    tp1 REAL,
                    confidence REAL,
                    reason TEXT,
                    raw_json TEXT,
                    status TEXT,
                    opened_at TEXT
                )
                """)

                _sh_con.execute("""
                CREATE TABLE IF NOT EXISTS shadow_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shadow_id TEXT,
                    client_trade_id TEXT,
                    market TEXT,
                    direction TEXT,
                    setup_name TEXT,
                    result TEXT,
                    pnl_r REAL,
                    exit_price REAL,
                    notes TEXT,
                    closed_at TEXT
                )
                """)

                _exists = _sh_con.execute(
                    "SELECT shadow_id FROM shadow_trades WHERE shadow_id=? LIMIT 1",
                    (_shadow_id,)
                ).fetchone()

                if _exists:
                    return False

                _sh_con.execute("""
                INSERT INTO shadow_trades (
                    shadow_id, client_trade_id, market, direction, setup_name,
                    entry, sl, tp1, confidence, reason, raw_json, status, opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """, (
                    _shadow_id,
                    str(_client_trade_id),
                    str(_market),
                    str(_direction),
                    str(_setup_name),
                    float(_entry or 0),
                    float(_sl or 0),
                    float(_tp1 or 0),
                    float(_confidence or 0),
                    str(_reason or ""),
                    str(_raw_json or "{}"),
                    _sh_dt.now(_sh_tz.utc).isoformat(),
                ))

                _sh_con.commit()
                return True
        except Exception as _sh_e:
            try:
                v7["shadow_store_error"] = str(_sh_e)
            except Exception:
                pass
            return False

    # === V7000 SHADOW TRADE HELPER PATCH END ===


    # === V7000 CLUSTER GUARD PATCH START ===
    # Regel:
    # - Kein globales Open-Trade-Limit
    # - Maximal 2 offene Trades pro Cluster
    # - Wenn Cluster voll ist: echter Trade wird blockiert, Signal kann aber als Shadow weiterlaufen
    try:
        import sqlite3 as _cg_sqlite3
        import os as _cg_os

        _V7000_CLUSTER_MAX_OPEN = 2

        def _v7000_cluster_name(_market):
            _m = str(_market or "").upper().strip()

            # Forex JPY Cluster zuerst, damit USDJPY nicht in USD fällt
            if _m.endswith("JPY") or _m in ("USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY"):
                return "JPY_FX"

            # USD Forex Majors
            if _m in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF"):
                return "USD_FX"

            # US Indices
            if _m in ("US100", "NAS100", "NASDAQ", "NQ", "MNQ", "US500", "SPX", "SP500", "ES", "MES", "US30", "DOW", "YM", "MYM"):
                return "US_INDEX"

            # EU/UK Indices
            if _m in ("GER40", "DAX", "DE40", "FRA40", "CAC40", "EU50", "STOXX50"):
                return "EU_INDEX"

            if _m in ("FTSE100", "UK100"):
                return "UK_INDEX"

            # Metals / Crypto / Oil
            if _m in ("XAUUSD", "GOLD", "GC", "MGC", "XAGUSD", "SILVER"):
                return "METALS"

            if _m in ("BTCUSD", "BTCUSDT", "ETHUSD", "ETHUSDT", "BTC", "ETH"):
                return "CRYPTO"

            if _m in ("OIL", "USOIL", "UKOIL", "WTI", "BRENT", "CL", "MCL"):
                return "OIL"

            return "SINGLE_" + _m

        def _v7000_cluster_db_path():
            for _p in ("/app/data/v7000_learning.sqlite3", "data/v7000_learning.sqlite3"):
                if _cg_os.path.exists(_p):
                    return _p
            return "/app/data/v7000_learning.sqlite3"

        def _v7000_open_cluster_count(_market):
            _cluster = _v7000_cluster_name(_market)
            _db = _v7000_cluster_db_path()
            _con = _cg_sqlite3.connect(_db, timeout=15)
            _con.row_factory = _cg_sqlite3.Row
            try:
                _rows = _con.execute("""
                    SELECT market, direction, setup_name, opened_at
                    FROM open_trades
                    WHERE status='OPEN'
                    ORDER BY opened_at DESC
                """).fetchall()
            finally:
                _con.close()

            _same = []
            for _r in _rows:
                _rm = str(_r["market"] or "").upper().strip()
                if _v7000_cluster_name(_rm) == _cluster:
                    _same.append(dict(_r))

            return _cluster, len(_same), _same

        try:
            _cg_market = str((v7.get("market") if isinstance(v7, dict) else "") or (s.get("market") if isinstance(s, dict) else "") or m or "").upper().strip()
        except Exception:
            _cg_market = str(locals().get("m", "") or "").upper().strip()

        if bool(locals().get("trade_allowed", False)) and _cg_market:
            _cg_cluster, _cg_count, _cg_same = _v7000_open_cluster_count(_cg_market)

            if _cg_count >= _V7000_CLUSTER_MAX_OPEN:
                trade_allowed = False
                try:
                    v7_allowed = False
                except Exception:
                    pass

                try:
                    v7["cluster_guard_blocked"] = True
                    v7["cluster_name"] = _cg_cluster
                    v7["cluster_open_count"] = _cg_count
                    v7["cluster_max_open"] = _V7000_CLUSTER_MAX_OPEN
                    v7["cluster_open_markets"] = ",".join([str(x.get("market")) for x in _cg_same])
                    _old_reason = str(v7.get("reason") or "")
                    _extra_reason = (
                        f" BLOCK CLUSTER_GUARD: cluster {_cg_cluster} already has "
                        f"{_cg_count} open trades, max {_V7000_CLUSTER_MAX_OPEN}. "
                        f"Open markets: {v7.get('cluster_open_markets')}."
                    )
                    v7["reason"] = (_old_reason + _extra_reason).strip()
                except Exception:
                    pass

                try:
                    print(
                        f"V7000_CLUSTER_GUARD_BLOCK market={_cg_market} "
                        f"cluster={_cg_cluster} open={_cg_count} max={_V7000_CLUSTER_MAX_OPEN}"
                    )
                except Exception:
                    pass
    except Exception as _cg_e:
        try:
            print("V7000_CLUSTER_GUARD_ERROR", repr(_cg_e))
        except Exception:
            pass
    # === V7000 CLUSTER GUARD PATCH END ===


    # === V7000 FINAL SIGNAL AUDIT PATCH ===

    # Speichert jede fertige Entry-Entscheidung direkt vor der Trade-Ausführung.

    try:

        import sqlite3 as _sqlite3

        import json as _json

        from datetime import datetime as _dt, timezone as _tz


        _learned = v7.get("learned") or {}

        _confidence = _learned.get("adjusted_confidence", v7.get("base_confidence", 0))

        _nm = v7.get("news_market") or {}

        _event_risk = str(_nm.get("event_risk") or "normal")

        _reason = str(v7.get("reason") or "")

        _direction = v7.get("direction") or str(getattr(s, "side", "")).upper()


        _raw = {

            "signal": s.model_dump() if hasattr(s, "model_dump") else s.dict(),

            "v6000": old_d,

            "v7000": v7,

        }


        with _sqlite3.connect("data/v7000_learning.sqlite3") as _con:

            _con.execute("""

            CREATE TABLE IF NOT EXISTS signal_audit (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                created_at TEXT NOT NULL,

                client_trade_id TEXT,

                market TEXT,

                direction TEXT,

                trigger TEXT,

                timeframe TEXT,

                entry REAL,

                technical_score REAL,

                confidence REAL,

                news_score REAL,

                event_risk TEXT,

                risk_r REAL,

                allow_trade INTEGER,

                reason TEXT,

                bias_gate TEXT,

                entry_state TEXT,

                chase_state TEXT,

                impulse_state TEXT,

                raw_json TEXT

            )

            """)

            _con.execute("""

            INSERT INTO signal_audit (

                created_at, client_trade_id, market, direction, trigger, timeframe,

                entry, technical_score, confidence, news_score, event_risk, risk_r,

                allow_trade, reason, bias_gate, entry_state, chase_state, impulse_state, raw_json

            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            """, (

                _dt.now(_tz.utc).isoformat(),

                client_trade_id,

                m,

                _direction,

                getattr(s, "trigger", None),

                getattr(s, "timeframe", None),

                getattr(s, "price", None),

                v7.get("technical_score"),

                _confidence,

                v7.get("news_score"),

                _event_risk,

                v7.get("risk_r"),

                1 if trade_allowed else 0,

                _reason,

                getattr(s, "bias_gate", None),

                getattr(s, "entry_state", None),

                getattr(s, "chase_state", None),

                getattr(s, "impulse_state", None),

                _json.dumps(_raw, ensure_ascii=False, default=str),

            ))

            _con.commit()

    except Exception as _audit_final_e:

        try:

            v7["final_signal_audit_error"] = str(_audit_final_e)

        except Exception:

            pass



    # === V7000 SHADOW TRADE STORE PATCH ===
    # Geblockte, aber technisch interessante Signale als Shadow-Trade speichern.
    # Öffnet keinen echten Trade. Dient nur zur Qualitätsprüfung der Blocks.
    try:
        import json as _sh_json

        def _sh_f(v, d=0.0):
            try:
                if v is None:
                    return d
                return float(v)
            except Exception:
                return d

        def _sh_s(v):
            return str(v or "").lower().strip()

        _sh_conf = _sh_f((v7.get("learned") or {}).get("adjusted_confidence", v7.get("base_confidence", 0)))
        _sh_smc = _sh_f(getattr(s, "smc_score", 0))
        _sh_vp = _sh_f(getattr(s, "vp_score", 0))
        _sh_delta = _sh_f(getattr(s, "delta_score", 0))
        _sh_entry_state = _sh_s(getattr(s, "entry_state", ""))
        _sh_chase_state = _sh_s(getattr(s, "chase_state", ""))
        _sh_impulse_state = _sh_s(getattr(s, "impulse_state", ""))
        _sh_bias_gate = _sh_s(getattr(s, "bias_gate", ""))
        _sh_trigger = str(getattr(s, "trigger", "") or "").upper().strip()
        _sh_event = _sh_s((v7.get("news_market") or {}).get("event_risk", "normal"))

        _sh_is_v29 = any(x in _sh_trigger for x in [
            "REVERSAL", "MSS", "BOS", "FVG_RETEST", "OB_RETEST",
            "BPR_RETEST", "SWEEP", "ABSORPTION"
        ])

        _sh_filters_clean = (
            _sh_entry_state == "entry_ok"
            and _sh_chase_state == "ok"
            and _sh_impulse_state == "ok"
            and _sh_event != "high"
        )

        # Shadow-Regel:
        # 1) Near-Miss ab 50 Confidence speichern
        # 2) Oder 45–50 speichern, wenn SMC/VP/Delta stark sind
        _sh_near_miss = (
            (not trade_allowed)
            and _sh_is_v29
            and _sh_filters_clean
            and (
                _sh_conf >= 50.0
                or (_sh_conf >= 45.0 and _sh_smc >= 75.0 and _sh_vp >= 50.0 and _sh_delta >= 50.0)
            )
        )

        if _sh_near_miss:
            _sh_raw = {
                "signal": s.model_dump() if hasattr(s, "model_dump") else s.dict(),
                "v6000": old_d,
                "v7000": v7,
                "shadow_reason": "BLOCKED_NEAR_MISS_SHADOW",
            }

            _stored_shadow = _v7000_store_shadow_trade(
                client_trade_id,
                m,
                v7.get("direction") or str(getattr(s, "side", "")).upper(),
                getattr(s, "trigger", None),
                getattr(s, "price", None),
                v7.get("sl"),
                v7.get("tp1"),
                str(v7.get("reason") or ""),
                _sh_conf,
                _sh_json.dumps(_sh_raw, ensure_ascii=False, default=str),
            )

            if _stored_shadow:
                v7["shadow_trade_stored"] = True
                v7["shadow_reason"] = "BLOCKED_NEAR_MISS_SHADOW"
    except Exception as _sh_e:
        try:
            v7["shadow_trade_error"] = str(_sh_e)
        except Exception:
            pass
    # === V7000 SHADOW TRADE STORE PATCH END ===

    if trade_allowed:
        TRADES[m] = {
            "market": m,
            "side": s.side.upper(),
            "entry": s.price,
            "trigger": s.trigger,
            "status": "OPEN",
            "v7000_decision_id": v7.get("decision_id"),
            "client_trade_id": client_trade_id,
        }

        if client_trade_id:
            v7000_store_open_trade(client_trade_id, v7)

        await send_telegram(_format_v7000_telegram(s, old_d, v7))
    # V8004_SHADOW_TELEGRAM_BRANCH_BEGIN
    elif v7.get("v8004_route") == "SHADOW":
        try:
            from app.v8004_quality_router import v8004_shadow_telegram
            await send_telegram(v8004_shadow_telegram(s, v7))
        except Exception as _v8004_tg_e:
            try:
                v7["v8004_shadow_telegram_error"] = str(_v8004_tg_e)
            except Exception:
                pass
    # V8004_SHADOW_TELEGRAM_BRANCH_END
    elif v7.get("event_risk_blocked") or v7.get("cluster_guard_blocked") or (old_allowed and not v7_allowed):
        # V7000 blocks because of news/calendar/learning/risk.
        await send_telegram(_format_v7000_block_telegram(s, old_d, v7))

    return {
        "accepted": True,
        "trade_allowed": trade_allowed,
        "client_trade_id": client_trade_id,
        "v6000": old_d,
        "v7000": v7,
    }


@app.post("/news")
def news(n: NewsIn):
    return add_news(n.text)


@app.get("/v7000/news/bias")
def v7000_news_bias(minutes: int = 90):
    return _v7000_news_snapshot(minutes=minutes)


@app.post("/v7000/outcome")
def v7000_outcome(o: OutcomeIn):
    existing = _v7000_existing_outcome(o.decision_id)
    if existing and existing.get("outcome_id") and not bool(o.force_update):
        return {
            "stored": False,
            "duplicate": True,
            "decision_id": o.decision_id,
            "existing_outcome": existing,
            "incoming": {"result": o.result, "pnl_r": o.pnl_r, "exit_price": o.exit_price, "notes": o.notes or ""},
        }
    replaced = 0
    if existing and existing.get("outcome_id") and bool(o.force_update):
        replaced = _v7000_delete_outcomes(o.decision_id)
    outcome_id = V7000_DECIDER.learning.remember_outcome(
        decision_id=o.decision_id,
        result=o.result,
        pnl_r=o.pnl_r,
        exit_price=o.exit_price,
        notes=o.notes or "",
    )
    return {"stored": True, "duplicate": False, "replaced": replaced, "outcome_id": outcome_id, "decision_id": o.decision_id}


@app.post("/history/upload/{market}/{tf}")
async def history_upload(market: str, tf: str, file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8", "ignore")
    return upload_csv(market, tf, text)


@app.get("/backtest/{market}/{tf}")
def bt(market: str, tf: str):
    return backtest(market, tf)


@app.get("/scan")
def scan():
    return {"version": "V6000+V7000", "scan": scan_all()}


@app.post("/telegram")
async def telegram(cmd: NewsIn):
    text = cmd.text.lower().strip()
    if text in ["/scan", "scan"]:
        rows = scan_all()[:10]
        msg = "📡 V7000 SCAN\n" + "\n".join([f"{i+1}. {r['market']} {r['bias']} | {r['score']}/100 | {r['grade']} | {r['decision']} | {r['trigger']}" for i, r in enumerate(rows)])
    elif text in ["/news", "news"]:
        rows = NEWS[:8]
        msg = "📰 V7000 NEWS\n" + "\n".join([f"{i+1}. {r.get('headline_de', r.get('text', ''))}" for i, r in enumerate(rows)])
    elif text in ["/risk", "risk"]:
        r = risk_status()
        msg = f"🛡️ V7000 RISK\nBalance: {r['balance']}\nRisk/Trade: {r['risk_per_trade']}%\nOpen Trades: {r['open_trades']}\nMax Trades: {r['max_trades']}"
    elif text in ["/v7000", "v7000", "/bias", "bias"]:
        snap = _v7000_news_snapshot(minutes=90)
        mk = snap.get("markets", {})
        keys = ["US100", "US500", "US30", "GER40", "XAUUSD", "USOIL", "USD", "EUR", "GBP", "JPY", "CAD"]
        msg = "🧠 V7000 NEWS BIAS\n" + "\n".join([f"{k}: {mk.get(k, {}).get('score', 0)} | {mk.get(k, {}).get('bias', 'neutral')} | risk={mk.get(k, {}).get('event_risk', 'normal')}" for k in keys])
    else:
        msg = "Befehle: /scan /news /risk /v7000"
    return await send_telegram(msg)


@app.get("/terminal", response_class=HTMLResponse)
def terminal():
    rows = scan_all()[:20]
    news_html = "".join([f"<div class='news'><b>⭐ {n.get('headline_de', n.get('text', ''))}</b><br><small>{n.get('time', '')}</small></div>" for n in NEWS[:12]])
    table = "".join([f"<tr><td>{r['market']}</td><td>{r['bias']}</td><td>{r['score']}</td><td>{r['grade']}</td><td>{r['decision']}</td><td>{r['trigger']}</td></tr>" for r in rows])
    return f"""
    <html><head><title>TradingBot V7000</title><style>
    body{{font-family:Arial;background:#07111f;color:#e8f1ff;padding:24px}}
    .card{{background:#101d33;border-radius:14px;padding:18px;margin-bottom:18px}}
    table{{width:100%;border-collapse:collapse}} td,th{{padding:10px;border-bottom:1px solid #263854;text-align:left}}
    th{{color:#8ec5ff}} .wait{{color:#ffd166}} .news{{margin:14px 0}}
    </style></head><body>
    <h1>TradingBot V6000 + V7000 Decision Layer</h1>
    <div class='card'><b>Status:</b> Online<br><b>Märkte:</b> {len(WATCHLIST)}<br><b>Offene Trades:</b> {len(TRADES)}</div>
    <div class='card'><h2>Institutional Scan</h2><table><tr><th>Market</th><th>Bias</th><th>Score</th><th>Grade</th><th>Entscheidung</th><th>Trigger</th></tr>{table}</table></div>
    <div class='card'><h2>Deutsche News AI</h2>{news_html or 'Keine News geladen'}</div>
    <div class='card'><h2>Learning Engine</h2>History Files: {len(HISTORY)} | Learned Setups: {len(LEARNING)} | Backtests: {len(BACKTESTS)}</div>
    </body></html>
    """


# ============================================================
# V7000 DASHBOARD ROUTES PATCH
# ============================================================

def _v7_html_escape(x):
    import html
    return html.escape(str(x if x is not None else ""))

def _v7_status_badge(text):
    t = str(text or "").lower()
    if "allow" in t or "online" in t or "bullish" in t:
        c = "#22c55e"
    elif "block" in t or "bearish" in t or "loss" in t:
        c = "#ef4444"
    elif "wait" in t or "neutral" in t:
        c = "#facc15"
    else:
        c = "#94a3b8"
    return f"<span style='color:{c};font-weight:700'>{_v7_html_escape(text)}</span>"

def _v7_base_html(title, body):
    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_v7_html_escape(title)}</title>
<style>
body {{
    margin:0;
    font-family: Arial, sans-serif;
    background:#07111f;
    color:#e8f1ff;
}}
.wrap {{
    padding:18px;
    max-width:1100px;
    margin:auto;
}}
h1,h2 {{
    margin: 12px 0;
}}
.card {{
    background:#0b1b31;
    border:1px solid #163250;
    border-radius:14px;
    padding:14px;
    margin:14px 0;
    box-shadow:0 0 12px rgba(0,0,0,.25);
}}
.grid {{
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
    gap:10px;
}}
.kpi {{
    background:#10243d;
    border-radius:12px;
    padding:12px;
}}
.kpi b {{
    display:block;
    font-size:22px;
    margin-top:6px;
}}
table {{
    width:100%;
    border-collapse:collapse;
    font-size:14px;
}}
th,td {{
    padding:8px;
    border-bottom:1px solid #203956;
    text-align:left;
}}
th {{
    color:#8fc6ff;
}}
a {{
    color:#60a5fa;
    text-decoration:none;
    font-weight:700;
}}
.small {{
    color:#9fb3c8;
    font-size:13px;
}}
pre {{
    white-space:pre-wrap;
    word-break:break-word;
    background:#06101c;
    border-radius:12px;
    padding:12px;
    overflow:auto;
}}
</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>
"""

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    rows = scan_all()
    news = _v7000_news_snapshot(minutes=90)
    markets = news.get("markets", {}) if isinstance(news, dict) else {}

    table = ""
    for r in rows[:40]:
        m = r.get("market", "-")
        nb = markets.get(m, {}) if isinstance(markets, dict) else {}
        table += "<tr>"
        table += f"<td>{_v7_html_escape(m)}</td>"
        table += f"<td>{_v7_html_escape(r.get('bias','-'))}</td>"
        table += f"<td>{_v7_html_escape(r.get('score','-'))}</td>"
        table += f"<td>{_v7_status_badge(r.get('grade','-'))}</td>"
        table += f"<td>{_v7_status_badge(r.get('decision','-'))}</td>"
        table += f"<td>{_v7_html_escape(r.get('trigger','-'))}</td>"
        table += f"<td>{_v7_status_badge(nb.get('bias','neutral'))}</td>"
        table += f"<td>{_v7_html_escape(nb.get('score',0))}</td>"
        table += "</tr>"

    body = f"""
    <h1>TradingBot V6000 + V7000 Dashboard</h1>
    <div class="small">
        <a href="/terminal">Terminal</a> ·
        <a href="/intelligence">Intelligence</a> ·
        <a href="/health">Health</a> ·
        <a href="/v7000/news/bias?minutes=240">News JSON</a>
    </div>

    <div class="grid">
        <div class="kpi">Status<b>{_v7_status_badge("online")}</b></div>
        <div class="kpi">Märkte<b>{len(WATCHLIST)}</b></div>
        <div class="kpi">Offene Trades<b>{len(TRADES)}</b></div>
        <div class="kpi">Scan Rows<b>{len(rows)}</b></div>
    </div>

    <div class="card">
        <h2>Institutional Scan + News Bias</h2>
        <table>
            <tr>
                <th>Market</th><th>Bias</th><th>Score</th><th>Grade</th>
                <th>Decision</th><th>Trigger</th><th>News</th><th>News Score</th>
            </tr>
            {table}
        </table>
    </div>
    """
    return _v7_base_html("V7000 Dashboard", body)

@app.get("/intelligence", response_class=HTMLResponse)
def intelligence():
    news = _v7000_news_snapshot(minutes=90)
    markets = news.get("markets", {}) if isinstance(news, dict) else {}

    market_cards = ""
    for m, d in markets.items():
        heads = d.get("top_headlines") or []
        head_html = ""
        for h in heads[:3]:
            title = h.get("title", "-") if isinstance(h, dict) else str(h)
            head_html += f"<div class='small'>• {_v7_html_escape(title)}</div>"
        if not head_html:
            head_html = "<div class='small'>Keine Headlines</div>"

        market_cards += f"""
        <div class="card">
            <h2>{_v7_html_escape(m)} — {_v7_status_badge(d.get('bias','neutral'))}</h2>
            <div>Score: <b>{_v7_html_escape(d.get('score',0))}</b></div>
            <div>Risk: {_v7_status_badge(d.get('event_risk','normal'))}</div>
            {head_html}
        </div>
        """

    body = f"""
    <h1>V7000 Intelligence</h1>
    <div class="small">
        <a href="/dashboard">Dashboard</a> ·
        <a href="/terminal">Terminal</a> ·
        <a href="/v7000/news/bias?minutes=240">Raw JSON</a>
    </div>

    <div class="card">
        <h2>News Engine</h2>
        <div>Lookback: {_v7_html_escape(news.get('lookback_minutes', '-'))} Minuten</div>
        <div>Generated: {_v7_html_escape(news.get('generated_at', '-'))}</div>
    </div>

    {market_cards}
    """
    return _v7_base_html("V7000 Intelligence", body)

@app.get("/webhook", response_class=HTMLResponse)
def webhook_info():
    body = """
    <h1>V7000 Webhook</h1>
    <div class="card">
        <h2>Status</h2>
        <p>Der Browser-Endpunkt <b>/webhook</b> ist nur eine Info-Seite.</p>
        <p>TradingView muss per <b>POST</b> senden an:</p>
        <pre>http://91.107.237.214/webhook/tradingview</pre>
    </div>
    <div class="card">
        <h2>Benötigtes JSON Beispiel</h2>
        <pre>{
  "market":"US100",
  "side":"BUY",
  "price":25000,
  "trigger":"V7000_SIGNAL",
  "timeframe":"5m",
  "trend_state":"trend",
  "volume_state":"spike",
  "vwap_state":"above",
  "structure_state":"bos",
  "session":"NY",
  "atr":50,
  "risk_score":0
}</pre>
    </div>
    <div class="small">
        <a href="/dashboard">Dashboard</a> ·
        <a href="/terminal">Terminal</a> ·
        <a href="/health">Health</a>
    </div>
    """
    return _v7_base_html("V7000 Webhook", body)

# ============================================================
# V7000 AUTO ECONOMIC CALENDAR INTEGRATION
# ============================================================

from fastapi import Body as V7Body

def v7000_calendar_db_path():
    return "data/v7000_news.sqlite3"

def v7000_calendar_recent(limit: int = 100):
    import sqlite3
    con = sqlite3.connect(v7000_calendar_db_path())
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("""
        SELECT *
        FROM macro_events
        ORDER BY id DESC
        LIMIT ?
        """, (limit,)).fetchall()
    except Exception:
        rows = []
    con.close()
    return [dict(r) for r in rows]

def v7000_calendar_targets_and_score(currency: str, score: float, title: str):
    c = str(currency or "").upper()
    t = str(title or "").lower()
    s = float(score or 0)

    inflation_like = any(w in t for w in ["cpi", "ppi", "inflation", "prices", "price index", "pce"])
    rate_like = any(w in t for w in ["rate", "interest", "fed", "ecb", "boe"])
    oil_like = any(w in t for w in ["oil", "crude", "inventory", "inventories", "opec"])

    out = []

    if c == "USD":
        out.append(("USD", s))
        out.append(("DXY", s))
        if inflation_like or rate_like:
            out += [("US100", -s * 0.8), ("US500", -s * 0.6), ("US30", -s * 0.4), ("XAUUSD", -s)]
        else:
            out += [("US100", s * 0.6), ("US500", s * 0.5), ("US30", s * 0.4), ("XAUUSD", -s * 0.5)]

    elif c == "EUR":
        out.append(("EUR", s))
        if inflation_like or rate_like:
            out += [("GER40", -s * 0.5), ("FRA40", -s * 0.4)]
        else:
            out += [("GER40", s * 0.5), ("FRA40", s * 0.4)]

    elif c == "GBP":
        out.append(("GBP", s))
        if inflation_like or rate_like:
            out.append(("FTSE100", -s * 0.4))
        else:
            out.append(("FTSE100", s * 0.4))

    elif c == "CAD":
        out.append(("CAD", s))
        if oil_like:
            out.append(("USOIL", s * 0.7))

    elif c == "JPY":
        out.append(("JPY", s))
        out += [("US100", -s * 0.3), ("US500", -s * 0.2), ("XAUUSD", s * 0.3)]

    elif c == "AUD":
        out.append(("AUD", s))
        out.append(("ASX200", s * 0.5))

    elif c == "NZD":
        out.append(("NZD", s))

    else:
        if c:
            out.append((c, s))

    return out

_v7000_news_snapshot_before_calendar_auto = _v7000_news_snapshot

def _v7000_news_snapshot(minutes: int = 90):
    snap = _v7000_news_snapshot_before_calendar_auto(minutes=minutes)

    markets = snap.get("markets", {})
    if not isinstance(markets, dict):
        return snap

    events = v7000_calendar_recent(limit=80)

    for ev in events:
        imp = int(ev.get("importance") or 1)
        score = float(ev.get("score") or 0)
        currency = ev.get("currency") or ""
        title = ev.get("title") or ""

        if imp < 2 and abs(score) < 1:
            continue

        headline = {
            "title": f"CALENDAR {currency} {title} | actual {ev.get('actual') or '-'} | forecast {ev.get('forecast') or '-'} | previous {ev.get('previous') or '-'}",
            "source": ev.get("source") or "faireconomy",
            "impact": imp,
            "event_risk": "high" if imp >= 3 else "medium",
            "score": score,
            "matched": ["economic calendar", currency]
        }

        for target, target_score in v7000_calendar_targets_and_score(currency, score, title):
            if target not in markets:
                continue

            m = markets[target]
            old_score = float(m.get("score") or 0)
            old_direct = float(m.get("direct_score") or 0)

            m["score"] = round(old_score + target_score, 3)
            m["direct_score"] = round(old_direct + target_score, 3)

            comps = m.get("components") or {}
            comps["calendar"] = round(float(comps.get("calendar") or 0) + target_score, 3)
            m["components"] = comps

            heads = m.get("top_headlines") or []
            heads.insert(0, headline)
            m["top_headlines"] = heads[:6]

            if imp >= 3:
                m["event_risk"] = "high"
            elif imp == 2 and m.get("event_risk") != "high":
                m["event_risk"] = "medium"

            if m["score"] >= 3:
                m["bias"] = "bullish"
            elif m["score"] <= -3:
                m["bias"] = "bearish"
            else:
                m["bias"] = "neutral"

    snap["calendar_events"] = events[:30]
    return snap

@app.post("/v7000/calendar/sync")
def v7000_calendar_sync():
    from tradingbot_v7000.calendar_engine import fetch_calendar, upsert_events
    events = fetch_calendar()
    return upsert_events(v7000_calendar_db_path(), events)

@app.get("/v7000/calendar")
def v7000_calendar_get(limit: int = 100):
    return {
        "source": "faireconomy",
        "events": v7000_calendar_recent(limit=limit)
    }

@app.get("/calendar", response_class=HTMLResponse)
def calendar_page():
    events = v7000_calendar_recent(limit=120)

    rows = ""
    for e in events:
        rows += "<tr>"
        rows += f"<td>{_v7_html_escape(e.get('currency'))}</td>"
        rows += f"<td>{_v7_html_escape(e.get('date_raw'))} {_v7_html_escape(e.get('time_raw'))}</td>"
        rows += f"<td>{_v7_html_escape(e.get('title'))}</td>"
        rows += f"<td>{_v7_html_escape(e.get('actual'))}</td>"
        rows += f"<td>{_v7_html_escape(e.get('forecast'))}</td>"
        rows += f"<td>{_v7_html_escape(e.get('previous'))}</td>"
        rows += f"<td>{_v7_html_escape(e.get('impact'))}</td>"
        rows += f"<td>{_v7_html_escape(e.get('score'))}</td>"
        rows += "</tr>"

    body = f"""
    <h1>V7000 Economic Calendar</h1>
    <div class="small">
        <a href="/dashboard">Dashboard</a> ·
        <a href="/intelligence">Intelligence</a> ·
        <a href="/v7000/calendar">Raw Calendar JSON</a> ·
        <a href="/v7000/calendar/sync">Sync ist POST</a>
    </div>

    <div class="card">
        <h2>Automatischer Kalender</h2>
        <p>Quelle: FairEconomy / ForexFactory weekly XML</p>
        <table>
            <tr>
                <th>CCY</th><th>Zeit</th><th>Event</th><th>Actual</th><th>Forecast</th>
                <th>Previous</th><th>Impact</th><th>Score</th>
            </tr>
            {rows}
        </table>
    </div>
    """
    return _v7_base_html("V7000 Economic Calendar", body)


# ============================================================
# V7000 CALENDAR ACTIVE WINDOW FIX
# Only recent/upcoming macro events affect intelligence/news bias.
# Full weekly calendar stays visible on /calendar.
# ============================================================

def _v7_ff_parse_event_dt(ev):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    import re

    date_raw = str(ev.get("date_raw") or "").strip()
    time_raw = str(ev.get("time_raw") or "").strip().lower().replace(" ", "")

    if not date_raw or not time_raw:
        return None

    if time_raw in {"all-day", "allday", "tentative", "day1", "day2", "holiday"}:
        return None

    # FairEconomy / ForexFactory format normally: 07-06-2026 + 2:00pm
    try:
        dt_naive = datetime.strptime(f"{date_raw} {time_raw}", "%m-%d-%Y %I:%M%p")
        # FairEconomy XML times are treated as UTC/GMT.
        # Then we display them in Europe/Berlin on /calendar.
        return dt_naive.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _v7_calendar_active_events(minutes_past=240, minutes_future=240, limit=100):
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    lo = now - timedelta(minutes=minutes_past)
    hi = now + timedelta(minutes=minutes_future)

    events = v7000_calendar_recent(limit=250)
    active = []

    for ev in events:
        dt = _v7_ff_parse_event_dt(ev)
        if dt is None:
            continue

        ev["_dt_utc"] = dt.isoformat()

        if lo <= dt <= hi:
            active.append(ev)

    active.sort(key=lambda x: x.get("_dt_utc", ""))
    return active[:limit]


def _v7_calendar_all_sorted(limit=150):
    events = v7000_calendar_recent(limit=300)

    def key(ev):
        dt = _v7_ff_parse_event_dt(ev)
        return dt.isoformat() if dt else "9999"

    events.sort(key=key)
    return events[:limit]


def _v7_calendar_berlin_time(ev):
    from zoneinfo import ZoneInfo
    dt = _v7_ff_parse_event_dt(ev)
    if not dt:
        return f"{ev.get('date_raw') or ''} {ev.get('time_raw') or ''}".strip()
    return dt.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")


# base snapshot ohne den fehlerhaften Weekly-Full-Calendar-Einfluss nutzen
try:
    _v7000_news_snapshot_clean_base = _v7000_news_snapshot_before_calendar_auto
except Exception:
    _v7000_news_snapshot_clean_base = _v7000_news_snapshot


def _v7000_news_snapshot(minutes: int = 90):
    snap = _v7000_news_snapshot_clean_base(minutes=minutes)

    markets = snap.get("markets", {})
    if not isinstance(markets, dict):
        return snap

    # Nur aktives Zeitfenster beeinflusst Bias/Risk.
    events = _v7_calendar_active_events(
        minutes_past=minutes,
        minutes_future=240,
        limit=80
    )

    for ev in events:
        imp = int(ev.get("importance") or 1)
        score = float(ev.get("score") or 0)
        currency = ev.get("currency") or ""
        title = ev.get("title") or ""
        actual = ev.get("actual") or ""
        forecast = ev.get("forecast") or ""
        previous = ev.get("previous") or ""

        # Event ohne Actual: Risk ja, Bias-Score nein.
        has_actual = bool(str(actual).strip())
        effective_score = score if has_actual else 0.0

        if imp < 2 and abs(effective_score) < 1:
            continue

        headline = {
            "title": f"CALENDAR {currency} {title} | actual {actual or '-'} | forecast {forecast or '-'} | previous {previous or '-'}",
            "source": ev.get("source") or "faireconomy",
            "impact": imp,
            "event_risk": "high" if imp >= 3 else "medium",
            "score": effective_score,
            "matched": ["economic calendar", currency]
        }

        for target, target_score in v7000_calendar_targets_and_score(currency, effective_score, title):
            if target not in markets:
                continue

            m = markets[target]
            old_score = float(m.get("score") or 0)
            old_direct = float(m.get("direct_score") or 0)

            m["score"] = round(old_score + target_score, 3)
            m["direct_score"] = round(old_direct + target_score, 3)

            comps = m.get("components") or {}
            comps["calendar"] = round(float(comps.get("calendar") or 0) + target_score, 3)
            m["components"] = comps

            heads = m.get("top_headlines") or []
            heads.insert(0, headline)
            m["top_headlines"] = heads[:6]

            if imp >= 3:
                m["event_risk"] = "high"
            elif imp == 2 and m.get("event_risk") != "high":
                m["event_risk"] = "medium"

            if m["score"] >= 3:
                m["bias"] = "bullish"
            elif m["score"] <= -3:
                m["bias"] = "bearish"
            else:
                m["bias"] = "neutral"

    snap["calendar_active_events"] = events[:30]
    return snap


# Alte Calendar-Routes entfernen, damit /calendar sauber ersetzt wird.
try:
    app.router.routes = [
        r for r in app.router.routes
        if not (
            getattr(r, "path", "") in {"/calendar", "/v7000/calendar/active"}
            or (getattr(r, "path", "") == "/v7000/calendar" and "GET" in getattr(r, "methods", set()))
        )
    ]
except Exception:
    pass


@app.get("/v7000/calendar/active")
def v7000_calendar_active_get(minutes_past: int = 90, minutes_future: int = 90):
    return {
        "minutes_past": minutes_past,
        "minutes_future": minutes_future,
        "events": _v7_calendar_active_events(minutes_past, minutes_future, limit=100)
    }


@app.get("/v7000/calendar")
def v7000_calendar_get(limit: int = 150):
    return {
        "source": "faireconomy",
        "mode": "weekly_all_sorted",
        "events": _v7_calendar_all_sorted(limit=limit)
    }


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page():
    active = _v7_calendar_active_events(minutes_past=240, minutes_future=240, limit=80)
    all_events = _v7_calendar_all_sorted(limit=160)

    def row(e):
        risk = "high" if int(e.get("importance") or 1) >= 3 else "medium" if int(e.get("importance") or 1) == 2 else "low"
        return (
            "<tr>"
            f"<td>{_v7_html_escape(e.get('currency'))}</td>"
            f"<td>{_v7_html_escape(_v7_calendar_berlin_time(e))}</td>"
            f"<td>{_v7_html_escape(e.get('title'))}</td>"
            f"<td>{_v7_html_escape(e.get('actual'))}</td>"
            f"<td>{_v7_html_escape(e.get('forecast'))}</td>"
            f"<td>{_v7_html_escape(e.get('previous'))}</td>"
            f"<td>{_v7_status_badge(risk)}</td>"
            f"<td>{_v7_html_escape(e.get('score'))}</td>"
            "</tr>"
        )

    active_rows = "".join(row(e) for e in active) or "<tr><td colspan='8'>Keine aktiven Kalenderdaten im Zeitfenster.</td></tr>"
    all_rows = "".join(row(e) for e in all_events)

    body = f"""
    <h1>V7000 Economic Calendar</h1>
    <div class="small">
        <a href="/dashboard">Dashboard</a> ·
        <a href="/intelligence">Intelligence</a> ·
        <a href="/v7000/calendar/active">Active JSON</a> ·
        <a href="/v7000/calendar">Raw Calendar JSON</a>
    </div>

    <div class="card">
        <h2>Aktiv für Trading-Bias</h2>
        <p>Nur diese Events beeinflussen aktuell News-Bias und Event-Risk.</p>
        <table>
            <tr>
                <th>CCY</th><th>Deutschland Zeit</th><th>Event</th><th>Actual</th><th>Forecast</th>
                <th>Previous</th><th>Risk</th><th>Score</th>
            </tr>
            {active_rows}
        </table>
    </div>

    <div class="card">
        <h2>Kompletter Wochenkalender</h2>
        <table>
            <tr>
                <th>CCY</th><th>Deutschland Zeit</th><th>Event</th><th>Actual</th><th>Forecast</th>
                <th>Previous</th><th>Risk</th><th>Score</th>
            </tr>
            {all_rows}
        </table>
    </div>
    """
    return _v7_base_html("V7000 Economic Calendar", body)


# ============================================================
# V7000 DASHBOARD RISK VIEW PATCH
# Mobile-friendly dashboard with visible Event Risk column.
# ============================================================

def _v7_dash_escape(x):
    try:
        return _v7_html_escape(x)
    except Exception:
        import html
        return html.escape(str(x if x is not None else ""))

def _v7_dash_badge(text):
    t = str(text or "").lower()
    if "high" in t or "block" in t or "loss" in t:
        c = "#ef4444"
    elif "medium" in t or "wait" in t or "neutral" in t:
        c = "#facc15"
    elif "allow" in t or "online" in t or "bullish" in t:
        c = "#22c55e"
    else:
        c = "#9ca3af"
    return f"<b style='color:{c}'>{_v7_dash_escape(text)}</b>"

def _v7_dash_open_trade_count():
    try:
        v7000_init_open_trades()
        con = v7000_db()
        cur = con.cursor()
        n = cur.execute("SELECT COUNT(*) FROM open_trades WHERE status='OPEN'").fetchone()[0]
        con.close()
        return int(n)
    except Exception:
        try:
            return len(TRADES)
        except Exception:
            return 0

def _v7_dash_open_trades_html():
    try:
        v7000_init_open_trades()
        con = v7000_db()
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT client_trade_id, decision_id, market, direction, setup_name, entry, sl, tp1, status, opened_at
            FROM open_trades
            WHERE status='OPEN'
            ORDER BY opened_at DESC
            LIMIT 20
        """).fetchall()
        con.close()

        if not rows:
            return "<p>Keine offenen Trades.</p>"

        out = """
        <table>
        <tr>
            <th>Client ID</th><th>Decision</th><th>Market</th><th>Side</th>
            <th>Setup</th><th>Entry</th><th>SL</th><th>TP1</th><th>Status</th>
        </tr>
        """
        for r in rows:
            out += (
                "<tr>"
                f"<td>{_v7_dash_escape(r['client_trade_id'])}</td>"
                f"<td>{_v7_dash_escape(r['decision_id'])}</td>"
                f"<td>{_v7_dash_escape(r['market'])}</td>"
                f"<td>{_v7_dash_escape(r['direction'])}</td>"
                f"<td>{_v7_dash_escape(r['setup_name'])}</td>"
                f"<td>{_v7_dash_escape(r['entry'])}</td>"
                f"<td>{_v7_dash_escape(r['sl'])}</td>"
                f"<td>{_v7_dash_escape(r['tp1'])}</td>"
                f"<td>{_v7_dash_badge(r['status'])}</td>"
                "</tr>"
            )
        out += "</table>"
        return out
    except Exception as e:
        return f"<p>Open-Trades Anzeige Fehler: {_v7_dash_escape(e)}</p>"

def _v7_dash_headlines(news_market):
    heads = (news_market or {}).get("top_headlines") or []
    if not heads:
        return "-"
    titles = []
    for h in heads[:2]:
        titles.append(str(h.get("title") or "")[:120])
    return "<br>".join(_v7_dash_escape(x) for x in titles if x) or "-"

def _v7_dashboard_html():
    scan_rows = scan_all()
    news_snap = _v7000_news_snapshot(minutes=90)
    news_markets = news_snap.get("markets", {}) if isinstance(news_snap, dict) else {}

    high_risk = []
    for mk, data in news_markets.items():
        if str((data or {}).get("event_risk", "")).lower() == "high":
            high_risk.append(mk)

    open_n = _v7_dash_open_trade_count()

    rows_html = ""
    for r in scan_rows:
        market = str(r.get("market") or "").upper()
        n = news_markets.get(market) or {}

        event_risk = n.get("event_risk", "normal")
        news_bias = n.get("bias", "neutral")
        news_score = n.get("score", 0)

        rows_html += (
            "<tr>"
            f"<td><b>{_v7_dash_escape(market)}</b></td>"
            f"<td>{_v7_dash_escape(r.get('bias'))}</td>"
            f"<td>{_v7_dash_escape(r.get('score'))}</td>"
            f"<td>{_v7_dash_badge(r.get('grade'))}</td>"
            f"<td>{_v7_dash_badge(r.get('decision'))}</td>"
            f"<td>{_v7_dash_escape(r.get('trigger') or '-')}</td>"
            f"<td>{_v7_dash_badge(news_bias)}</td>"
            f"<td>{_v7_dash_escape(news_score)}</td>"
            f"<td>{_v7_dash_badge(event_risk)}</td>"
            f"<td>{_v7_dash_headlines(n)}</td>"
            "</tr>"
        )

    high_risk_text = ", ".join(high_risk[:12]) if high_risk else "Keine"

    body = f"""
    <h1>TradingBot V6000 + V7000 Dashboard</h1>
    <div class="small">
        <a href="/terminal">Terminal</a> ·
        <a href="/intelligence">Intelligence</a> ·
        <a href="/calendar">Calendar</a> ·
        <a href="/health">Health</a> ·
        <a href="/v7000/news/bias">News JSON</a>
    </div>

    <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:14px">
        <div class="card"><div>Status</div><h2 style="color:#22c55e">online</h2></div>
        <div class="card"><div>Märkte</div><h2>{len(WATCHLIST)}</h2></div>
        <div class="card"><div>Offene Trades</div><h2>{open_n}</h2></div>
        <div class="card"><div>High Risk Märkte</div><h2 style="color:#ef4444">{len(high_risk)}</h2></div>
    </div>

    <div class="card">
        <h2>Aktives Event Risk</h2>
        <p>{_v7_dash_escape(high_risk_text)}</p>
        <p class="small">High Risk kommt aus aktivem Kalenderfenster. Score bleibt 0.0, solange kein Actual/Überraschung gewertet wird.</p>
    </div>

    <div class="card">
        <h2>Institutional Scan + News Bias + Event Risk</h2>
        <div style="overflow-x:auto">
        <table>
            <tr>
                <th>Market</th>
                <th>Bias</th>
                <th>Score</th>
                <th>Grade</th>
                <th>Decision</th>
                <th>Trigger</th>
                <th>News</th>
                <th>News Score</th>
                <th>Event Risk</th>
                <th>Headlines</th>
            </tr>
            {rows_html}
        </table>
        </div>
    </div>

    <div class="card">
        <h2>Open Trades</h2>
        <div style="overflow-x:auto">
        {_v7_dash_open_trades_html()}
        </div>
    </div>
    """
    try:
        return _v7_base_html("V7000 Dashboard", body)
    except Exception:
        return f"""
        <html>
        <head>
            <title>V7000 Dashboard</title>
            <style>
                body {{font-family:Arial;background:#07111f;color:#e8f1ff;padding:24px}}
                .card {{background:#0b1b2f;border:1px solid #123456;border-radius:14px;padding:18px;margin:14px 0}}
                table {{border-collapse:collapse;width:100%;font-size:14px}}
                th,td {{padding:9px;border-bottom:1px solid #22334a;text-align:left;vertical-align:top}}
                a {{color:#60a5fa}}
                .small {{color:#9ca3af;font-size:13px}}
            </style>
        </head>
        <body>{body}</body>
        </html>
        """

try:
    app.router.routes = [
        r for r in app.router.routes
        if getattr(r, "path", "") != "/dashboard"
    ]
except Exception:
    pass

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return _v7_dashboard_html()



# ==== V7000 DUPLICATE WEBHOOK GUARD PATCH ====
# Blockt doppelte TradingView-Webhooks mit gleicher client_trade_id,
# solange der Trade in open_trades noch OPEN ist.
try:
    from fastapi import Request as _V7000Request
    from fastapi.responses import JSONResponse as _V7000JSONResponse
    import json as _v7000_json
    import sqlite3 as _v7000_sqlite3
    from pathlib import Path as _V7000Path

    def _v7000_open_trade_exists(_client_trade_id: str) -> bool:
        _client_trade_id = str(_client_trade_id or "").strip()
        if not _client_trade_id:
            return False

        _db = _V7000Path("data/v7000_learning.sqlite3")
        if not _db.exists():
            _db = _V7000Path("/app/data/v7000_learning.sqlite3")

        if not _db.exists():
            return False

        try:
            with _v7000_sqlite3.connect(str(_db)) as _con:
                _row = _con.execute(
                    """
                    SELECT 1
                    FROM open_trades
                    WHERE client_trade_id = ?
                      AND COALESCE(status, 'OPEN') = 'OPEN'
                    LIMIT 1
                    """,
                    (_client_trade_id,),
                ).fetchone()
            return _row is not None
        except Exception:
            return False

    @app.middleware("http")
    async def _v7000_duplicate_tradingview_guard(request: _V7000Request, call_next):
        if request.method.upper() == "POST" and request.url.path == "/webhook/tradingview":
            _body = await request.body()

            try:
                _payload = _v7000_json.loads(_body.decode("utf-8") or "{}")
            except Exception:
                _payload = {}

            _client_trade_id = str(_payload.get("client_trade_id") or "").strip()

            if _client_trade_id and _v7000_open_trade_exists(_client_trade_id):
                return _V7000JSONResponse({
                    "accepted": True,
                    "trade_allowed": False,
                    "duplicate_blocked": True,
                    "client_trade_id": _client_trade_id,
                    "reason": "DUPLICATE_BLOCK: client_trade_id already OPEN; no second trade/telegram sent."
                })

            async def _receive():
                return {"type": "http.request", "body": _body, "more_body": False}

            request = _V7000Request(request.scope, _receive)

        return await call_next(request)

except Exception as _e:
    print("V7000 duplicate guard init failed:", _e)
# ==== END V7000 DUPLICATE WEBHOOK GUARD PATCH ====


# ==== V7000 CALENDAR RISK PHASE V2 PATCH ====
# Ziel:
# - Kein starres +-90 Minuten Hard-Block mehr.
# - Normale High-News: 30 Min vor / 15 Min nach HARD BLOCK, danach 180 Min Soft Bias.
# - CPI/NFP/FOMC/Rate Decision: 60 Min vor / 30 Min nach HARD BLOCK, danach 360 Min Soft Bias.
# - event_risk="high" nur noch während HARD BLOCK.
# - Soft Bias setzt event_risk maximal auf "medium", blockiert aber nicht hart.

try:
    import re as _v7_re
    from datetime import datetime as _v7_datetime, timezone as _v7_timezone
    from zoneinfo import ZoneInfo as _v7_ZoneInfo

    # Möglichst den cleanen News-Snapshot ohne alten Kalender-Wrapper nehmen.
    _v7000_news_snapshot_calendar_risk_v2_base = globals().get(
        "_v7000_news_snapshot_clean_base",
        globals().get("_v7000_news_snapshot_before_calendar_auto", _v7000_news_snapshot)
    )

    _V7_SPECIAL_EVENT_RE = _v7_re.compile(
        r"\b("
        r"cpi|core cpi|pce|core pce|inflation|"
        r"nfp|non[- ]?farm|payrolls?|employment report|average hourly earnings|"
        r"fomc|fed rate|federal funds|rate decision|interest rate decision|"
        r"powell|ecb rate|boe rate|boj rate|boc rate|rba rate|rbnz rate|snb rate"
        r")\b",
        _v7_re.I
    )

    _V7_CURRENCY_TARGETS = {
        "USD": ["USD", "US100", "US500", "US30", "XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD"],
        "EUR": ["EUR", "GER40", "FRA40", "EURUSD", "EURGBP", "EURJPY"],
        "GBP": ["GBP", "FTSE100", "GBPUSD", "EURGBP", "GBPJPY"],
        "JPY": ["JPY", "USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY", "US100", "US500"],
        "CAD": ["CAD", "USDCAD", "CADJPY", "USOIL", "UKOIL", "BRENT"],
        "AUD": ["AUD", "AUDUSD", "AUDJPY", "AUDCAD", "AUDNZD", "GER40"],
        "NZD": ["NZD", "NZDUSD", "NZDJPY", "AUDNZD"],
        "CHF": ["CHF", "USDCHF", "EURCHF", "CHFJPY", "XAUUSD"],
        "CNY": ["AUD", "NZD", "GER40", "US100", "US500"],
        "CNH": ["AUD", "NZD", "GER40", "US100", "US500"],
    }

    def _v7_to_float(x, default=0.0):
        try:
            if x is None:
                return default
            return float(x)
        except Exception:
            return default

    def _v7_impact_num(x):
        s = str(x or "").strip().lower()
        if not s:
            return 1
        try:
            return int(float(s))
        except Exception:
            pass
        if "high" in s or "red" in s:
            return 3
        if "medium" in s or "orange" in s:
            return 2
        if "low" in s or "yellow" in s:
            return 1
        return 1

    def _v7_parse_dt_value(v, assume_tz="utc"):
        if v is None:
            return None

        if isinstance(v, _v7_datetime):
            dt = v
        else:
            s = str(v).strip()
            if not s:
                return None

            # Unix timestamp
            if s.isdigit() and len(s) >= 10:
                try:
                    return _v7_datetime.fromtimestamp(int(s[:10]), tz=_v7_timezone.utc)
                except Exception:
                    pass

            s2 = s.replace("Z", "+00:00")

            dt = None
            try:
                dt = _v7_datetime.fromisoformat(s2)
            except Exception:
                pass

            if dt is None:
                fmts = [
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M",
                    "%d.%m.%Y %H:%M:%S",
                    "%d.%m.%Y %H:%M",
                    "%m-%d-%Y %H:%M",
                    "%m/%d/%Y %H:%M",
                    "%b %d %Y %H:%M",
                ]
                for fmt in fmts:
                    try:
                        dt = _v7_datetime.strptime(s, fmt)
                        break
                    except Exception:
                        continue

            if dt is None:
                return None

        if dt.tzinfo is None:
            if assume_tz == "berlin":
                dt = dt.replace(tzinfo=_v7_ZoneInfo("Europe/Berlin"))
            else:
                dt = dt.replace(tzinfo=_v7_timezone.utc)

        return dt.astimezone(_v7_timezone.utc)

    def _v7_event_time_utc(e):
        if not isinstance(e, dict):
            return None

        utc_keys = [
            "datetime_utc", "event_time_utc", "time_utc", "dt_utc",
            "timestamp_utc", "start_utc", "starts_at_utc", "utc_time",
            "published_at_utc"
        ]
        berlin_keys = [
            "datetime_berlin", "event_time_berlin", "time_berlin",
            "time_de", "local_time", "datetime_local"
        ]
        generic_keys = [
            "datetime", "event_time", "timestamp", "starts_at", "time"
        ]

        for k in utc_keys:
            if k in e:
                dt = _v7_parse_dt_value(e.get(k), "utc")
                if dt:
                    return dt

        for k in berlin_keys:
            if k in e:
                dt = _v7_parse_dt_value(e.get(k), "berlin")
                if dt:
                    return dt

        for k in generic_keys:
            if k in e:
                dt = _v7_parse_dt_value(e.get(k), "utc")
                if dt:
                    return dt

        # date + time fallback
        d = e.get("date") or e.get("event_date")
        t = e.get("time") or e.get("event_time_text")
        if d and t:
            dt = _v7_parse_dt_value(str(d) + " " + str(t), "utc")
            if dt:
                return dt

        return None

    def _v7_calendar_targets(currency, score, title):
        currency = str(currency or "").upper().strip()
        title = str(title or "")
        score = _v7_to_float(score, 0.0)

        # Erst vorhandene Bot-Funktion nutzen, falls vorhanden.
        try:
            fn = globals().get("v7000_calendar_targets_and_score")
            if fn:
                res = fn(currency, score, title)
                if isinstance(res, dict) and res:
                    return {str(k): _v7_to_float(v, 0.0) for k, v in res.items()}
        except Exception:
            pass

        targets = _V7_CURRENCY_TARGETS.get(currency, [currency] if currency else [])
        out = {}

        # Fallback Bias-Logik.
        for m in targets:
            val = score
            # USD positive Daten: USD eher +, Indizes/Gold/Krypto eher -
            if currency == "USD" and m in ["US100", "US500", "US30", "XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD"]:
                val = -score
            # EUR/GBP positive Daten: Index kann wegen höherer Zinsen eher belastet sein
            if currency == "EUR" and m in ["GER40", "FRA40"]:
                val = -score * 0.7
            if currency == "GBP" and m == "FTSE100":
                val = -score * 0.5
            # JPY stark/hawkish kann Risk-Off sein
            if currency == "JPY" and m in ["US100", "US500"]:
                val = -abs(score) * 0.4 if score > 0 else abs(score) * 0.3

            out[m] = val

        return out

    def _v7_apply_calendar_event_to_market(market_data, event, phase, is_hard, is_soft, delta_min, policy, target_score):
        if not isinstance(market_data, dict):
            return

        title = str(event.get("title") or event.get("event") or event.get("name") or "Calendar Event")
        currency = str(event.get("currency") or event.get("country") or event.get("ccy") or "").upper()
        impact = _v7_impact_num(event.get("impact") or event.get("importance"))
        old_risk = str(market_data.get("event_risk") or "normal").lower()

        if is_hard:
            market_data["event_risk"] = "high"
            market_data["calendar_hard_block"] = True
        elif is_soft and old_risk != "high":
            market_data["event_risk"] = "medium"
            market_data["calendar_soft_bias"] = True

        market_data["calendar_phase"] = phase
        market_data["calendar_delta_min"] = round(delta_min, 1)
        market_data["calendar_policy"] = policy

        # Soft Bias soll Richtung/Score beeinflussen, Hard Block braucht vor allem risk=high.
        if is_soft and abs(target_score) > 0:
            old_score = _v7_to_float(market_data.get("score"), 0.0)
            market_data["score"] = round(old_score + target_score, 2)
            market_data["calendar_bias_score"] = round(target_score, 2)

            if market_data["score"] > 0.25:
                market_data["bias"] = "bullish"
            elif market_data["score"] < -0.25:
                market_data["bias"] = "bearish"

        h = {
            "source": "calendar",
            "title": f"{currency} {title}".strip(),
            "impact": impact,
            "event_risk": "high" if is_hard else "medium" if is_soft else "normal",
            "phase": phase,
            "delta_min": round(delta_min, 1),
            "policy": policy,
            "score": round(target_score, 2),
        }

        headlines = market_data.get("headlines")
        if not isinstance(headlines, list):
            headlines = []
        headlines.insert(0, h)
        market_data["headlines"] = headlines[:8]

    def _v7000_news_snapshot(minutes: int = 90):
        # Clean News Snapshot ohne alten Kalender-Hardblock
        snap = _v7000_news_snapshot_calendar_risk_v2_base(minutes=minutes)

        if not isinstance(snap, dict):
            return snap

        markets = snap.setdefault("markets", {})
        if not isinstance(markets, dict):
            snap["markets"] = {}
            markets = snap["markets"]

        now = _v7_datetime.now(_v7_timezone.utc)

        # Max Fenster wegen Spezialevents: 60 Min vorher + 360 Min danach.
        try:
            events = _v7_calendar_active_events(minutes_past=390, minutes_future=70, limit=200)
        except Exception as e:
            snap["calendar_risk_v2_error"] = str(e)
            events = []

        hard_markets = set()
        soft_markets = set()
        phase_events = []

        for e in events or []:
            if not isinstance(e, dict):
                continue

            title = str(e.get("title") or e.get("event") or e.get("name") or "")
            currency = str(e.get("currency") or e.get("country") or e.get("ccy") or "").upper().strip()
            impact = _v7_impact_num(e.get("impact") or e.get("importance"))

            # Nur Medium/High Events für Risk-Phasen.
            if impact < 2:
                continue

            dt_utc = _v7_event_time_utc(e)
            if not dt_utc:
                continue

            delta_min = (dt_utc - now).total_seconds() / 60.0
            is_special = bool(_V7_SPECIAL_EVENT_RE.search(title))

            if impact >= 3:
                pre_hard = 60 if is_special else 30
                post_hard = 30 if is_special else 15
                soft_after = 360 if is_special else 180
            else:
                # Medium Events: kürzer, kein brutaler langer Block.
                pre_hard = 15
                post_hard = 5
                soft_after = 60

            is_hard = (-post_hard <= delta_min <= pre_hard)
            is_soft = (-soft_after <= delta_min < -post_hard)

            if not is_hard and not is_soft:
                continue

            phase = "hard_block" if is_hard else "soft_bias_after"
            policy = {
                "special": is_special,
                "impact": impact,
                "pre_hard_min": pre_hard,
                "post_hard_min": post_hard,
                "soft_after_min": soft_after,
            }

            raw_score = _v7_to_float(e.get("score"), 0.0)
            targets = _v7_calendar_targets(currency, raw_score, title)

            # Wenn Score noch 0 ist, trotzdem Märkte markieren wegen Hard/Soft Phase.
            if not targets:
                for m in _V7_CURRENCY_TARGETS.get(currency, [currency] if currency else []):
                    targets[m] = 0.0

            event_out = dict(e)
            event_out["phase"] = phase
            event_out["delta_min"] = round(delta_min, 1)
            event_out["special_event"] = is_special
            event_out["risk_policy"] = policy
            phase_events.append(event_out)

            for m, target_score in targets.items():
                if not m:
                    continue

                md = markets.setdefault(m, {
                    "score": 0.0,
                    "bias": "neutral",
                    "event_risk": "normal",
                    "headlines": [],
                })

                _v7_apply_calendar_event_to_market(
                    md, e, phase, is_hard, is_soft, delta_min, policy, target_score
                )

                if is_hard:
                    hard_markets.add(m)
                elif is_soft:
                    soft_markets.add(m)

        snap["calendar_risk_engine"] = "phase_v2"
        snap["calendar_risk_policy"] = {
            "normal_high_impact": {
                "pre_hard_min": 30,
                "post_hard_min": 15,
                "soft_after_min": 180,
            },
            "special_cpi_nfp_fomc_rates": {
                "pre_hard_min": 60,
                "post_hard_min": 30,
                "soft_after_min": 360,
            },
            "medium_impact": {
                "pre_hard_min": 15,
                "post_hard_min": 5,
                "soft_after_min": 60,
            }
        }
        snap["calendar_hard_block_markets"] = sorted(hard_markets)
        snap["calendar_soft_bias_markets"] = sorted(soft_markets)
        snap["calendar_phase_events"] = phase_events[:40]

        return snap

except Exception as _e:
    print("V7000 calendar risk phase v2 init failed:", _e)
# ==== END V7000 CALENDAR RISK PHASE V2 PATCH ====


# ==== V7000 CALENDAR RISK V2.1 TIME PARSER PATCH ====
# Ergänzt Zeit-Erkennung für macro_events:
# ts, _dt_utc, date_raw/time_raw, FairEconomy AM/PM Formate.

try:
    from datetime import datetime as _v721_datetime, timezone as _v721_timezone
    from zoneinfo import ZoneInfo as _v721_ZoneInfo

    def _v7_parse_dt_value(v, assume_tz="utc"):
        if v is None:
            return None

        if isinstance(v, _v721_datetime):
            dt = v
        else:
            s = str(v).strip()
            if not s:
                return None

            if s.isdigit() and len(s) >= 10:
                try:
                    return _v721_datetime.fromtimestamp(int(s[:10]), tz=_v721_timezone.utc)
                except Exception:
                    pass

            s2 = s.replace("Z", "+00:00")

            dt = None
            try:
                dt = _v721_datetime.fromisoformat(s2)
            except Exception:
                pass

            if dt is None:
                fmts = [
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M",
                    "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M",
                    "%d.%m.%Y %H:%M:%S",
                    "%d.%m.%Y %H:%M",
                    "%m-%d-%Y %H:%M",
                    "%m-%d-%Y %I:%M%p",
                    "%m-%d-%Y %I:%M %p",
                    "%m/%d/%Y %H:%M",
                    "%m/%d/%Y %I:%M%p",
                    "%m/%d/%Y %I:%M %p",
                    "%b %d %Y %H:%M",
                    "%b %d %Y %I:%M%p",
                    "%b %d %Y %I:%M %p",
                ]

                s3 = s.upper().replace(" AM", "AM").replace(" PM", "PM")
                for candidate in [s, s3]:
                    for fmt in fmts:
                        try:
                            dt = _v721_datetime.strptime(candidate, fmt)
                            break
                        except Exception:
                            continue
                    if dt is not None:
                        break

            if dt is None:
                return None

        if dt.tzinfo is None:
            if assume_tz == "berlin":
                dt = dt.replace(tzinfo=_v721_ZoneInfo("Europe/Berlin"))
            else:
                dt = dt.replace(tzinfo=_v721_timezone.utc)

        return dt.astimezone(_v721_timezone.utc)

    def _v7_event_time_utc(e):
        if not isinstance(e, dict):
            return None

        utc_keys = [
            "_dt_utc", "dt_utc", "datetime_utc", "event_time_utc", "time_utc",
            "timestamp_utc", "start_utc", "starts_at_utc", "utc_time",
            "published_at_utc", "ts"
        ]

        berlin_keys = [
            "datetime_berlin", "event_time_berlin", "time_berlin",
            "time_de", "local_time", "datetime_local"
        ]

        generic_keys = [
            "datetime", "event_time", "timestamp", "starts_at", "time"
        ]

        for k in utc_keys:
            if k in e:
                dt = _v7_parse_dt_value(e.get(k), "utc")
                if dt:
                    return dt

        for k in berlin_keys:
            if k in e:
                dt = _v7_parse_dt_value(e.get(k), "berlin")
                if dt:
                    return dt

        for k in generic_keys:
            if k in e:
                dt = _v7_parse_dt_value(e.get(k), "utc")
                if dt:
                    return dt

        d = e.get("date_raw") or e.get("date") or e.get("event_date")
        t = e.get("time_raw") or e.get("time") or e.get("event_time_text")
        if d and t:
            dt = _v7_parse_dt_value(str(d) + " " + str(t), "utc")
            if dt:
                return dt

        return None

except Exception as _e:
    print("V7000 calendar risk v2.1 time parser init failed:", _e)
# ==== END V7000 CALENDAR RISK V2.1 TIME PARSER PATCH ====


# ==== V7000 PAUSE SWITCH + MOBILE STATUS PATCH ====
# Mobile Buttons mit Token:
# /mobile?token=DEIN_TOKEN
# /pause/on?token=DEIN_TOKEN
# /pause/off?token=DEIN_TOKEN

try:
    from fastapi import Request as _V7PauseRequest
    from fastapi.responses import JSONResponse as _V7PauseJSONResponse
    from fastapi.responses import HTMLResponse as _V7PauseHTMLResponse
    from fastapi.responses import RedirectResponse as _V7PauseRedirectResponse
    from pathlib import Path as _V7PausePath
    from datetime import datetime as _V7PauseDatetime, timezone as _V7PauseTimezone
    import sqlite3 as _v7_pause_sqlite3
    import html as _v7_pause_html
    import secrets as _v7_pause_secrets

    def _v7_data_dirs():
        return [
            _V7PausePath("/app/data"),
            _V7PausePath("data"),
            _V7PausePath("/opt/tradingbot_v6000/data"),
        ]

    def _v7_token_file():
        for d in _v7_data_dirs():
            try:
                d.mkdir(parents=True, exist_ok=True)
                return d / "MOBILE_TOKEN"
            except Exception:
                pass
        return _V7PausePath("data/MOBILE_TOKEN")

    def _v7_get_token():
        p = _v7_token_file()
        try:
            if not p.exists() or not p.read_text().strip():
                p.write_text(_v7_pause_secrets.token_urlsafe(32))
            return p.read_text().strip()
        except Exception:
            return ""

    def _v7_token_ok(request):
        token = str(request.query_params.get("token") or "").strip()
        real = _v7_get_token()
        return bool(token and real and token == real)

    def _v7_pause_files():
        return [
            _V7PausePath("/app/data/PAUSE_TRADING"),
            _V7PausePath("data/PAUSE_TRADING"),
            _V7PausePath("/opt/tradingbot_v6000/data/PAUSE_TRADING"),
        ]

    def _v7_pause_active():
        for _p in _v7_pause_files():
            try:
                if _p.exists():
                    return True, str(_p)
            except Exception:
                pass
        return False, None

    def _v7_set_pause(active: bool):
        if active:
            for d in _v7_data_dirs():
                try:
                    d.mkdir(parents=True, exist_ok=True)
                    f = d / "PAUSE_TRADING"
                    f.write_text("paused")
                    return str(f)
                except Exception:
                    continue
            f = _V7PausePath("data/PAUSE_TRADING")
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("paused")
            return str(f)
        else:
            removed = []
            for f in _v7_pause_files():
                try:
                    if f.exists():
                        f.unlink()
                        removed.append(str(f))
                except Exception:
                    pass
            return removed

    def _v7_learning_db_path():
        for _p in [
            _V7PausePath("/app/data/v7000_learning.sqlite3"),
            _V7PausePath("data/v7000_learning.sqlite3"),
            _V7PausePath("/opt/tradingbot_v6000/data/v7000_learning.sqlite3"),
        ]:
            try:
                if _p.exists():
                    return str(_p)
            except Exception:
                pass
        return "data/v7000_learning.sqlite3"

    def _v7_open_trades(limit=20):
        db = _v7_learning_db_path()
        try:
            with _v7_pause_sqlite3.connect(db) as con:
                con.row_factory = _v7_pause_sqlite3.Row
                rows = con.execute("""
                    SELECT client_trade_id, market, direction, status, opened_at
                    FROM open_trades
                    ORDER BY opened_at DESC
                    LIMIT ?
                """, (int(limit),)).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            return [{"error": str(e)}]

    def _v7_e(x):
        return _v7_pause_html.escape(str(x if x is not None else ""))

    @app.middleware("http")
    async def _v7_pause_trading_guard(request: _V7PauseRequest, call_next):
        if request.method.upper() == "POST" and request.url.path == "/webhook/tradingview":
            paused, pause_file = _v7_pause_active()
            if paused:
                return _V7PauseJSONResponse({
                    "accepted": True,
                    "trade_allowed": False,
                    "paused": True,
                    "pause_file": pause_file,
                    "reason": "PAUSE_TRADING_ACTIVE: emergency switch is ON; no new trade/telegram sent."
                })
        return await call_next(request)

    @app.get("/pause/status")
    def v7000_pause_status():
        paused, pause_file = _v7_pause_active()
        return {
            "paused": paused,
            "pause_file": pause_file,
            "mobile_page": "/mobile?token=HIDDEN",
            "generated_at": _V7PauseDatetime.now(_V7PauseTimezone.utc).isoformat()
        }

    @app.get("/pause/on")
    def v7000_pause_on(request: _V7PauseRequest):
        if not _v7_token_ok(request):
            return _V7PauseJSONResponse({"ok": False, "error": "invalid_token"}, status_code=403)
        token = request.query_params.get("token")
        _v7_set_pause(True)
        return _V7PauseRedirectResponse(url=f"/mobile?token={token}", status_code=303)

    @app.get("/pause/off")
    def v7000_pause_off(request: _V7PauseRequest):
        if not _v7_token_ok(request):
            return _V7PauseJSONResponse({"ok": False, "error": "invalid_token"}, status_code=403)
        token = request.query_params.get("token")
        _v7_set_pause(False)
        return _V7PauseRedirectResponse(url=f"/mobile?token={token}", status_code=303)

    @app.get("/mobile", response_class=_V7PauseHTMLResponse)
    def v7000_mobile_status(request: _V7PauseRequest):
        token_ok = _v7_token_ok(request)
        token = str(request.query_params.get("token") or "").strip()

        paused, pause_file = _v7_pause_active()

        try:
            news = _v7000_news_snapshot(minutes=90)
        except Exception as e:
            news = {"error": str(e), "markets": {}}

        hard = news.get("calendar_hard_block_markets") or []
        soft = news.get("calendar_soft_bias_markets") or []
        engine = news.get("calendar_risk_engine", "-")
        phase_events = news.get("calendar_phase_events") or []

        open_trades = _v7_open_trades(20)

        status_color = "#ef4444" if paused else "#22c55e"
        status_text = "PAUSIERT" if paused else "LIVE"

        if token_ok:
            buttons = f"""
            <div class="btnrow">
                <a class="btn danger" href="/pause/on?token={_v7_e(token)}">PAUSE AKTIVIEREN</a>
                <a class="btn ok" href="/pause/off?token={_v7_e(token)}">PAUSE BEENDEN</a>
            </div>
            """
        else:
            buttons = """
            <div class="warn">
                Buttons gesperrt: Öffne die Seite mit deinem Mobile-Token.
                Den Link bekommst du im Terminal mit dem Befehl unten.
            </div>
            <div class="cmd">cat /opt/tradingbot_v6000/data/MOBILE_TOKEN</div>
            """

        trade_rows = ""
        if open_trades:
            for r in open_trades:
                if "error" in r:
                    trade_rows += f"<tr><td colspan='5'>{_v7_e(r.get('error'))}</td></tr>"
                else:
                    trade_rows += (
                        "<tr>"
                        f"<td>{_v7_e(r.get('client_trade_id'))}</td>"
                        f"<td>{_v7_e(r.get('market'))}</td>"
                        f"<td>{_v7_e(r.get('direction'))}</td>"
                        f"<td>{_v7_e(r.get('status'))}</td>"
                        f"<td>{_v7_e(r.get('opened_at'))}</td>"
                        "</tr>"
                    )
        else:
            trade_rows = "<tr><td colspan='5'>Keine offenen Trades</td></tr>"

        event_rows = ""
        if phase_events:
            for e in phase_events[:10]:
                event_rows += (
                    "<tr>"
                    f"<td>{_v7_e(e.get('currency') or e.get('country') or e.get('ccy'))}</td>"
                    f"<td>{_v7_e(e.get('title') or e.get('event') or e.get('name'))}</td>"
                    f"<td>{_v7_e(e.get('phase'))}</td>"
                    f"<td>{_v7_e(e.get('delta_min'))}</td>"
                    f"<td>{_v7_e(e.get('special_event'))}</td>"
                    "</tr>"
                )
        else:
            event_rows = "<tr><td colspan='5'>Keine aktive Hard/Soft News-Phase</td></tr>"

        html = f"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="20">
<title>V7000 Mobile</title>
<style>
body {{
    margin:0;
    background:#0b1220;
    color:#e5e7eb;
    font-family:Arial, sans-serif;
}}
.header {{
    padding:16px;
    background:#111827;
    position:sticky;
    top:0;
    border-bottom:1px solid #263244;
}}
.badge {{
    display:inline-block;
    padding:8px 12px;
    border-radius:999px;
    background:{status_color};
    color:#020617;
    font-weight:bold;
}}
.card {{
    margin:12px;
    padding:14px;
    background:#111827;
    border:1px solid #263244;
    border-radius:14px;
}}
h2 {{ margin:0 0 10px 0; font-size:18px; }}
.small {{ color:#94a3b8; font-size:13px; word-break:break-all; }}
.cmd {{
    background:#020617;
    border:1px solid #334155;
    padding:10px;
    border-radius:10px;
    font-family:monospace;
    overflow:auto;
}}
.warn {{
    background:#3b2600;
    border:1px solid #f59e0b;
    color:#fde68a;
    padding:10px;
    border-radius:10px;
    margin:10px 0;
}}
.btnrow {{
    display:flex;
    gap:10px;
    flex-wrap:wrap;
    margin-top:12px;
}}
.btn {{
    display:block;
    padding:14px 16px;
    border-radius:12px;
    text-decoration:none;
    color:#020617;
    font-weight:bold;
    text-align:center;
    flex:1;
    min-width:140px;
}}
.btn.danger {{ background:#ef4444; }}
.btn.ok {{ background:#22c55e; }}
table {{
    width:100%;
    border-collapse:collapse;
    font-size:12px;
}}
td, th {{
    border-bottom:1px solid #263244;
    padding:6px;
    text-align:left;
    vertical-align:top;
}}
a {{ color:#60a5fa; }}
</style>
</head>
<body>
<div class="header">
    <div class="badge">{status_text}</div>
    <div class="small">V7000 Mobile Status · Auto-Refresh 20s</div>
</div>

<div class="card">
    <h2>Notfall-Schalter</h2>
    <div>Status: <b style="color:{status_color}">{status_text}</b></div>
    <div class="small">Pause-Datei: {_v7_e(pause_file or "nicht aktiv")}</div>
    {buttons}
</div>

<div class="card">
    <h2>News / Kalender Risk</h2>
    <div>Engine: <b>{_v7_e(engine)}</b></div>
    <div>Hard Block Märkte: <b style="color:#ef4444">{_v7_e(", ".join(hard) if hard else "Keine")}</b></div>
    <div>Soft Bias Märkte: <b style="color:#facc15">{_v7_e(", ".join(soft) if soft else "Keine")}</b></div>
</div>

<div class="card">
    <h2>Aktive News-Phasen</h2>
    <table>
        <tr><th>CCY</th><th>Event</th><th>Phase</th><th>Delta</th><th>Special</th></tr>
        {event_rows}
    </table>
</div>

<div class="card">
    <h2>Offene Trades</h2>
    <table>
        <tr><th>ID</th><th>Markt</th><th>Richtung</th><th>Status</th><th>Zeit</th></tr>
        {trade_rows}
    </table>
</div>

<div class="card small">
    <div>Links:</div>
    <div><a href="/dashboard">Dashboard</a></div>
    <div><a href="/intelligence">Intelligence</a></div>
    <div><a href="/pause/status">Pause JSON</a></div>
    <div><a href="/v7000/news/bias?minutes=90">News JSON</a></div>
</div>
</body>
</html>
"""
        return html

except Exception as _e:
    print("V7000 pause/mobile patch init failed:", _e)
# ==== END V7000 PAUSE SWITCH + MOBILE STATUS PATCH ====



# === V7000 DECISION HISTORY PAGE PATCH ===
@app.get("/decisions.json")
def decisions_json(limit: int = 80):
    import sqlite3
    con = sqlite3.connect("data/v7000_learning.sqlite3")
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS signal_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        client_trade_id TEXT,
        market TEXT,
        direction TEXT,
        trigger TEXT,
        timeframe TEXT,
        entry REAL,
        technical_score REAL,
        confidence REAL,
        news_score REAL,
        event_risk TEXT,
        risk_r REAL,
        allow_trade INTEGER,
        reason TEXT,
        bias_gate TEXT,
        entry_state TEXT,
        chase_state TEXT,
        impulse_state TEXT,
        raw_json TEXT
    )
    """)
    rows = [dict(r) for r in cur.execute("""
        SELECT id, created_at, market, direction, trigger, timeframe, entry,
               technical_score, confidence, news_score, event_risk, risk_r,
               allow_trade, reason, bias_gate, entry_state, chase_state, impulse_state
        FROM signal_audit
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()]
    con.close()
    return {"count": len(rows), "rows": rows}


@app.get("/decisions")
def decisions_page(limit: int = 80):
    import sqlite3, html
    from fastapi.responses import HTMLResponse

    def q(v):
        return html.escape("" if v is None else str(v))

    con = sqlite3.connect("data/v7000_learning.sqlite3")
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS signal_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        client_trade_id TEXT,
        market TEXT,
        direction TEXT,
        trigger TEXT,
        timeframe TEXT,
        entry REAL,
        technical_score REAL,
        confidence REAL,
        news_score REAL,
        event_risk TEXT,
        risk_r REAL,
        allow_trade INTEGER,
        reason TEXT,
        bias_gate TEXT,
        entry_state TEXT,
        chase_state TEXT,
        impulse_state TEXT,
        raw_json TEXT
    )
    """)
    rows = cur.execute("""
        SELECT id, created_at, market, direction, trigger, timeframe, entry,
               technical_score, confidence, news_score, event_risk, risk_r,
               allow_trade, reason, bias_gate, entry_state, chase_state, impulse_state
        FROM signal_audit
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    con.close()

    trs = []
    for r in rows:
        allow = int(r["allow_trade"] or 0) == 1
        cls = "allow" if allow else "block"
        decision = "ALLOW" if allow else "BLOCK"
        trs.append(f"""
        <tr class="{cls}">
          <td>{q(r["id"])}</td>
          <td>{q(r["created_at"])[11:19]}</td>
          <td><b>{q(r["market"])}</b></td>
          <td>{q(r["direction"])}</td>
          <td>{q(r["trigger"])}</td>
          <td>{q(r["timeframe"])}</td>
          <td>{q(r["entry"])}</td>
          <td>{q(r["confidence"])}</td>
          <td>{q(r["event_risk"])}</td>
          <td>{q(r["risk_r"])}</td>
          <td><b>{decision}</b></td>
          <td>{q(r["bias_gate"])}</td>
          <td>{q(r["entry_state"])}/{q(r["chase_state"])}/{q(r["impulse_state"])}</td>
          <td class="reason">{q(r["reason"])}</td>
        </tr>
        """)

    body = "\n".join(trs) if trs else "<tr><td colspan='14'>Noch keine Signal-Audit-Historie vorhanden.</td></tr>"

    html_page = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta http-equiv="refresh" content="20">
      <title>V7000 Decisions</title>
      <style>
        body {{
          background:#07111f;
          color:#e8f1ff;
          font-family:Arial, sans-serif;
          margin:24px;
        }}
        a {{ color:#58a6ff; text-decoration:none; }}
        h1 {{ margin-bottom:4px; }}
        .nav {{ margin-bottom:22px; color:#9fb4d0; }}
        table {{
          width:100%;
          border-collapse:collapse;
          font-size:13px;
        }}
        th {{
          text-align:left;
          color:#9ed0ff;
          border-bottom:1px solid #28415f;
          padding:8px;
          position:sticky;
          top:0;
          background:#07111f;
        }}
        td {{
          border-bottom:1px solid #20344d;
          padding:8px;
          vertical-align:top;
        }}
        tr.allow {{ background:rgba(0,140,70,0.13); }}
        tr.block {{ background:rgba(170,30,30,0.10); }}
        tr.allow td:nth-child(11) {{ color:#37e681; }}
        tr.block td:nth-child(11) {{ color:#ff6b6b; }}
        .reason {{ max-width:520px; }}
        .cards {{
          display:grid;
          grid-template-columns: repeat(4, 1fr);
          gap:12px;
          margin:16px 0 22px 0;
        }}
        .card {{
          background:#0b1b30;
          border:1px solid #173655;
          border-radius:10px;
          padding:14px;
        }}
      </style>
    </head>
    <body>
      <h1>V7000 Decision History</h1>
      <div class="nav">
        <a href="/dashboard">Dashboard</a> ·
        <a href="/intelligence">Intelligence</a> ·
        <a href="/calendar">Calendar</a> ·
        <a href="/health">Health</a> ·
        <a href="/decisions.json">JSON</a>
      </div>

      <div class="cards">
        <div class="card">Auto Refresh<br><b>20s</b></div>
        <div class="card">Angezeigt<br><b>{len(rows)}</b></div>
        <div class="card">ALLOW grün<br><b>Trade erlaubt</b></div>
        <div class="card">BLOCK rot<br><b>Grund prüfen</b></div>
      </div>

      <table>
        <thead>
          <tr>
            <th>ID</th><th>Zeit UTC</th><th>Markt</th><th>Richtung</th><th>Trigger</th>
            <th>TF</th><th>Entry</th><th>Conf</th><th>Risk</th><th>R</th>
            <th>Decision</th><th>Bias Gate</th><th>Entry/Chase/Impulse</th><th>Reason</th>
          </tr>
        </thead>
        <tbody>{body}</tbody>
      </table>
    </body>
    </html>
    """
    return HTMLResponse(html_page)


# === V7000 TP SL PRICE HEARTBEAT PATCH ===
@app.post("/webhook/price")
async def price_heartbeat(payload: dict):
    """
    TradingView 1m price heartbeat.
    Prüft offene Trades gegen High/Low der Kerze.
    """
    import sqlite3
    import json
    from datetime import datetime, timezone

    def _f(v, default=None):
        try:
            if v is None:
                return default
            return float(v)
        except Exception:
            return default

    def _s(v):
        return str(v or "").strip()

    market = _s(payload.get("market") or payload.get("symbol")).upper()
    tf = _s(payload.get("timeframe") or payload.get("tf"))
    close = _f(payload.get("close"), _f(payload.get("price")))
    high = _f(payload.get("high"), close)
    low = _f(payload.get("low"), close)
    source = _s(payload.get("source") or "tradingview_price_heartbeat")

    if not market or high is None or low is None:
        return {
            "accepted": False,
            "error": "missing market/high/low",
            "payload": payload,
        }

    # === V7000 PRICE HEARTBEAT DEBUG LOG ===
    print(f"PRICE_HEARTBEAT market={market} tf={tf} high={high} low={low} close={close} source={source}", flush=True)
    # === V7000 HEARTBEAT STORE PATCH START ===
    try:
        import sqlite3 as _hb_sqlite3, json as _hb_json, os as _hb_os
        _hb_db = "/app/data/v7000_learning.sqlite3" if _hb_os.path.exists("/app/data/v7000_learning.sqlite3") else "data/v7000_learning.sqlite3"
        _hb_con = _hb_sqlite3.connect(_hb_db)
        _hb_con.execute("""
            CREATE TABLE IF NOT EXISTS price_heartbeats (
                market TEXT PRIMARY KEY,
                timeframe TEXT,
                ticker TEXT,
                source TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                received_at TEXT NOT NULL,
                raw_json TEXT
            )
        """)
        _hb_loc = locals()
        _hb_data = _hb_loc.get("data") or _hb_loc.get("payload") or _hb_loc.get("body") or {}
        if not isinstance(_hb_data, dict):
            _hb_data = {}
        _hb_ticker = _hb_data.get("ticker") or _hb_loc.get("ticker") or ""
        _hb_open = _hb_data.get("open")
        _hb_raw = _hb_json.dumps(_hb_data, ensure_ascii=False, default=str)
        _hb_received = datetime.now(timezone.utc).isoformat()
        _hb_con.execute("""
            INSERT INTO price_heartbeats (
                market, timeframe, ticker, source, open, high, low, close, received_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market) DO UPDATE SET
                timeframe=excluded.timeframe,
                ticker=excluded.ticker,
                source=excluded.source,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                received_at=excluded.received_at,
                raw_json=excluded.raw_json
        """, (
            str(market),
            str(tf),
            str(_hb_ticker),
            str(source),
            _hb_open,
            high,
            low,
            close,
            _hb_received,
            _hb_raw,
        ))
        _hb_con.commit()
        _hb_con.close()
    except Exception as _hb_e:
        print(f"HEARTBEAT_STORE_ERROR {type(_hb_e).__name__}: {_hb_e}", flush=True)
    # === V7000 HEARTBEAT STORE PATCH END ===

    now = datetime.now(timezone.utc).isoformat()
    closed = []
    checked = 0

    con = sqlite3.connect("data/v7000_learning.sqlite3")
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS trade_outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER,
        result TEXT,
        pnl_r REAL,
        exit_price REAL,
        notes TEXT,
        closed_at TEXT
    )
    """)

    rows = cur.execute("""
        SELECT client_trade_id, decision_id, market, direction, setup_name,
               entry, sl, tp1, status, opened_at
        FROM open_trades
        WHERE UPPER(market)=?
          AND UPPER(COALESCE(status,'OPEN'))='OPEN'
        ORDER BY opened_at ASC
    """, (market,)).fetchall()

    for r in rows:
        checked += 1

        client_trade_id = r["client_trade_id"]
        decision_id = r["decision_id"]
        direction = _s(r["direction"]).upper()
        setup_name = r["setup_name"]
        entry = _f(r["entry"])
        sl = _f(r["sl"])
        tp1 = _f(r["tp1"])

        if entry is None or sl is None or tp1 is None:
            continue

        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        win_r = round(reward / risk, 2) if risk > 0 else 1.0

        tp_hit = False
        sl_hit = False

        if direction == "LONG":
            sl_hit = low <= sl
            tp_hit = high >= tp1
        elif direction == "SHORT":
            sl_hit = high >= sl
            tp_hit = low <= tp1
        else:
            continue

        if not tp_hit and not sl_hit:
            continue

        ambiguous = tp_hit and sl_hit

        # Konservativ: Wenn TP und SL in derselben 1m-Kerze berührt wurden,
        # wird SL angenommen, weil die Reihenfolge ohne Tickdaten nicht sicher ist.
        if ambiguous:
            result = "LOSS"
            pnl_r = -1.0
            exit_price = sl
            close_reason = "AMBIGUOUS_TP_SL_SAME_CANDLE_CONSERVATIVE_SL"
            status = "CLOSED_AMBIGUOUS_SL"
        elif sl_hit:
            result = "LOSS"
            pnl_r = -1.0
            exit_price = sl
            close_reason = "SL_HIT"
            status = "CLOSED_SL"
        else:
            result = "WIN"
            pnl_r = win_r
            exit_price = tp1
            close_reason = "TP1_HIT"
            status = "CLOSED_TP1"

        notes = (
            f"{close_reason}; client_trade_id={client_trade_id}; "
            f"market={market}; direction={direction}; setup={setup_name}; "
            f"entry={entry}; sl={sl}; tp1={tp1}; candle_high={high}; candle_low={low}; "
            f"close={close}; timeframe={tf}; source={source}"
        )

        cur.execute("""
            INSERT INTO trade_outcomes (decision_id, result, pnl_r, exit_price, notes, closed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (decision_id, result, pnl_r, exit_price, notes, now))

        # Entfernen aus open_trades, damit Dashboard wirklich nur offene Trades zeigt.
        cur.execute("""
            DELETE FROM open_trades
            WHERE client_trade_id=?
        """, (client_trade_id,))

        closed_item = {
            "client_trade_id": client_trade_id,
            "decision_id": decision_id,
            "market": market,
            "direction": direction,
            "setup_name": setup_name,
            "result": result,
            "pnl_r": pnl_r,
            "exit_price": exit_price,
            "reason": close_reason,
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "high": high,
            "low": low,
            "timeframe": tf,
        }
        closed.append(closed_item)

        # Telegram senden, außer bei TEST-Markt.
        if market != "TEST":
            try:
                icon = "✅" if result == "WIN" else "❌"
                title = "TP1 HIT" if result == "WIN" else "SL HIT"
                if ambiguous:
                    title = "AMBIGUOUS TP/SL - CONSERVATIVE SL"

                msg = (
                    f"{icon} V7000 {title}\n"
                    f"Market: {market} {direction}\n"
                    f"Setup: {setup_name}\n"
                    f"Entry: {entry}\n"
                    f"Exit: {exit_price}\n"
                    f"SL: {sl}\n"
                    f"TP1: {tp1}\n"
                    f"PnL R: {pnl_r}\n"
                    f"Decision ID: {decision_id}\n"
                    f"Reason: {close_reason}"
                )

                if "send_telegram" in globals():
                    await send_telegram(msg)
            except Exception as tg_e:
                closed_item["telegram_error"] = str(tg_e)

    con.commit()
    con.close()


    # === V7000 SHADOW HEARTBEAT MONITOR PATCH ===
    shadow_closed = []
    try:
        import sqlite3 as _sh_sqlite3
        from datetime import datetime as _sh_dt, timezone as _sh_tz

        _sh_con = _sh_sqlite3.connect("data/v7000_learning.sqlite3")
        _sh_con.row_factory = _sh_sqlite3.Row

        _sh_con.execute("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            shadow_id TEXT PRIMARY KEY,
            client_trade_id TEXT,
            market TEXT,
            direction TEXT,
            setup_name TEXT,
            entry REAL,
            sl REAL,
            tp1 REAL,
            confidence REAL,
            reason TEXT,
            raw_json TEXT,
            status TEXT,
            opened_at TEXT
        )
        """)

        _sh_con.execute("""
        CREATE TABLE IF NOT EXISTS shadow_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shadow_id TEXT,
            client_trade_id TEXT,
            market TEXT,
            direction TEXT,
            setup_name TEXT,
            result TEXT,
            pnl_r REAL,
            exit_price REAL,
            notes TEXT,
            closed_at TEXT
        )
        """)

        _rows = _sh_con.execute("""
            SELECT *
            FROM shadow_trades
            WHERE market = ?
              AND status = 'OPEN'
        """, (market,)).fetchall()

        for _r in _rows:
            _t = dict(_r)
            _dir = str(_t.get("direction") or "").upper()
            _entry = float(_t.get("entry") or 0)
            _sl = float(_t.get("sl") or 0)
            _tp1 = float(_t.get("tp1") or 0)
            _risk = abs(_entry - _sl)

            if _risk <= 0:
                continue

            _hit_sl = False
            _hit_tp = False

            if _dir == "SHORT":
                _hit_sl = high >= _sl
                _hit_tp = low <= _tp1
            else:
                _hit_sl = low <= _sl
                _hit_tp = high >= _tp1

            if not _hit_sl and not _hit_tp:
                continue

            if _hit_sl and _hit_tp:
                _result = "LOSS"
                _pnl_r = -1.0
                _exit = _sl
                _reason = "SHADOW_AMBIGUOUS_TP_SL_CONSERVATIVE_SL"
            elif _hit_sl:
                _result = "LOSS"
                _pnl_r = -1.0
                _exit = _sl
                _reason = "SHADOW_SL_HIT"
            else:
                _result = "WIN"
                if _dir == "SHORT":
                    _pnl_r = round((_entry - _tp1) / _risk, 2)
                else:
                    _pnl_r = round((_tp1 - _entry) / _risk, 2)
                _exit = _tp1
                _reason = "SHADOW_TP1_HIT"

            _closed_at = _sh_dt.now(_sh_tz.utc).isoformat()
            _notes = (
                f"{_reason}; shadow_id={_t.get('shadow_id')}; "
                f"client_trade_id={_t.get('client_trade_id')}; market={_t.get('market')}; "
                f"direction={_dir}; setup={_t.get('setup_name')}; "
                f"entry={_entry}; sl={_sl}; tp1={_tp1}; "
                f"candle_high={high}; candle_low={low}; close={close}"
            )

            _sh_con.execute("""
                INSERT INTO shadow_outcomes (
                    shadow_id, client_trade_id, market, direction, setup_name,
                    result, pnl_r, exit_price, notes, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                _t.get("shadow_id"),
                _t.get("client_trade_id"),
                _t.get("market"),
                _dir,
                _t.get("setup_name"),
                _result,
                _pnl_r,
                _exit,
                _notes,
                _closed_at,
            ))

            _sh_con.execute("""
                UPDATE shadow_trades
                SET status = 'CLOSED'
                WHERE shadow_id = ?
            """, (_t.get("shadow_id"),))

            shadow_closed.append({
                "shadow_id": _t.get("shadow_id"),
                "market": _t.get("market"),
                "direction": _dir,
                "setup_name": _t.get("setup_name"),
                "result": _result,
                "pnl_r": _pnl_r,
                "exit_price": _exit,
                "reason": _reason,
            })

        _sh_con.commit()
        _sh_con.close()
    except Exception as _sh_e:
        try:
            print(f"SHADOW_HEARTBEAT_ERROR {type(_sh_e).__name__}: {_sh_e}", flush=True)
        except Exception:
            pass
    # === V7000 SHADOW HEARTBEAT MONITOR PATCH END ===

    return {
        "accepted": True,
        "type": "price_heartbeat",
        "market": market,
        "timeframe": tf,
        "high": high,
        "low": low,
        "close": close,
        "checked_open_trades": checked,
        "closed_count": len(closed),
        "closed": closed,
        "shadow_closed_count": len(shadow_closed),
        "shadow_closed": shadow_closed,
    }


# === V7000 LEARNING PAGE PATCH START ===

from fastapi.responses import HTMLResponse as _V7000_HTMLResponse, JSONResponse as _V7000_JSONResponse

def _v7000_learning_db_path():
    import os
    for _p in ("/app/data/v7000_learning.sqlite3", "data/v7000_learning.sqlite3"):
        if os.path.exists(_p):
            return _p
    return "/app/data/v7000_learning.sqlite3"

def _v7000_learning_rows(_sql, _params=()):
    import sqlite3
    _con = sqlite3.connect(_v7000_learning_db_path())
    _con.row_factory = sqlite3.Row
    try:
        return [dict(_r) for _r in _con.execute(_sql, _params).fetchall()]
    finally:
        _con.close()

def _v7000_q(_x):
    import html
    return html.escape("" if _x is None else str(_x))

def _v7000_num(_x):
    try:
        return float(_x)
    except Exception:
        return 0.0

def _v7000_learning_payload():
    _totals = _v7000_learning_rows("""
        SELECT
          COUNT(*) AS closed_trades,
          COALESCE(SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END), 0) AS wins,
          COALESCE(SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END), 0) AS losses,
          ROUND(COALESCE(AVG(pnl_r), 0), 2) AS avg_r,
          ROUND(COALESCE(SUM(pnl_r), 0), 2) AS total_r,
          CASE
            WHEN COUNT(*) > 0 THEN ROUND(100.0 * SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) / COUNT(*), 1)
            ELSE 0
          END AS winrate
        FROM trade_outcomes
    """)

    _groups = _v7000_learning_rows("""
        SELECT
          d.market,
          d.direction,
          d.setup_name,
          d.session,
          d.timeframe,
          COUNT(*) AS closed_trades,
          SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN o.pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
          CASE
            WHEN COUNT(*) > 0 THEN ROUND(100.0 * SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END) / COUNT(*), 1)
            ELSE 0
          END AS winrate,
          ROUND(AVG(o.pnl_r), 2) AS avg_r,
          ROUND(SUM(o.pnl_r), 2) AS total_r,
          ROUND(AVG(d.risk_r), 2) AS avg_risk_r,
          MAX(o.closed_at) AS last_closed,
          CASE
            WHEN COUNT(*) >= 3 THEN 'LEARNING_ACTIVE'
            ELSE 'WAITING_FOR_3_TRADES'
          END AS learning_status
        FROM trade_outcomes o
        JOIN setup_decisions d ON d.id = o.decision_id
        GROUP BY d.market, d.direction, d.setup_name, d.session, d.timeframe
        ORDER BY closed_trades DESC, total_r DESC, last_closed DESC
    """)

    _recent = _v7000_learning_rows("""
        SELECT
          o.id AS outcome_id,
          o.closed_at,
          d.id AS decision_id,
          d.market,
          d.direction,
          d.setup_name,
          d.session,
          d.timeframe,
          d.technical_score,
          d.risk_r,
          o.result,
          o.pnl_r,
          o.exit_price,
          substr(o.notes, 1, 180) AS notes
        FROM trade_outcomes o
        JOIN setup_decisions d ON d.id = o.decision_id
        ORDER BY o.id DESC
        LIMIT 50
    """)

    _open = _v7000_learning_rows("""
        SELECT
          client_trade_id,
          decision_id,
          market,
          direction,
          setup_name,
          entry,
          sl,
          tp1,
          status,
          opened_at
        FROM open_trades
        ORDER BY opened_at DESC
        LIMIT 50
    """)

    return {
        "ok": True,
        "db": _v7000_learning_db_path(),
        "totals": _totals[0] if _totals else {},
        "groups": _groups,
        "recent_outcomes": _recent,
        "open_trades": _open,
    }

def _v7000_learning_html():
    _data = _v7000_learning_payload()
    _t = _data.get("totals", {})

    def _badge_status(_s):
        if _s == "LEARNING_ACTIVE":
            return '<span class="badge active">ACTIVE</span>'
        return '<span class="badge wait">WAIT</span>'

    def _r_class(_v):
        _n = _v7000_num(_v)
        if _n > 0:
            return "pos"
        if _n < 0:
            return "neg"
        return ""

    _group_rows = ""
    for r in _data.get("groups", []):
        _group_rows += f"""
        <tr>
          <td><b>{_v7000_q(r.get("market"))}</b></td>
          <td>{_v7000_q(r.get("direction"))}</td>
          <td>{_v7000_q(r.get("setup_name"))}</td>
          <td>{_v7000_q(r.get("session"))}</td>
          <td>{_v7000_q(r.get("timeframe"))}</td>
          <td>{_v7000_q(r.get("closed_trades"))}</td>
          <td class="pos">{_v7000_q(r.get("wins"))}</td>
          <td class="neg">{_v7000_q(r.get("losses"))}</td>
          <td>{_v7000_q(r.get("winrate"))}%</td>
          <td class="{_r_class(r.get("avg_r"))}">{_v7000_q(r.get("avg_r"))}</td>
          <td class="{_r_class(r.get("total_r"))}"><b>{_v7000_q(r.get("total_r"))}</b></td>
          <td>{_v7000_q(r.get("avg_risk_r"))}</td>
          <td>{_badge_status(r.get("learning_status"))}</td>
          <td class="small">{_v7000_q(r.get("last_closed"))}</td>
        </tr>
        """

    if not _group_rows:
        _group_rows = '<tr><td colspan="14">Noch keine abgeschlossenen Trades vorhanden.</td></tr>'

    _recent_rows = ""
    for r in _data.get("recent_outcomes", []):
        _recent_rows += f"""
        <tr>
          <td>{_v7000_q(r.get("outcome_id"))}</td>
          <td><b>{_v7000_q(r.get("market"))}</b></td>
          <td>{_v7000_q(r.get("direction"))}</td>
          <td>{_v7000_q(r.get("setup_name"))}</td>
          <td>{_v7000_q(r.get("session"))}</td>
          <td>{_v7000_q(r.get("timeframe"))}</td>
          <td>{_v7000_q(r.get("result"))}</td>
          <td class="{_r_class(r.get("pnl_r"))}"><b>{_v7000_q(r.get("pnl_r"))}</b></td>
          <td>{_v7000_q(r.get("risk_r"))}</td>
          <td>{_v7000_q(r.get("exit_price"))}</td>
          <td class="small">{_v7000_q(r.get("closed_at"))}</td>
        </tr>
        """

    if not _recent_rows:
        _recent_rows = '<tr><td colspan="11">Noch keine Outcomes.</td></tr>'

    _open_rows = ""
    for r in _data.get("open_trades", []):
        _open_rows += f"""
        <tr>
          <td><b>{_v7000_q(r.get("market"))}</b></td>
          <td>{_v7000_q(r.get("direction"))}</td>
          <td>{_v7000_q(r.get("setup_name"))}</td>
          <td>{_v7000_q(r.get("entry"))}</td>
          <td>{_v7000_q(r.get("sl"))}</td>
          <td>{_v7000_q(r.get("tp1"))}</td>
          <td>{_v7000_q(r.get("status"))}</td>
          <td class="small">{_v7000_q(r.get("opened_at"))}</td>
        </tr>
        """

    if not _open_rows:
        _open_rows = '<tr><td colspan="8">Keine offenen Trades.</td></tr>'

    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="20">
<title>V7000 Learning</title>
<style>
body {{
  margin:0;
  font-family: Arial, sans-serif;
  background:#0b0f14;
  color:#e8eef7;
}}
.header {{
  padding:18px;
  background:#121a24;
  border-bottom:1px solid #253142;
  position:sticky;
  top:0;
  z-index:10;
}}
h1 {{ margin:0 0 5px 0; font-size:24px; }}
.sub {{ color:#94a3b8; font-size:13px; }}
.wrap {{ padding:14px; }}
.cards {{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:10px;
  margin-bottom:14px;
}}
.card {{
  background:#121a24;
  border:1px solid #253142;
  border-radius:12px;
  padding:14px;
}}
.card .label {{ color:#94a3b8; font-size:12px; }}
.card .value {{ font-size:24px; font-weight:bold; margin-top:6px; }}
.section {{
  margin-top:18px;
  background:#121a24;
  border:1px solid #253142;
  border-radius:12px;
  overflow:hidden;
}}
.section h2 {{
  margin:0;
  padding:12px 14px;
  font-size:18px;
  border-bottom:1px solid #253142;
}}
.table-wrap {{ overflow-x:auto; }}
table {{
  width:100%;
  border-collapse:collapse;
  min-width:900px;
}}
th, td {{
  padding:9px 10px;
  border-bottom:1px solid #253142;
  text-align:left;
  font-size:13px;
  white-space:nowrap;
}}
th {{
  color:#94a3b8;
  background:#0f1620;
}}
.pos {{ color:#22c55e; }}
.neg {{ color:#ef4444; }}
.badge {{
  display:inline-block;
  padding:4px 8px;
  border-radius:999px;
  font-size:11px;
  font-weight:bold;
}}
.badge.active {{ background:#123d24; color:#22c55e; }}
.badge.wait {{ background:#3b2f12; color:#facc15; }}
.small {{ font-size:11px; color:#94a3b8; }}
.links a {{
  color:#93c5fd;
  text-decoration:none;
  margin-right:12px;
}}
@media (max-width:700px) {{
  h1 {{ font-size:20px; }}
  .card .value {{ font-size:20px; }}
  th, td {{ font-size:12px; padding:8px; }}
}}
</style>
</head>
<body>
<div class="header">
  <h1>🧠 V7000 Learning</h1>
  <div class="sub">Auto-Refresh alle 20 Sekunden · DB: {_v7000_q(_data.get("db"))}</div>
  <div class="links">
    <a href="/dashboard">Dashboard</a>
    <a href="/decisions">Decisions</a>
    <a href="/learning.json">JSON</a>
  </div>
</div>

<div class="wrap">
  <div class="cards">
    <div class="card"><div class="label">Closed Trades</div><div class="value">{_v7000_q(_t.get("closed_trades"))}</div></div>
    <div class="card"><div class="label">Wins</div><div class="value pos">{_v7000_q(_t.get("wins"))}</div></div>
    <div class="card"><div class="label">Losses</div><div class="value neg">{_v7000_q(_t.get("losses"))}</div></div>
    <div class="card"><div class="label">Winrate</div><div class="value">{_v7000_q(_t.get("winrate"))}%</div></div>
    <div class="card"><div class="label">Avg R</div><div class="value {_r_class(_t.get("avg_r"))}">{_v7000_q(_t.get("avg_r"))}</div></div>
    <div class="card"><div class="label">Total R</div><div class="value {_r_class(_t.get("total_r"))}">{_v7000_q(_t.get("total_r"))}</div></div>
  </div>

  <div class="section">
    <h2>Learning Gruppen</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Market</th><th>Side</th><th>Setup</th><th>Session</th><th>TF</th>
            <th>Trades</th><th>Wins</th><th>Loss</th><th>Winrate</th>
            <th>Avg R</th><th>Total R</th><th>Risk R</th><th>Status</th><th>Last</th>
          </tr>
        </thead>
        <tbody>{_group_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <h2>Letzte Outcomes</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>Market</th><th>Side</th><th>Setup</th><th>Session</th><th>TF</th>
            <th>Result</th><th>R</th><th>Risk R</th><th>Exit</th><th>Closed</th>
          </tr>
        </thead>
        <tbody>{_recent_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <h2>Offene Trades</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Market</th><th>Side</th><th>Setup</th><th>Entry</th><th>SL</th><th>TP1</th><th>Status</th><th>Opened</th>
          </tr>
        </thead>
        <tbody>{_open_rows}</tbody>
      </table>
    </div>
  </div>
</div>
</body>
</html>
"""

@app.get("/learning.json")
def v7000_learning_json():
    try:
        return _V7000_JSONResponse(_v7000_learning_payload())
    except Exception as _e:
        return _V7000_JSONResponse({"ok": False, "error": str(_e)}, status_code=500)

@app.get("/learning", response_class=_V7000_HTMLResponse)
def v7000_learning_page():
    try:
        return _V7000_HTMLResponse(_v7000_learning_html())
    except Exception as _e:
        return _V7000_HTMLResponse("<h1>V7000 Learning Error</h1><pre>" + _v7000_q(str(_e)) + "</pre>", status_code=500)

# === V7000 LEARNING PAGE PATCH END ===


# === V7000 MANUAL OUTCOME BUTTONS PATCH START ===

from fastapi import Request as _V7000_Manual_Request
from fastapi.responses import HTMLResponse as _V7000_Manual_HTMLResponse, JSONResponse as _V7000_Manual_JSONResponse

def _v7000_manual_db_path():
    import os
    for _p in ("/app/data/v7000_learning.sqlite3", "data/v7000_learning.sqlite3"):
        if os.path.exists(_p):
            return _p
    return "/app/data/v7000_learning.sqlite3"

def _v7000_manual_token_path():
    import os
    for _p in ("/app/data/MANUAL_CLOSE_TOKEN", "data/MANUAL_CLOSE_TOKEN"):
        if os.path.exists(_p):
            return _p
    return "/app/data/MANUAL_CLOSE_TOKEN"

def _v7000_manual_token_ok(_token):
    try:
        from pathlib import Path
        _real = Path(_v7000_manual_token_path()).read_text().strip()
        return bool(_real) and str(_token or "").strip() == _real
    except Exception:
        return False

def _v7000_manual_q(_x):
    import html
    return html.escape("" if _x is None else str(_x))

def _v7000_manual_f(_x, _default=0.0):
    try:
        if _x is None or _x == "":
            return _default
        return float(_x)
    except Exception:
        return _default

def _v7000_manual_rows(_sql, _params=()):
    import sqlite3
    _con = sqlite3.connect(_v7000_manual_db_path())
    _con.row_factory = sqlite3.Row
    try:
        return [dict(_r) for _r in _con.execute(_sql, _params).fetchall()]
    finally:
        _con.close()

def _v7000_manual_open_trades():
    return _v7000_manual_rows("""
        SELECT
          client_trade_id,
          decision_id,
          market,
          direction,
          setup_name,
          entry,
          sl,
          tp1,
          status,
          opened_at
        FROM open_trades
        ORDER BY opened_at DESC
        LIMIT 100
    """)

def _v7000_manual_calc_pnl_r(_direction, _entry, _sl, _exit_price):
    _entry = _v7000_manual_f(_entry)
    _sl = _v7000_manual_f(_sl)
    _exit = _v7000_manual_f(_exit_price)
    _risk = abs(_entry - _sl)
    if _risk <= 0:
        return 0.0

    _dir = str(_direction or "").upper()
    if _dir == "SHORT":
        return round((_entry - _exit) / _risk, 2)
    return round((_exit - _entry) / _risk, 2)

def _v7000_manual_close_trade(_client_trade_id, _result, _exit_price=None, _note=""):
    import sqlite3
    from datetime import datetime, timezone

    _result = str(_result or "").upper().strip()
    if _result not in ("WIN", "LOSS", "BE", "MANUAL"):
        return {"ok": False, "error": "Invalid result. Use WIN, LOSS, BE, MANUAL."}

    _con = sqlite3.connect(_v7000_manual_db_path())
    _con.row_factory = sqlite3.Row

    try:
        _trade = _con.execute("""
            SELECT
              client_trade_id,
              decision_id,
              market,
              direction,
              setup_name,
              entry,
              sl,
              tp1,
              status,
              opened_at
            FROM open_trades
            WHERE client_trade_id = ?
            LIMIT 1
        """, (_client_trade_id,)).fetchone()

        if not _trade:
            return {"ok": False, "error": "Open trade not found", "client_trade_id": _client_trade_id}

        _t = dict(_trade)
        _entry = _v7000_manual_f(_t.get("entry"))
        _sl = _v7000_manual_f(_t.get("sl"))
        _tp1 = _v7000_manual_f(_t.get("tp1"), _entry)

        if _result == "WIN":
            _exit = _v7000_manual_f(_exit_price, _tp1)
            _reason = "MANUAL_WIN"
        elif _result == "LOSS":
            _exit = _v7000_manual_f(_exit_price, _sl)
            _reason = "MANUAL_LOSS"
        elif _result == "BE":
            _exit = _v7000_manual_f(_exit_price, _entry)
            _reason = "MANUAL_BE"
        else:
            _exit = _v7000_manual_f(_exit_price, _entry)
            _reason = "MANUAL_CLOSE"

        _pnl_r = _v7000_manual_calc_pnl_r(_t.get("direction"), _entry, _sl, _exit)

        if _result == "BE":
            _pnl_r = 0.0

        _closed_at = datetime.now(timezone.utc).isoformat()

        _notes = (
            f"{_reason}; "
            f"client_trade_id={_t.get('client_trade_id')}; "
            f"market={_t.get('market')}; "
            f"direction={_t.get('direction')}; "
            f"setup={_t.get('setup_name')}; "
            f"entry={_entry}; sl={_sl}; tp1={_tp1}; "
            f"manual_exit={_exit}; pnl_r={_pnl_r}; "
            f"note={_note or ''}"
        )

        _con.execute("""
            INSERT INTO trade_outcomes (
              decision_id, result, pnl_r, exit_price, notes, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            _t.get("decision_id"),
            "BE" if _result == "BE" else ("WIN" if _pnl_r > 0 else ("LOSS" if _pnl_r < 0 else "BE")),
            _pnl_r,
            _exit,
            _notes,
            _closed_at,
        ))

        _con.execute("""
            DELETE FROM open_trades
            WHERE client_trade_id = ?
        """, (_client_trade_id,))

        _con.commit()

        return {
            "ok": True,
            "closed": True,
            "client_trade_id": _t.get("client_trade_id"),
            "decision_id": _t.get("decision_id"),
            "market": _t.get("market"),
            "direction": _t.get("direction"),
            "setup_name": _t.get("setup_name"),
            "result": "BE" if _result == "BE" else ("WIN" if _pnl_r > 0 else ("LOSS" if _pnl_r < 0 else "BE")),
            "pnl_r": _pnl_r,
            "exit_price": _exit,
            "reason": _reason,
            "closed_at": _closed_at,
        }

    except Exception as _e:
        try:
            _con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(_e)}
    finally:
        _con.close()

def _v7000_manual_html(_token):
    _rows = _v7000_manual_open_trades()

    _cards = ""
    for r in _rows:
        _cid = _v7000_manual_q(r.get("client_trade_id"))
        _market = _v7000_manual_q(r.get("market"))
        _direction = _v7000_manual_q(r.get("direction"))
        _setup = _v7000_manual_q(r.get("setup_name"))
        _entry = _v7000_manual_q(r.get("entry"))
        _sl = _v7000_manual_q(r.get("sl"))
        _tp1 = _v7000_manual_q(r.get("tp1"))
        _opened = _v7000_manual_q(r.get("opened_at"))

        _cards += f"""
        <div class="trade">
          <div class="top">
            <div>
              <div class="market">{_market} <span>{_direction}</span></div>
              <div class="setup">{_setup}</div>
            </div>
            <div class="status">OPEN</div>
          </div>

          <div class="levels">
            <div><span>Entry</span><b>{_entry}</b></div>
            <div><span>SL</span><b>{_sl}</b></div>
            <div><span>TP1</span><b>{_tp1}</b></div>
          </div>

          <div class="opened">{_opened}</div>

          <div class="buttons">
            <button class="win" onclick="closeTrade('{_cid}', 'WIN', '{_tp1}')">WIN / TP</button>
            <button class="loss" onclick="closeTrade('{_cid}', 'LOSS', '{_sl}')">LOSS / SL</button>
            <button class="be" onclick="closeTrade('{_cid}', 'BE', '{_entry}')">BE</button>
            <button class="manual" onclick="manualClose('{_cid}', '{_entry}')">MANUAL PRICE</button>
          </div>
        </div>
        """

    if not _cards:
        _cards = '<div class="empty">Keine offenen Trades.</div>'

    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>V7000 Manual Close</title>
<style>
body {{
  margin:0;
  font-family:Arial,sans-serif;
  background:#0b0f14;
  color:#e8eef7;
}}
.header {{
  padding:18px;
  background:#121a24;
  border-bottom:1px solid #253142;
  position:sticky;
  top:0;
  z-index:10;
}}
h1 {{ margin:0 0 6px 0; font-size:24px; }}
.sub {{ color:#94a3b8; font-size:13px; }}
.links a {{
  color:#93c5fd;
  text-decoration:none;
  margin-right:12px;
}}
.wrap {{ padding:14px; }}
.trade {{
  background:#121a24;
  border:1px solid #253142;
  border-radius:14px;
  padding:14px;
  margin-bottom:14px;
}}
.top {{
  display:flex;
  justify-content:space-between;
  gap:12px;
  align-items:flex-start;
}}
.market {{
  font-size:24px;
  font-weight:bold;
}}
.market span {{
  font-size:16px;
  color:#94a3b8;
  margin-left:8px;
}}
.setup {{
  color:#94a3b8;
  margin-top:4px;
  font-size:13px;
  word-break:break-word;
}}
.status {{
  color:#facc15;
  background:#3b2f12;
  padding:5px 9px;
  border-radius:999px;
  font-weight:bold;
  font-size:12px;
}}
.levels {{
  display:grid;
  grid-template-columns:repeat(3,1fr);
  gap:10px;
  margin-top:14px;
}}
.levels div {{
  background:#0f1620;
  border:1px solid #253142;
  border-radius:10px;
  padding:10px;
}}
.levels span {{
  display:block;
  color:#94a3b8;
  font-size:12px;
}}
.levels b {{
  font-size:16px;
}}
.opened {{
  color:#94a3b8;
  font-size:11px;
  margin-top:10px;
}}
.buttons {{
  display:grid;
  grid-template-columns:repeat(2,1fr);
  gap:10px;
  margin-top:14px;
}}
button {{
  border:0;
  border-radius:12px;
  padding:14px 10px;
  font-size:15px;
  font-weight:bold;
  color:white;
}}
.win {{ background:#16a34a; }}
.loss {{ background:#dc2626; }}
.be {{ background:#64748b; }}
.manual {{ background:#2563eb; }}
.empty {{
  background:#121a24;
  border:1px solid #253142;
  border-radius:14px;
  padding:20px;
  color:#94a3b8;
}}
.warn {{
  margin-top:10px;
  color:#facc15;
  font-size:13px;
}}
@media (max-width:700px) {{
  .market {{ font-size:21px; }}
  .levels {{ grid-template-columns:1fr; }}
  .buttons {{ grid-template-columns:1fr; }}
}}
</style>
</head>
<body>
<div class="header">
  <h1>🛠 V7000 Manual Close</h1>
  <div class="sub">Manuelle Outcomes für Learning · Auto-Refresh 30s</div>
  <div class="links">
    <a href="/learning">Learning</a>
    <a href="/dashboard">Dashboard</a>
    <a href="/decisions">Decisions</a>
  </div>
  <div class="warn">Nur echte Trades schließen. Jeder Button löscht den Trade aus Open Trades und speichert das Outcome.</div>
</div>

<div class="wrap">
  {_cards}
</div>

<script>
const TOKEN = "{_v7000_manual_q(_token)}";

async function closeTrade(clientId, result, exitPrice) {{
  let ok = confirm("Trade wirklich schließen als " + result + "?");
  if (!ok) return;

  let note = prompt("Notiz optional:", "");
  if (note === null) note = "";

  const res = await fetch("/manual/outcome?token=" + encodeURIComponent(TOKEN), {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{
      client_trade_id: clientId,
      result: result,
      exit_price: exitPrice,
      note: note
    }})
  }});

  const data = await res.json();
  if (!data.ok) {{
    alert("FEHLER: " + JSON.stringify(data));
    return;
  }}

  alert("Gespeichert: " + data.market + " " + data.direction + " " + data.result + " " + data.pnl_r + "R");
  location.reload();
}}

async function manualClose(clientId, defaultEntry) {{
  let result = prompt("Ergebnis eingeben: WIN, LOSS, BE oder MANUAL", "MANUAL");
  if (result === null) return;
  result = result.toUpperCase().trim();

  let price = prompt("Exit Preis eingeben:", defaultEntry);
  if (price === null) return;

  let note = prompt("Notiz optional:", "manual_close");
  if (note === null) note = "";

  const res = await fetch("/manual/outcome?token=" + encodeURIComponent(TOKEN), {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{
      client_trade_id: clientId,
      result: result,
      exit_price: price,
      note: note
    }})
  }});

  const data = await res.json();
  if (!data.ok) {{
    alert("FEHLER: " + JSON.stringify(data));
    return;
  }}

  alert("Gespeichert: " + data.market + " " + data.direction + " " + data.result + " " + data.pnl_r + "R");
  location.reload();
}}
</script>
</body>
</html>
"""

@app.get("/manual", response_class=_V7000_Manual_HTMLResponse)
def v7000_manual_page(token: str = ""):
    if not _v7000_manual_token_ok(token):
        return _V7000_Manual_HTMLResponse("<h1>403</h1><p>Token fehlt oder ist falsch.</p>", status_code=403)
    return _V7000_Manual_HTMLResponse(_v7000_manual_html(token))

@app.post("/manual/outcome")
async def v7000_manual_outcome(request: _V7000_Manual_Request, token: str = ""):
    if not _v7000_manual_token_ok(token):
        return _V7000_Manual_JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    try:
        _data = await request.json()
    except Exception as _e:
        return _V7000_Manual_JSONResponse({"ok": False, "error": "invalid json", "detail": str(_e)}, status_code=400)

    _res = _v7000_manual_close_trade(
        _data.get("client_trade_id"),
        _data.get("result"),
        _data.get("exit_price"),
        _data.get("note", "")
    )

    return _V7000_Manual_JSONResponse(_res, status_code=200 if _res.get("ok") else 400)

# === V7000 MANUAL OUTCOME BUTTONS PATCH END ===


# === V7000 HEARTBEAT STATUS PAGE PATCH START ===

from fastapi.responses import HTMLResponse as _V7000_HB_HTMLResponse, JSONResponse as _V7000_HB_JSONResponse

def _v7000_hb_db_path():
    import os
    for _p in ("/app/data/v7000_learning.sqlite3", "data/v7000_learning.sqlite3"):
        if os.path.exists(_p):
            return _p
    return "/app/data/v7000_learning.sqlite3"

def _v7000_hb_q(_x):
    import html
    return html.escape("" if _x is None else str(_x))

def _v7000_hb_rows(_sql, _params=()):
    import sqlite3
    _con = sqlite3.connect(_v7000_hb_db_path())
    _con.row_factory = sqlite3.Row
    try:
        return [dict(_r) for _r in _con.execute(_sql, _params).fetchall()]
    finally:
        _con.close()

def _v7000_hb_ensure_table():
    import sqlite3
    _con = sqlite3.connect(_v7000_hb_db_path())
    try:
        _con.execute("""
            CREATE TABLE IF NOT EXISTS price_heartbeats (
                market TEXT PRIMARY KEY,
                timeframe TEXT,
                ticker TEXT,
                source TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                received_at TEXT NOT NULL,
                raw_json TEXT
            )
        """)
        _con.commit()
    finally:
        _con.close()

def _v7000_hb_age_seconds(_ts):
    from datetime import datetime, timezone
    try:
        _dt = datetime.fromisoformat(str(_ts).replace("Z", "+00:00"))
        if _dt.tzinfo is None:
            _dt = _dt.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - _dt).total_seconds())
    except Exception:
        return None

def _v7000_hb_status(_age, _missing=False):
    if _missing:
        return "MISSING"
    if _age is None:
        return "UNKNOWN"
    if _age <= 180:
        return "OK"
    if _age <= 300:
        return "WARN"
    return "STALE"

def _v7000_hb_payload():
    _v7000_hb_ensure_table()

    _hb = _v7000_hb_rows("""
        SELECT market, timeframe, ticker, source, open, high, low, close, received_at
        FROM price_heartbeats
        ORDER BY received_at DESC
    """)

    _open = _v7000_hb_rows("""
        SELECT market, COUNT(*) AS open_count
        FROM open_trades
        GROUP BY market
    """)

    _open_map = {str(r.get("market")): int(r.get("open_count") or 0) for r in _open}
    _seen = set()
    _rows = []

    for r in _hb:
        m = str(r.get("market") or "")
        _seen.add(m)
        age = _v7000_hb_age_seconds(r.get("received_at"))
        r["age_seconds"] = age
        r["status"] = _v7000_hb_status(age)
        r["open_trades"] = _open_map.get(m, 0)
        _rows.append(r)

    for m, cnt in _open_map.items():
        if m not in _seen:
            _rows.append({
                "market": m,
                "timeframe": "",
                "ticker": "",
                "source": "",
                "open": None,
                "high": None,
                "low": None,
                "close": None,
                "received_at": None,
                "age_seconds": None,
                "status": "MISSING",
                "open_trades": cnt,
            })

    _rows.sort(key=lambda x: (
        0 if x.get("open_trades", 0) else 1,
        {"MISSING": 0, "STALE": 1, "WARN": 2, "OK": 3, "UNKNOWN": 4}.get(x.get("status"), 9),
        str(x.get("market") or "")
    ))

    _summary = {
        "markets": len(_rows),
        "ok": sum(1 for r in _rows if r.get("status") == "OK"),
        "warn": sum(1 for r in _rows if r.get("status") == "WARN"),
        "stale": sum(1 for r in _rows if r.get("status") == "STALE"),
        "missing": sum(1 for r in _rows if r.get("status") == "MISSING"),
        "open_trade_markets": sum(1 for r in _rows if r.get("open_trades", 0) > 0),
    }

    return {
        "ok": True,
        "db": _v7000_hb_db_path(),
        "summary": _summary,
        "heartbeats": _rows,
    }

def _v7000_hb_html():
    _data = _v7000_hb_payload()
    _s = _data.get("summary", {})

    def badge(st):
        st = str(st or "UNKNOWN")
        cls = st.lower()
        return f'<span class="badge {cls}">{_v7000_hb_q(st)}</span>'

    rows = ""
    for r in _data.get("heartbeats", []):
        rows += f"""
        <tr>
          <td><b>{_v7000_hb_q(r.get("market"))}</b></td>
          <td>{badge(r.get("status"))}</td>
          <td>{_v7000_hb_q(r.get("age_seconds"))}</td>
          <td>{_v7000_hb_q(r.get("timeframe"))}</td>
          <td>{_v7000_hb_q(r.get("close"))}</td>
          <td>{_v7000_hb_q(r.get("high"))}</td>
          <td>{_v7000_hb_q(r.get("low"))}</td>
          <td>{_v7000_hb_q(r.get("open_trades"))}</td>
          <td>{_v7000_hb_q(r.get("source"))}</td>
          <td class="small">{_v7000_hb_q(r.get("received_at"))}</td>
        </tr>
        """

    if not rows:
        rows = '<tr><td colspan="10">Noch keine Heartbeats gespeichert. Warte 1–2 Minuten oder prüfe TradingView-Alarme.</td></tr>'

    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="20">
<title>V7000 Heartbeat</title>
<style>
body {{
  margin:0;
  font-family:Arial,sans-serif;
  background:#0b0f14;
  color:#e8eef7;
}}
.header {{
  padding:18px;
  background:#121a24;
  border-bottom:1px solid #253142;
  position:sticky;
  top:0;
  z-index:10;
}}
h1 {{ margin:0 0 6px 0; font-size:24px; }}
.sub {{ color:#94a3b8; font-size:13px; }}
.links a {{ color:#93c5fd; text-decoration:none; margin-right:12px; }}
.wrap {{ padding:14px; }}
.cards {{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
  gap:10px;
  margin-bottom:14px;
}}
.card {{
  background:#121a24;
  border:1px solid #253142;
  border-radius:12px;
  padding:14px;
}}
.card .label {{ color:#94a3b8; font-size:12px; }}
.card .value {{ font-size:24px; font-weight:bold; margin-top:6px; }}
.section {{
  background:#121a24;
  border:1px solid #253142;
  border-radius:12px;
  overflow:hidden;
}}
.section h2 {{
  margin:0;
  padding:12px 14px;
  border-bottom:1px solid #253142;
}}
.table-wrap {{ overflow-x:auto; }}
table {{
  width:100%;
  border-collapse:collapse;
  min-width:900px;
}}
th, td {{
  padding:9px 10px;
  border-bottom:1px solid #253142;
  text-align:left;
  font-size:13px;
  white-space:nowrap;
}}
th {{ color:#94a3b8; background:#0f1620; }}
.small {{ color:#94a3b8; font-size:11px; }}
.badge {{
  display:inline-block;
  padding:4px 8px;
  border-radius:999px;
  font-size:11px;
  font-weight:bold;
}}
.ok {{ background:#123d24; color:#22c55e; }}
.warn {{ background:#3b2f12; color:#facc15; }}
.stale {{ background:#3a1a1a; color:#f87171; }}
.missing {{ background:#3a1a1a; color:#ef4444; }}
.unknown {{ background:#334155; color:#cbd5e1; }}
@media (max-width:700px) {{
  h1 {{ font-size:21px; }}
  .card .value {{ font-size:20px; }}
}}
</style>
</head>
<body>
<div class="header">
  <h1>💓 V7000 Heartbeats</h1>
  <div class="sub">Auto-Refresh 20s · OK bis 180s · WARN bis 300s · STALE über 300s</div>
  <div class="links">
    <a href="/dashboard">Dashboard</a>
    <a href="/learning">Learning</a>
    <a href="/decisions">Decisions</a>
    <a href="/heartbeat.json">JSON</a>
  </div>
</div>

<div class="wrap">
  <div class="cards">
    <div class="card"><div class="label">Markets</div><div class="value">{_v7000_hb_q(_s.get("markets"))}</div></div>
    <div class="card"><div class="label">OK</div><div class="value ok">{_v7000_hb_q(_s.get("ok"))}</div></div>
    <div class="card"><div class="label">WARN</div><div class="value warn">{_v7000_hb_q(_s.get("warn"))}</div></div>
    <div class="card"><div class="label">STALE</div><div class="value stale">{_v7000_hb_q(_s.get("stale"))}</div></div>
    <div class="card"><div class="label">MISSING</div><div class="value missing">{_v7000_hb_q(_s.get("missing"))}</div></div>
    <div class="card"><div class="label">Open Markets</div><div class="value">{_v7000_hb_q(_s.get("open_trade_markets"))}</div></div>
  </div>

  <div class="section">
    <h2>Letzte Price Heartbeats</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Market</th><th>Status</th><th>Age sec</th><th>TF</th><th>Close</th><th>High</th><th>Low</th><th>Open Trades</th><th>Source</th><th>Received</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
</div>
</body>
</html>
"""

@app.get("/heartbeat.json")
def v7000_heartbeat_json():
    try:
        return _V7000_HB_JSONResponse(_v7000_hb_payload())
    except Exception as _e:
        return _V7000_HB_JSONResponse({"ok": False, "error": str(_e)}, status_code=500)

@app.get("/heartbeat", response_class=_V7000_HB_HTMLResponse)
def v7000_heartbeat_page():
    try:
        return _V7000_HB_HTMLResponse(_v7000_hb_html())
    except Exception as _e:
        return _V7000_HB_HTMLResponse("<h1>Heartbeat Error</h1><pre>" + _v7000_hb_q(str(_e)) + "</pre>", status_code=500)

# === V7000 HEARTBEAT STATUS PAGE PATCH END ===


# === V7000 MASTER PAGE PATCH START ===

from fastapi.responses import HTMLResponse as _V7000_Master_HTMLResponse, JSONResponse as _V7000_Master_JSONResponse

def _v7000_master_db_path():
    import os
    for _p in ("/app/data/v7000_learning.sqlite3", "data/v7000_learning.sqlite3"):
        if os.path.exists(_p):
            return _p
    return "/app/data/v7000_learning.sqlite3"

def _v7000_master_token_path():
    import os
    for _p in ("/app/data/MANUAL_CLOSE_TOKEN", "data/MANUAL_CLOSE_TOKEN"):
        if os.path.exists(_p):
            return _p
    return "/app/data/MANUAL_CLOSE_TOKEN"

def _v7000_master_token_ok(_token):
    try:
        from pathlib import Path
        _real = Path(_v7000_master_token_path()).read_text().strip()
        return bool(_real) and str(_token or "").strip() == _real
    except Exception:
        return False

def _v7000_master_q(_x):
    import html
    return html.escape("" if _x is None else str(_x))

def _v7000_master_rows(_sql, _params=()):
    import sqlite3
    _con = sqlite3.connect(_v7000_master_db_path())
    _con.row_factory = sqlite3.Row
    try:
        return [dict(_r) for _r in _con.execute(_sql, _params).fetchall()]
    finally:
        _con.close()

def _v7000_master_stats():
    import sqlite3
    from datetime import datetime, timezone

    db = _v7000_master_db_path()

    def rows(sql, params=()):
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    try:
        open_trades = rows("""
            SELECT client_trade_id, market, direction, setup_name, entry, sl, tp1, opened_at
            FROM open_trades
            ORDER BY opened_at DESC
        """)
    except Exception:
        open_trades = []

    try:
        out = rows("""
            SELECT
              COUNT(*) AS closed_trades,
              COALESCE(SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END), 0) AS wins,
              COALESCE(SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END), 0) AS losses,
              ROUND(COALESCE(SUM(pnl_r), 0), 2) AS total_r,
              CASE
                WHEN COUNT(*) > 0 THEN ROUND(100.0 * SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) / COUNT(*), 1)
                ELSE 0
              END AS winrate
            FROM trade_outcomes
        """)[0]
    except Exception:
        out = {"closed_trades": 0, "wins": 0, "losses": 0, "total_r": 0, "winrate": 0}

    try:
        allowed = rows("SELECT COUNT(*) AS n FROM signal_audit WHERE allow_trade=1")[0]["n"]
        blocked = rows("SELECT COUNT(*) AS n FROM signal_audit WHERE allow_trade=0")[0]["n"]
    except Exception:
        allowed = 0
        blocked = 0

    try:
        hb_rows = rows("""
            SELECT market, timeframe, close, high, low, received_at
            FROM price_heartbeats
        """)
    except Exception:
        hb_rows = []

    hb_map = {}
    for h in hb_rows:
        market = str(h.get("market") or "")
        ts = h.get("received_at")
        age = None
        status = "UNKNOWN"
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = int((datetime.now(timezone.utc) - dt).total_seconds())
            if age <= 180:
                status = "OK"
            elif age <= 300:
                status = "WARN"
            else:
                status = "STALE"
        except Exception:
            age = None
            status = "UNKNOWN"

        hb_map[market] = {
            "market": market,
            "age_seconds": age,
            "status": status,
            "close": h.get("close"),
            "received_at": ts,
            "timeframe": h.get("timeframe"),
        }

    open_missing = []
    open_stale = []
    open_ok = []

    for t in open_trades:
        m = str(t.get("market") or "")
        hb = hb_map.get(m)
        item = {
            "market": m,
            "direction": t.get("direction"),
            "setup_name": t.get("setup_name"),
            "entry": t.get("entry"),
            "sl": t.get("sl"),
            "tp1": t.get("tp1"),
            "heartbeat": hb,
        }

        if not hb:
            open_missing.append(item)
        elif hb.get("status") in ("STALE", "WARN", "UNKNOWN"):
            open_stale.append(item)
        else:
            open_ok.append(item)

    hb_ok = sum(1 for h in hb_map.values() if h.get("status") == "OK")
    hb_warn = sum(1 for h in hb_map.values() if h.get("status") == "WARN")
    hb_stale = sum(1 for h in hb_map.values() if h.get("status") == "STALE")

    return {
        "open_trades": len(open_trades),
        "open_trade_list": open_trades,
        "closed_trades": out.get("closed_trades", 0),
        "wins": out.get("wins", 0),
        "losses": out.get("losses", 0),
        "winrate": out.get("winrate", 0),
        "total_r": out.get("total_r", 0),
        "allowed": allowed,
        "blocked": blocked,
        "hb_markets": len(hb_map),
        "hb_ok": hb_ok,
        "hb_warn": hb_warn,
        "hb_stale": hb_stale,
        "open_hb_ok": len(open_ok),
        "open_hb_missing": len(open_missing),
        "open_hb_stale": len(open_stale),
        "open_missing_list": open_missing,
        "open_stale_list": open_stale,
    }

def _v7000_master_html(_token):
    s = _v7000_master_stats()
    manual = "/manual?token=" + _token
    mobile = "/mobile?token=" + _token

    def cls_num(v):
        try:
            f = float(v)
            if f > 0:
                return "pos"
            if f < 0:
                return "neg"
        except Exception:
            pass
        return ""

    cards = [
        ("📊", "Dashboard", "Hauptübersicht mit Märkten, offenen Trades und Status.", "/dashboard", "primary"),
        ("🧠", "Learning", "Outcomes, Winrate, R-Multiple und Learning-Gruppen.", "/learning", "green"),
        ("💓", "Heartbeats", "Welche Märkte senden aktuelle 1m-Preise.", "/heartbeat", "red"),
        ("🧾", "Decisions", "Mobile ALLOW/BLOCK Kartenansicht.", "/decisions2?token=" + _token, "blue"),
        ("🧪", "Shadow Trades", "Geblockte Near-Miss Signale als Paper-Trade.", "/shadow?token=" + _token, "purple"),
        ("🛠", "Manual Close", "Offene Trades manuell als WIN, LOSS oder BE schließen.", manual, "orange"),
        ("🛰", "Intelligence", "News-, Bias- und Marktintelligenz.", "/intelligence", "purple"),
        ("📅", "Calendar", "Wirtschaftskalender und Event-Risk.", "/calendar", "cyan"),
        ("📱", "Mobile Control", "Mobile Bot-Kontrolle inklusive Pause-Funktion.", mobile, "pink"),
        ("🖥", "Terminal", "Web-Terminal / Server-Zugriff.", "/terminal", "gray"),
        ("🔗", "Webhook Info", "Webhook-Status und TradingView-Endpunkte.", "/webhook", "gray"),
    ]

    card_html = ""
    for icon, title, desc, href, color in cards:
        card_html += f"""
        <a class="navcard {color}" href="{_v7000_master_q(href)}">
          <div class="icon">{_v7000_master_q(icon)}</div>
          <div class="ctxt">
            <div class="ctitle">{_v7000_master_q(title)}</div>
            <div class="cdesc">{_v7000_master_q(desc)}</div>
          </div>
          <div class="arrow">›</div>
        </a>
        """

    alert_html = ""
    if s.get("open_hb_missing", 0) > 0 or s.get("open_hb_stale", 0) > 0:
        alert_html += '<div class="alert bad"><b>⚠️ Heartbeat-Warnung für offene Trades</b><br>'
        for x in s.get("open_missing_list", []):
            alert_html += f'{_v7000_master_q(x.get("market"))} {_v7000_master_q(x.get("direction"))}: kein Heartbeat gespeichert<br>'
        for x in s.get("open_stale_list", []):
            hb = x.get("heartbeat") or {}
            alert_html += f'{_v7000_master_q(x.get("market"))} {_v7000_master_q(x.get("direction"))}: Heartbeat {hb.get("status")} / {hb.get("age_seconds")}s alt<br>'
        alert_html += '</div>'
    else:
        alert_html += '<div class="alert good"><b>✅ Offene Trades haben aktive Heartbeats</b></div>'

    open_html = ""
    for t in s.get("open_trade_list", []):
        open_html += f"""
        <div class="openitem">
          <b>{_v7000_master_q(t.get("market"))} {_v7000_master_q(t.get("direction"))}</b>
          <span>{_v7000_master_q(t.get("setup_name"))}</span>
          <small>Entry {_v7000_master_q(t.get("entry"))} · SL {_v7000_master_q(t.get("sl"))} · TP1 {_v7000_master_q(t.get("tp1"))}</small>
        </div>
        """
    if not open_html:
        open_html = '<div class="openitem"><b>Keine offenen Trades</b><span>Bot wartet auf neue Setups.</span></div>'

    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>V7000 Master</title>
<style>
:root {{
  --bg:#071019; --panel:#101a27; --border:#233247; --text:#e8eef7; --muted:#94a3b8;
  --green:#22c55e; --red:#ef4444; --yellow:#facc15; --blue:#3b82f6;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; font-family:Arial,sans-serif;
  background:radial-gradient(circle at top left,rgba(59,130,246,.18),transparent 32%),var(--bg);
  color:var(--text);
}}
.header {{
  padding:22px 18px; background:rgba(16,26,39,.94); border-bottom:1px solid var(--border);
  position:sticky; top:0; z-index:10;
}}
.hrow {{ display:flex; justify-content:space-between; gap:14px; align-items:center; max-width:1200px; margin:0 auto; }}
.logo {{ display:flex; align-items:center; gap:12px; }}
.mark {{ font-size:34px; }}
h1 {{ margin:0; font-size:28px; }}
.sub {{ color:var(--muted); font-size:13px; margin-top:4px; }}
.pill {{ padding:8px 12px; border:1px solid var(--border); border-radius:999px; background:#0b1220; font-size:13px; }}
.wrap {{ max-width:1200px; margin:0 auto; padding:16px; }}
.stats {{ display:grid; grid-template-columns:repeat(6,1fr); gap:12px; margin-bottom:14px; }}
.stat {{ background:rgba(16,26,39,.94); border:1px solid var(--border); border-radius:16px; padding:14px; min-height:88px; }}
.label {{ color:var(--muted); font-size:12px; }}
.value {{ margin-top:8px; font-size:26px; font-weight:bold; }}
.pos {{ color:var(--green); }} .neg {{ color:var(--red); }} .warntext {{ color:var(--yellow); }}
.alert {{ border-radius:16px; padding:14px; margin-bottom:14px; line-height:1.45; border:1px solid var(--border); }}
.alert.good {{ background:rgba(34,197,94,.08); border-color:#14532d; color:#86efac; }}
.alert.bad {{ background:rgba(239,68,68,.10); border-color:#7f1d1d; color:#fecaca; }}
.openbox {{ background:rgba(16,26,39,.94); border:1px solid var(--border); border-radius:16px; padding:14px; margin-bottom:14px; }}
.openbox h2 {{ margin:0 0 10px 0; font-size:18px; }}
.openitem {{ background:#0b1220; border:1px solid var(--border); border-radius:12px; padding:10px; margin-top:8px; }}
.openitem b {{ display:block; font-size:17px; }}
.openitem span {{ display:block; color:var(--muted); font-size:13px; margin-top:3px; }}
.openitem small {{ display:block; color:#cbd5e1; margin-top:6px; }}
.grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }}
.navcard {{
  display:flex; align-items:center; gap:14px; text-decoration:none; color:var(--text);
  background:rgba(16,26,39,.94); border:1px solid var(--border); border-radius:18px;
  padding:16px; min-height:108px;
}}
.icon {{ width:52px; height:52px; border-radius:16px; display:flex; align-items:center; justify-content:center; font-size:28px; background:#0b1220; border:1px solid var(--border); }}
.ctxt {{ flex:1; min-width:0; }} .ctitle {{ font-size:19px; font-weight:bold; margin-bottom:5px; }}
.cdesc {{ color:var(--muted); font-size:13px; line-height:1.35; }}
.arrow {{ font-size:34px; color:#64748b; }}
.primary {{ border-left:4px solid #60a5fa; }} .green {{ border-left:4px solid #22c55e; }}
.red {{ border-left:4px solid #ef4444; }} .blue {{ border-left:4px solid #3b82f6; }}
.orange {{ border-left:4px solid #f97316; }} .purple {{ border-left:4px solid #a855f7; }}
.cyan {{ border-left:4px solid #06b6d4; }} .pink {{ border-left:4px solid #ec4899; }}
.gray {{ border-left:4px solid #64748b; }}
.footer {{ color:var(--muted); font-size:12px; text-align:center; padding:24px 10px 40px; }}
@media(max-width:950px) {{ .stats {{ grid-template-columns:repeat(3,1fr); }} .grid {{ grid-template-columns:repeat(2,1fr); }} }}
@media(max-width:640px) {{
  .pill {{ display:none; }} h1 {{ font-size:24px; }} .wrap {{ padding:12px; }}
  .stats {{ grid-template-columns:repeat(2,1fr); gap:10px; }} .value {{ font-size:24px; }}
  .grid {{ grid-template-columns:1fr; gap:11px; }} .navcard {{ min-height:92px; padding:14px; }}
}}
</style>
</head>
<body>
<div class="header">
  <div class="hrow">
    <div class="logo">
      <div class="mark">🚀</div>
      <div>
        <h1>V7000 Master</h1>
        <div class="sub">Zentrale Bot-Steuerung · Auto-Refresh 30s · Handy & Desktop</div>
      </div>
    </div>
    <div class="pill">Online</div>
  </div>
</div>

<div class="wrap">
  <div class="stats">
    <div class="stat"><div class="label">Open Trades</div><div class="value">{_v7000_master_q(s.get("open_trades"))}</div></div>
    <div class="stat"><div class="label">Closed</div><div class="value">{_v7000_master_q(s.get("closed_trades"))}</div></div>
    <div class="stat"><div class="label">Winrate</div><div class="value">{_v7000_master_q(s.get("winrate"))}%</div></div>
    <div class="stat"><div class="label">Total R</div><div class="value {cls_num(s.get("total_r"))}">{_v7000_master_q(s.get("total_r"))}</div></div>
    <div class="stat"><div class="label">HB OK</div><div class="value pos">{_v7000_master_q(s.get("hb_ok"))}/{_v7000_master_q(s.get("hb_markets"))}</div></div>
    <div class="stat"><div class="label">Open HB Missing</div><div class="value {'neg' if s.get("open_hb_missing") else 'pos'}">{_v7000_master_q(s.get("open_hb_missing"))}</div></div>
  </div>

  {alert_html}

  {_v72425e_master_widget_html(_token)}

  {_compact_calendar_widget(_token)}

  {_compact_fj_widget(_token)}

  {_compact_fj_impact_widget(_token)}

  <div class="openbox">
    <h2>Offene Trades</h2>
    {open_html}
  </div>

  <div class="grid">
    {card_html}
  </div>

  {_v7100_master_compact_board_html(_token)}

  {_v7000_master_outcome_board_html(_token)}

  {_v7000_master_live_r_board_html(_token)}

  {_v7000_master_cluster_board_html(_token)}

  {_v7000_master_shadow_board_html(_token)}
</div>

<div class="footer">V7000 Decision Layer · Master Navigation · DB: {_v7000_master_q(_v7000_master_db_path())}</div>
</body>
</html>
"""



# === V7000 MASTER SAFE SHADOW BOARD PATCH ===
def _v7000_master_shadow_board_html(_token):
    import sqlite3

    def q(x):
        import html
        return html.escape("" if x is None else str(x))

    def cls(v):
        try:
            f = float(v)
            if f > 0:
                return "#22c55e"
            if f < 0:
                return "#ef4444"
        except Exception:
            pass
        return "#e8eef7"

    db = _v7000_master_db_path()
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    try:
        con.execute("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            shadow_id TEXT PRIMARY KEY,
            client_trade_id TEXT,
            market TEXT,
            direction TEXT,
            setup_name TEXT,
            entry REAL,
            sl REAL,
            tp1 REAL,
            confidence REAL,
            reason TEXT,
            raw_json TEXT,
            status TEXT,
            opened_at TEXT
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS shadow_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shadow_id TEXT,
            client_trade_id TEXT,
            market TEXT,
            direction TEXT,
            setup_name TEXT,
            result TEXT,
            pnl_r REAL,
            exit_price REAL,
            notes TEXT,
            closed_at TEXT
        )
        """)

        s = dict(con.execute("""
            SELECT
              (SELECT COUNT(*) FROM shadow_trades WHERE status='OPEN') AS open_shadow,
              (SELECT COUNT(*) FROM shadow_trades WHERE status='CLOSED') AS closed_shadow,
              COUNT(o.id) AS outcomes,
              COALESCE(SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END),0) AS wins,
              COALESCE(SUM(CASE WHEN o.pnl_r <= 0 THEN 1 ELSE 0 END),0) AS losses,
              ROUND(COALESCE(SUM(o.pnl_r),0),2) AS total_r,
              CASE
                WHEN COUNT(o.id) > 0 THEN ROUND(100.0 * SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END) / COUNT(o.id),1)
                ELSE 0
              END AS winrate
            FROM shadow_outcomes o
        """).fetchone())

        open_rows = [dict(r) for r in con.execute("""
            SELECT market, direction, setup_name, entry, sl, tp1, confidence, opened_at
            FROM shadow_trades
            WHERE status='OPEN'
            ORDER BY opened_at DESC
            LIMIT 5
        """).fetchall()]

        recent_rows = [dict(r) for r in con.execute("""
            SELECT market, direction, setup_name, result, pnl_r, exit_price, closed_at
            FROM shadow_outcomes
            ORDER BY id DESC
            LIMIT 5
        """).fetchall()]

        group_rows = [dict(r) for r in con.execute("""
            SELECT
              o.market,
              o.direction,
              o.setup_name,
              COUNT(*) AS trades,
              SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN o.pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
              ROUND(SUM(o.pnl_r),2) AS total_r,
              ROUND(AVG(COALESCE(t.confidence,0)),1) AS avg_confidence
            FROM shadow_outcomes o
            LEFT JOIN shadow_trades t ON t.shadow_id=o.shadow_id
            GROUP BY o.market, o.direction, o.setup_name
            ORDER BY trades DESC, total_r DESC
            LIMIT 8
        """).fetchall()]
    finally:
        con.close()

    def stat(label, value, color="#e8eef7"):
        return f"""
        <div style="background:#0b1220;border:1px solid #233247;border-radius:12px;padding:12px;">
          <div style="color:#94a3b8;font-size:12px;">{q(label)}</div>
          <div style="font-size:24px;font-weight:bold;margin-top:6px;color:{color};">{q(value)}</div>
        </div>
        """

    open_html = ""
    for r in open_rows:
        open_html += f"""
        <div style="background:#0b1220;border:1px solid #233247;border-radius:12px;padding:10px;margin-top:8px;">
          <b>{q(r.get('market'))} {q(r.get('direction'))}</b>
          <div style="color:#94a3b8;font-size:13px;margin-top:3px;">{q(r.get('setup_name'))} · Conf {q(r.get('confidence'))}</div>
          <div style="color:#cbd5e1;font-size:12px;margin-top:5px;">Entry {q(r.get('entry'))} · SL {q(r.get('sl'))} · TP1 {q(r.get('tp1'))}</div>
        </div>
        """
    if not open_html:
        open_html = '<div style="background:#0b1220;border:1px solid #233247;border-radius:12px;padding:10px;margin-top:8px;color:#94a3b8;">Keine offenen Shadow Trades.</div>'

    recent_html = ""
    for r in recent_rows:
        recent_html += f"""
        <div style="background:#0b1220;border:1px solid #233247;border-radius:12px;padding:10px;margin-top:8px;">
          <b>{q(r.get('market'))} {q(r.get('direction'))} · {q(r.get('result'))}</b>
          <div style="color:#94a3b8;font-size:13px;margin-top:3px;">{q(r.get('setup_name'))}</div>
          <div style="color:{cls(r.get('pnl_r'))};font-size:12px;margin-top:5px;">R {q(r.get('pnl_r'))} · Exit {q(r.get('exit_price'))}</div>
        </div>
        """
    if not recent_html:
        recent_html = '<div style="background:#0b1220;border:1px solid #233247;border-radius:12px;padding:10px;margin-top:8px;color:#94a3b8;">Keine Shadow Outcomes.</div>'

    table_rows = ""
    for r in group_rows:
        table_rows += f"""
        <tr>
          <td><b>{q(r.get('market'))}</b></td>
          <td>{q(r.get('direction'))}</td>
          <td>{q(r.get('setup_name'))}</td>
          <td>{q(r.get('trades'))}</td>
          <td style="color:#22c55e;">{q(r.get('wins'))}</td>
          <td style="color:#ef4444;">{q(r.get('losses'))}</td>
          <td style="color:{cls(r.get('total_r'))};"><b>{q(r.get('total_r'))}</b></td>
          <td>{q(r.get('avg_confidence'))}</td>
        </tr>
        """
    if not table_rows:
        table_rows = '<tr><td colspan="8" style="color:#94a3b8;">Noch keine echten Shadow Outcomes.</td></tr>'

    return f"""
  <br>
  <div style="background:rgba(16,26,39,.94);border:1px solid #233247;border-radius:16px;padding:14px;margin-bottom:14px;">
    <h2 style="margin:0 0 6px 0;font-size:20px;">🧪 Shadow Board</h2>
    <div style="color:#94a3b8;font-size:13px;margin-bottom:12px;">
      Geblockte Near-Miss Signale werden als Paper-Trades überwacht. Keine echten Trades.
      <a href="/shadow?token={q(_token)}" style="color:#93c5fd;text-decoration:none;margin-left:8px;">Shadow öffnen</a>
    </div>

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:12px;">
      {stat("Open Shadow", s.get("open_shadow"), "#facc15")}
      {stat("Closed Shadow", s.get("closed_shadow"))}
      {stat("Wins", s.get("wins"), "#22c55e")}
      {stat("Losses", s.get("losses"), "#ef4444")}
      {stat("Winrate", str(s.get("winrate")) + "%")}
      {stat("Total R", s.get("total_r"), cls(s.get("total_r")))}
    </div>

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;">
      <div>
        <h3 style="margin:0 0 6px 0;">Offene Shadow</h3>
        {open_html}
      </div>
      <div>
        <h3 style="margin:0 0 6px 0;">Letzte Shadow Outcomes</h3>
        {recent_html}
      </div>
    </div>

    <div style="overflow-x:auto;margin-top:12px;border:1px solid #233247;border-radius:12px;">
      <table style="width:100%;border-collapse:collapse;min-width:720px;">
        <thead>
          <tr style="background:#0b1220;color:#94a3b8;">
            <th style="padding:9px;text-align:left;">Market</th>
            <th style="padding:9px;text-align:left;">Side</th>
            <th style="padding:9px;text-align:left;">Setup</th>
            <th style="padding:9px;text-align:left;">Trades</th>
            <th style="padding:9px;text-align:left;">Wins</th>
            <th style="padding:9px;text-align:left;">Loss</th>
            <th style="padding:9px;text-align:left;">Total R</th>
            <th style="padding:9px;text-align:left;">Conf</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
  </div>
"""

# === V7000 MASTER SAFE SHADOW BOARD PATCH END ===



# === V7000 MASTER CLUSTER BOARD PATCH START ===
def _v7000_master_cluster_board_html(_token):
    import sqlite3
    from collections import defaultdict

    def q(x):
        import html
        return html.escape("" if x is None else str(x))

    def cluster(market):
        m = str(market or "").upper().strip()

        if m.endswith("JPY") or m in ("USDJPY","EURJPY","GBPJPY","AUDJPY","NZDJPY","CADJPY","CHFJPY"):
            return "JPY_FX"

        if m in ("EURUSD","GBPUSD","AUDUSD","NZDUSD","USDCAD","USDCHF"):
            return "USD_FX"

        if m in ("US100","NAS100","NASDAQ","NQ","MNQ","US500","SPX","SP500","ES","MES","US30","DOW","YM","MYM"):
            return "US_INDEX"

        if m in ("GER40","DAX","DE40","FRA40","CAC40","EU50","STOXX50"):
            return "EU_INDEX"

        if m in ("FTSE100","UK100"):
            return "UK_INDEX"

        if m in ("XAUUSD","GOLD","GC","MGC","XAGUSD","SILVER"):
            return "METALS"

        if m in ("BTCUSD","BTCUSDT","ETHUSD","ETHUSDT","BTC","ETH"):
            return "CRYPTO"

        if m in ("OIL","USOIL","UKOIL","WTI","BRENT","CL","MCL"):
            return "OIL"

        return "SINGLE_" + m

    MAX_CLUSTER = 2
    db = _v7000_master_db_path()

    try:
        con = sqlite3.connect(db, timeout=15)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("""
            SELECT market, direction, setup_name, entry, sl, tp1, opened_at
            FROM open_trades
            WHERE status='OPEN'
            ORDER BY opened_at DESC
        """).fetchall()]
        con.close()
    except Exception as e:
        return f"""
        <br>
        <div style="background:rgba(16,26,39,.94);border:1px solid #7f1d1d;border-radius:16px;padding:14px;margin-bottom:14px;">
          <h2 style="margin:0 0 6px 0;font-size:20px;">🧩 Cluster Board</h2>
          <div style="color:#fecaca;">Cluster konnten nicht gelesen werden: {q(e)}</div>
        </div>
        """

    groups = defaultdict(list)
    for r in rows:
        groups[cluster(r.get("market"))].append(r)

    preferred = ["JPY_FX", "USD_FX", "US_INDEX", "EU_INDEX", "UK_INDEX", "METALS", "CRYPTO", "OIL"]
    all_clusters = preferred[:]

    for c in sorted(groups.keys()):
        if c not in all_clusters:
            all_clusters.append(c)

    cards = ""
    for c in all_clusters:
        items = groups.get(c, [])
        count = len(items)

        if count >= MAX_CLUSTER:
            border = "#ef4444"
            status = "FULL"
            color = "#ef4444"
        elif count == 1:
            border = "#facc15"
            status = "OK 1/2"
            color = "#facc15"
        else:
            border = "#22c55e"
            status = "FREE"
            color = "#22c55e"

        item_html = ""
        for x in items:
            item_html += f"""
            <div style="margin-top:8px;background:#0b1220;border:1px solid #233247;border-radius:10px;padding:9px;">
              <b>{q(x.get("market"))} {q(x.get("direction"))}</b>
              <div style="color:#94a3b8;font-size:12px;margin-top:3px;">{q(x.get("setup_name"))}</div>
              <div style="color:#cbd5e1;font-size:12px;margin-top:5px;">
                Entry {q(x.get("entry"))} · SL {q(x.get("sl"))} · TP1 {q(x.get("tp1"))}
              </div>
            </div>
            """

        if not item_html:
            item_html = '<div style="color:#64748b;font-size:13px;margin-top:8px;">Keine offenen Trades.</div>'

        cards += f"""
        <div style="background:#101a27;border:1px solid #233247;border-left:5px solid {border};border-radius:15px;padding:13px;">
          <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;">
            <div>
              <div style="font-size:18px;font-weight:bold;">{q(c)}</div>
              <div style="color:#94a3b8;font-size:12px;margin-top:3px;">Open {count}/{MAX_CLUSTER}</div>
            </div>
            <div style="background:#0b1220;border:1px solid #233247;border-radius:999px;padding:7px 10px;font-weight:bold;color:{color};font-size:12px;">
              {status}
            </div>
          </div>
          {item_html}
        </div>
        """

    return f"""
  <br>
  <div style="background:rgba(16,26,39,.94);border:1px solid #233247;border-radius:16px;padding:14px;margin-bottom:14px;">
    <h2 style="margin:0 0 6px 0;font-size:20px;">🧩 Cluster Board</h2>
    <div style="color:#94a3b8;font-size:13px;margin-bottom:12px;">
      Maximal 2 offene Trades pro Cluster. Es gibt kein globales Open-Trade-Limit.
      Wenn ein Cluster FULL ist, wird der nächste echte Trade dort geblockt und weiter als Shadow verfolgt.
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;">
      {cards}
    </div>
  </div>
"""

# === V7000 MASTER CLUSTER BOARD PATCH END ===



# === V7000 MASTER LIVE R BOARD PATCH START ===
def _v7000_master_live_r_board_html(_token):
    import sqlite3
    from datetime import datetime, timezone

    def q(x):
        import html
        return html.escape("" if x is None else str(x))

    def parse_ts(x):
        try:
            dt = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    def color_r(v):
        try:
            f = float(v)
            if f >= 1.0:
                return "#22c55e"
            if f >= 0.5:
                return "#84cc16"
            if f >= 0:
                return "#facc15"
            return "#ef4444"
        except Exception:
            return "#e8eef7"

    db = _v7000_master_db_path()

    try:
        con = sqlite3.connect(db, timeout=15)
        con.row_factory = sqlite3.Row

        trades = [dict(r) for r in con.execute("""
            SELECT client_trade_id, market, direction, setup_name, entry, sl, tp1, opened_at
            FROM open_trades
            WHERE status='OPEN'
            ORDER BY opened_at DESC
        """).fetchall()]

        hbs = {
            str(r["market"]).upper(): dict(r)
            for r in con.execute("""
                SELECT market, close, high, low, received_at
                FROM price_heartbeats
            """).fetchall()
        }

        con.close()
    except Exception as e:
        return f"""
        <br>
        <div style="background:rgba(16,26,39,.94);border:1px solid #7f1d1d;border-radius:16px;padding:14px;margin-bottom:14px;">
          <h2 style="margin:0 0 6px 0;font-size:20px;">📈 Live-R Monitor</h2>
          <div style="color:#fecaca;">Live-R konnte nicht gelesen werden: {q(e)}</div>
        </div>
        """

    now = datetime.now(timezone.utc)

    cards = ""

    for t in trades:
        m = str(t.get("market") or "").upper()
        side = str(t.get("direction") or "").upper()
        setup = t.get("setup_name")

        entry = float(t.get("entry") or 0)
        sl = float(t.get("sl") or 0)
        tp1 = float(t.get("tp1") or 0)

        hb = hbs.get(m)

        if not hb:
            cards += f"""
            <div style="background:#101a27;border:1px solid #233247;border-left:5px solid #ef4444;border-radius:15px;padding:13px;">
              <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;">
                <div>
                  <div style="font-size:20px;font-weight:bold;">{q(m)} {q(side)}</div>
                  <div style="color:#94a3b8;font-size:12px;margin-top:3px;">{q(setup)}</div>
                </div>
                <div style="background:#0b1220;border:1px solid #233247;border-radius:999px;padding:7px 10px;font-weight:bold;color:#ef4444;font-size:12px;">HB MISSING</div>
              </div>
              <div style="margin-top:10px;color:#cbd5e1;font-size:13px;">
                Entry {q(entry)} · SL {q(sl)} · TP1 {q(tp1)}
              </div>
            </div>
            """
            continue

        close = float(hb.get("close") or 0)
        risk = abs(entry - sl) if entry and sl else 0

        if risk > 0 and close > 0:
            if side == "LONG":
                r_now = (close - entry) / risk
            else:
                r_now = (entry - close) / risk
        else:
            r_now = 0

        to_tp = abs(tp1 - close) if tp1 and close else 0
        to_sl = abs(close - sl) if sl and close else 0

        dt = parse_ts(hb.get("received_at"))
        age = None
        hb_status = "UNKNOWN"
        hb_color = "#94a3b8"

        if dt:
            age = int((now - dt).total_seconds())
            if age <= 180:
                hb_status = "HB OK"
                hb_color = "#22c55e"
            elif age <= 300:
                hb_status = "HB WARN"
                hb_color = "#facc15"
            else:
                hb_status = "HB STALE"
                hb_color = "#ef4444"

        r_col = color_r(r_now)

        if r_now >= 1:
            border = "#22c55e"
        elif r_now >= 0:
            border = "#facc15"
        else:
            border = "#ef4444"

        cards += f"""
        <div style="background:#101a27;border:1px solid #233247;border-left:5px solid {border};border-radius:15px;padding:13px;">
          <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;">
            <div>
              <div style="font-size:20px;font-weight:bold;">{q(m)} {q(side)}</div>
              <div style="color:#94a3b8;font-size:12px;margin-top:3px;">{q(setup)}</div>
            </div>
            <div style="background:#0b1220;border:1px solid #233247;border-radius:999px;padding:7px 10px;font-weight:bold;color:{hb_color};font-size:12px;">
              {q(hb_status)}
            </div>
          </div>

          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(105px,1fr));gap:8px;margin-top:12px;">
            <div style="background:#0b1220;border:1px solid #233247;border-radius:10px;padding:9px;">
              <div style="color:#94a3b8;font-size:11px;">Close</div>
              <div style="font-weight:bold;margin-top:4px;">{q(round(close,5))}</div>
            </div>
            <div style="background:#0b1220;border:1px solid #233247;border-radius:10px;padding:9px;">
              <div style="color:#94a3b8;font-size:11px;">Live R</div>
              <div style="font-weight:bold;margin-top:4px;color:{r_col};">{q(round(r_now,2))}R</div>
            </div>
            <div style="background:#0b1220;border:1px solid #233247;border-radius:10px;padding:9px;">
              <div style="color:#94a3b8;font-size:11px;">To TP1</div>
              <div style="font-weight:bold;margin-top:4px;">{q(round(to_tp,5))}</div>
            </div>
            <div style="background:#0b1220;border:1px solid #233247;border-radius:10px;padding:9px;">
              <div style="color:#94a3b8;font-size:11px;">To SL</div>
              <div style="font-weight:bold;margin-top:4px;">{q(round(to_sl,5))}</div>
            </div>
          </div>

          <div style="margin-top:10px;color:#cbd5e1;font-size:13px;">
            Entry {q(entry)} · SL {q(sl)} · TP1 {q(tp1)} · Age {q(age)}s
          </div>
        </div>
        """

    if not cards:
        cards = '<div style="color:#94a3b8;background:#0b1220;border:1px solid #233247;border-radius:12px;padding:12px;">Keine offenen Trades.</div>'

    return f"""
  <br>
  <div style="background:rgba(16,26,39,.94);border:1px solid #233247;border-radius:16px;padding:14px;margin-bottom:14px;">
    <h2 style="margin:0 0 6px 0;font-size:20px;">📈 Live-R Monitor</h2>
    <div style="color:#94a3b8;font-size:13px;margin-bottom:12px;">
      Aktueller Stand aller offenen Trades: Close, Live-R, Abstand zu TP1/SL und Heartbeat.
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;">
      {cards}
    </div>
  </div>
"""

# === V7000 MASTER LIVE R BOARD PATCH END ===



# === V7000 MASTER OUTCOME BOARD PATCH START ===
def _v7000_master_outcome_board_html(_token):
    import sqlite3

    def q(x):
        import html
        return html.escape("" if x is None else str(x))

    def col(v):
        try:
            f = float(v)
            if f > 0:
                return "#22c55e"
            if f < 0:
                return "#ef4444"
        except Exception:
            pass
        return "#e8eef7"

    db = _v7000_master_db_path()

    try:
        con = sqlite3.connect(db, timeout=15)
        con.row_factory = sqlite3.Row

        total = dict(con.execute("""
            SELECT
              COUNT(*) AS trades,
              COALESCE(SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END),0) AS wins,
              COALESCE(SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END),0) AS losses,
              ROUND(COALESCE(SUM(pnl_r),0),2) AS total_r,
              ROUND(COALESCE(AVG(pnl_r),0),2) AS avg_r,
              CASE
                WHEN COUNT(*) > 0 THEN ROUND(100.0 * SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) / COUNT(*),1)
                ELSE 0
              END AS winrate
            FROM trade_outcomes
        """).fetchone())

        today = dict(con.execute("""
            SELECT
              COUNT(*) AS trades,
              COALESCE(SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END),0) AS wins,
              COALESCE(SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END),0) AS losses,
              ROUND(COALESCE(SUM(pnl_r),0),2) AS total_r,
              ROUND(COALESCE(AVG(pnl_r),0),2) AS avg_r,
              CASE
                WHEN COUNT(*) > 0 THEN ROUND(100.0 * SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) / COUNT(*),1)
                ELSE 0
              END AS winrate
            FROM trade_outcomes
            WHERE substr(closed_at,1,10)=date('now')
        """).fetchone())

        recent = [dict(r) for r in con.execute("""
            SELECT
              o.id,
              o.result,
              o.pnl_r,
              o.exit_price,
              o.closed_at,
              COALESCE(d.market,'?') AS market,
              COALESCE(d.direction,'?') AS direction,
              COALESCE(d.setup_name,'?') AS setup_name
            FROM trade_outcomes o
            LEFT JOIN setup_decisions d ON d.id=o.decision_id
            ORDER BY o.id DESC
            LIMIT 8
        """).fetchall()]

        today_groups = [dict(r) for r in con.execute("""
            SELECT
              COALESCE(d.market,'?') AS market,
              COALESCE(d.direction,'?') AS direction,
              COALESCE(d.setup_name,'?') AS setup_name,
              COUNT(*) AS trades,
              SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN o.pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
              ROUND(SUM(o.pnl_r),2) AS total_r,
              ROUND(AVG(o.pnl_r),2) AS avg_r
            FROM trade_outcomes o
            LEFT JOIN setup_decisions d ON d.id=o.decision_id
            WHERE substr(o.closed_at,1,10)=date('now')
            GROUP BY d.market, d.direction, d.setup_name
            ORDER BY total_r DESC, trades DESC
            LIMIT 8
        """).fetchall()]

        con.close()
    except Exception as e:
        return f"""
        <br>
        <div style="background:rgba(16,26,39,.94);border:1px solid #7f1d1d;border-radius:16px;padding:14px;margin-bottom:14px;">
          <h2 style="margin:0 0 6px 0;font-size:20px;">🏁 Outcome Board</h2>
          <div style="color:#fecaca;">Outcome Board konnte nicht gelesen werden: {q(e)}</div>
        </div>
        """

    def stat(label, value, color="#e8eef7"):
        return f"""
        <div style="background:#0b1220;border:1px solid #233247;border-radius:12px;padding:12px;">
          <div style="color:#94a3b8;font-size:12px;">{q(label)}</div>
          <div style="font-size:24px;font-weight:bold;margin-top:6px;color:{color};">{q(value)}</div>
        </div>
        """

    recent_html = ""
    for r in recent:
        result_color = "#22c55e" if float(r.get("pnl_r") or 0) > 0 else "#ef4444"
        recent_html += f"""
        <div style="background:#0b1220;border:1px solid #233247;border-radius:12px;padding:10px;margin-top:8px;">
          <div style="display:flex;justify-content:space-between;gap:10px;">
            <b>{q(r.get("market"))} {q(r.get("direction"))}</b>
            <b style="color:{result_color};">{q(r.get("pnl_r"))}R</b>
          </div>
          <div style="color:#94a3b8;font-size:13px;margin-top:3px;">{q(r.get("setup_name"))}</div>
          <div style="color:#cbd5e1;font-size:12px;margin-top:5px;">{q(r.get("result"))} · Exit {q(r.get("exit_price"))} · {q(r.get("closed_at"))}</div>
        </div>
        """
    if not recent_html:
        recent_html = '<div style="background:#0b1220;border:1px solid #233247;border-radius:12px;padding:10px;margin-top:8px;color:#94a3b8;">Noch keine Outcomes.</div>'

    group_rows = ""
    for r in today_groups:
        group_rows += f"""
        <tr>
          <td><b>{q(r.get("market"))}</b></td>
          <td>{q(r.get("direction"))}</td>
          <td>{q(r.get("setup_name"))}</td>
          <td>{q(r.get("trades"))}</td>
          <td style="color:#22c55e;">{q(r.get("wins"))}</td>
          <td style="color:#ef4444;">{q(r.get("losses"))}</td>
          <td style="color:{col(r.get("total_r"))};"><b>{q(r.get("total_r"))}</b></td>
          <td style="color:{col(r.get("avg_r"))};">{q(r.get("avg_r"))}</td>
        </tr>
        """
    if not group_rows:
        group_rows = '<tr><td colspan="8" style="color:#94a3b8;">Heute noch keine abgeschlossenen Trades.</td></tr>'

    return f"""
  <br>
  <div style="background:rgba(16,26,39,.94);border:1px solid #233247;border-radius:16px;padding:14px;margin-bottom:14px;">
    <h2 style="margin:0 0 6px 0;font-size:20px;">🏁 Outcome Board</h2>
    <div style="color:#94a3b8;font-size:13px;margin-bottom:12px;">
      Echte abgeschlossene Trades · Heute basiert auf UTC-Datum.
    </div>

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:10px;margin-bottom:12px;">
      {stat("Heute Trades", today.get("trades"))}
      {stat("Heute Wins", today.get("wins"), "#22c55e")}
      {stat("Heute Losses", today.get("losses"), "#ef4444")}
      {stat("Heute WR", str(today.get("winrate")) + "%")}
      {stat("Heute R", today.get("total_r"), col(today.get("total_r")))}
      {stat("Gesamt R", total.get("total_r"), col(total.get("total_r")))}
    </div>

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;">
      <div>
        <h3 style="margin:0 0 6px 0;">Letzte Outcomes</h3>
        {recent_html}
      </div>

      <div>
        <h3 style="margin:0 0 6px 0;">Heute Setup-Gruppen</h3>
        <div style="overflow-x:auto;border:1px solid #233247;border-radius:12px;">
          <table style="width:100%;border-collapse:collapse;min-width:650px;">
            <thead>
              <tr style="background:#0b1220;color:#94a3b8;">
                <th style="padding:9px;text-align:left;">Market</th>
                <th style="padding:9px;text-align:left;">Side</th>
                <th style="padding:9px;text-align:left;">Setup</th>
                <th style="padding:9px;text-align:left;">Trades</th>
                <th style="padding:9px;text-align:left;">W</th>
                <th style="padding:9px;text-align:left;">L</th>
                <th style="padding:9px;text-align:left;">Total R</th>
                <th style="padding:9px;text-align:left;">Avg R</th>
              </tr>
            </thead>
            <tbody>{group_rows}</tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
"""

# === V7000 MASTER OUTCOME BOARD PATCH END ===



# === V7100 COMPACT MASTER BOARD PATCH START ===
def _v7100_master_compact_board_html(_token):
    import sqlite3
    import html
    from datetime import datetime, timezone

    def q(x):
        return html.escape("" if x is None else str(x))

    def color(v):
        try:
            f = float(v)
            if f > 0:
                return "#22c55e"
            if f < 0:
                return "#ef4444"
        except Exception:
            pass
        return "#e8eef7"

    def cluster(market):
        m = str(market or "").upper().strip()
        if m.endswith("JPY") or m in ("USDJPY","EURJPY","GBPJPY","AUDJPY","NZDJPY","CADJPY","CHFJPY"):
            return "JPY_FX"
        if m in ("EURUSD","GBPUSD","AUDUSD","NZDUSD","USDCAD","USDCHF"):
            return "USD_FX"
        if m in ("US100","NAS100","NASDAQ","NQ","MNQ","US500","SPX","SP500","ES","MES","US30","DOW","YM","MYM"):
            return "US_INDEX"
        if m in ("GER40","DAX","DE40","FRA40","CAC40","EU50","STOXX50"):
            return "EU_INDEX"
        if m in ("FTSE100","UK100"):
            return "UK_INDEX"
        if m in ("XAUUSD","GOLD","GC","MGC","XAGUSD","SILVER"):
            return "METALS"
        if m in ("BTCUSD","BTCUSDT","ETHUSD","ETHUSDT","BTC","ETH"):
            return "CRYPTO"
        if m in ("OIL","USOIL","UKOIL","WTI","BRENT","CL","MCL","UKOILSPOT"):
            return "OIL"
        return "SINGLE_" + m

    try:
        db = _v7000_master_db_path()
        con = sqlite3.connect(db, timeout=15)
        con.row_factory = sqlite3.Row

        open_trades = [dict(r) for r in con.execute("""
            SELECT market,direction,setup_name,entry,sl,tp1,opened_at
            FROM open_trades
            WHERE status='OPEN'
            ORDER BY opened_at DESC
        """).fetchall()]

        today = dict(con.execute("""
            SELECT
              COUNT(*) AS trades,
              COALESCE(SUM(CASE WHEN pnl_r>0 THEN 1 ELSE 0 END),0) AS wins,
              COALESCE(SUM(CASE WHEN pnl_r<=0 THEN 1 ELSE 0 END),0) AS losses,
              ROUND(COALESCE(SUM(pnl_r),0),2) AS total_r,
              CASE WHEN COUNT(*)>0 THEN ROUND(100.0*SUM(CASE WHEN pnl_r>0 THEN 1 ELSE 0 END)/COUNT(*),1) ELSE 0 END AS winrate
            FROM trade_outcomes
            WHERE substr(closed_at,1,10)=date('now')
        """).fetchone())

        best = [dict(r) for r in con.execute("""
            SELECT COALESCE(d.market,'?') AS market, COALESCE(d.direction,'?') AS direction,
                   COALESCE(d.setup_name,'?') AS setup_name,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN o.pnl_r>0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN o.pnl_r<=0 THEN 1 ELSE 0 END) AS losses,
                   ROUND(SUM(o.pnl_r),2) AS total_r,
                   ROUND(AVG(o.pnl_r),2) AS avg_r
            FROM trade_outcomes o
            LEFT JOIN setup_decisions d ON d.id=o.decision_id
            GROUP BY d.market,d.direction,d.setup_name
            ORDER BY total_r DESC,trades DESC
            LIMIT 6
        """).fetchall()]

        worst = [dict(r) for r in con.execute("""
            SELECT COALESCE(d.market,'?') AS market, COALESCE(d.direction,'?') AS direction,
                   COALESCE(d.setup_name,'?') AS setup_name,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN o.pnl_r>0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN o.pnl_r<=0 THEN 1 ELSE 0 END) AS losses,
                   ROUND(SUM(o.pnl_r),2) AS total_r,
                   ROUND(AVG(o.pnl_r),2) AS avg_r
            FROM trade_outcomes o
            LEFT JOIN setup_decisions d ON d.id=o.decision_id
            GROUP BY d.market,d.direction,d.setup_name
            ORDER BY total_r ASC,trades DESC
            LIMIT 6
        """).fetchall()]

        dcols = set([r[1] for r in con.execute("PRAGMA table_info(setup_decisions)").fetchall()])
        session_expr = "COALESCE(d.session,'UNKNOWN')" if "session" in dcols else "'UNKNOWN'"
        tf_expr = "COALESCE(d.timeframe,'?')" if "timeframe" in dcols else "'?'"

        sessions = [dict(r) for r in con.execute(f"""
            SELECT {session_expr} AS session, {tf_expr} AS timeframe,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN o.pnl_r>0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN o.pnl_r<=0 THEN 1 ELSE 0 END) AS losses,
                   ROUND(SUM(o.pnl_r),2) AS total_r,
                   CASE WHEN COUNT(*)>0 THEN ROUND(100.0*SUM(CASE WHEN o.pnl_r>0 THEN 1 ELSE 0 END)/COUNT(*),1) ELSE 0 END AS winrate
            FROM trade_outcomes o
            LEFT JOIN setup_decisions d ON d.id=o.decision_id
            GROUP BY session,timeframe
            ORDER BY total_r DESC,trades DESC
            LIMIT 8
        """).fetchall()]

        dq = dict(con.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN allow_trade=1 THEN 1 ELSE 0 END) AS allowed,
                   SUM(CASE WHEN allow_trade=0 THEN 1 ELSE 0 END) AS blocked,
                   ROUND(AVG(confidence),1) AS avg_conf,
                   ROUND(AVG(CASE WHEN allow_trade=1 THEN confidence END),1) AS allow_conf,
                   ROUND(AVG(CASE WHEN allow_trade=0 THEN confidence END),1) AS block_conf
            FROM signal_audit
        """).fetchone())

        block_groups = [dict(r) for r in con.execute("""
            SELECT
              CASE
                WHEN reason LIKE '%CLUSTER_GUARD%' THEN 'CLUSTER_GUARD'
                WHEN reason LIKE '%confidence%' THEN 'CONFIDENCE'
                WHEN reason LIKE '%bias%' OR reason LIKE '%countertrend%' THEN 'BIAS_GATE'
                WHEN reason LIKE '%event%' OR reason LIKE '%EVENT%' THEN 'EVENT_RISK'
                WHEN reason LIKE '%chase%' THEN 'CHASE'
                WHEN reason LIKE '%impulse%' THEN 'IMPULSE'
                ELSE 'OTHER'
              END AS block_group,
              COUNT(*) AS count,
              ROUND(AVG(confidence),1) AS avg_conf
            FROM signal_audit
            WHERE allow_trade=0
            GROUP BY block_group
            ORDER BY count DESC, avg_conf DESC
        """).fetchall()]

        shadow = dict(con.execute("""
            SELECT (SELECT COUNT(*) FROM shadow_trades WHERE status='OPEN') AS open_shadow,
                   COUNT(o.id) AS outcomes,
                   ROUND(COALESCE(SUM(o.pnl_r),0),2) AS total_r
            FROM shadow_outcomes o
        """).fetchone())

        con.close()
    except Exception as e:
        return f"""
  <br>
  <div style="background:rgba(16,26,39,.94);border:1px solid #7f1d1d;border-radius:16px;padding:14px;margin-bottom:14px;">
    <h2 style="margin:0 0 6px 0;font-size:20px;">🤖 V7100 AI Boards</h2>
    <div style="color:#fecaca;">V7100 Board Fehler: {q(e)}</div>
  </div>
"""

    recs = []
    rr = float(today.get("total_r") or 0)
    if rr > 0:
        recs.append(("✅", "#86efac", f"Heute positiv: {today.get('trades')} Trades, {today.get('winrate')}% WR, {rr}R. Keine aggressiven Regeländerungen nötig."))
    elif rr < 0:
        recs.append(("⚠️", "#fecaca", f"Heute negativ: {today.get('trades')} Trades, {today.get('winrate')}% WR, {rr}R. Live-Lockerungen vermeiden."))
    else:
        recs.append(("ℹ️", "#cbd5e1", "Heute neutral oder noch keine abgeschlossenen Trades. Daten sammeln."))

    groups = {}
    for t in open_trades:
        groups.setdefault(cluster(t.get("market")), []).append(t)

    for c, items in sorted(groups.items()):
        if len(items) >= 2:
            mk = ", ".join([f"{x.get('market')} {x.get('direction')}" for x in items])
            recs.append(("🧩", "#fde68a", f"{c} FULL 2/2: keine weiteren echten Trades in diesem Cluster. Offen: {mk}."))

    if int(shadow.get("outcomes") or 0) < 10:
        recs.append(("🧪", "#cbd5e1", "Shadow-Datenbasis noch klein. Keine Shadow-to-Live Freischaltung."))

    rec_html = ""
    for icon, c, text in recs:
        rec_html += f"""
        <div style="background:#0b1220;border:1px solid #233247;border-radius:12px;padding:10px;margin-top:8px;color:{c};">
          <b>{icon}</b> {q(text)}
        </div>
        """

    def rank_rows(rows):
        out = ""
        for r in rows:
            out += f"""
            <tr>
              <td><b>{q(r.get('market'))}</b></td><td>{q(r.get('direction'))}</td>
              <td>{q(r.get('setup_name'))}</td><td>{q(r.get('trades'))}</td>
              <td style="color:#22c55e;">{q(r.get('wins'))}</td><td style="color:#ef4444;">{q(r.get('losses'))}</td>
              <td style="color:{color(r.get('total_r'))};"><b>{q(r.get('total_r'))}</b></td>
              <td style="color:{color(r.get('avg_r'))};">{q(r.get('avg_r'))}</td>
            </tr>
            """
        return out or '<tr><td colspan="8" style="color:#94a3b8;">Keine Daten.</td></tr>'

    def session_rows(rows):
        out = ""
        for r in rows:
            out += f"""
            <tr>
              <td><b>{q(r.get('session'))}</b></td><td>{q(r.get('timeframe'))}</td><td>{q(r.get('trades'))}</td>
              <td style="color:#22c55e;">{q(r.get('wins'))}</td><td style="color:#ef4444;">{q(r.get('losses'))}</td>
              <td>{q(r.get('winrate'))}%</td><td style="color:{color(r.get('total_r'))};"><b>{q(r.get('total_r'))}</b></td>
            </tr>
            """
        return out or '<tr><td colspan="7" style="color:#94a3b8;">Keine Session-Daten.</td></tr>'

    bg_rows = ""
    for r in block_groups:
        bg_rows += f"<tr><td><b>{q(r.get('block_group'))}</b></td><td>{q(r.get('count'))}</td><td>{q(r.get('avg_conf'))}</td></tr>"
    if not bg_rows:
        bg_rows = '<tr><td colspan="3" style="color:#94a3b8;">Keine Blocks.</td></tr>'

    table_css = "width:100%;border-collapse:collapse;min-width:620px;"
    box = "overflow-x:auto;border:1px solid #233247;border-radius:12px;margin-top:8px;"
    th_rank = """
    <tr style="background:#0b1220;color:#94a3b8;">
      <th style="padding:9px;text-align:left;">Market</th><th style="padding:9px;text-align:left;">Side</th><th style="padding:9px;text-align:left;">Setup</th>
      <th style="padding:9px;text-align:left;">T</th><th style="padding:9px;text-align:left;">W</th><th style="padding:9px;text-align:left;">L</th>
      <th style="padding:9px;text-align:left;">R</th><th style="padding:9px;text-align:left;">Avg</th>
    </tr>
    """
    th_session = """
    <tr style="background:#0b1220;color:#94a3b8;">
      <th style="padding:9px;text-align:left;">Session</th><th style="padding:9px;text-align:left;">TF</th><th style="padding:9px;text-align:left;">T</th>
      <th style="padding:9px;text-align:left;">W</th><th style="padding:9px;text-align:left;">L</th><th style="padding:9px;text-align:left;">WR</th><th style="padding:9px;text-align:left;">R</th>
    </tr>
    """

    return f"""
  <br>
  <div style="background:rgba(16,26,39,.94);border:1px solid #233247;border-radius:16px;padding:14px;margin-bottom:14px;">
    <h2 style="margin:0 0 6px 0;font-size:20px;">🤖 V7100 AI Boards</h2>
    <div style="color:#94a3b8;font-size:13px;margin-bottom:10px;">
      Diagnose ohne automatische Regeländerung: AI Empfehlung, Setup-Ranking, Sessions und Decision Quality.
    </div>

    <h3 style="margin:8px 0 4px 0;">AI Empfehlung</h3>
    {rec_html}

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-top:14px;">
      <div>
        <h3 style="margin:0 0 4px 0;">Beste Setups</h3>
        <div style="{box}"><table style="{table_css}"><thead>{th_rank}</thead><tbody>{rank_rows(best)}</tbody></table></div>
      </div>
      <div>
        <h3 style="margin:0 0 4px 0;">Schwächste Setups</h3>
        <div style="{box}"><table style="{table_css}"><thead>{th_rank}</thead><tbody>{rank_rows(worst)}</tbody></table></div>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-top:14px;">
      <div>
        <h3 style="margin:0 0 4px 0;">Session Board</h3>
        <div style="{box}"><table style="{table_css}"><thead>{th_session}</thead><tbody>{session_rows(sessions)}</tbody></table></div>
      </div>
      <div>
        <h3 style="margin:0 0 4px 0;">Decision Quality</h3>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px;">
          <div style="background:#0b1220;border:1px solid #233247;border-radius:10px;padding:9px;"><div style="color:#94a3b8;font-size:11px;">Total</div><b>{q(dq.get('total'))}</b></div>
          <div style="background:#0b1220;border:1px solid #233247;border-radius:10px;padding:9px;"><div style="color:#94a3b8;font-size:11px;">Allowed</div><b style="color:#22c55e;">{q(dq.get('allowed'))}</b></div>
          <div style="background:#0b1220;border:1px solid #233247;border-radius:10px;padding:9px;"><div style="color:#94a3b8;font-size:11px;">Blocked</div><b style="color:#ef4444;">{q(dq.get('blocked'))}</b></div>
        </div>
        <div style="{box}"><table style="width:100%;border-collapse:collapse;min-width:320px;">
          <thead><tr style="background:#0b1220;color:#94a3b8;"><th style="padding:9px;text-align:left;">Block</th><th style="padding:9px;text-align:left;">Count</th><th style="padding:9px;text-align:left;">Avg Conf</th></tr></thead>
          <tbody>{bg_rows}</tbody>
        </table></div>
      </div>
    </div>
  </div>
"""
# === V7100 COMPACT MASTER BOARD PATCH END ===

@app.get("/master", response_class=_V7000_Master_HTMLResponse)
def v7000_master_page(token: str = ""):
    if not _v7000_master_token_ok(token):
        return _V7000_Master_HTMLResponse("<h1>403</h1><p>Token fehlt oder ist falsch.</p>", status_code=403)
    return _V7000_Master_HTMLResponse(_v7000_master_html(token))

@app.get("/master.json")
def v7000_master_json(token: str = ""):
    if not _v7000_master_token_ok(token):
        return _V7000_Master_JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return _V7000_Master_JSONResponse({"ok": True, "stats": _v7000_master_stats()})

# === V7000 MASTER PAGE PATCH END ===


# === V7000 DECISIONS V2 PATCH START ===

from fastapi.responses import HTMLResponse as _V7000_D2_HTMLResponse, JSONResponse as _V7000_D2_JSONResponse

def _v7000_d2_db_path():
    import os
    for _p in ("/app/data/v7000_learning.sqlite3", "data/v7000_learning.sqlite3"):
        if os.path.exists(_p):
            return _p
    return "/app/data/v7000_learning.sqlite3"

def _v7000_d2_token_path():
    import os
    for _p in ("/app/data/MANUAL_CLOSE_TOKEN", "data/MANUAL_CLOSE_TOKEN"):
        if os.path.exists(_p):
            return _p
    return "/app/data/MANUAL_CLOSE_TOKEN"

def _v7000_d2_token_ok(_token):
    try:
        from pathlib import Path
        _real = Path(_v7000_d2_token_path()).read_text().strip()
        return bool(_real) and str(_token or "").strip() == _real
    except Exception:
        return False

def _v7000_d2_q(_x):
    import html
    return html.escape("" if _x is None else str(_x))

def _v7000_d2_rows(_sql, _params=()):
    import sqlite3
    _con = sqlite3.connect(_v7000_d2_db_path())
    _con.row_factory = sqlite3.Row
    try:
        return [dict(_r) for _r in _con.execute(_sql, _params).fetchall()]
    finally:
        _con.close()

def _v7000_d2_payload(limit=80):
    try:
        limit = int(limit)
    except Exception:
        limit = 80
    limit = max(10, min(limit, 200))

    rows = _v7000_d2_rows("""
        SELECT
          id,
          created_at,
          client_trade_id,
          market,
          direction,
          trigger,
          timeframe,
          entry,
          technical_score,
          confidence,
          news_score,
          event_risk,
          risk_r,
          allow_trade,
          reason,
          bias_gate,
          entry_state,
          chase_state,
          impulse_state,
          raw_json
        FROM signal_audit
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    summary_rows = _v7000_d2_rows("""
        SELECT
          COUNT(*) AS total,
          COALESCE(SUM(CASE WHEN allow_trade=1 THEN 1 ELSE 0 END),0) AS allowed,
          COALESCE(SUM(CASE WHEN allow_trade=0 THEN 1 ELSE 0 END),0) AS blocked,
          ROUND(COALESCE(AVG(confidence),0),1) AS avg_confidence,
          ROUND(COALESCE(AVG(CASE WHEN allow_trade=1 THEN confidence END),0),1) AS avg_allowed_confidence
        FROM signal_audit
    """)

    market_rows = _v7000_d2_rows("""
        SELECT
          market,
          COUNT(*) AS total,
          SUM(CASE WHEN allow_trade=1 THEN 1 ELSE 0 END) AS allowed,
          SUM(CASE WHEN allow_trade=0 THEN 1 ELSE 0 END) AS blocked,
          ROUND(AVG(confidence),1) AS avg_confidence
        FROM signal_audit
        GROUP BY market
        ORDER BY total DESC, allowed DESC
        LIMIT 50
    """)

    return {
        "ok": True,
        "db": _v7000_d2_db_path(),
        "summary": summary_rows[0] if summary_rows else {},
        "markets": market_rows,
        "decisions": rows,
    }

def _v7000_d2_html(_token):
    data = _v7000_d2_payload()
    s = data.get("summary", {})

    def cls_allow(v):
        try:
            return "allow" if int(v) == 1 else "block"
        except Exception:
            return "block"

    def txt_allow(v):
        try:
            return "ALLOW" if int(v) == 1 else "BLOCK"
        except Exception:
            return "BLOCK"

    def score_cls(v):
        try:
            f = float(v)
            if f >= 62:
                return "good"
            if f >= 50:
                return "mid"
            return "bad"
        except Exception:
            return ""

    cards = ""
    for r in data.get("decisions", []):
        allow_class = cls_allow(r.get("allow_trade"))
        allow_txt = txt_allow(r.get("allow_trade"))

        reason = str(r.get("reason") or "")
        if len(reason) > 230:
            reason = reason[:230] + "..."

        raw_link = "/decisions2/raw/" + str(r.get("id")) + "?token=" + _token

        cards += f"""
        <div class="decision {allow_class}">
          <div class="dtop">
            <div>
              <div class="market">{_v7000_d2_q(r.get("market"))} <span>{_v7000_d2_q(r.get("direction"))}</span></div>
              <div class="trigger">{_v7000_d2_q(r.get("trigger"))}</div>
            </div>
            <div class="badge {allow_class}">{allow_txt}</div>
          </div>

          <div class="metrics">
            <div><span>Confidence</span><b class="{score_cls(r.get("confidence"))}">{_v7000_d2_q(r.get("confidence"))}</b></div>
            <div><span>Risk R</span><b>{_v7000_d2_q(r.get("risk_r"))}</b></div>
            <div><span>TF</span><b>{_v7000_d2_q(r.get("timeframe"))}</b></div>
            <div><span>Event</span><b>{_v7000_d2_q(r.get("event_risk"))}</b></div>
          </div>

          <div class="states">
            <span>Bias: {_v7000_d2_q(r.get("bias_gate"))}</span>
            <span>Entry: {_v7000_d2_q(r.get("entry_state"))}</span>
            <span>Chase: {_v7000_d2_q(r.get("chase_state"))}</span>
            <span>Impulse: {_v7000_d2_q(r.get("impulse_state"))}</span>
          </div>

          <div class="reason">{_v7000_d2_q(reason)}</div>

          <div class="bottom">
            <span>{_v7000_d2_q(r.get("created_at"))}</span>
            <a href="{_v7000_d2_q(raw_link)}">Raw JSON</a>
          </div>
        </div>
        """

    if not cards:
        cards = '<div class="empty">Noch keine Decisions gespeichert.</div>'

    market_rows = ""
    for m in data.get("markets", []):
        market_rows += f"""
        <tr>
          <td><b>{_v7000_d2_q(m.get("market"))}</b></td>
          <td>{_v7000_d2_q(m.get("total"))}</td>
          <td class="pos">{_v7000_d2_q(m.get("allowed"))}</td>
          <td class="neg">{_v7000_d2_q(m.get("blocked"))}</td>
          <td>{_v7000_d2_q(m.get("avg_confidence"))}</td>
        </tr>
        """

    if not market_rows:
        market_rows = '<tr><td colspan="5">Noch keine Daten.</td></tr>'

    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="20">
<title>V7000 Decisions V2</title>
<style>
:root {{
  --bg:#071019; --panel:#101a27; --border:#233247; --text:#e8eef7; --muted:#94a3b8;
  --green:#22c55e; --red:#ef4444; --yellow:#facc15; --blue:#3b82f6;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0;
  font-family:Arial,sans-serif;
  background:radial-gradient(circle at top left,rgba(59,130,246,.16),transparent 34%),var(--bg);
  color:var(--text);
}}
.header {{
  padding:20px 16px;
  background:rgba(16,26,39,.95);
  border-bottom:1px solid var(--border);
  position:sticky;
  top:0;
  z-index:10;
}}
.hin {{
  max-width:1200px;
  margin:0 auto;
}}
h1 {{
  margin:0 0 5px 0;
  font-size:27px;
}}
.sub {{
  color:var(--muted);
  font-size:13px;
}}
.links {{
  margin-top:8px;
}}
.links a {{
  color:#93c5fd;
  text-decoration:none;
  margin-right:12px;
}}
.wrap {{
  max-width:1200px;
  margin:0 auto;
  padding:14px;
}}
.stats {{
  display:grid;
  grid-template-columns:repeat(5,1fr);
  gap:10px;
  margin-bottom:14px;
}}
.stat {{
  background:rgba(16,26,39,.94);
  border:1px solid var(--border);
  border-radius:15px;
  padding:13px;
}}
.stat .label {{
  color:var(--muted);
  font-size:12px;
}}
.stat .value {{
  margin-top:7px;
  font-size:25px;
  font-weight:bold;
}}
.pos {{ color:var(--green); }}
.neg {{ color:var(--red); }}
.good {{ color:var(--green); }}
.mid {{ color:var(--yellow); }}
.bad {{ color:var(--red); }}
.grid {{
  display:grid;
  grid-template-columns:1.2fr .8fr;
  gap:14px;
}}
.decision {{
  background:rgba(16,26,39,.96);
  border:1px solid var(--border);
  border-radius:17px;
  padding:14px;
  margin-bottom:12px;
  border-left:5px solid #64748b;
}}
.decision.allow {{
  border-left-color:var(--green);
}}
.decision.block {{
  border-left-color:var(--red);
}}
.dtop {{
  display:flex;
  justify-content:space-between;
  align-items:flex-start;
  gap:10px;
}}
.market {{
  font-size:24px;
  font-weight:bold;
}}
.market span {{
  font-size:16px;
  color:var(--muted);
  margin-left:6px;
}}
.trigger {{
  color:var(--muted);
  margin-top:4px;
  font-size:13px;
  word-break:break-word;
}}
.badge {{
  padding:7px 10px;
  border-radius:999px;
  font-weight:bold;
  font-size:12px;
}}
.badge.allow {{
  background:#123d24;
  color:#86efac;
}}
.badge.block {{
  background:#3a1a1a;
  color:#fecaca;
}}
.metrics {{
  display:grid;
  grid-template-columns:repeat(4,1fr);
  gap:8px;
  margin-top:13px;
}}
.metrics div {{
  background:#0b1220;
  border:1px solid var(--border);
  border-radius:12px;
  padding:10px;
}}
.metrics span {{
  display:block;
  color:var(--muted);
  font-size:11px;
}}
.metrics b {{
  display:block;
  font-size:18px;
  margin-top:4px;
}}
.states {{
  display:flex;
  flex-wrap:wrap;
  gap:7px;
  margin-top:11px;
}}
.states span {{
  background:#0b1220;
  border:1px solid var(--border);
  color:#cbd5e1;
  padding:6px 8px;
  border-radius:999px;
  font-size:12px;
}}
.reason {{
  margin-top:11px;
  color:#dbeafe;
  line-height:1.4;
  font-size:13px;
}}
.bottom {{
  margin-top:11px;
  display:flex;
  justify-content:space-between;
  gap:10px;
  color:var(--muted);
  font-size:11px;
}}
.bottom a {{
  color:#93c5fd;
  text-decoration:none;
}}
.sidebox {{
  background:rgba(16,26,39,.94);
  border:1px solid var(--border);
  border-radius:17px;
  overflow:hidden;
  align-self:start;
  position:sticky;
  top:120px;
}}
.sidebox h2 {{
  margin:0;
  padding:13px;
  border-bottom:1px solid var(--border);
}}
.tablewrap {{
  overflow-x:auto;
}}
table {{
  width:100%;
  border-collapse:collapse;
}}
th, td {{
  padding:9px 10px;
  border-bottom:1px solid var(--border);
  text-align:left;
  font-size:13px;
  white-space:nowrap;
}}
th {{
  color:var(--muted);
  background:#0b1220;
}}
.empty {{
  background:rgba(16,26,39,.94);
  border:1px solid var(--border);
  border-radius:17px;
  padding:20px;
  color:var(--muted);
}}
@media(max-width:900px) {{
  .grid {{
    grid-template-columns:1fr;
  }}
  .sidebox {{
    position:static;
  }}
  .stats {{
    grid-template-columns:repeat(2,1fr);
  }}
}}
@media(max-width:640px) {{
  h1 {{ font-size:24px; }}
  .wrap {{ padding:12px; }}
  .market {{ font-size:22px; }}
  .metrics {{ grid-template-columns:repeat(2,1fr); }}
  .bottom {{ flex-direction:column; }}
}}
</style>
</head>
<body>
<div class="header">
  <div class="hin">
    <h1>🧾 V7000 Decisions</h1>
    <div class="sub">Mobile ALLOW/BLOCK Übersicht · Auto-Refresh 20s</div>
    <div class="links">
      <a href="/master?token={_v7000_d2_q(_token)}">Master</a>
      <a href="/learning">Learning</a>
      <a href="/heartbeat">Heartbeat</a>
      <a href="/decisions">alte Decisions</a>
      <a href="/decisions2.json?token={_v7000_d2_q(_token)}">JSON</a>
    </div>
  </div>
</div>

<div class="wrap">
  <div class="stats">
    <div class="stat"><div class="label">Total</div><div class="value">{_v7000_d2_q(s.get("total"))}</div></div>
    <div class="stat"><div class="label">Allowed</div><div class="value pos">{_v7000_d2_q(s.get("allowed"))}</div></div>
    <div class="stat"><div class="label">Blocked</div><div class="value neg">{_v7000_d2_q(s.get("blocked"))}</div></div>
    <div class="stat"><div class="label">Avg Conf</div><div class="value">{_v7000_d2_q(s.get("avg_confidence"))}</div></div>
    <div class="stat"><div class="label">Allowed Conf</div><div class="value good">{_v7000_d2_q(s.get("avg_allowed_confidence"))}</div></div>
  </div>

  <div class="grid">
    <div>
      {cards}
    </div>

    <div class="sidebox">
      <h2>Markets</h2>
      <div class="tablewrap">
        <table>
          <thead>
            <tr><th>Market</th><th>Total</th><th>Allow</th><th>Block</th><th>Conf</th></tr>
          </thead>
          <tbody>{market_rows}</tbody>
        </table>
      </div>
    </div>
  </div>
</div>
</body>
</html>
"""

@app.get("/decisions2", response_class=_V7000_D2_HTMLResponse)
def v7000_decisions2_page(token: str = ""):
    if not _v7000_d2_token_ok(token):
        return _V7000_D2_HTMLResponse("<h1>403</h1><p>Token fehlt oder ist falsch.</p>", status_code=403)
    return _V7000_D2_HTMLResponse(_v7000_d2_html(token))

@app.get("/decisions2.json")
def v7000_decisions2_json(token: str = "", limit: int = 80):
    if not _v7000_d2_token_ok(token):
        return _V7000_D2_JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return _V7000_D2_JSONResponse(_v7000_d2_payload(limit))

@app.get("/decisions2/raw/{decision_id}")
def v7000_decisions2_raw(decision_id: int, token: str = ""):
    if not _v7000_d2_token_ok(token):
        return _V7000_D2_JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    rows = _v7000_d2_rows("""
        SELECT id, created_at, market, direction, trigger, confidence, allow_trade, reason, raw_json
        FROM signal_audit
        WHERE id = ?
        LIMIT 1
    """, (decision_id,))

    if not rows:
        return _V7000_D2_JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    import json
    r = rows[0]
    raw = r.get("raw_json")
    try:
        parsed = json.loads(raw) if raw else {}
    except Exception:
        parsed = {"raw_json_parse_error": True, "raw": raw}

    return _V7000_D2_JSONResponse({"ok": True, "decision": r, "raw": parsed})

# === V7000 DECISIONS V2 PATCH END ===


# === V7000 SHADOW PAGE PATCH START ===

from fastapi.responses import HTMLResponse as _V7000_Shadow_HTMLResponse, JSONResponse as _V7000_Shadow_JSONResponse

def _v7000_shadow_db_path():
    import os
    for _p in ("/app/data/v7000_learning.sqlite3", "data/v7000_learning.sqlite3"):
        if os.path.exists(_p):
            return _p
    return "/app/data/v7000_learning.sqlite3"

def _v7000_shadow_token_path():
    import os
    for _p in ("/app/data/MANUAL_CLOSE_TOKEN", "data/MANUAL_CLOSE_TOKEN"):
        if os.path.exists(_p):
            return _p
    return "/app/data/MANUAL_CLOSE_TOKEN"

def _v7000_shadow_token_ok(_token):
    try:
        from pathlib import Path
        _real = Path(_v7000_shadow_token_path()).read_text().strip()
        return bool(_real) and str(_token or "").strip() == _real
    except Exception:
        return False

def _v7000_shadow_q(_x):
    import html
    return html.escape("" if _x is None else str(_x))

def _v7000_shadow_rows(_sql, _params=()):
    import sqlite3
    _con = sqlite3.connect(_v7000_shadow_db_path())
    _con.row_factory = sqlite3.Row
    try:
        return [dict(_r) for _r in _con.execute(_sql, _params).fetchall()]
    finally:
        _con.close()

def _v7000_shadow_ensure():
    import sqlite3
    _con = sqlite3.connect(_v7000_shadow_db_path())
    try:
        _con.execute("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            shadow_id TEXT PRIMARY KEY,
            client_trade_id TEXT,
            market TEXT,
            direction TEXT,
            setup_name TEXT,
            entry REAL,
            sl REAL,
            tp1 REAL,
            confidence REAL,
            reason TEXT,
            raw_json TEXT,
            status TEXT,
            opened_at TEXT
        )
        """)
        _con.execute("""
        CREATE TABLE IF NOT EXISTS shadow_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shadow_id TEXT,
            client_trade_id TEXT,
            market TEXT,
            direction TEXT,
            setup_name TEXT,
            result TEXT,
            pnl_r REAL,
            exit_price REAL,
            notes TEXT,
            closed_at TEXT
        )
        """)
        _con.commit()
    finally:
        _con.close()

def _v7000_shadow_payload():
    _v7000_shadow_ensure()

    _summary = _v7000_shadow_rows("""
        SELECT
          (SELECT COUNT(*) FROM shadow_trades WHERE status='OPEN') AS open_shadow,
          (SELECT COUNT(*) FROM shadow_trades WHERE status='CLOSED') AS closed_shadow,
          COUNT(o.id) AS outcomes,
          COALESCE(SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END),0) AS wins,
          COALESCE(SUM(CASE WHEN o.pnl_r <= 0 THEN 1 ELSE 0 END),0) AS losses,
          ROUND(COALESCE(AVG(o.pnl_r),0),2) AS avg_r,
          ROUND(COALESCE(SUM(o.pnl_r),0),2) AS total_r,
          CASE
            WHEN COUNT(o.id) > 0 THEN ROUND(100.0 * SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END) / COUNT(o.id),1)
            ELSE 0
          END AS winrate
        FROM shadow_outcomes o
    """)

    _groups = _v7000_shadow_rows("""
        SELECT
          o.market,
          o.direction,
          o.setup_name,
          COUNT(*) AS trades,
          SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN o.pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
          CASE
            WHEN COUNT(*) > 0 THEN ROUND(100.0 * SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END) / COUNT(*),1)
            ELSE 0
          END AS winrate,
          ROUND(AVG(o.pnl_r),2) AS avg_r,
          ROUND(SUM(o.pnl_r),2) AS total_r,
          ROUND(AVG(COALESCE(t.confidence,0)),1) AS avg_confidence,
          MAX(o.closed_at) AS last_closed
        FROM shadow_outcomes o
        LEFT JOIN shadow_trades t ON t.shadow_id=o.shadow_id
        GROUP BY o.market, o.direction, o.setup_name
        ORDER BY trades DESC, total_r DESC, last_closed DESC
        LIMIT 100
    """)

    _open = _v7000_shadow_rows("""
        SELECT
          shadow_id,
          client_trade_id,
          market,
          direction,
          setup_name,
          entry,
          sl,
          tp1,
          confidence,
          reason,
          status,
          opened_at
        FROM shadow_trades
        WHERE status='OPEN'
        ORDER BY opened_at DESC
        LIMIT 100
    """)

    _recent = _v7000_shadow_rows("""
        SELECT
          o.id,
          o.shadow_id,
          o.client_trade_id,
          o.market,
          o.direction,
          o.setup_name,
          o.result,
          o.pnl_r,
          o.exit_price,
          COALESCE(t.confidence,0) AS confidence,
          substr(o.notes,1,220) AS notes,
          o.closed_at
        FROM shadow_outcomes o
        LEFT JOIN shadow_trades t ON t.shadow_id=o.shadow_id
        ORDER BY o.id DESC
        LIMIT 100
    """)

    return {
        "ok": True,
        "db": _v7000_shadow_db_path(),
        "summary": _summary[0] if _summary else {},
        "groups": _groups,
        "open_shadow": _open,
        "recent_outcomes": _recent,
    }

def _v7000_shadow_html(_token):
    _data = _v7000_shadow_payload()
    _s = _data.get("summary", {})

    def _r_class(v):
        try:
            f = float(v)
            if f > 0:
                return "pos"
            if f < 0:
                return "neg"
        except Exception:
            pass
        return ""

    _group_rows = ""
    for r in _data.get("groups", []):
        _group_rows += f"""
        <tr>
          <td><b>{_v7000_shadow_q(r.get("market"))}</b></td>
          <td>{_v7000_shadow_q(r.get("direction"))}</td>
          <td>{_v7000_shadow_q(r.get("setup_name"))}</td>
          <td>{_v7000_shadow_q(r.get("trades"))}</td>
          <td class="pos">{_v7000_shadow_q(r.get("wins"))}</td>
          <td class="neg">{_v7000_shadow_q(r.get("losses"))}</td>
          <td>{_v7000_shadow_q(r.get("winrate"))}%</td>
          <td class="{_r_class(r.get("avg_r"))}">{_v7000_shadow_q(r.get("avg_r"))}</td>
          <td class="{_r_class(r.get("total_r"))}"><b>{_v7000_shadow_q(r.get("total_r"))}</b></td>
          <td>{_v7000_shadow_q(r.get("avg_confidence"))}</td>
          <td class="small">{_v7000_shadow_q(r.get("last_closed"))}</td>
        </tr>
        """
    if not _group_rows:
        _group_rows = '<tr><td colspan="11">Noch keine Shadow Outcomes.</td></tr>'

    _open_cards = ""
    for r in _data.get("open_shadow", []):
        _open_cards += f"""
        <div class="shadowcard open">
          <div class="top">
            <div>
              <div class="market">{_v7000_shadow_q(r.get("market"))} <span>{_v7000_shadow_q(r.get("direction"))}</span></div>
              <div class="setup">{_v7000_shadow_q(r.get("setup_name"))}</div>
            </div>
            <div class="badge wait">OPEN</div>
          </div>
          <div class="levels">
            <div><span>Entry</span><b>{_v7000_shadow_q(r.get("entry"))}</b></div>
            <div><span>SL</span><b>{_v7000_shadow_q(r.get("sl"))}</b></div>
            <div><span>TP1</span><b>{_v7000_shadow_q(r.get("tp1"))}</b></div>
            <div><span>Conf</span><b>{_v7000_shadow_q(r.get("confidence"))}</b></div>
          </div>
          <div class="reason">{_v7000_shadow_q(r.get("reason"))}</div>
          <div class="small">{_v7000_shadow_q(r.get("opened_at"))}</div>
        </div>
        """
    if not _open_cards:
        _open_cards = '<div class="empty">Keine offenen Shadow Trades.</div>'

    _recent_rows = ""
    for r in _data.get("recent_outcomes", []):
        _recent_rows += f"""
        <tr>
          <td>{_v7000_shadow_q(r.get("id"))}</td>
          <td><b>{_v7000_shadow_q(r.get("market"))}</b></td>
          <td>{_v7000_shadow_q(r.get("direction"))}</td>
          <td>{_v7000_shadow_q(r.get("setup_name"))}</td>
          <td>{_v7000_shadow_q(r.get("result"))}</td>
          <td class="{_r_class(r.get("pnl_r"))}"><b>{_v7000_shadow_q(r.get("pnl_r"))}</b></td>
          <td>{_v7000_shadow_q(r.get("exit_price"))}</td>
          <td>{_v7000_shadow_q(r.get("confidence"))}</td>
          <td class="small">{_v7000_shadow_q(r.get("closed_at"))}</td>
        </tr>
        """
    if not _recent_rows:
        _recent_rows = '<tr><td colspan="9">Noch keine Shadow Outcomes.</td></tr>'

    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="20">
<title>V7000 Shadow Trades</title>
<style>
:root {{
  --bg:#071019; --panel:#101a27; --border:#233247; --text:#e8eef7; --muted:#94a3b8;
  --green:#22c55e; --red:#ef4444; --yellow:#facc15; --blue:#3b82f6;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; font-family:Arial,sans-serif;
  background:radial-gradient(circle at top left,rgba(250,204,21,.12),transparent 34%),var(--bg);
  color:var(--text);
}}
.header {{
  padding:20px 16px; background:rgba(16,26,39,.95); border-bottom:1px solid var(--border);
  position:sticky; top:0; z-index:10;
}}
.hin {{ max-width:1200px; margin:0 auto; }}
h1 {{ margin:0 0 5px 0; font-size:27px; }}
.sub {{ color:var(--muted); font-size:13px; }}
.links {{ margin-top:8px; }}
.links a {{ color:#93c5fd; text-decoration:none; margin-right:12px; }}
.wrap {{ max-width:1200px; margin:0 auto; padding:14px; }}
.stats {{
  display:grid; grid-template-columns:repeat(6,1fr); gap:10px; margin-bottom:14px;
}}
.stat {{
  background:rgba(16,26,39,.94); border:1px solid var(--border); border-radius:15px; padding:13px;
}}
.stat .label {{ color:var(--muted); font-size:12px; }}
.stat .value {{ margin-top:7px; font-size:25px; font-weight:bold; }}
.pos {{ color:var(--green); }} .neg {{ color:var(--red); }} .warn {{ color:var(--yellow); }}
.info {{
  background:rgba(250,204,21,.08); border:1px solid #3b2f12; color:#facc15;
  border-radius:15px; padding:13px; margin-bottom:14px; line-height:1.45; font-size:13px;
}}
.section {{
  background:rgba(16,26,39,.94); border:1px solid var(--border); border-radius:17px; overflow:hidden; margin-bottom:14px;
}}
.section h2 {{ margin:0; padding:13px; border-bottom:1px solid var(--border); font-size:18px; }}
.tablewrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; min-width:850px; }}
th, td {{
  padding:9px 10px; border-bottom:1px solid var(--border); text-align:left; font-size:13px; white-space:nowrap;
}}
th {{ color:var(--muted); background:#0b1220; }}
.small {{ color:var(--muted); font-size:11px; }}
.shadowcard {{
  margin:12px; padding:14px; border-radius:16px; background:#0b1220; border:1px solid var(--border); border-left:5px solid var(--yellow);
}}
.top {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
.market {{ font-size:23px; font-weight:bold; }}
.market span {{ color:var(--muted); font-size:15px; margin-left:7px; }}
.setup {{ color:var(--muted); margin-top:4px; font-size:13px; }}
.badge {{ padding:7px 10px; border-radius:999px; font-size:12px; font-weight:bold; }}
.badge.wait {{ background:#3b2f12; color:#facc15; }}
.levels {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-top:12px; }}
.levels div {{ background:#101a27; border:1px solid var(--border); border-radius:12px; padding:10px; }}
.levels span {{ display:block; color:var(--muted); font-size:11px; }}
.levels b {{ display:block; font-size:16px; margin-top:4px; }}
.reason {{ margin-top:10px; color:#dbeafe; font-size:13px; line-height:1.4; }}
.empty {{ padding:18px; color:var(--muted); }}
@media(max-width:900px) {{
  .stats {{ grid-template-columns:repeat(3,1fr); }}
}}
@media(max-width:640px) {{
  h1 {{ font-size:23px; }}
  .wrap {{ padding:12px; }}
  .stats {{ grid-template-columns:repeat(2,1fr); }}
  .levels {{ grid-template-columns:repeat(2,1fr); }}
  .market {{ font-size:21px; }}
}}
</style>
</head>
<body>
<div class="header">
  <div class="hin">
    <h1>🧪 V7000 Shadow Trades</h1>
    <div class="sub">Geblockte Near-Miss Signale werden als Paper-Trade verfolgt · Auto-Refresh 20s</div>
    <div class="links">
      <a href="/master?token={_v7000_shadow_q(_token)}">Master</a>
      <a href="/decisions2?token={_v7000_shadow_q(_token)}">Decisions</a>
      <a href="/learning">Learning</a>
      <a href="/heartbeat">Heartbeat</a>
      <a href="/shadow.json?token={_v7000_shadow_q(_token)}">JSON</a>
    </div>
  </div>
</div>

<div class="wrap">
  <div class="stats">
    <div class="stat"><div class="label">Open Shadow</div><div class="value warn">{_v7000_shadow_q(_s.get("open_shadow"))}</div></div>
    <div class="stat"><div class="label">Closed Shadow</div><div class="value">{_v7000_shadow_q(_s.get("closed_shadow"))}</div></div>
    <div class="stat"><div class="label">Wins</div><div class="value pos">{_v7000_shadow_q(_s.get("wins"))}</div></div>
    <div class="stat"><div class="label">Losses</div><div class="value neg">{_v7000_shadow_q(_s.get("losses"))}</div></div>
    <div class="stat"><div class="label">Winrate</div><div class="value">{_v7000_shadow_q(_s.get("winrate"))}%</div></div>
    <div class="stat"><div class="label">Total R</div><div class="value {_r_class(_s.get("total_r"))}">{_v7000_shadow_q(_s.get("total_r"))}</div></div>
  </div>

  <div class="info">
    Shadow Trades öffnen keine echten Trades. Sie prüfen nur, ob geblockte Signale später TP oder SL erreicht hätten.
  </div>

  <div class="section">
    <h2>Offene Shadow Trades</h2>
    {_open_cards}
  </div>

  <div class="section">
    <h2>Shadow Gruppen</h2>
    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th>Market</th><th>Side</th><th>Setup</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Winrate</th><th>Avg R</th><th>Total R</th><th>Conf</th><th>Last</th>
          </tr>
        </thead>
        <tbody>{_group_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <h2>Letzte Shadow Outcomes</h2>
    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>Market</th><th>Side</th><th>Setup</th><th>Result</th><th>R</th><th>Exit</th><th>Conf</th><th>Closed</th>
          </tr>
        </thead>
        <tbody>{_recent_rows}</tbody>
      </table>
    </div>
  </div>
</div>
</body>
</html>
"""

@app.get("/shadow", response_class=_V7000_Shadow_HTMLResponse)
def v7000_shadow_page(token: str = ""):
    if not _v7000_shadow_token_ok(token):
        return _V7000_Shadow_HTMLResponse("<h1>403</h1><p>Token fehlt oder ist falsch.</p>", status_code=403)
    return _V7000_Shadow_HTMLResponse(_v7000_shadow_html(token))

@app.get("/shadow.json")
def v7000_shadow_json(token: str = ""):
    if not _v7000_shadow_token_ok(token):
        return _V7000_Shadow_JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return _V7000_Shadow_JSONResponse(_v7000_shadow_payload())

# === V7000 SHADOW PAGE PATCH END ===


# === V7100 BLOCKED SIGNAL BOARD PATCH START ===
from fastapi.responses import HTMLResponse as _V7100_BLOCKED_HTMLResponse

def _v7100_blocked_db_path():
    import os
    for p in ["/app/data/v7000_learning.sqlite3", "data/v7000_learning.sqlite3", "/opt/tradingbot_v6000/data/v7000_learning.sqlite3"]:
        if os.path.exists(p):
            return p
    return "data/v7000_learning.sqlite3"

def _v7100_blocked_token_ok(token):
    import os, secrets
    for p in ["/app/data/MANUAL_CLOSE_TOKEN", "data/MANUAL_CLOSE_TOKEN", "/opt/tradingbot_v6000/data/MANUAL_CLOSE_TOKEN"]:
        try:
            if os.path.exists(p):
                real = open(p, "r", encoding="utf-8", errors="ignore").read().strip()
                return bool(token and real and secrets.compare_digest(str(token), real))
        except Exception:
            pass
    return False

def _v7100_blocked_q(x):
    import html
    return html.escape("" if x is None else str(x))

def _v7100_blocked_rows(sql):
    import sqlite3
    con = sqlite3.connect(_v7100_blocked_db_path(), timeout=15)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()

def _v7100_blocked_table(rows):
    q = _v7100_blocked_q
    if not rows:
        return '<div class="empty">Keine Daten.</div>'
    cols = list(rows[0].keys())
    th = "".join([f"<th>{q(c)}</th>" for c in cols])
    body = ""
    for r in rows:
        body += "<tr>"
        for c in cols:
            v = r.get(c)
            color = ""
            if c in ("avg_conf","conf","confidence"):
                try:
                    f = float(v)
                    color = "good" if f >= 60 else "warn" if f >= 50 else ""
                except Exception:
                    pass
            body += f'<td class="{color}">{q(v)}</td>'
        body += "</tr>"
    return f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'

@app.get("/blocked", response_class=_V7100_BLOCKED_HTMLResponse)
def v7100_blocked_board(token: str = ""):
    if not _v7100_blocked_token_ok(token):
        return _V7100_BLOCKED_HTMLResponse("<h1>403</h1><p>Token fehlt oder falsch.</p>", status_code=403)

    q = _v7100_blocked_q

    try:
        summary = _v7100_blocked_rows("""
        SELECT
          COUNT(*) AS total_decisions,
          SUM(CASE WHEN allow_trade=1 THEN 1 ELSE 0 END) AS allowed,
          SUM(CASE WHEN allow_trade=0 THEN 1 ELSE 0 END) AS blocked,
          ROUND(AVG(confidence),1) AS avg_conf,
          ROUND(AVG(CASE WHEN allow_trade=1 THEN confidence END),1) AS allow_avg_conf,
          ROUND(AVG(CASE WHEN allow_trade=0 THEN confidence END),1) AS block_avg_conf
        FROM signal_audit
        """)

        block_groups = _v7100_blocked_rows("""
        SELECT
          CASE
            WHEN reason LIKE '%CLUSTER_GUARD%' THEN 'CLUSTER_GUARD'
            WHEN reason LIKE '%confidence%' THEN 'CONFIDENCE'
            WHEN reason LIKE '%bias%' OR reason LIKE '%countertrend%' THEN 'BIAS_GATE'
            WHEN reason LIKE '%event%' OR reason LIKE '%EVENT%' THEN 'EVENT_RISK'
            WHEN reason LIKE '%chase%' THEN 'CHASE'
            WHEN reason LIKE '%impulse%' THEN 'IMPULSE'
            ELSE 'OTHER'
          END AS block_group,
          COUNT(*) AS count,
          ROUND(AVG(confidence),1) AS avg_conf,
          MAX(created_at) AS last_seen
        FROM signal_audit
        WHERE allow_trade=0
        GROUP BY block_group
        ORDER BY count DESC, avg_conf DESC
        """)

        near_miss = _v7100_blocked_rows("""
        SELECT id, created_at, market, direction, trigger,
               ROUND(confidence,1) AS conf,
               event_risk, bias_gate, entry_state, chase_state, impulse_state,
               substr(reason,1,220) AS reason
        FROM signal_audit
        WHERE allow_trade=0 AND confidence>=50
        ORDER BY confidence DESC, id DESC
        LIMIT 80
        """)

        by_market = _v7100_blocked_rows("""
        SELECT market,
               COUNT(*) AS blocks,
               ROUND(AVG(confidence),1) AS avg_conf,
               MAX(created_at) AS last_block
        FROM signal_audit
        WHERE allow_trade=0
        GROUP BY market
        ORDER BY blocks DESC, avg_conf DESC
        LIMIT 40
        """)

        recent = _v7100_blocked_rows("""
        SELECT id, created_at, market, direction, trigger,
               ROUND(confidence,1) AS conf,
               event_risk,
               substr(reason,1,220) AS reason
        FROM signal_audit
        WHERE allow_trade=0
        ORDER BY id DESC
        LIMIT 60
        """)

        body = f"""
        <div class="box">
          <h2>Decision Summary</h2>
          {_v7100_blocked_table(summary)}
        </div>

        <div class="box">
          <h2>Block Gruppen</h2>
          {_v7100_blocked_table(block_groups)}
        </div>

        <div class="box">
          <h2>Near-Miss Blocks ab Confidence 50</h2>
          <p>Diese Signale waren knapp genug, um später mit Shadow/Outcomes bewertet zu werden.</p>
          {_v7100_blocked_table(near_miss)}
        </div>

        <div class="box">
          <h2>Blocks nach Markt</h2>
          {_v7100_blocked_table(by_market)}
        </div>

        <div class="box">
          <h2>Letzte Blocks</h2>
          {_v7100_blocked_table(recent)}
        </div>
        """
    except Exception as e:
        body = f'<div class="box err">Fehler: {q(repr(e))}</div>'

    html = f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <meta http-equiv="refresh" content="30">
      <title>V7100 Blocked Signal Analyse</title>
      <style>
        body {{ background:#070b12; color:#e8eef7; font-family:Arial,Helvetica,sans-serif; margin:0; padding:14px; }}
        a {{ color:#93c5fd; text-decoration:none; }}
        h1 {{ font-size:24px; margin:4px 0 10px 0; }}
        h2 {{ font-size:19px; margin:0 0 10px 0; }}
        p {{ color:#94a3b8; margin:4px 0 10px 0; }}
        .nav {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
        .nav a {{ background:#132033; border:1px solid #233247; padding:9px 11px; border-radius:10px; }}
        .box {{ background:rgba(16,26,39,.94); border:1px solid #233247; border-radius:16px; padding:14px; margin-bottom:14px; }}
        .tablewrap {{ overflow-x:auto; border:1px solid #233247; border-radius:12px; }}
        table {{ width:100%; border-collapse:collapse; min-width:760px; }}
        th {{ padding:8px; text-align:left; color:#94a3b8; background:#0b1220; border-bottom:1px solid #233247; }}
        td {{ padding:8px; border-bottom:1px solid #172033; vertical-align:top; }}
        .good {{ color:#22c55e; font-weight:bold; }}
        .warn {{ color:#facc15; font-weight:bold; }}
        .err {{ color:#fecaca; }}
        .empty {{ color:#94a3b8; padding:10px; }}
      </style>
    </head>
    <body>
      <h1>🧾 V7100 Blocked Signal Analyse</h1>
      <div class="nav">
        <a href="/master?token={q(token)}">Master</a>
        <a href="/decisions2?token={q(token)}">Decisions</a>
        <a href="/shadow?token={q(token)}">Shadow</a>
        <a href="/learning">Learning</a>
        <a href="/heartbeat">Heartbeat</a>
      </div>
      {body}
    </body>
    </html>
    """
    return _V7100_BLOCKED_HTMLResponse(html)
# === V7100 BLOCKED SIGNAL BOARD PATCH END ===



# === V7100 SHADOW DETAIL BOARD PATCH START ===
from fastapi.responses import HTMLResponse as _V7100_SHADOW_HTMLResponse

def _v7100_shadow_db_path():
    import os
    for p in ["/app/data/v7000_learning.sqlite3", "data/v7000_learning.sqlite3", "/opt/tradingbot_v6000/data/v7000_learning.sqlite3"]:
        if os.path.exists(p):
            return p
    return "data/v7000_learning.sqlite3"

def _v7100_shadow_token_ok(token):
    import os, secrets
    for p in ["/app/data/MANUAL_CLOSE_TOKEN", "data/MANUAL_CLOSE_TOKEN", "/opt/tradingbot_v6000/data/MANUAL_CLOSE_TOKEN"]:
        try:
            if os.path.exists(p):
                real = open(p, "r", encoding="utf-8", errors="ignore").read().strip()
                return bool(token and real and secrets.compare_digest(str(token), real))
        except Exception:
            pass
    return False

def _v7100_shadow_q(x):
    import html
    return html.escape("" if x is None else str(x))

def _v7100_shadow_rows(sql):
    import sqlite3
    con = sqlite3.connect(_v7100_shadow_db_path(), timeout=15)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()

def _v7100_shadow_table(rows):
    q = _v7100_shadow_q
    if not rows:
        return '<div class="empty">Keine Daten.</div>'
    cols = list(rows[0].keys())
    th = "".join([f"<th>{q(c)}</th>" for c in cols])
    body = ""
    for r in rows:
        body += "<tr>"
        for c in cols:
            v = r.get(c)
            css = ""
            if c in ("pnl_r","total_r","avg_r","live_r"):
                try:
                    f = float(v)
                    css = "good" if f > 0 else "bad" if f < 0 else ""
                except Exception:
                    pass
            body += f'<td class="{css}">{q(v)}</td>'
        body += "</tr>"
    return f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'

@app.get("/shadow-detail", response_class=_V7100_SHADOW_HTMLResponse)
def v7100_shadow_detail_board(token: str = ""):
    if not _v7100_shadow_token_ok(token):
        return _V7100_SHADOW_HTMLResponse("<h1>403</h1><p>Token fehlt oder falsch.</p>", status_code=403)

    q = _v7100_shadow_q

    try:
        stats = _v7100_shadow_rows("""
        SELECT
          (SELECT COUNT(*) FROM shadow_trades WHERE status='OPEN') AS open_shadow,
          (SELECT COUNT(*) FROM shadow_trades WHERE status='CLOSED') AS closed_shadow,
          COUNT(o.id) AS outcomes,
          COALESCE(SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END),0) AS wins,
          COALESCE(SUM(CASE WHEN o.pnl_r <= 0 THEN 1 ELSE 0 END),0) AS losses,
          ROUND(COALESCE(AVG(o.pnl_r),0),2) AS avg_r,
          ROUND(COALESCE(SUM(o.pnl_r),0),2) AS total_r,
          CASE WHEN COUNT(o.id)>0 THEN ROUND(100.0*SUM(CASE WHEN o.pnl_r>0 THEN 1 ELSE 0 END)/COUNT(o.id),1) ELSE 0 END AS winrate
        FROM shadow_outcomes o
        """)

        open_shadow = _v7100_shadow_rows("""
        SELECT
          s.shadow_id,
          s.market,
          s.direction,
          s.setup_name,
          s.entry,
          s.sl,
          s.tp1,
          p.close AS last_price,
          CASE
            WHEN s.direction='LONG' AND (s.entry-s.sl)!=0 THEN ROUND((p.close-s.entry)/(s.entry-s.sl),2)
            WHEN s.direction='SHORT' AND (s.sl-s.entry)!=0 THEN ROUND((s.entry-p.close)/(s.sl-s.entry),2)
            ELSE NULL
          END AS live_r,
          s.confidence,
          s.status,
          s.opened_at
        FROM shadow_trades s
        LEFT JOIN price_heartbeats p ON UPPER(p.market)=UPPER(s.market)
        WHERE s.status='OPEN'
        ORDER BY s.opened_at DESC
        LIMIT 50
        """)

        groups = _v7100_shadow_rows("""
        SELECT
          COALESCE(s.market,'?') AS market,
          COALESCE(s.direction,'?') AS direction,
          COALESCE(s.setup_name,'?') AS setup_name,
          COUNT(o.id) AS trades,
          SUM(CASE WHEN o.pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN o.pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
          ROUND(SUM(o.pnl_r),2) AS total_r,
          ROUND(AVG(o.pnl_r),2) AS avg_r
        FROM shadow_outcomes o
        LEFT JOIN shadow_trades s ON s.shadow_id=o.shadow_id
        GROUP BY s.market, s.direction, s.setup_name
        ORDER BY total_r DESC, trades DESC
        LIMIT 50
        """)

        outcomes = _v7100_shadow_rows("""
        SELECT
          o.id,
          o.shadow_id,
          COALESCE(s.market,'?') AS market,
          COALESCE(s.direction,'?') AS direction,
          COALESCE(s.setup_name,'?') AS setup_name,
          o.result,
          o.pnl_r,
          o.exit_price,
          o.closed_at
        FROM shadow_outcomes o
        LEFT JOIN shadow_trades s ON s.shadow_id=o.shadow_id
        ORDER BY o.id DESC
        LIMIT 60
        """)

        body = f"""
        <div class="box">
          <h2>Shadow Stats</h2>
          {_v7100_shadow_table(stats)}
        </div>

        <div class="box">
          <h2>Open Shadow Trades mit Live-R</h2>
          <p>Diese Trades sind nicht live eröffnet, sondern Paper/Shadow zur Bewertung geblockter Signale.</p>
          {_v7100_shadow_table(open_shadow)}
        </div>

        <div class="box">
          <h2>Shadow Gruppen</h2>
          <p>Erst ab genug Outcomes bewerten. Aktuell nur Beobachtung.</p>
          {_v7100_shadow_table(groups)}
        </div>

        <div class="box">
          <h2>Letzte Shadow Outcomes</h2>
          {_v7100_shadow_table(outcomes)}
        </div>
        """
    except Exception as e:
        body = f'<div class="box err">Fehler: {q(repr(e))}</div>'

    html = f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <meta http-equiv="refresh" content="30">
      <title>V7100 Shadow Detail</title>
      <style>
        body {{ background:#070b12; color:#e8eef7; font-family:Arial,Helvetica,sans-serif; margin:0; padding:14px; }}
        a {{ color:#93c5fd; text-decoration:none; }}
        h1 {{ font-size:24px; margin:4px 0 10px 0; }}
        h2 {{ font-size:19px; margin:0 0 10px 0; }}
        p {{ color:#94a3b8; margin:4px 0 10px 0; }}
        .nav {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
        .nav a {{ background:#132033; border:1px solid #233247; padding:9px 11px; border-radius:10px; }}
        .box {{ background:rgba(16,26,39,.94); border:1px solid #233247; border-radius:16px; padding:14px; margin-bottom:14px; }}
        .tablewrap {{ overflow-x:auto; border:1px solid #233247; border-radius:12px; }}
        table {{ width:100%; border-collapse:collapse; min-width:760px; }}
        th {{ padding:8px; text-align:left; color:#94a3b8; background:#0b1220; border-bottom:1px solid #233247; }}
        td {{ padding:8px; border-bottom:1px solid #172033; vertical-align:top; }}
        .good {{ color:#22c55e; font-weight:bold; }}
        .bad {{ color:#ef4444; font-weight:bold; }}
        .err {{ color:#fecaca; }}
        .empty {{ color:#94a3b8; padding:10px; }}
      </style>
    </head>
    <body>
      <h1>🧪 V7100 Shadow Detail Board</h1>
      <div class="nav">
        <a href="/master?token={q(token)}">Master</a>
        <a href="/blocked?token={q(token)}">Blocked</a>
        <a href="/shadow?token={q(token)}">Shadow Alt</a>
        <a href="/learning">Learning</a>
        <a href="/heartbeat">Heartbeat</a>
      </div>
      {body}
    </body>
    </html>
    """
    return _V7100_SHADOW_HTMLResponse(html)
# === V7100 SHADOW DETAIL BOARD PATCH END ===



# === V7100 MARKET PAGES PATCH START ===
from fastapi.responses import HTMLResponse as _V7100_MARKET_HTMLResponse

def _v7100_market_db_path():
    import os
    for p in ["/app/data/v7000_learning.sqlite3", "data/v7000_learning.sqlite3", "/opt/tradingbot_v6000/data/v7000_learning.sqlite3"]:
        if os.path.exists(p):
            return p
    return "data/v7000_learning.sqlite3"

def _v7100_market_token_ok(token):
    import os, secrets
    for p in ["/app/data/MANUAL_CLOSE_TOKEN", "data/MANUAL_CLOSE_TOKEN", "/opt/tradingbot_v6000/data/MANUAL_CLOSE_TOKEN"]:
        try:
            if os.path.exists(p):
                real = open(p, "r", encoding="utf-8", errors="ignore").read().strip()
                return bool(token and real and secrets.compare_digest(str(token), real))
        except Exception:
            pass
    return False

def _v7100_market_q(x):
    import html
    return html.escape("" if x is None else str(x))

def _v7100_market_rows(sql, params=()):
    import sqlite3
    con = sqlite3.connect(_v7100_market_db_path(), timeout=15)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()

def _v7100_market_table(rows, html_cols=None):
    q = _v7100_market_q
    html_cols = set(html_cols or [])
    if not rows:
        return '<div class="empty">Keine Daten.</div>'
    cols = list(rows[0].keys())
    th = "".join([f"<th>{q(c)}</th>" for c in cols])
    body = ""
    for r in rows:
        body += "<tr>"
        for c in cols:
            v = r.get(c)
            css = ""
            if c in ("total_r","avg_r","pnl_r","live_r"):
                try:
                    f = float(v)
                    css = "good" if f > 0 else "bad" if f < 0 else ""
                except Exception:
                    pass
            if c in html_cols:
                body += f'<td class="{css}">{v}</td>'
            else:
                body += f'<td class="{css}">{q(v)}</td>'
        body += "</tr>"
    return f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'

def _v7100_market_page(title, token, body):
    q = _v7100_market_q
    html = f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <meta http-equiv="refresh" content="30">
      <title>{q(title)}</title>
      <style>
        body {{ background:#070b12; color:#e8eef7; font-family:Arial,Helvetica,sans-serif; margin:0; padding:14px; }}
        a {{ color:#93c5fd; text-decoration:none; }}
        h1 {{ font-size:24px; margin:4px 0 10px 0; }}
        h2 {{ font-size:19px; margin:0 0 10px 0; }}
        p {{ color:#94a3b8; margin:4px 0 10px 0; }}
        .nav {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
        .nav a {{ background:#132033; border:1px solid #233247; padding:9px 11px; border-radius:10px; }}
        .box {{ background:rgba(16,26,39,.94); border:1px solid #233247; border-radius:16px; padding:14px; margin-bottom:14px; }}
        .tablewrap {{ overflow-x:auto; border:1px solid #233247; border-radius:12px; }}
        table {{ width:100%; border-collapse:collapse; min-width:760px; }}
        th {{ padding:8px; text-align:left; color:#94a3b8; background:#0b1220; border-bottom:1px solid #233247; }}
        td {{ padding:8px; border-bottom:1px solid #172033; vertical-align:top; }}
        .good {{ color:#22c55e; font-weight:bold; }}
        .bad {{ color:#ef4444; font-weight:bold; }}
        .empty {{ color:#94a3b8; padding:10px; }}
      </style>
    </head>
    <body>
      <h1>{q(title)}</h1>
      <div class="nav">
        <a href="/master?token={q(token)}">Master</a>
        <a href="/markets?token={q(token)}">Markets</a>
        <a href="/blocked?token={q(token)}">Blocked</a>
        <a href="/shadow-detail?token={q(token)}">Shadow Detail</a>
        <a href="/learning">Learning</a>
        <a href="/heartbeat">Heartbeat</a>
      </div>
      {body}
    </body>
    </html>
    """
    return _V7100_MARKET_HTMLResponse(html)

@app.get("/markets", response_class=_V7100_MARKET_HTMLResponse)
def v7100_markets_page(token: str = ""):
    if not _v7100_market_token_ok(token):
        return _V7100_MARKET_HTMLResponse("<h1>403</h1><p>Token fehlt oder falsch.</p>", status_code=403)

    q = _v7100_market_q

    try:
        rows = _v7100_market_rows("""
        SELECT
          h.market AS market,
          h.timeframe AS tf,
          h.close AS close,
          h.received_at AS heartbeat,
          COALESCE(d.decisions,0) AS decisions,
          COALESCE(d.allowed,0) AS allowed,
          COALESCE(d.blocked,0) AS blocked,
          COALESCE(o.outcomes,0) AS outcomes,
          COALESCE(o.total_r,0) AS total_r,
          COALESCE(s.open_shadow,0) AS open_shadow
        FROM price_heartbeats h
        LEFT JOIN (
          SELECT market,
                 COUNT(*) AS decisions,
                 SUM(CASE WHEN allow_trade=1 THEN 1 ELSE 0 END) AS allowed,
                 SUM(CASE WHEN allow_trade=0 THEN 1 ELSE 0 END) AS blocked
          FROM signal_audit
          GROUP BY market
        ) d ON UPPER(d.market)=UPPER(h.market)
        LEFT JOIN (
          SELECT d.market AS market,
                 COUNT(o.id) AS outcomes,
                 ROUND(COALESCE(SUM(o.pnl_r),0),2) AS total_r
          FROM trade_outcomes o
          LEFT JOIN setup_decisions d ON d.id=o.decision_id
          GROUP BY d.market
        ) o ON UPPER(o.market)=UPPER(h.market)
        LEFT JOIN (
          SELECT market, COUNT(*) AS open_shadow
          FROM shadow_trades
          WHERE status='OPEN'
          GROUP BY market
        ) s ON UPPER(s.market)=UPPER(h.market)
        ORDER BY h.market
        """)

        for r in rows:
            m = str(r.get("market") or "").upper()
            r["market"] = f'<a href="/market?token={q(token)}&market={q(m)}"><b>{q(m)}</b></a>'

        body = f"""
        <div class="box">
          <h2>Alle Märkte</h2>
          <p>Heartbeat, Decisions, Blocks, Outcomes, Total R und offene Shadow Trades pro Markt.</p>
          {_v7100_market_table(rows, html_cols=["market"])}
        </div>
        """
    except Exception as e:
        body = f'<div class="box bad">Fehler: {q(repr(e))}</div>'

    return _v7100_market_page("📊 V7100 Market Pages", token, body)

@app.get("/market", response_class=_V7100_MARKET_HTMLResponse)
def v7100_market_detail_page(token: str = "", market: str = ""):
    if not _v7100_market_token_ok(token):
        return _V7100_MARKET_HTMLResponse("<h1>403</h1><p>Token fehlt oder falsch.</p>", status_code=403)

    q = _v7100_market_q
    m = str(market or "").upper().strip()

    if not m:
        return _v7100_market_page("Market Detail", token, '<div class="box">Market fehlt.</div>')

    try:
        hb = _v7100_market_rows("""
        SELECT market, timeframe, open, high, low, close, received_at
        FROM price_heartbeats
        WHERE UPPER(market)=?
        ORDER BY received_at DESC
        LIMIT 5
        """, (m,))

        open_trades = _v7100_market_rows("""
        SELECT client_trade_id, decision_id, market, direction, setup_name, entry, sl, tp1, status, opened_at
        FROM open_trades
        WHERE UPPER(market)=?
        ORDER BY opened_at DESC
        LIMIT 20
        """, (m,))

        decisions = _v7100_market_rows("""
        SELECT id, created_at, market, direction, trigger,
               ROUND(confidence,1) AS conf,
               event_risk, risk_r, allow_trade,
               bias_gate, entry_state, chase_state, impulse_state,
               substr(reason,1,220) AS reason
        FROM signal_audit
        WHERE UPPER(market)=?
        ORDER BY id DESC
        LIMIT 60
        """, (m,))

        outcomes = _v7100_market_rows("""
        SELECT o.id, o.decision_id, d.market, d.direction, d.setup_name,
               o.result, o.pnl_r, o.exit_price, o.closed_at
        FROM trade_outcomes o
        LEFT JOIN setup_decisions d ON d.id=o.decision_id
        WHERE UPPER(d.market)=?
        ORDER BY o.id DESC
        LIMIT 40
        """, (m,))

        shadow = _v7100_market_rows("""
        SELECT
          s.shadow_id, s.market, s.direction, s.setup_name,
          s.entry, s.sl, s.tp1,
          p.close AS last_price,
          CASE
            WHEN s.direction='LONG' AND (s.entry-s.sl)!=0 THEN ROUND((p.close-s.entry)/(s.entry-s.sl),2)
            WHEN s.direction='SHORT' AND (s.sl-s.entry)!=0 THEN ROUND((s.entry-p.close)/(s.sl-s.entry),2)
            ELSE NULL
          END AS live_r,
          s.confidence, s.status, s.opened_at
        FROM shadow_trades s
        LEFT JOIN price_heartbeats p ON UPPER(p.market)=UPPER(s.market)
        WHERE UPPER(s.market)=?
        ORDER BY s.opened_at DESC
        LIMIT 40
        """, (m,))

        body = f"""
        <div class="box"><h2>{q(m)} Heartbeat</h2>{_v7100_market_table(hb)}</div>
        <div class="box"><h2>Open Trades</h2>{_v7100_market_table(open_trades)}</div>
        <div class="box"><h2>Letzte Decisions</h2>{_v7100_market_table(decisions)}</div>
        <div class="box"><h2>Outcomes</h2>{_v7100_market_table(outcomes)}</div>
        <div class="box"><h2>Shadow / Paper Trades</h2>{_v7100_market_table(shadow)}</div>
        """
    except Exception as e:
        body = f'<div class="box bad">Fehler: {q(repr(e))}</div>'

    return _v7100_market_page("📊 Market Detail " + q(m), token, body)
# === V7100 MARKET PAGES PATCH END ===



# === V7100 CLUSTER SESSION INTEL PATCH START ===
from fastapi.responses import HTMLResponse as _V7100_INTEL_HTMLResponse

def _v7100_intel_db_path():
    import os
    for p in ["/app/data/v7000_learning.sqlite3", "data/v7000_learning.sqlite3", "/opt/tradingbot_v6000/data/v7000_learning.sqlite3"]:
        if os.path.exists(p):
            return p
    return "data/v7000_learning.sqlite3"

def _v7100_intel_token_ok(token):
    import os, secrets
    for p in ["/app/data/MANUAL_CLOSE_TOKEN", "data/MANUAL_CLOSE_TOKEN", "/opt/tradingbot_v6000/data/MANUAL_CLOSE_TOKEN"]:
        try:
            if os.path.exists(p):
                real = open(p, "r", encoding="utf-8", errors="ignore").read().strip()
                return bool(token and real and secrets.compare_digest(str(token), real))
        except Exception:
            pass
    return False

def _v7100_intel_q(x):
    import html
    return html.escape("" if x is None else str(x))

def _v7100_intel_rows(sql, params=()):
    import sqlite3
    con = sqlite3.connect(_v7100_intel_db_path(), timeout=15)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()

def _v7100_intel_cols(table):
    try:
        return [r["name"] for r in _v7100_intel_rows(f"PRAGMA table_info({table})")]
    except Exception:
        return []

def _v7100_intel_cluster(market):
    m = str(market or "").upper().strip()
    if m.endswith("JPY") or m in ("USDJPY","EURJPY","GBPJPY","AUDJPY","NZDJPY","CADJPY","CHFJPY"):
        return "JPY_FX"
    if m in ("EURUSD","GBPUSD","AUDUSD","NZDUSD","USDCAD","USDCHF"):
        return "USD_FX"
    if m in ("US100","NAS100","NASDAQ","NQ","MNQ","US500","SPX","SP500","ES","MES","US30","DOW","YM","MYM"):
        return "US_INDEX"
    if m in ("GER40","DAX","DE40","FRA40","CAC40","EU50","STOXX50"):
        return "EU_INDEX"
    if m in ("FTSE100","UK100"):
        return "UK_INDEX"
    if m in ("XAUUSD","GOLD","GC","MGC","XAGUSD","SILVER"):
        return "METALS"
    if m in ("BTCUSD","BTCUSDT","ETHUSD","ETHUSDT","BTC","ETH"):
        return "CRYPTO"
    if m in ("OIL","USOIL","UKOIL","WTI","BRENT","CL","MCL","UKOILSPOT"):
        return "OIL"
    return "SINGLE_" + m

def _v7100_intel_table(rows):
    q = _v7100_intel_q
    if not rows:
        return '<div class="empty">Keine Daten.</div>'
    cols = list(rows[0].keys())
    th = "".join([f"<th>{q(c)}</th>" for c in cols])
    body = ""
    for r in rows:
        body += "<tr>"
        for c in cols:
            v = r.get(c)
            css = ""
            if c in ("total_r","avg_r","today_r","shadow_r"):
                try:
                    f = float(v)
                    css = "good" if f > 0 else "bad" if f < 0 else ""
                except Exception:
                    pass
            if c in ("status","cluster_status"):
                if "FULL" in str(v):
                    css = "warn"
                elif "FREE" in str(v):
                    css = "good"
            body += f'<td class="{css}">{q(v)}</td>'
        body += "</tr>"
    return f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'

def _v7100_intel_page(title, token, body):
    q = _v7100_intel_q
    return _V7100_INTEL_HTMLResponse(f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <meta http-equiv="refresh" content="30">
      <title>{q(title)}</title>
      <style>
        body {{ background:#070b12; color:#e8eef7; font-family:Arial,Helvetica,sans-serif; margin:0; padding:14px; }}
        a {{ color:#93c5fd; text-decoration:none; }}
        h1 {{ font-size:24px; margin:4px 0 10px 0; }}
        h2 {{ font-size:19px; margin:0 0 10px 0; }}
        p {{ color:#94a3b8; margin:4px 0 10px 0; }}
        .nav {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
        .nav a {{ background:#132033; border:1px solid #233247; padding:9px 11px; border-radius:10px; }}
        .box {{ background:rgba(16,26,39,.94); border:1px solid #233247; border-radius:16px; padding:14px; margin-bottom:14px; }}
        .tablewrap {{ overflow-x:auto; border:1px solid #233247; border-radius:12px; }}
        table {{ width:100%; border-collapse:collapse; min-width:760px; }}
        th {{ padding:8px; text-align:left; color:#94a3b8; background:#0b1220; border-bottom:1px solid #233247; }}
        td {{ padding:8px; border-bottom:1px solid #172033; vertical-align:top; }}
        .good {{ color:#22c55e; font-weight:bold; }}
        .bad {{ color:#ef4444; font-weight:bold; }}
        .warn {{ color:#facc15; font-weight:bold; }}
        .empty {{ color:#94a3b8; padding:10px; }}
      </style>
    </head>
    <body>
      <h1>{q(title)}</h1>
      <div class="nav">
        <a href="/master?token={q(token)}">Master</a>
        <a href="/cluster-intel?token={q(token)}">Cluster Intel</a>
        <a href="/session-intel?token={q(token)}">Session Intel</a>
        <a href="/markets?token={q(token)}">Markets</a>
        <a href="/blocked?token={q(token)}">Blocked</a>
        <a href="/shadow-detail?token={q(token)}">Shadow Detail</a>
      </div>
      {body}
    </body>
    </html>
    """)

@app.get("/cluster-intel", response_class=_V7100_INTEL_HTMLResponse)
def v7100_cluster_intel(token: str = ""):
    if not _v7100_intel_token_ok(token):
        return _V7100_INTEL_HTMLResponse("<h1>403</h1><p>Token fehlt oder falsch.</p>", status_code=403)

    try:
        open_rows = _v7100_intel_rows("""
        SELECT market, direction, setup_name, opened_at
        FROM open_trades
        WHERE status='OPEN'
        ORDER BY opened_at DESC
        """)

        outcomes = _v7100_intel_rows("""
        SELECT d.market, d.direction, d.setup_name, o.pnl_r, o.closed_at
        FROM trade_outcomes o
        LEFT JOIN setup_decisions d ON d.id=o.decision_id
        """)

        shadow_open = _v7100_intel_rows("""
        SELECT market, direction, setup_name, confidence, opened_at
        FROM shadow_trades
        WHERE status='OPEN'
        ORDER BY opened_at DESC
        """)

        shadow_out = _v7100_intel_rows("""
        SELECT s.market, s.direction, s.setup_name, o.pnl_r, o.closed_at
        FROM shadow_outcomes o
        LEFT JOIN shadow_trades s ON s.shadow_id=o.shadow_id
        """)

        blocks = _v7100_intel_rows("""
        SELECT market, direction, trigger, confidence, reason, created_at
        FROM signal_audit
        WHERE allow_trade=0 AND reason LIKE '%CLUSTER_GUARD%'
        ORDER BY id DESC
        LIMIT 30
        """)

        clusters = ["JPY_FX","USD_FX","US_INDEX","EU_INDEX","UK_INDEX","METALS","CRYPTO","OIL"]
        data = {c: {"cluster": c, "open": 0, "open_markets": "-", "status": "FREE", "trades": 0, "wins": 0, "losses": 0, "total_r": 0.0, "avg_r": 0.0, "shadow_open": 0, "shadow_r": 0.0} for c in clusters}

        open_by = {}
        for r in open_rows:
            c = _v7100_intel_cluster(r.get("market"))
            if c not in data:
                data[c] = {"cluster": c, "open": 0, "open_markets": "-", "status": "FREE", "trades": 0, "wins": 0, "losses": 0, "total_r": 0.0, "avg_r": 0.0, "shadow_open": 0, "shadow_r": 0.0}
            data[c]["open"] += 1
            open_by.setdefault(c, []).append(str(r.get("market")) + " " + str(r.get("direction")))

        for c, arr in open_by.items():
            data[c]["open_markets"] = ", ".join(arr)
            data[c]["status"] = "FULL 2/2" if len(arr) >= 2 else "OK 1/2"

        for r in outcomes:
            c = _v7100_intel_cluster(r.get("market"))
            if c not in data:
                data[c] = {"cluster": c, "open": 0, "open_markets": "-", "status": "FREE", "trades": 0, "wins": 0, "losses": 0, "total_r": 0.0, "avg_r": 0.0, "shadow_open": 0, "shadow_r": 0.0}
            pnl = float(r.get("pnl_r") or 0)
            data[c]["trades"] += 1
            data[c]["wins"] += 1 if pnl > 0 else 0
            data[c]["losses"] += 1 if pnl <= 0 else 0
            data[c]["total_r"] += pnl

        for r in shadow_open:
            c = _v7100_intel_cluster(r.get("market"))
            if c not in data:
                data[c] = {"cluster": c, "open": 0, "open_markets": "-", "status": "FREE", "trades": 0, "wins": 0, "losses": 0, "total_r": 0.0, "avg_r": 0.0, "shadow_open": 0, "shadow_r": 0.0}
            data[c]["shadow_open"] += 1

        for r in shadow_out:
            c = _v7100_intel_cluster(r.get("market"))
            if c not in data:
                data[c] = {"cluster": c, "open": 0, "open_markets": "-", "status": "FREE", "trades": 0, "wins": 0, "losses": 0, "total_r": 0.0, "avg_r": 0.0, "shadow_open": 0, "shadow_r": 0.0}
            data[c]["shadow_r"] += float(r.get("pnl_r") or 0)

        rows = []
        for c, d in data.items():
            t = d["trades"]
            d["avg_r"] = round(d["total_r"] / t, 2) if t else 0
            d["total_r"] = round(d["total_r"], 2)
            d["shadow_r"] = round(d["shadow_r"], 2)
            rows.append(d)

        rows = sorted(rows, key=lambda x: (x["open"], x["total_r"]), reverse=True)

        body = f"""
        <div class="box">
          <h2>Cluster Heat / Performance</h2>
          <p>Reine Anzeige. Kein automatisches Freischalten oder Blockieren.</p>
          {_v7100_intel_table(rows)}
        </div>
        <div class="box">
          <h2>Aktuelle Shadow Trades nach Cluster</h2>
          {_v7100_intel_table(shadow_open)}
        </div>
        <div class="box">
          <h2>Letzte Cluster Blocks</h2>
          {_v7100_intel_table(blocks)}
        </div>
        """
    except Exception as e:
        body = f'<div class="box bad">Fehler: {_v7100_intel_q(repr(e))}</div>'

    return _v7100_intel_page("🧩 V7100 Cluster Intelligence", token, body)

@app.get("/session-intel", response_class=_V7100_INTEL_HTMLResponse)
def v7100_session_intel(token: str = ""):
    if not _v7100_intel_token_ok(token):
        return _V7100_INTEL_HTMLResponse("<h1>403</h1><p>Token fehlt oder falsch.</p>", status_code=403)

    try:
        cols = _v7100_intel_cols("setup_decisions")
        sess = "COALESCE(d.session,'UNKNOWN')" if "session" in cols else "'UNKNOWN'"
        tf = "COALESCE(d.timeframe,'?')" if "timeframe" in cols else "'?'"

        session_rows = _v7100_intel_rows(f"""
        SELECT
          {sess} AS session,
          {tf} AS timeframe,
          COUNT(o.id) AS trades,
          SUM(CASE WHEN o.pnl_r>0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN o.pnl_r<=0 THEN 1 ELSE 0 END) AS losses,
          CASE WHEN COUNT(o.id)>0 THEN ROUND(100.0*SUM(CASE WHEN o.pnl_r>0 THEN 1 ELSE 0 END)/COUNT(o.id),1) ELSE 0 END AS winrate,
          ROUND(SUM(o.pnl_r),2) AS total_r,
          ROUND(AVG(o.pnl_r),2) AS avg_r
        FROM trade_outcomes o
        LEFT JOIN setup_decisions d ON d.id=o.decision_id
        GROUP BY session, timeframe
        ORDER BY total_r DESC, trades DESC
        """)

        market_session = _v7100_intel_rows(f"""
        SELECT
          {sess} AS session,
          COALESCE(d.market,'?') AS market,
          COALESCE(d.direction,'?') AS direction,
          COUNT(o.id) AS trades,
          SUM(CASE WHEN o.pnl_r>0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN o.pnl_r<=0 THEN 1 ELSE 0 END) AS losses,
          ROUND(SUM(o.pnl_r),2) AS total_r,
          ROUND(AVG(o.pnl_r),2) AS avg_r
        FROM trade_outcomes o
        LEFT JOIN setup_decisions d ON d.id=o.decision_id
        GROUP BY session, d.market, d.direction
        ORDER BY total_r DESC, trades DESC
        LIMIT 60
        """)

        today_rows = _v7100_intel_rows(f"""
        SELECT
          {sess} AS session,
          {tf} AS timeframe,
          COUNT(o.id) AS trades,
          SUM(CASE WHEN o.pnl_r>0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN o.pnl_r<=0 THEN 1 ELSE 0 END) AS losses,
          ROUND(SUM(o.pnl_r),2) AS today_r,
          ROUND(AVG(o.pnl_r),2) AS avg_r
        FROM trade_outcomes o
        LEFT JOIN setup_decisions d ON d.id=o.decision_id
        WHERE substr(o.closed_at,1,10)=date('now')
        GROUP BY session, timeframe
        ORDER BY today_r DESC, trades DESC
        """)

        recent = _v7100_intel_rows(f"""
        SELECT
          o.id,
          {sess} AS session,
          {tf} AS timeframe,
          d.market,
          d.direction,
          d.setup_name,
          o.result,
          o.pnl_r,
          o.closed_at
        FROM trade_outcomes o
        LEFT JOIN setup_decisions d ON d.id=o.decision_id
        ORDER BY o.id DESC
        LIMIT 30
        """)

        body = f"""
        <div class="box">
          <h2>Heute nach Session</h2>
          {_v7100_intel_table(today_rows)}
        </div>
        <div class="box">
          <h2>All-Time Session Performance</h2>
          {_v7100_intel_table(session_rows)}
        </div>
        <div class="box">
          <h2>Market + Direction nach Session</h2>
          {_v7100_intel_table(market_session)}
        </div>
        <div class="box">
          <h2>Letzte Outcomes mit Session</h2>
          {_v7100_intel_table(recent)}
        </div>
        """
    except Exception as e:
        body = f'<div class="box bad">Fehler: {_v7100_intel_q(repr(e))}</div>'

    return _v7100_intel_page("🕒 V7100 Session Intelligence", token, body)
# === V7100 CLUSTER SESSION INTEL PATCH END ===



# === V7200 EVENT RISK / NEWS COOLDOWN FASTAPI ROUTES ===
import os as _v7200_os
import json as _v7200_json
import html as _v7200_html
import re as _v7200_re
from pathlib import Path as _v7200_Path
from datetime import datetime as _v7200_datetime, timezone as _v7200_timezone, timedelta as _v7200_timedelta

from fastapi import Request as _V7200Request
from fastapi.responses import HTMLResponse as _V7200HTMLResponse
from fastapi.responses import JSONResponse as _V7200JSONResponse


def _v7200_project_root():
    here = _v7200_Path(__file__).resolve()
    candidates = [
        here.parent,
        here.parent.parent,
        _v7200_Path.cwd(),
        _v7200_Path("/opt/tradingbot_v6000")
    ]
    for c in candidates:
        if (c / "data").exists():
            return c
    return here.parent.parent


def _v7200_parse_utc(ts):
    if not ts:
        return None
    try:
        ts = str(ts).replace("Z", "+00:00")
        dt = _v7200_datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_v7200_timezone.utc)
        return dt.astimezone(_v7200_timezone.utc)
    except Exception:
        return None


def _v7200_known_tokens():
    tokens = set()

    for name in [
        "DASHBOARD_TOKEN",
        "TRADINGBOT_TOKEN",
        "TOKEN",
        "ADMIN_TOKEN",
        "MASTER_TOKEN"
    ]:
        val = str(_v7200_os.environ.get(name, "") or "").strip()
        if val:
            tokens.add(val)

    for name in [
        "DASHBOARD_TOKEN",
        "TRADINGBOT_TOKEN",
        "TOKEN",
        "ADMIN_TOKEN",
        "MASTER_TOKEN"
    ]:
        val = str(globals().get(name, "") or "").strip()
        if val:
            tokens.add(val)

    root = _v7200_project_root()
    for rel in ["ops/v7000_check.sh", "ops/v7100_menu.sh"]:
        p = root / rel
        if p.exists():
            try:
                s = p.read_text(encoding="utf-8", errors="ignore")
                for m in _v7200_re.findall(r"token=([A-Za-z0-9_\-]{20,})", s):
                    tokens.add(m)
                for m in _v7200_re.findall(r"TOKEN=['\"]?([A-Za-z0-9_\-]{20,})", s):
                    tokens.add(m)
            except Exception:
                pass

    return tokens


def _v7200_token_ok(request):
    tokens = _v7200_known_tokens()

    # Wenn keine Tokenquelle gefunden wird, Route nicht blockieren.
    if not tokens:
        return True

    supplied = (
        request.query_params.get("token")
        or request.headers.get("X-Token")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
        or ""
    ).strip()

    return supplied in tokens


def _v7200_event_risk_data():
    root = _v7200_project_root()
    path = _v7200_os.environ.get("EVENT_RISK_FILE") or str(root / "data" / "event_risk.json")

    now = _v7200_datetime.now(_v7200_timezone.utc)

    data = {
        "version": "V7200",
        "default_pre_minutes": 15,
        "default_post_minutes": 15,
        "events": []
    }

    load_error = None

    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = _v7200_json.load(f)
            if isinstance(loaded, dict):
                data.update(loaded)
    except Exception as exc:
        load_error = str(exc)

    default_pre = int(data.get("default_pre_minutes", 15) or 15)
    default_post = int(data.get("default_post_minutes", 15) or 15)

    rows = []
    active = []
    upcoming = []

    for ev in data.get("events", []):
        if not isinstance(ev, dict):
            continue

        event_time = _v7200_parse_utc(ev.get("time_utc"))
        pre = int(ev.get("pre_minutes", default_pre) or default_pre)
        post = int(ev.get("post_minutes", default_post) or default_post)

        status = "unknown"
        minutes_to_event = None
        cooldown_start = None
        cooldown_end = None

        if event_time:
            cooldown_start = event_time - _v7200_timedelta(minutes=pre)
            cooldown_end = event_time + _v7200_timedelta(minutes=post)
            minutes_to_event = round((event_time - now).total_seconds() / 60, 1)

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

        rows.append(row)

        if status == "ACTIVE_COOLDOWN":
            active.append(row)
        elif status == "upcoming":
            upcoming.append(row)

    rows.sort(key=lambda x: 999999999 if x["minutes_to_event"] is None else x["minutes_to_event"])
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
        "events": rows,
        "source_file": path,
        "load_error": load_error
    }


def _v7200_esc(x):
    return _v7200_html.escape(str(x))


def _v7200_event_row(ev):
    markets = ", ".join(ev.get("markets") or [])
    mins = ev.get("minutes_to_event")
    mins_txt = "-" if mins is None else str(mins)
    return f"""
    <tr>
      <td><b>{_v7200_esc(ev.get('status'))}</b></td>
      <td>{_v7200_esc(ev.get('impact'))}</td>
      <td>{_v7200_esc(ev.get('currency'))}</td>
      <td>{_v7200_esc(ev.get('title'))}</td>
      <td>{_v7200_esc(ev.get('time_utc'))}</td>
      <td>{_v7200_esc(mins_txt)}</td>
      <td>{_v7200_esc(ev.get('pre_minutes'))}/{_v7200_esc(ev.get('post_minutes'))} min</td>
      <td>{_v7200_esc(markets)}</td>
      <td>{_v7200_esc(ev.get('note', ''))}</td>
    </tr>
    """


@app.get("/event-risk", response_class=_V7200HTMLResponse)
def v7200_event_risk_page(request: _V7200Request):
    if not _v7200_token_ok(request):
        return _V7200HTMLResponse("unauthorized", status_code=401)

    d = _v7200_event_risk_data()

    badge = "OK"
    if d["risk_level"] == "HIGH":
        badge = "HIGH RISK / COOLDOWN ACTIVE"
    elif d["risk_level"] == "MEDIUM":
        badge = "MEDIUM RISK / COOLDOWN ACTIVE"

    active_html = "".join(_v7200_event_row(x) for x in d["active_events"]) or "<tr><td colspan='9'>No active cooldown.</td></tr>"
    upcoming_html = "".join(_v7200_event_row(x) for x in d["upcoming_events"]) or "<tr><td colspan='9'>No upcoming events.</td></tr>"
    all_html = "".join(_v7200_event_row(x) for x in d["events"]) or "<tr><td colspan='9'>No events configured.</td></tr>"

    error_html = ""
    if d.get("load_error"):
        error_html = f"<div class='err'>JSON load error: {_v7200_esc(d.get('load_error'))}</div>"

    token = request.query_params.get("token", "")
    token_q = f"?token={_v7200_esc(token)}" if token else ""

    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TradingBot V7200 Event Risk</title>
  <style>
    body {{
      background:#0b0f14;
      color:#e8eef5;
      font-family:Arial, sans-serif;
      margin:24px;
    }}
    a {{ color:#8cc8ff; text-decoration:none; }}
    .card {{
      background:#121923;
      border:1px solid #263447;
      border-radius:12px;
      padding:16px;
      margin-bottom:18px;
    }}
    .badge {{
      display:inline-block;
      padding:8px 12px;
      border-radius:999px;
      font-weight:bold;
      background:#1f6f3d;
    }}
    .HIGH {{ background:#8a1f1f; }}
    .MEDIUM {{ background:#8a6a1f; }}
    .LOW {{ background:#1f6f3d; }}
    table {{
      width:100%;
      border-collapse:collapse;
      margin-top:10px;
      font-size:13px;
    }}
    th, td {{
      border-bottom:1px solid #263447;
      padding:8px;
      text-align:left;
      vertical-align:top;
    }}
    th {{ color:#a9bfd6; }}
    .muted {{ color:#9fb0c0; }}
    .err {{
      background:#3b1515;
      border:1px solid #7a2d2d;
      padding:10px;
      border-radius:8px;
      margin-bottom:14px;
    }}
  </style>
</head>
<body>
  <h1>TradingBot V7200 — Event Risk / News Cooldown</h1>

  <div class="card">
    <div class="badge {_v7200_esc(d['risk_level'])}">{_v7200_esc(badge)}</div>
    <p class="muted">
      Now UTC: {_v7200_esc(d['now_utc'])}<br>
      Active events: {_v7200_esc(d['active_count'])} |
      Upcoming events: {_v7200_esc(d['upcoming_count'])}<br>
      Source: {_v7200_esc(d['source_file'])}
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

  {error_html}

  <div class="card">
    <h2>Active Cooldown</h2>
    <table>
      <tr>
        <th>Status</th><th>Impact</th><th>Currency</th><th>Title</th><th>Time UTC</th>
        <th>Min to Event</th><th>Pre/Post</th><th>Markets</th><th>Note</th>
      </tr>
      {active_html}
    </table>
  </div>

  <div class="card">
    <h2>Upcoming Events</h2>
    <table>
      <tr>
        <th>Status</th><th>Impact</th><th>Currency</th><th>Title</th><th>Time UTC</th>
        <th>Min to Event</th><th>Pre/Post</th><th>Markets</th><th>Note</th>
      </tr>
      {upcoming_html}
    </table>
  </div>

  <div class="card">
    <h2>All Configured Events</h2>
    <table>
      <tr>
        <th>Status</th><th>Impact</th><th>Currency</th><th>Title</th><th>Time UTC</th>
        <th>Min to Event</th><th>Pre/Post</th><th>Markets</th><th>Note</th>
      </tr>
      {all_html}
    </table>
  </div>
</body>
</html>
"""
    return _V7200HTMLResponse(html)


@app.get("/event-risk.json")
def v7200_event_risk_json(request: _V7200Request):
    if not _v7200_token_ok(request):
        return _V7200JSONResponse({"error": "unauthorized"}, status_code=401)
    return _V7200JSONResponse(_v7200_event_risk_data())
# === END V7200 EVENT RISK / NEWS COOLDOWN FASTAPI ROUTES ===


# === V7200 EVENT RISK ROUTER INCLUDE ===
try:
    from app.v7200_event_risk import router as v7200_event_risk_router
    app.include_router(v7200_event_risk_router)
    print("[V7200] Event Risk router loaded")
except Exception as exc:
    print("[V7200] Event Risk router failed:", exc)
# === END V7200 EVENT RISK ROUTER INCLUDE ===

# === V7200.1 EVENT RISK BANNERS INSTALL ===
try:
    from app.v7200_1_event_badges import install_v7200_1_event_badges
    install_v7200_1_event_badges(app)
except Exception as exc:
    print("[V7200.1] Event Risk banners failed:", exc)
# === END V7200.1 EVENT RISK BANNERS INSTALL ===

# === V7200.2 MARKET RISK INSTALL ===
try:
    from app.v7200_2_market_risk import install_v7200_2_market_risk
    install_v7200_2_market_risk(app)
except Exception as exc:
    print("[V7200.2] Market Risk install failed:", exc)
# === END V7200.2 MARKET RISK INSTALL ===

# === V7200.3 EVENT DECISION OBSERVER INSTALL ===
try:
    from app.v7200_3_event_decision_observer import install_v7200_3_event_decision_observer
    install_v7200_3_event_decision_observer(app)
except Exception as exc:
    print("[V7200.3] Event Decision Observer install failed:", exc)
# === END V7200.3 EVENT DECISION OBSERVER INSTALL ===

# === V7200.4 OBSERVER LOG BOARD INSTALL ===
try:
    from app.v7200_4_observer_log_board import install_v7200_4_observer_log_board
    install_v7200_4_observer_log_board(app)
except Exception as exc:
    print("[V7200.4] Observer Log Board install failed:", exc)
# === END V7200.4 OBSERVER LOG BOARD INSTALL ===

# === V7200.5 OBSERVER SUMMARY CARDS INSTALL ===
try:
    from app.v7200_5_observer_summary_cards import install_v7200_5_observer_summary_cards
    install_v7200_5_observer_summary_cards(app)
except Exception as exc:
    print("[V7200.5] Observer Summary Cards install failed:", exc)
# === END V7200.5 OBSERVER SUMMARY CARDS INSTALL ===

# === V7200.6 OBSERVER LOG ROTATION INSTALL ===
try:
    from app.v7200_6_observer_log_rotation import install_v7200_6_observer_log_rotation
    install_v7200_6_observer_log_rotation(app)
except Exception as exc:
    print("[V7200.6] Observer Log Rotation install failed:", exc)
# === END V7200.6 OBSERVER LOG ROTATION INSTALL ===

# === V7200.7 AUTO MAINTENANCE STATUS INSTALL ===
try:
    from app.v7200_7_auto_maintenance_status import install_v7200_7_auto_maintenance_status
    install_v7200_7_auto_maintenance_status(app)
except Exception as exc:
    print("[V7200.7] Auto Maintenance Status install failed:", exc)
# === END V7200.7 AUTO MAINTENANCE STATUS INSTALL ===

# === V7200.8 INTERNAL CALENDAR SYNC INSTALL ===
try:
    from app.v7200_8_calendar_sync import install_v7200_8_calendar_sync
    install_v7200_8_calendar_sync(app)
except Exception as exc:
    print("[V7200.8] Internal Calendar Sync install failed:", exc)
# === END V7200.8 INTERNAL CALENDAR SYNC INSTALL ===

# === V7201 EVENT GATE PACK INSTALL ===
try:
    from app.v7201_event_gate_pack import install_v7201_event_gate_pack
    install_v7201_event_gate_pack(app)
except Exception as exc:
    print("[V7201] Event Gate Pack install failed:", exc)
# === END V7201 EVENT GATE PACK INSTALL ===

# === V7201.3 GATE TEST ENDPOINT INSTALL ===
try:
    from app.v7201_3_gate_test_endpoint import install_v7201_3_gate_test_endpoint
    install_v7201_3_gate_test_endpoint(app)
except Exception as exc:
    print("[V7201.3.1] Gate Test Endpoint install failed:", exc)
# === END V7201.3 GATE TEST ENDPOINT INSTALL ===

# === V7202 TRADE PROTECTION INSTALL ===
try:
    from app.v7202_trade_protection import install_v7202_trade_protection
    install_v7202_trade_protection(app)
except Exception as exc:
    print("[V7202] Trade Protection install failed:", exc)
# === END V7202 TRADE PROTECTION INSTALL ===

# === V7203 PRE NEWS MANAGER INSTALL ===
try:
    from app.v7203_pre_news_manager import install_v7203_pre_news_manager
    install_v7203_pre_news_manager(app)
except Exception as exc:
    print("[V7203] Pre-News Manager install failed:", exc)
# === END V7203 PRE NEWS MANAGER INSTALL ===

# === V7204 NEWS AWARE ENTRY SCORING INSTALL ===
try:
    from app.v7204_news_aware_entry_scoring import install_v7204_news_aware_entry_scoring
    install_v7204_news_aware_entry_scoring(app)
except Exception as exc:
    print("[V7204] News-Aware Entry Scoring install failed:", exc)
# === END V7204 NEWS AWARE ENTRY SCORING INSTALL ===

# === V7205 SIGNAL QUALITY DASHBOARD INSTALL ===
try:
    from app.v7205_signal_quality_dashboard import install_v7205_signal_quality_dashboard
    install_v7205_signal_quality_dashboard(app)
except Exception as exc:
    print("[V7205] Signal Quality Dashboard install failed:", exc)
# === END V7205 SIGNAL QUALITY DASHBOARD INSTALL ===

# === V7206 RANKING SNAPSHOT COMPACT UI INSTALL ===
try:
    from app.v7206_ranking_snapshot_compact_ui import install_v7206_ranking_snapshot_compact_ui
    install_v7206_ranking_snapshot_compact_ui(app)
except Exception as exc:
    print("[V7206] Ranking Snapshot Compact UI install failed:", exc)
# === END V7206 RANKING SNAPSHOT COMPACT UI INSTALL ===

# === V7207 MASTER COMPACT CONTROL CENTER INSTALL ===
try:
    from app.v7207_master_compact_control_center import install_v7207_master_compact_control_center
    install_v7207_master_compact_control_center(app)
except Exception as exc:
    print("[V7207] Master Compact Control Center install failed:", exc)
# === END V7207 MASTER COMPACT CONTROL CENTER INSTALL ===

# === V7208 SINGLE HEADER MODE INSTALL ===
try:
    from app.v7208_single_header_mode import install_v7208_single_header_mode
    install_v7208_single_header_mode(app)
except Exception as exc:
    print("[V7208] Single Header Mode install failed:", exc)
# === END V7208 SINGLE HEADER MODE INSTALL ===

# === V7209 V7212 DECISION SUITE INSTALL ===
try:
    from app.v7209_v7212_decision_suite import install_v7209_v7212_decision_suite
    install_v7209_v7212_decision_suite(app)
except Exception as exc:
    print("[V7209-V7212] Decision Suite install failed:", exc)
# === END V7209 V7212 DECISION SUITE INSTALL ===

# === V7213 V7218 INTELLIGENCE PACK INSTALL ===
try:
    from app.v7213_v7218_intelligence_pack import install_v7213_v7218_intelligence_pack
    install_v7213_v7218_intelligence_pack(app)
except Exception as exc:
    print("[V7213-V7218] Intelligence Pack install failed:", exc)
# === END V7213 V7218 INTELLIGENCE PACK INSTALL ===

# === V7219 V7226 PERFORMANCE LEARNING PACK INSTALL ===
try:
    from app.v7219_v7226_performance_learning_pack import install_v7219_v7226_performance_learning_pack
    install_v7219_v7226_performance_learning_pack(app)
except Exception as exc:
    print("[V7219-V7226] Performance Learning Pack install failed:", exc)
# === END V7219 V7226 PERFORMANCE LEARNING PACK INSTALL ===

# === V7227 V7235 RISK TRADE MANAGEMENT PACK INSTALL ===
try:
    from app.v7227_v7235_risk_trade_management_pack import install_v7227_v7235_risk_trade_management_pack
    install_v7227_v7235_risk_trade_management_pack(app)
except Exception as exc:
    print("[V7227-V7235] Risk Trade Management Pack install failed:", exc)
# === END V7227 V7235 RISK TRADE MANAGEMENT PACK INSTALL ===

# === V7236 MASTER INTEGRATION PACK INSTALL ===
try:
    from app.v7236_master_integration_pack import install_v7236_master_integration_pack
    install_v7236_master_integration_pack(app)
except Exception as exc:
    print("[V7236] Master Integration Pack install failed:", exc)
# === END V7236 MASTER INTEGRATION PACK INSTALL ===

# === V7237 SETUP OPTIMIZER LIVE GATE INSTALL ===
try:
    from app.v7237_setup_optimizer_live_gate import install_v7237_setup_optimizer_live_gate
    install_v7237_setup_optimizer_live_gate(app)
except Exception as exc:
    print("[V7237] Setup Optimizer Live Gate install failed:", exc)
# === END V7237 SETUP OPTIMIZER LIVE GATE INSTALL ===

# === V7238 ENTRY TIMING BIAS OPTIMIZER INSTALL ===
try:
    from app.v7238_entry_timing_bias_optimizer import install_v7238_entry_timing_bias_optimizer
    install_v7238_entry_timing_bias_optimizer(app)
except Exception as exc:
    print("[V7238] Entry Timing Bias Optimizer install failed:", exc)
# === END V7238 ENTRY TIMING BIAS OPTIMIZER INSTALL ===

# === V7239 SOFT LIVE GATE ENFORCEMENT INSTALL ===
try:
    from app.v7239_soft_live_gate_enforcement import install_v7239_soft_live_gate_enforcement
    install_v7239_soft_live_gate_enforcement(app)
except Exception as exc:
    print("[V7239] Soft Live Gate Enforcement install failed:", exc)
# === END V7239 SOFT LIVE GATE ENFORCEMENT INSTALL ===

# === V7241 MASTER PINE EVALUATOR INSTALL ===
try:
    from app.v7241_master_pine_evaluator import install_v7241_master_pine_evaluator
    install_v7241_master_pine_evaluator(app)
except Exception as exc:
    print("[V7241] Master Pine Evaluator install failed:", exc)
# === END V7241 MASTER PINE EVALUATOR INSTALL ===

# === V7242 PINE RUNTIME INTEL INSTALL ===
try:
    from app.v7242_pine_runtime_intel import install_v7242_pine_runtime_intel
    install_v7242_pine_runtime_intel(app)
except Exception as exc:
    print("[V7242] Pine Runtime Intel install failed:", exc)
# === END V7242 PINE RUNTIME INTEL INSTALL ===

# === V7242.5E MASTER CLEAN PAGE INSTALL ===
try:
    from app.v72425e_master_clean_page import install_v72425e_master_clean_page
    install_v72425e_master_clean_page(app)
except Exception as exc:
    print("[V72425E_MASTER_CLEAN] install failed:", exc)
# === END V7242.5E MASTER CLEAN PAGE INSTALL ===



# === V7242.5E V700 MASTER WIDGET ===
def _v72425e_master_widget_html(_token):
    try:
        import sqlite3
        import json
        from pathlib import Path

        try:
            db = _v7000_master_db_path()
        except Exception:
            db = Path("/app/data/v7000_learning.sqlite3")
            if not db.exists():
                db = Path("/opt/tradingbot_v6000/data/v7000_learning.sqlite3")

        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row

        def table_exists(name):
            try:
                return con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                    (name,),
                ).fetchone() is not None
            except Exception:
                return False

        rows = []
        total_rows = 0
        if table_exists("v7242_pine_runtime_log"):
            total_rows = int(con.execute("SELECT COUNT(*) FROM v7242_pine_runtime_log").fetchone()[0])
            rows = [dict(r) for r in con.execute("""
                SELECT id, created_at, market, direction, setup_name,
                       v7242_score, v7242_action, v7242_effect,
                       final_gate_action, reasons_json
                FROM v7242_pine_runtime_log
                ORDER BY id DESC
                LIMIT 8
            """).fetchall()]
        con.close()

        redirect_rows = [r for r in rows if str(r.get("v7242_effect")) == "REDIRECT_TO_SHADOW"]
        live_rows = [r for r in rows if str(r.get("v7242_action")) == "LIVE_CANDIDATE"]

        try:
            e = _v7000_master_q
        except Exception:
            import html
            def e(x):
                return html.escape("" if x is None else str(x))

        latest = ""
        for r in rows[:6]:
            reason = ""
            try:
                rr = json.loads(r.get("reasons_json") or "[]")
                if isinstance(rr, list) and rr:
                    reason = rr[0]
            except Exception:
                pass

            latest += f"""
            <div class="openitem">
              <b>{e(r.get('market'))} {e(r.get('direction'))} · {e(r.get('v7242_effect'))}</b>
              <span>{e(r.get('setup_name'))} · Gate {e(r.get('final_gate_action'))} · Score {e(r.get('v7242_score'))}</span>
              <small>{e(reason) if reason else 'NO_CHANGE / kein Redirect'}</small>
            </div>
            """

        if not latest:
            latest = '<div class="openitem"><b>Noch keine V7242.5E Logs</b><span>Wartet auf neue Signale.</span></div>'

        return f"""
        <div class="openbox">
          <h2>V7242.5E Runtime · Shadow Redirect</h2>
          <div class="stats" style="grid-template-columns:repeat(4,1fr);margin-bottom:10px">
            <div class="stat"><div class="label">Runtime Logs</div><div class="value pos">{e(total_rows)}</div></div>
            <div class="stat"><div class="label">Redirects</div><div class="value warntext">{e(len(redirect_rows))}</div></div>
            <div class="stat"><div class="label">Live OK</div><div class="value pos">{e(len(live_rows))}</div></div>
            <div class="stat"><div class="label">Status</div><div class="value pos">AKTIV</div></div>
          </div>
          <div class="sub" style="margin-bottom:8px">
            Duplicate / Trend-Flip / Countertrend wird nicht gelöscht, sondern live verhindert und als Shadow weitergeführt.
            <a href="/master-clean?token={e(_token)}" style="color:#93c5fd">Master Clean öffnen</a>
          </div>
          {latest}
        </div>
        """
    except Exception as exc:
        try:
            return f'<div class="alert bad"><b>V7242.5E Widget Fehler</b><br>{_v7000_master_q(exc)}</div>'
        except Exception:
            return '<div class="alert bad"><b>V7242.5E Widget Fehler</b></div>'
# === END V7242.5E V700 MASTER WIDGET ===


# === V7243 FREE CALENDAR INSTALL ===
try:
    from app.v7243_free_calendar import install_v7243_free_calendar
    install_v7243_free_calendar(app)
except Exception as exc:
    print("[V7243_FREE_CALENDAR] install failed:", exc)
# === END V7243 FREE CALENDAR INSTALL ===


# === V7243 CALENDAR WIDGET IMPORT ===
try:
    from app.v7243_free_calendar import _compact_calendar_widget
except Exception as exc:
    print("[V7243_CALENDAR_WIDGET] import failed:", exc)
    def _compact_calendar_widget(token=""):
        return ""
# === END V7243 CALENDAR WIDGET IMPORT ===


# === V7243.3 FINANCIALJUICE BRIDGE INSTALL ===
try:
    from app.v7243_3_fj_bridge import install_v7243_3_financialjuice_bridge
    install_v7243_3_financialjuice_bridge(app)
except Exception as exc:
    print("[V7243_3_FJ_BRIDGE] install failed:", exc)
# === END V7243.3 FINANCIALJUICE BRIDGE INSTALL ===


# === V7243.3 FJ WIDGET IMPORT ===
try:
    from app.v7243_3_fj_bridge import _compact_fj_widget
except Exception as exc:
    print("[V7243_3_FJ_WIDGET] import failed:", exc)
    def _compact_fj_widget(token=""):
        return ""
# === END V7243.3 FJ WIDGET IMPORT ===


# === V7243.4 FJ NEWS IMPACT INSTALL ===
try:
    from app.v7243_4_fj_news_impact import install_v7243_4_fj_news_impact
    install_v7243_4_fj_news_impact(app)
except Exception as exc:
    print("[V7243_4_FJ_NEWS_IMPACT] install failed:", exc)
# === END V7243.4 FJ NEWS IMPACT INSTALL ===


# === V7243.4 FJ IMPACT WIDGET IMPORT ===
try:
    from app.v7243_4_fj_news_impact import _compact_fj_impact_widget
except Exception as exc:
    print("[V7243_4_FJ_IMPACT_WIDGET] import failed:", exc)
    def _compact_fj_impact_widget(token=""):
        return ""
# === END V7243.4 FJ IMPACT WIDGET IMPORT ===



# === V7244 UNIFIED MASTER INSTALL DISABLED BY V7244.1 ===
# Altes schönes V7000 Master Board bleibt aktiv:
# Live-R Monitor, Cluster Board, Shadow Board, Outcome Board, Navigation, V7100 AI Boards.
# V7243 Calendar / FinancialJuice / Impact bleiben als Detailseiten aktiv.
print("[V7244.1] V7244 unified override disabled; legacy V7000 /master preserved.")
# === END V7244 UNIFIED MASTER INSTALL DISABLED BY V7244.1 ===




# === V7244.1 MASTER-CLEAN REDIRECT ONLY ===
try:
    from fastapi import Request
    from fastapi.responses import RedirectResponse

    # Nur master-clean entfernen/ersetzen. /master und /master.json bleiben original V7000.
    _v72441_keep_routes = []
    for _r in list(app.router.routes):
        _p = getattr(_r, "path", "")
        if _p in {"/master-clean", "/master-clean.json"}:
            continue
        _v72441_keep_routes.append(_r)
    app.router.routes[:] = _v72441_keep_routes

    @app.api_route("/master-clean", methods=["GET", "HEAD"])
    def v72441_master_clean_redirect(request: Request):
        token = request.query_params.get("token", "")
        url = "/master" + (f"?token={token}" if token else "")
        return RedirectResponse(url=url, status_code=307)

    @app.api_route("/master-clean.json", methods=["GET", "HEAD"])
    def v72441_master_clean_json_redirect(request: Request):
        token = request.query_params.get("token", "")
        url = "/master.json" + (f"?token={token}" if token else "")
        return RedirectResponse(url=url, status_code=307)

    print("[V7244.1] master-clean redirects installed. /master remains legacy V7000 board.")
except Exception as exc:
    print("[V7244.1] master-clean redirect install failed:", exc)
# === END V7244.1 MASTER-CLEAN REDIRECT ONLY ===


# === V7243.5 CALENDAR FJ CLUSTER DECAY INSTALL ===
try:
    from app.v7243_5_calendar_fj_cluster_decay import install_v7243_5_calendar_fj_cluster_decay
    install_v7243_5_calendar_fj_cluster_decay(app)
except Exception as exc:
    print("[V7243_5_CALENDAR_FJ_CLUSTER_DECAY] install failed:", exc)
# === END V7243.5 CALENDAR FJ CLUSTER DECAY INSTALL ===


# === V7243.6 MACRO ACTUAL EXTRACTOR INSTALL ===
try:
    from app.v7243_6_macro_actual_extractor import install_v7243_6_macro_actual_extractor
    install_v7243_6_macro_actual_extractor(app)
except Exception as exc:
    print("[V7243_6_MACRO_ACTUAL_EXTRACTOR] install failed:", exc)
# === END V7243.6 MACRO ACTUAL EXTRACTOR INSTALL ===


# === V7243.7 SAFE LIVE PERMISSION ROUTES INSTALL ===
try:
    from app.v7243_7_safe_live_gate import install_v7243_7_safe_live_permission_gate
    install_v7243_7_safe_live_permission_gate(app)
except Exception as exc:
    print('[V7243_7_SAFE_LIVE_PERMISSION_ROUTES] install failed:', exc)
# === END V7243.7 SAFE LIVE PERMISSION ROUTES INSTALL ===












# === V7243.8C ULTRA FAST MASTER OVERRIDE ===
try:
    from fastapi import Request
    from fastapi.responses import HTMLResponse

    def _v72438c_page(request: Request):
        token = request.query_params.get("token", "")
        html = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradingBot Master Fast</title>
<style>
body{margin:0;background:#07111e;color:#eef6ff;font-family:Arial,sans-serif}
.wrap{padding:14px;max-width:900px;margin:0 auto}
h1{font-size:24px;margin:10px 0}
.sub{color:#9caec4;font-size:13px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:9px}
.card,.btn{background:#101d2e;border:1px solid #273a52;border-radius:12px;padding:12px}
.label{font-size:11px;color:#9caec4;text-transform:uppercase}
.value{font-size:20px;font-weight:800;margin-top:4px}
.nav{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:12px 0}
a.btn{display:block;color:#7dd3fc;text-decoration:none;font-weight:700}
.ok{color:#22c55e}.warn{color:#f59e0b}
</style>
</head>
<body>
<div class="wrap">
<h1>TradingBot Master Fast</h1>
<div class="sub">Ultra-schnelle Handy-Startseite. Keine schweren DB-Abfragen beim Laden.</div>

<div class="grid">
  <div class="card"><div class="label">Server</div><div class="value ok">Online</div></div>
  <div class="card"><div class="label">Master</div><div class="value warn">Fast Mode</div></div>
  <div class="card"><div class="label">V7243.7</div><div class="value">Safe Gate</div></div>
  <div class="card"><div class="label">Calendar</div><div class="value">FJ Actuals</div></div>
</div>

<div class="nav">
<a class="btn" href="/live-permission-v7243?token=TOKEN_PLACEHOLDER">Live Permission</a>
<a class="btn" href="/calendar?token=TOKEN_PLACEHOLDER">Calendar</a>
<a class="btn" href="/macro-actual-v7243?token=TOKEN_PLACEHOLDER">Macro Actual</a>
<a class="btn" href="/financialjuice-impact-v7243?token=TOKEN_PLACEHOLDER">FJ Impact</a>
<a class="btn" href="/pine-runtime?token=TOKEN_PLACEHOLDER">Pine Runtime</a>
<a class="btn" href="/master.json?token=TOKEN_PLACEHOLDER">Master JSON</a>
<a class="btn" href="/soft-live-gate.json?token=TOKEN_PLACEHOLDER">Soft Gate JSON</a>
<a class="btn" href="/health">Health</a>
</div>
</div>
</body>
</html>"""
        return html.replace("TOKEN_PLACEHOLDER", token)

    # alle alten /master Fast-Routen raus
    app.router.routes[:] = [
        r for r in app.router.routes
        if getattr(r, "path", "") not in {"/master", "/master-fast", "/master-mobile"}
    ]

    @app.get("/master")
    def v72438c_master(request: Request):
        return HTMLResponse(_v72438c_page(request))

    @app.get("/master-fast")
    def v72438c_master_fast(request: Request):
        return HTMLResponse(_v72438c_page(request))

    @app.get("/master-mobile")
    def v72438c_master_mobile(request: Request):
        return HTMLResponse(_v72438c_page(request))

    # neue Routen ganz nach vorne setzen, damit keine alte Route vorher matched
    front = []
    rest = []
    for r in app.router.routes:
        if getattr(r, "path", "") in {"/master", "/master-fast", "/master-mobile"}:
            front.append(r)
        else:
            rest.append(r)
    app.router.routes[:] = front + rest

    print("[V7243.8C] Ultra fast /master override installed at route front.")
except Exception as exc:
    print("[V7243.8C] install failed:", exc)
# === END V7243.8C ULTRA FAST MASTER OVERRIDE ===




# === V7243.8D MASTER MIDDLEWARE BYPASS ===
try:
    from fastapi import Request
    from fastapi.responses import HTMLResponse

    def _v72438d_html(token=""):
        t = str(token or "")
        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradingBot Master Fast</title>
<style>
body{{margin:0;background:#07111e;color:#eef6ff;font-family:Arial,sans-serif}}
.wrap{{padding:14px;max-width:900px;margin:0 auto}}
h1{{font-size:24px;margin:10px 0}}
.sub{{color:#9caec4;font-size:13px;margin-bottom:12px}}
.grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:9px}}
.card,.btn{{background:#101d2e;border:1px solid #273a52;border-radius:12px;padding:12px}}
.label{{font-size:11px;color:#9caec4;text-transform:uppercase}}
.value{{font-size:20px;font-weight:800;margin-top:4px}}
.nav{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:12px 0}}
a.btn{{display:block;color:#7dd3fc;text-decoration:none;font-weight:700}}
.ok{{color:#22c55e}}.warn{{color:#f59e0b}}
</style>
</head>
<body>
<div class="wrap">
<h1>TradingBot Master Fast</h1>
<div class="sub">V7243.8D Middleware Bypass aktiv. Diese Seite lädt ohne schwere Master-Abfragen.</div>

<div class="grid">
  <div class="card"><div class="label">Server</div><div class="value ok">Online</div></div>
  <div class="card"><div class="label">Master</div><div class="value warn">Fast</div></div>
  <div class="card"><div class="label">Live Gate</div><div class="value">V7243.7</div></div>
  <div class="card"><div class="label">Calendar</div><div class="value">FJ Actuals</div></div>
</div>

<div class="nav">
<a class="btn" href="/live-permission-v7243?token={t}">Live Permission</a>
<a class="btn" href="/calendar?token={t}">Calendar</a>
<a class="btn" href="/macro-actual-v7243?token={t}">Macro Actual</a>
<a class="btn" href="/financialjuice-impact-v7243?token={t}">FJ Impact</a>
<a class="btn" href="/pine-runtime?token={t}">Pine Runtime</a>
<a class="btn" href="/master.json?token={t}">Master JSON</a>
<a class="btn" href="/soft-live-gate.json?token={t}">Soft Gate JSON</a>
<a class="btn" href="/health">Health</a>
</div>
</div>
</body>
</html>"""

    @app.middleware("http")
    async def v72438d_master_bypass(request: Request, call_next):
        if request.url.path in {"/master", "/master-fast", "/master-mobile"}:
            return HTMLResponse(_v72438d_html(request.query_params.get("token", "")))
        return await call_next(request)

    print("[V7243.8D] Master middleware bypass installed.")
except Exception as exc:
    print("[V7243.8D] install failed:", exc)
# === END V7243.8D MASTER MIDDLEWARE BYPASS ===




# === V7243.8E MASTER POSITIONS VIEW ===
try:
    import sqlite3, json, html
    from pathlib import Path as _Path
    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse

    def _v72438e_db():
        for _p in [_Path("/app/data/v7000_learning.sqlite3"), _Path("/opt/tradingbot_v6000/data/v7000_learning.sqlite3")]:
            if _p.exists():
                return str(_p)
        return "/opt/tradingbot_v6000/data/v7000_learning.sqlite3"

    def _v72438e_num(x):
        try:
            return float(x)
        except Exception:
            return None

    def _v72438e_price(con, market):
        try:
            r = con.execute("""
                SELECT close, received_at
                FROM price_heartbeats
                WHERE UPPER(market)=UPPER(?)
                ORDER BY received_at DESC
                LIMIT 1
            """, (market,)).fetchone()
            if not r:
                return None, ""
            return _v72438e_num(r["close"]), r["received_at"]
        except Exception:
            return None, ""

    def _v72438e_calc(row, px):
        entry = _v72438e_num(row.get("entry"))
        sl = _v72438e_num(row.get("sl"))
        tp = _v72438e_num(row.get("tp1"))
        direction = str(row.get("direction") or "").upper()

        pnl_points = None
        r_now = None
        status_hint = "NO_PRICE"

        if px is not None and entry is not None:
            if direction == "LONG":
                pnl_points = px - entry
                risk = entry - sl if sl is not None else None
                if tp is not None and px >= tp:
                    status_hint = "TP_HIT_NOW"
                elif sl is not None and px <= sl:
                    status_hint = "SL_HIT_NOW"
                else:
                    status_hint = "RUNNING"
            elif direction == "SHORT":
                pnl_points = entry - px
                risk = sl - entry if sl is not None else None
                if tp is not None and px <= tp:
                    status_hint = "TP_HIT_NOW"
                elif sl is not None and px >= sl:
                    status_hint = "SL_HIT_NOW"
                else:
                    status_hint = "RUNNING"
            else:
                risk = None

            if risk and risk > 0:
                r_now = pnl_points / risk

        return {
            "current_price": px,
            "pnl_points": round(pnl_points, 4) if pnl_points is not None else None,
            "r_now": round(r_now, 3) if r_now is not None else None,
            "status_hint": status_hint,
        }

    def _v72438e_rows(con, table, kind):
        rows = []
        try:
            if table == "open_trades":
                data = con.execute("""
                    SELECT market, direction, setup_name, entry, sl, tp1, status, opened_at
                    FROM open_trades
                    WHERE UPPER(COALESCE(status,''))='OPEN'
                    ORDER BY opened_at DESC
                    LIMIT 50
                """).fetchall()
            else:
                data = con.execute("""
                    SELECT shadow_id, market, direction, setup_name, entry, sl, tp1, status, opened_at
                    FROM shadow_trades
                    WHERE UPPER(COALESCE(status,''))='OPEN'
                    ORDER BY opened_at DESC
                    LIMIT 80
                """).fetchall()

            for r in data:
                d = dict(r)
                px, px_time = _v72438e_price(con, d.get("market"))
                calc = _v72438e_calc(d, px)
                d.update(calc)
                d["price_time"] = px_time
                d["kind"] = kind
                rows.append(d)
        except Exception as exc:
            rows.append({"kind": kind, "error": str(exc)})
        return rows

    def _v72438e_summary():
        con = sqlite3.connect(_v72438e_db(), timeout=8)
        con.row_factory = sqlite3.Row
        try:
            live = _v72438e_rows(con, "open_trades", "LIVE")
            shadow = _v72438e_rows(con, "shadow_trades", "SHADOW")

            def total_r(items):
                vals = [x.get("r_now") for x in items if isinstance(x.get("r_now"), (int,float))]
                return round(sum(vals), 3), len(vals)

            live_r, live_n = total_r(live)
            shadow_r, shadow_n = total_r(shadow)

            return {
                "version": "V7243.8E-MASTER-POSITIONS-VIEW",
                "open_live_count": len([x for x in live if not x.get("error")]),
                "open_shadow_count": len([x for x in shadow if not x.get("error")]),
                "live_total_r": live_r,
                "shadow_total_r": shadow_r,
                "live_r_count": live_n,
                "shadow_r_count": shadow_n,
                "live": live,
                "shadow": shadow,
            }
        finally:
            con.close()

    @app.get("/master-fast-state.json")
    def v72438e_master_fast_state():
        return JSONResponse(_v72438e_summary())

    @app.get("/positions-state.json")
    def v72438e_positions_state():
        return JSONResponse(_v72438e_summary())

    def _v72438e_page(token=""):
        t = html.escape(str(token or ""))
        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradingBot Master Positions</title>
<style>
body{{margin:0;background:#07111e;color:#eef6ff;font-family:Arial,sans-serif}}
.wrap{{padding:14px;max-width:1100px;margin:0 auto}}
h1{{font-size:24px;margin:10px 0}}
h2{{font-size:18px;margin:0 0 8px}}
.sub{{color:#9caec4;font-size:13px;margin-bottom:12px}}
.grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:9px}}
.card,.section,.btn{{background:#101d2e;border:1px solid #273a52;border-radius:12px;padding:12px}}
.label{{font-size:11px;color:#9caec4;text-transform:uppercase}}
.value{{font-size:21px;font-weight:800;margin-top:4px}}
.nav{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:12px 0}}
a.btn{{display:block;color:#7dd3fc;text-decoration:none;font-weight:700}}
.section{{margin-top:12px;overflow:auto}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{border-bottom:1px solid #273a52;padding:7px;text-align:left;white-space:nowrap}}
th{{color:#a5d8ff}}
.pos{{color:#22c55e;font-weight:700}}
.neg{{color:#ef4444;font-weight:700}}
.warn{{color:#f59e0b;font-weight:700}}
.muted{{color:#9caec4}}
</style>
</head>
<body>
<div class="wrap">
<h1>TradingBot Master Positions</h1>
<div class="sub">Live + Shadow offene Trades mit aktuellem Preis, Punkte +/− und R +/−.</div>

<div class="grid">
  <div class="card"><div class="label">Open Live</div><div id="liveCount" class="value">lade...</div></div>
  <div class="card"><div class="label">Live Total R</div><div id="liveR" class="value">lade...</div></div>
  <div class="card"><div class="label">Open Shadow</div><div id="shadowCount" class="value">lade...</div></div>
  <div class="card"><div class="label">Shadow Total R</div><div id="shadowR" class="value">lade...</div></div>
</div>

<div class="nav">
<a class="btn" href="/master?token={t}">Refresh</a>
<a class="btn" href="/calendar?token={t}">Calendar</a>
<a class="btn" href="/live-permission-v7243?token={t}">Live Permission</a>
<a class="btn" href="/macro-actual-v7243?token={t}">Macro Actual</a>
<a class="btn" href="/financialjuice-impact-v7243?token={t}">FJ Impact</a>
<a class="btn" href="/pine-runtime?token={t}">Pine Runtime</a>
</div>

<div class="section">
<h2>Open Live Trades</h2>
<table>
<thead><tr><th>Market</th><th>Dir</th><th>Setup</th><th>Entry</th><th>Price</th><th>Punkte</th><th>R</th><th>SL</th><th>TP</th><th>Status</th></tr></thead>
<tbody id="liveRows"><tr><td colspan="10">lade...</td></tr></tbody>
</table>
</div>

<div class="section">
<h2>Open Shadow Trades</h2>
<table>
<thead><tr><th>Market</th><th>Dir</th><th>Setup</th><th>Entry</th><th>Price</th><th>Punkte</th><th>R</th><th>SL</th><th>TP</th><th>Status</th></tr></thead>
<tbody id="shadowRows"><tr><td colspan="10">lade...</td></tr></tbody>
</table>
</div>

<div class="sub" id="updated"></div>
</div>

<script>
function cls(v){{
  if(v === null || v === undefined) return "";
  if(Number(v) > 0) return "pos";
  if(Number(v) < 0) return "neg";
  return "warn";
}}
function fmt(v){{
  if(v === null || v === undefined) return "-";
  if(typeof v === "number") return Number(v).toFixed(3).replace(/\\.000$/,"");
  return v;
}}
function row(x){{
  let pcls = cls(x.pnl_points);
  let rcls = cls(x.r_now);
  return `<tr>
    <td>${{x.market||"-"}}</td>
    <td>${{x.direction||"-"}}</td>
    <td>${{x.setup_name||"-"}}</td>
    <td>${{fmt(x.entry)}}</td>
    <td>${{fmt(x.current_price)}}</td>
    <td class="${{pcls}}">${{fmt(x.pnl_points)}}</td>
    <td class="${{rcls}}">${{fmt(x.r_now)}}</td>
    <td>${{fmt(x.sl)}}</td>
    <td>${{fmt(x.tp1)}}</td>
    <td>${{x.status_hint||x.status||"-"}}</td>
  </tr>`;
}}
async function load(){{
  try {{
    const r = await fetch("/master-fast-state.json?token={t}&ts="+Date.now(), {{cache:"no-store"}});
    const d = await r.json();

    liveCount.textContent = d.open_live_count;
    shadowCount.textContent = d.open_shadow_count;
    liveR.textContent = fmt(d.live_total_r);
    shadowR.textContent = fmt(d.shadow_total_r);

    liveR.className = "value " + cls(d.live_total_r);
    shadowR.className = "value " + cls(d.shadow_total_r);

    liveRows.innerHTML = d.live.length ? d.live.map(row).join("") : '<tr><td colspan="10">Keine offenen Live-Trades.</td></tr>';
    shadowRows.innerHTML = d.shadow.length ? d.shadow.map(row).join("") : '<tr><td colspan="10">Keine offenen Shadow-Trades.</td></tr>';

    updated.textContent = "Letztes Update: " + new Date().toLocaleTimeString();
  }} catch(e) {{
    liveRows.innerHTML = '<tr><td colspan="10">Fehler: '+e+'</td></tr>';
    shadowRows.innerHTML = '<tr><td colspan="10">Fehler: '+e+'</td></tr>';
  }}
}}
load();
setInterval(load, 15000);
</script>
</body>
</html>"""

    @app.middleware("http")
    async def v72438e_master_positions_middleware(request: Request, call_next):
        if request.url.path in {"/master", "/master-fast", "/master-mobile"}:
            return HTMLResponse(_v72438e_page(request.query_params.get("token", "")))
        return await call_next(request)

    print("[V7243.8E] Master positions view installed.")
except Exception as exc:
    print("[V7243.8E] install failed:", exc)
# === END V7243.8E MASTER POSITIONS VIEW ===




# === V7243.8F V7000 MASTER CARDS VIEW ===
try:
    from fastapi import Request
    from fastapi.responses import HTMLResponse

    def _v72438f_page(token=""):
        t = str(token or "")
        html = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>V7000 Master</title>
<style>
body{margin:0;background:#07111e;color:#eef6ff;font-family:Arial,sans-serif}
.wrap{padding:14px;max-width:980px;margin:0 auto}
.hero{display:flex;gap:14px;align-items:center;margin:12px 0 18px}
.rocket{font-size:46px}
h1{font-size:34px;margin:0}
.sub{color:#9caec4;font-size:16px;line-height:1.35}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:12px 0}
.stat,.card,.section{background:#101d2e;border:1px solid #273a52;border-radius:16px;padding:14px}
.label{font-size:12px;color:#9caec4;text-transform:uppercase}
.value{font-size:25px;font-weight:900;margin-top:5px}
.pos{color:#22c55e}.neg{color:#ef4444}.warn{color:#f59e0b}
.card{display:flex;align-items:center;gap:14px;text-decoration:none;color:#eef6ff;min-height:86px}
.icon{font-size:34px;width:56px;height:56px;display:flex;align-items:center;justify-content:center;background:#0b1728;border:1px solid #24364c;border-radius:14px}
.card h2{font-size:24px;margin:0 0 5px}
.card p{margin:0;color:#9caec4;font-size:15px;line-height:1.35}
.arrow{margin-left:auto;color:#718096;font-size:30px}
.section{margin:14px 0;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{border-bottom:1px solid #273a52;padding:7px;text-align:left;white-space:nowrap}
th{color:#a5d8ff}
@media(max-width:700px){.grid{grid-template-columns:repeat(2,1fr)}h1{font-size:30px}.card h2{font-size:22px}.wrap{padding:12px}}
</style>
</head>
<body>
<div class="wrap">

<div class="hero">
  <div class="rocket">🚀</div>
  <div>
    <h1>V7000 Master</h1>
    <div class="sub">Zentrale Bot-Steuerung · Auto-Refresh 15s · Handy & Desktop</div>
  </div>
</div>

<div class="grid">
  <div class="stat"><div class="label">Open Live</div><div id="liveCount" class="value">...</div></div>
  <div class="stat"><div class="label">Live Total R</div><div id="liveR" class="value">...</div></div>
  <div class="stat"><div class="label">Open Shadow</div><div id="shadowCount" class="value">...</div></div>
  <div class="stat"><div class="label">Shadow Total R</div><div id="shadowR" class="value">...</div></div>
</div>

<div class="section">
<h2>📈 Live-R Monitor</h2>
<div class="sub">Aktueller Stand offener Live-Trades: Preis, Punkte, R, TP/SL.</div>
<table>
<thead><tr><th>Market</th><th>Dir</th><th>Setup</th><th>Entry</th><th>Price</th><th>Punkte</th><th>R</th><th>Status</th></tr></thead>
<tbody id="liveRows"><tr><td colspan="8">lade...</td></tr></tbody>
</table>
</div>

<a class="card" href="/dashboard?token=TOKEN_PLACEHOLDER">
  <div class="icon">📊</div><div><h2>Dashboard</h2><p>Hauptübersicht mit Märkten, offenen Trades und Status.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/learning?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧠</div><div><h2>Learning</h2><p>Outcomes, Winrate, R-Multiple und Learning-Gruppen.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/heartbeat?token=TOKEN_PLACEHOLDER">
  <div class="icon">💗</div><div><h2>Heartbeats</h2><p>Welche Märkte senden aktuelle 1m-Preise.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/decisions?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧾</div><div><h2>Decisions</h2><p>Mobile ALLOW/BLOCK Kartenansicht.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/shadow-detail?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧪</div><div><h2>Shadow Trades</h2><p>Geblockte Near-Miss Signale als Paper-Trade.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/manual?token=TOKEN_PLACEHOLDER">
  <div class="icon">🛠️</div><div><h2>Manual Close</h2><p>Offene Trades manuell als WIN, LOSS oder BE schließen.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/intelligence?token=TOKEN_PLACEHOLDER">
  <div class="icon">🛰️</div><div><h2>Intelligence</h2><p>News, Bias und Marktintelligenz.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/calendar?token=TOKEN_PLACEHOLDER">
  <div class="icon">📅</div><div><h2>Calendar</h2><p>Wirtschaftskalender, Actuals und Event-Risk.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/live-permission-v7243?token=TOKEN_PLACEHOLDER">
  <div class="icon">🛡️</div><div><h2>Live Permission</h2><p>V7243.7 Safe Live Gate mit News-Relevanz.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/master-fast-state.json?token=TOKEN_PLACEHOLDER">
  <div class="icon">🔗</div><div><h2>Master State JSON</h2><p>Live + Shadow Positionsdaten als JSON.</p></div><div class="arrow">›</div>
</a>

<div class="section">
<h2>🧪 Shadow Trades</h2>
<div class="sub">Letzte offene Shadow-Trades mit aktuellem R.</div>
<table>
<thead><tr><th>Market</th><th>Dir</th><th>Setup</th><th>Entry</th><th>Price</th><th>Punkte</th><th>R</th><th>Status</th></tr></thead>
<tbody id="shadowRows"><tr><td colspan="8">lade...</td></tr></tbody>
</table>
</div>

</div>

<script>
function cls(v){
  if(v===null || v===undefined) return "";
  if(Number(v)>0) return "pos";
  if(Number(v)<0) return "neg";
  return "warn";
}
function fmt(v){
  if(v===null || v===undefined) return "-";
  if(typeof v==="number") return Number(v).toFixed(3).replace(/\\.000$/,"");
  return v;
}
function row(x){
  return `<tr>
  <td>${x.market||"-"}</td>
  <td>${x.direction||"-"}</td>
  <td>${x.setup_name||"-"}</td>
  <td>${fmt(x.entry)}</td>
  <td>${fmt(x.current_price)}</td>
  <td class="${cls(x.pnl_points)}">${fmt(x.pnl_points)}</td>
  <td class="${cls(x.r_now)}">${fmt(x.r_now)}</td>
  <td>${x.status_hint||x.status||"-"}</td>
  </tr>`;
}
async function load(){
  try{
    const r=await fetch("/master-fast-state.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
    const d=await r.json();

    liveCount.textContent=d.open_live_count;
    shadowCount.textContent=d.open_shadow_count;
    liveR.textContent=fmt(d.live_total_r);
    shadowR.textContent=fmt(d.shadow_total_r);

    liveR.className="value "+cls(d.live_total_r);
    shadowR.className="value "+cls(d.shadow_total_r);

    liveRows.innerHTML=(d.live||[]).length ? d.live.map(row).join("") : '<tr><td colspan="8">Keine offenen Live-Trades.</td></tr>';
    shadowRows.innerHTML=(d.shadow||[]).slice(0,25).map(row).join("") || '<tr><td colspan="8">Keine offenen Shadow-Trades.</td></tr>';
  }catch(e){
    liveRows.innerHTML='<tr><td colspan="8">Fehler: '+e+'</td></tr>';
  }
}
load();
setInterval(load,15000);

async function loadStability(){
  try{
    const r=await fetch("/stability-v7243.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
    const d=await r.json();
    const el=document.getElementById("stabilityStatus");
    if(!el) return;
    const o=(d.summary&&d.summary.overall)||"UNKNOWN";
    const pass=(d.summary&&d.summary.pass)||0;
    const warn=(d.summary&&d.summary.warn)||0;
    const fail=(d.summary&&d.summary.fail)||0;
    el.textContent="Overall: "+o+" · PASS "+pass+" / WARN "+warn+" / FAIL "+fail;
    el.style.color = o==="PASS" ? "#22c55e" : (o==="WARN" ? "#facc15" : "#ef4444");
  }catch(e){
    const el=document.getElementById("stabilityStatus");
    if(el){el.textContent="Stability Fehler"; el.style.color="#ef4444";}
  }
}
loadStability();
setInterval(loadStability,30000);

</script>
</body>
</html>"""
        return html.replace("TOKEN_PLACEHOLDER", t)

    @app.middleware("http")
    async def v72438f_v7000_master_cards_middleware(request: Request, call_next):
        if request.url.path in {"/master", "/v7000-master", "/master-v7000"}:
            return HTMLResponse(_v72438f_page(request.query_params.get("token", "")))
        return await call_next(request)

    print("[V7243.8F] V7000 master cards view installed.")
except Exception as exc:
    print("[V7243.8F] install failed:", exc)
# === END V7243.8F V7000 MASTER CARDS VIEW ===




# === V7243.8G TODAY UPDATE CARDS ===
try:
    from fastapi import Request
    from fastapi.responses import HTMLResponse

    def _v72438g_page(token=""):
        t = str(token or "")
        html = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>V7000 Master</title>
<style>
body{margin:0;background:#07111e;color:#eef6ff;font-family:Arial,sans-serif}
.wrap{padding:14px;max-width:980px;margin:0 auto}
.hero{display:flex;gap:14px;align-items:center;margin:12px 0 18px}
.rocket{font-size:46px}
h1{font-size:34px;margin:0}
h2{font-size:23px;margin:18px 0 10px}
.sub{color:#9caec4;font-size:16px;line-height:1.35}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:12px 0}
.stat,.card,.section{background:#101d2e;border:1px solid #273a52;border-radius:16px;padding:14px}
.label{font-size:12px;color:#9caec4;text-transform:uppercase}
.value{font-size:25px;font-weight:900;margin-top:5px}
.pos{color:#22c55e}.neg{color:#ef4444}.warn{color:#f59e0b}
.card{display:flex;align-items:center;gap:14px;text-decoration:none;color:#eef6ff;min-height:86px;margin:10px 0}
.icon{font-size:34px;width:56px;height:56px;display:flex;align-items:center;justify-content:center;background:#0b1728;border:1px solid #24364c;border-radius:14px}
.card h3{font-size:23px;margin:0 0 5px}
.card p{margin:0;color:#9caec4;font-size:15px;line-height:1.35}
.arrow{margin-left:auto;color:#718096;font-size:30px}
.section{margin:14px 0;overflow:auto}
.badge{display:inline-block;border-radius:999px;padding:4px 10px;background:#12301f;color:#86efac;font-weight:800;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{border-bottom:1px solid #273a52;padding:7px;text-align:left;white-space:nowrap}
th{color:#a5d8ff}
.small{font-size:12px;color:#9caec4}
@media(max-width:700px){.grid{grid-template-columns:repeat(2,1fr)}h1{font-size:30px}.card h3{font-size:22px}.wrap{padding:12px}}
</style>
</head>
<body>
<div class="wrap">

<div class="hero">
  <div class="rocket">🚀</div>
  <div>
    <h1>V7000 Master</h1>
    <div class="sub">Zentrale Bot-Steuerung · Auto-Refresh 15s · Handy & Desktop</div>
  </div>
</div>

<div class="section">
  <span class="badge">V7243.8G AKTIV</span>
  <div class="sub" style="margin-top:8px">
    Heute ergänzt: FinancialJuice Actuals, Macro Extractor, Safe Live Permission, Positions View und Master Fast.
  </div>
</div>

<div class="grid">
  <div class="stat"><div class="label">Open Live</div><div id="liveCount" class="value">...</div></div>
  <div class="stat"><div class="label">Live Total R</div><div id="liveR" class="value">...</div></div>
  <div class="stat"><div class="label">Open Shadow</div><div id="shadowCount" class="value">...</div></div>
  <div class="stat"><div class="label">Shadow Total R</div><div id="shadowR" class="value">...</div></div>
</div>

<div class="section">
<h2>📈 Live-R Monitor</h2>
<div class="sub">Aktueller Stand offener Live-Trades: Preis, Punkte, R, TP/SL.</div>
<table>
<thead><tr><th>Market</th><th>Dir</th><th>Setup</th><th>Entry</th><th>Price</th><th>Punkte</th><th>R</th><th>Status</th></tr></thead>
<tbody id="liveRows"><tr><td colspan="8">lade...</td></tr></tbody>
</table>
</div>

<h2>🆕 Heute installierte Updates</h2>

<a class="card" href="/calendar?token=TOKEN_PLACEHOLDER">
  <div class="icon">📅</div><div><h3>V7243.5 Calendar + FJ Cluster</h3><p>Kalender mit FinancialJuice News, Cluster/Decay und Event-Risk.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/macro-actual-v7243?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧾</div><div><h3>V7243.6 Macro Actuals</h3><p>Extrahiert Actual/Forecast/Previous aus FinancialJuice News und schreibt sie in den Kalender.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/live-permission-v7243?token=TOKEN_PLACEHOLDER">
  <div class="icon">🛡️</div><div><h3>V7243.7 Safe Live Permission</h3><p>Live-Erlaubnis mit Confidence, News-Konflikt, offenen Positionen und Shadow/Review.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/master-fast-state.json?token=TOKEN_PLACEHOLDER">
  <div class="icon">📌</div><div><h3>V7243.8E Positions State</h3><p>Live + Shadow offene Trades mit Preis, Punkte +/− und R +/− als JSON.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/financialjuice-v7243?token=TOKEN_PLACEHOLDER">
  <div class="icon">📰</div><div><h3>FinancialJuice Bridge</h3><p>Stream-Status, News, Calendar/Bulk Events und Verbindung.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/financialjuice-impact-v7243?token=TOKEN_PLACEHOLDER">
  <div class="icon">⚡</div><div><h3>FinancialJuice Impact</h3><p>News-Relevanz nach Markt: Öl, Gold, US-Indizes, FX und Risk-Bias.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/pine-runtime?token=TOKEN_PLACEHOLDER">
  <div class="icon">🌲</div><div><h3>Pine Runtime</h3><p>V7242/V7242.5E Runtime Redirects, Shadow-Weiterleitung und Signalprüfung.</p></div><div class="arrow">›</div>
</a>


<a class="card" href="/stability-v7243?token=TOKEN_PLACEHOLDER">
  <div class="icon">🛡️</div><div><h3>V7243.9B Stability Pack</h3><p>Live-Systemcheck: FinancialJuice, Macro, Guards, Safe Live, Pine, Heartbeats, DB.</p><p id="stabilityStatus" style="font-weight:900;margin-top:6px;color:#facc15;font-size:15px;line-height:1.2">Stability lädt...</p></div><div class="arrow">›</div>
</a>

<script>
// === V7243.9C MASTER STABILITY INLINE STATUS FIX ===
(async function(){
  async function updateMasterStability(){
    try{
      var el=document.getElementById("stabilityStatus");
      if(!el) return;
      var r=await fetch("/stability-v7243.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var s=(d.summary&&d.summary.overall)||"UNKNOWN";
      var pass=(d.summary&&d.summary.pass)||0;
      var warn=(d.summary&&d.summary.warn)||0;
      var fail=(d.summary&&d.summary.fail)||0;
      el.textContent=(s==="PASS"?"✅ ":"⚠️ ")+s+" · "+pass+"/"+warn+"/"+fail;
      el.style.color = s==="PASS" ? "#22c55e" : (s==="WARN" ? "#facc15" : "#ef4444");
    }catch(e){
      var el=document.getElementById("stabilityStatus");
      if(el){
        el.textContent="Stability Fehler";
        el.style.color="#ef4444";
      }
    }
  }
  updateMasterStability();
  setInterval(updateMasterStability,30000);
})();
// === END V7243.9C MASTER STABILITY INLINE STATUS FIX ===
</script>


<a class="card" href="/v7244-live-quality?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧠</div><div><h3>V7244A Live Quality</h3><p>Observe-Only: dynamische Live/Review/Shadow Bewertung nach Markt, News, Confidence und offenen Positionen.</p><p id="v7244QualityStatus" style="font-weight:900;margin-top:6px;color:#facc15;font-size:15px;line-height:1.2">V7244 lädt...</p></div><div class="arrow">›</div>
</a>

<script>
// === V7244B MASTER QUALITY COMPACT STATUS ===
(async function(){
  async function updateV7244Quality(){
    try{
      var el=document.getElementById("v7244QualityStatus");
      if(!el) return;
      var r=await fetch("/v7244-live-quality.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var s=d.summary||{};
      var counts=s.counts||{};
      var evaluated=s.evaluated||0;
      var live=s.live_candidates||0;
      var review=s.review_candidates||0;
      var shadow=counts.SHADOW_ONLY_RECOMMENDED||0;
      el.textContent="Observe PASS · E"+evaluated+" · L"+live+" / R"+review+" / S"+shadow;
      el.style.color = live>0 ? "#22c55e" : "#facc15";
    }catch(e){
      var el=document.getElementById("v7244QualityStatus");
      if(el){el.textContent="V7244 Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7244Quality();
  setInterval(updateV7244Quality,30000);
})();
// === END V7244B MASTER QUALITY COMPACT STATUS ===
</script>


<a class="card" href="/v7244-explain?token=TOKEN_PLACEHOLDER">
  <div class="icon">🔎</div><div><h3>V7244C Explain</h3><p>Erklärt pro Signal: V7243.7 vs V7244, fehlende Confidence, News-Konflikt und Live-Anforderungen.</p></div><div class="arrow">›</div>
</a>


<a class="card" href="/v7244d-next-live?token=TOKEN_PLACEHOLDER">
  <div class="icon">🎯</div><div><h3>V7244D Next Live</h3><p>Zeigt, welcher Kandidat am nächsten an Live/Review ist und was noch fehlt.</p><p id="v7244dStatus" style="font-weight:900;margin-top:6px;color:#facc15;font-size:15px;line-height:1.2">Next Live lädt...</p></div><div class="arrow">›</div>
</a>

<script>
// === V7244D MASTER STATUS ===
(async function(){
  async function updateV7244D(){
    try{
      var el=document.getElementById("v7244dStatus");
      if(!el) return;
      var r=await fetch("/v7244d-next-live.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var b=d.best_clean_no_hard_block || d.best_overall || {};
      var m=b.market || "-";
      var dir=b.direction || "";
      var miss=b.missing_conf_to_live;
      el.textContent=m+" "+dir+" · fehlt "+miss;
      el.style.color="#facc15";
    }catch(e){
      var el=document.getElementById("v7244dStatus");
      if(el){el.textContent="V7244D Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7244D();
  setInterval(updateV7244D,30000);
})();
// === END V7244D MASTER STATUS ===
</script>


<a class="card" href="/v7244e-threshold-sim?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧪</div><div><h3>V7244E Simulator</h3><p>Simuliert: was passiert bei lockeren/strengeren Confidence- und News-Schwellen?</p></div><div class="arrow">›</div>
</a>


<a class="card" href="/v7244f-recommended-preset?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧭</div><div><h3>V7244F Preset</h3><p>Empfiehlt SAFE/NORMAL/AGGRESSIVE_CANDIDATE auf Basis von Simulator, News-Risk und Shadow-Learning.</p><p id="v7244fStatus" style="font-weight:900;margin-top:6px;color:#facc15;font-size:15px;line-height:1.2">Preset lädt...</p></div><div class="arrow">›</div>
</a>

<script>
// === V7244F MASTER STATUS ===
(async function(){
  async function updateV7244F(){
    try{
      var el=document.getElementById("v7244fStatus");
      if(!el) return;
      var r=await fetch("/v7244f-recommended-preset.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var m=d.metrics||{};
      var cur=m.current||{};
      el.textContent=d.recommended_preset+" · L"+(cur.LIVE||0)+" / R"+(cur.REVIEW||0)+" / S"+(cur.SHADOW||0);
      el.style.color = d.recommended_preset==="NORMAL" ? "#22c55e" : "#facc15";
    }catch(e){
      var el=document.getElementById("v7244fStatus");
      if(el){el.textContent="V7244F Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7244F();
  setInterval(updateV7244F,30000);
})();
// === END V7244F MASTER STATUS ===
</script>


<a class="card" href="/v7245-excursion?token=TOKEN_PLACEHOLDER">
  <div class="icon">📈</div><div><h3>V7245A Excursion</h3><p>MFE/MAE Recorder: wie viel R Live & Shadow maximal im Plus/Minus waren.</p><p id="v7245aStatus" style="font-weight:900;margin-top:6px;color:#facc15;font-size:15px;line-height:1.2">Excursion lädt...</p></div><div class="arrow">›</div>
</a>

<script>
(async function(){
  async function updateV7245A(){
    try{
      var el=document.getElementById("v7245aStatus");
      if(!el) return;
      var r=await fetch("/v7245-excursion.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var s=d.summary||{};
      el.textContent="Rows "+(s.total||0)+" · 0.5R "+(s.hit_0_5r||0)+" · 1R "+(s.hit_1r||0)+" · 2R "+(s.hit_2r||0);
      el.style.color="#facc15";
    }catch(e){
      var el=document.getElementById("v7245aStatus");
      if(el){el.textContent="V7245A Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7245A();
  setInterval(updateV7245A,30000);
})();
</script>


<a class="card" href="/v7245b-tp-optimizer?token=TOKEN_PLACEHOLDER">
  <div class="icon">🎯</div>
  <div>
    <h3>V7245B TP Optimizer</h3>
    <p>CRV/TP-Auswertung aus MFE/MAE: 0.5R, 1R, 1.5R, 2R, 3R pro Setup.</p>
    <p id="v7245bStatus" style="font-weight:900;margin-top:6px;color:#facc15;font-size:15px;line-height:1.2">TP Optimizer lädt...</p>
  </div>
  <div class="arrow">›</div>
</a>

<script>
(async function(){
  async function updateV7245B(){
    try{
      var el=document.getElementById("v7245bStatus");
      if(!el) return;
      var r=await fetch("/v7245b-tp-optimizer.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var s=d.summary||{};
      el.textContent="Groups "+(s.groups||0)+" · genug "+(s.enough_data_groups||0)+" · Collect "+(s.collect_more_groups||0);
      el.style.color="#facc15";
    }catch(e){
      var el=document.getElementById("v7245bStatus");
      if(el){el.textContent="V7245B Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7245B();
  setInterval(updateV7245B,30000);
})();
</script>


<a class="card" href="/v7245c-recommendations?token=TOKEN_PLACEHOLDER">
  <div class="icon">✅</div>
  <div>
    <h3>V7245C TP Recommendation</h3>
    <p>Automatische TP/CRV-Empfehlung sobald genug MFE/MAE-Daten je Setup vorhanden sind.</p>
    <p id="v7245cStatus" style="font-weight:900;margin-top:6px;color:#facc15;font-size:15px;line-height:1.2">Recommendation lädt...</p>
  </div>
  <div class="arrow">›</div>
</a>

<script>
(async function(){
  async function updateV7245C(){
    try{
      var el=document.getElementById("v7245cStatus");
      if(!el) return;
      var r=await fetch("/v7245c-recommendations.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var s=d.summary||{};
      el.textContent=(d.status||"") + " · Ready " + (s.ready||0) + " · Near " + (s.near_ready||0) + " · Waiting " + (s.waiting||0);
      el.style.color=(s.ready||0)>0 ? "#22c55e" : "#facc15";
    }catch(e){
      var el=document.getElementById("v7245cStatus");
      if(el){el.textContent="V7245C Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7245C();
  setInterval(updateV7245C,30000);
})();
</script>


<a class="card" href="/v7246b-entry-schema?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧠</div>
  <div>
    <h3>V7246B Entry Schema</h3>
    <p>Auswertung nach Einstiegsschema: MSS, BOS, OB, FVG, BPR, Bull/Bear und konkrete Setup-Namen.</p>
    <p id="v7246bStatus" style="font-weight:900;margin-top:6px;color:#38bdf8;font-size:15px;line-height:1.2">Entry Schema lädt...</p>
  </div>
  <div class="arrow">›</div>
</a>

<script>
(async function(){
  async function updateV7246B(){
    try{
      var el=document.getElementById("v7246bStatus");
      if(!el) return;
      var r=await fetch("/v7246b-entry-schema.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var s=d.summary||{};
      el.textContent="Trades " + (s.rows||0) + " · Strong " + (s.strong_setups||0) + " · Risky " + (s.risky_setups||0);
      el.style.color="#38bdf8";
    }catch(e){
      var el=document.getElementById("v7246bStatus");
      if(el){el.textContent="V7246B Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7246B();
  setInterval(updateV7246B,30000);
})();
</script>


<a class="card" href="/v7246c-entry-overlay?token=TOKEN_PLACEHOLDER">
  <div class="icon">🎯</div>
  <div>
    <h3>V7246C Entry Overlay</h3>
    <p>Score Overlay je Setup: Grade, TP-Bias, Upgrade/Downgrade aus Entry-Schema-Daten.</p>
    <p id="v7246cStatus" style="font-weight:900;margin-top:6px;color:#22c55e;font-size:15px;line-height:1.2">Entry Overlay lädt...</p>
  </div>
  <div class="arrow">›</div>
</a>

<script>
(async function(){
  async function updateV7246C(){
    try{
      var el=document.getElementById("v7246cStatus");
      if(!el) return;
      var r=await fetch("/v7246c-entry-overlay.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var s=d.summary||{};
      el.textContent="Upgrade " + (s.strong_overlays||0) + " · Downgrade " + (s.risky_overlays||0) + " · Rows " + (s.source_rows||0);
      el.style.color="#22c55e";
    }catch(e){
      var el=document.getElementById("v7246cStatus");
      if(el){el.textContent="V7246C Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7246C();
  setInterval(updateV7246C,30000);
})();
</script>


<a class="card" href="/v7246d-signal-overlay?token=TOKEN_PLACEHOLDER">
  <div class="icon">🔗</div>
  <div>
    <h3>V7246D Signal Overlay</h3>
    <p>Verbindet echte Pine/Live-Permission-Signale mit Entry Grade, TP Bias und Upgrade/Downgrade.</p>
    <p id="v7246dStatus" style="font-weight:900;margin-top:6px;color:#22c55e;font-size:15px;line-height:1.2">Signal Overlay lädt...</p>
  </div>
  <div class="arrow">›</div>
</a>

<script>
(async function(){
  async function updateV7246D(){
    try{
      var el=document.getElementById("v7246dStatus");
      if(!el) return;
      var r=await fetch("/v7246d-signal-overlay.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var s=d.summary||{};
      el.textContent="Signals " + (s.signals||0) + " · Strong " + (s.upgrade_strong||0) + " · Down " + (s.downgrade||0);
      el.style.color="#22c55e";
    }catch(e){
      var el=document.getElementById("v7246dStatus");
      if(el){el.textContent="V7246D Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7246D();
  setInterval(updateV7246D,30000);
})();
</script>


<a class="card" href="/v7246e-entry-upgrade-sim?token=TOKEN_PLACEHOLDER">
  <div class="icon">🚀</div>
  <div>
    <h3>V7246E Entry Upgrade</h3>
    <p>Simuliert, welche Signale durch Entry-Qualität hoch- oder runtergestuft würden.</p>
    <p id="v7246eStatus" style="font-weight:900;margin-top:6px;color:#22c55e;font-size:15px;line-height:1.2">Entry Upgrade lädt...</p>
  </div>
  <div class="arrow">›</div>
</a>

<script>
(async function(){
  async function updateV7246E(){
    try{
      var el=document.getElementById("v7246eStatus");
      if(!el) return;
      var r=await fetch("/v7246e-entry-upgrade-sim.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var s=d.summary||{};
      el.textContent="Strong " + (s.would_upgrade_strong||0) + " · Up " + (s.would_upgrade||0) + " · Down " + (s.would_downgrade||0);
      el.style.color="#22c55e";
    }catch(e){
      var el=document.getElementById("v7246eStatus");
      if(el){el.textContent="V7246E Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7246E();
  setInterval(updateV7246E,30000);
})();
</script>


<a class="card" href="/v7246f-clean-upgrade-candidates?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧹</div>
  <div>
    <h3>V7246F Clean Candidates</h3>
    <p>Deduplizierte Upgrade-/Downgrade-Kandidaten aus Entry-Overlay-Simulation.</p>
    <p id="v7246fStatus" style="font-weight:900;margin-top:6px;color:#22c55e;font-size:15px;line-height:1.2">Clean Candidates lädt...</p>
  </div>
  <div class="arrow">›</div>
</a>

<script>
(async function(){
  async function updateV7246F(){
    try{
      var el=document.getElementById("v7246fStatus");
      if(!el) return;
      var r=await fetch("/v7246f-clean-upgrade-candidates.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var s=d.summary||{};
      el.textContent="Clean " + (s.clean_signals||0) + " · Review " + (s.potential_review_candidates||0) + " · Live " + (s.potential_live_candidates||0) + " · Down " + (s.downgrade_candidates||0);
      el.style.color="#22c55e";
    }catch(e){
      var el=document.getElementById("v7246fStatus");
      if(el){el.textContent="V7246F Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7246F();
  setInterval(updateV7246F,30000);
})();
</script>


<a class="card" href="/v7246g-entry-guard?token=TOKEN_PLACEHOLDER">
  <div class="icon">🛡️</div>
  <div>
    <h3>V7246G Entry Guard</h3>
    <p>Empfiehlt, ob Entry-Bonus später sicher wäre: +0/+1/+2/+3, Scope und Warnungen.</p>
    <p id="v7246gStatus" style="font-weight:900;margin-top:6px;color:#facc15;font-size:15px;line-height:1.2">Entry Guard lädt...</p>
  </div>
  <div class="arrow">›</div>
</a>

<script>
(async function(){
  async function updateV7246G(){
    try{
      var el=document.getElementById("v7246gStatus");
      if(!el) return;
      var r=await fetch("/v7246g-entry-guard.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var rec=d.recommendation||{};
      var s=d.summary||{};
      el.textContent=(rec.grade||"") + " · Bonus +" + (rec.recommended_bonus||0) + " · Safe " + (rec.safe_to_apply||false) + " · Review " + (s.potential_review_candidates||0);
      el.style.color=(rec.safe_to_apply===true) ? "#22c55e" : "#facc15";
    }catch(e){
      var el=document.getElementById("v7246gStatus");
      if(el){el.textContent="V7246G Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV7246G();
  setInterval(updateV7246G,30000);
})();
</script>


<a class="card" href="/v8000/master?token=TOKEN_PLACEHOLDER">
  <div class="icon">🚀</div>
  <div>
    <h3>V8000 A+ Engine</h3>
    <p>Dual Playbook: HTF OB-Reaction + Session Momentum. Portfolio Manager vor Entry-Signalen.</p>
    <p id="v8000Status" style="font-weight:900;margin-top:6px;color:#22c55e;font-size:15px;line-height:1.2">V8000 lädt...</p>
  </div>
  <div class="arrow">›</div>
</a>

<script>
(async function(){
  async function updateV8000(){
    try{
      var el=document.getElementById("v8000Status");
      if(!el) return;
      var r=await fetch("/v8000/master.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
      var d=await r.json();
      var s=d.summary||{};
      el.textContent="A+ Review " + (s.a_plus_review_candidates||0) + " · Live " + (s.a_plus_live_candidates||0) + " · Guard " + (s.guard_grade||"");
      el.style.color=(s.a_plus_live_candidates||0)>0 ? "#22c55e" : "#facc15";
    }catch(e){
      var el=document.getElementById("v8000Status");
      if(el){el.textContent="V8000 Fehler"; el.style.color="#ef4444";}
    }
  }
  updateV8000();
  setInterval(updateV8000,45000);
})();
</script>

<h2>📂 V7000 Boards</h2>

<a class="card" href="/dashboard?token=TOKEN_PLACEHOLDER">
  <div class="icon">📊</div><div><h3>Dashboard</h3><p>Hauptübersicht mit Märkten, offenen Trades und Status.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/learning?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧠</div><div><h3>Learning</h3><p>Outcomes, Winrate, R-Multiple und Learning-Gruppen.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/heartbeat?token=TOKEN_PLACEHOLDER">
  <div class="icon">💗</div><div><h3>Heartbeats</h3><p>Welche Märkte senden aktuelle 1m-Preise.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/decisions?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧾</div><div><h3>Decisions</h3><p>Mobile ALLOW/BLOCK Kartenansicht.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/shadow-detail?token=TOKEN_PLACEHOLDER">
  <div class="icon">🧪</div><div><h3>Shadow Trades</h3><p>Geblockte Near-Miss Signale als Paper-Trade.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/manual?token=TOKEN_PLACEHOLDER">
  <div class="icon">🛠️</div><div><h3>Manual Close</h3><p>Offene Trades manuell als WIN, LOSS oder BE schließen.</p></div><div class="arrow">›</div>
</a>

<a class="card" href="/intelligence?token=TOKEN_PLACEHOLDER">
  <div class="icon">🛰️</div><div><h3>Intelligence</h3><p>News, Bias und Marktintelligenz.</p></div><div class="arrow">›</div>
</a>

<div class="section">
<h2>🧪 Shadow Trades</h2>
<div class="sub">Letzte offene Shadow-Trades mit aktuellem R.</div>
<table>
<thead><tr><th>Market</th><th>Dir</th><th>Setup</th><th>Entry</th><th>Price</th><th>Punkte</th><th>R</th><th>Status</th></tr></thead>
<tbody id="shadowRows"><tr><td colspan="8">lade...</td></tr></tbody>
</table>
</div>

</div>

<script>
function cls(v){
  if(v===null || v===undefined) return "";
  if(Number(v)>0) return "pos";
  if(Number(v)<0) return "neg";
  return "warn";
}
function fmt(v){
  if(v===null || v===undefined) return "-";
  if(typeof v==="number") return Number(v).toFixed(3).replace(/\\.000$/,"");
  return v;
}
function row(x){
  return `<tr>
  <td>${x.market||"-"}</td>
  <td>${x.direction||"-"}</td>
  <td>${x.setup_name||"-"}</td>
  <td>${fmt(x.entry)}</td>
  <td>${fmt(x.current_price)}</td>
  <td class="${cls(x.pnl_points)}">${fmt(x.pnl_points)}</td>
  <td class="${cls(x.r_now)}">${fmt(x.r_now)}</td>
  <td>${x.status_hint||x.status||"-"}</td>
  </tr>`;
}
async function load(){
  try{
    const r=await fetch("/master-fast-state.json?token=TOKEN_PLACEHOLDER&ts="+Date.now(),{cache:"no-store"});
    const d=await r.json();

    liveCount.textContent=d.open_live_count;
    shadowCount.textContent=d.open_shadow_count;
    liveR.textContent=fmt(d.live_total_r);
    shadowR.textContent=fmt(d.shadow_total_r);

    liveR.className="value "+cls(d.live_total_r);
    shadowR.className="value "+cls(d.shadow_total_r);

    liveRows.innerHTML=(d.live||[]).length ? d.live.map(row).join("") : '<tr><td colspan="8">Keine offenen Live-Trades.</td></tr>';
    shadowRows.innerHTML=(d.shadow||[]).slice(0,25).map(row).join("") || '<tr><td colspan="8">Keine offenen Shadow-Trades.</td></tr>';
  }catch(e){
    liveRows.innerHTML='<tr><td colspan="8">Fehler: '+e+'</td></tr>';
  }
}
load();
setInterval(load,15000);
</script>
</body>
</html>"""
        return html.replace("TOKEN_PLACEHOLDER", t)

    @app.middleware("http")
    async def v72438g_today_update_cards_middleware(request: Request, call_next):
        if request.url.path in {"/master", "/v7000-master", "/master-v7000"}:
            return HTMLResponse(_v72438g_page(request.query_params.get("token", "")))
        return await call_next(request)

    print("[V7243.8G] Today update cards installed on V7000 master.")
except Exception as exc:
    print("[V7243.8G] install failed:", exc)
# === END V7243.8G TODAY UPDATE CARDS ===




# === V7243.6B MACRO ACTUAL MATCH FIX MIDDLEWARE ===
try:
    from app.v7243_6b_macro_actual_match_fix import v72436b_apply_macro_actual_match_fix

    @app.middleware("http")
    async def v72436b_macro_actual_match_fix_middleware(request, call_next):
        response = await call_next(request)
        try:
            if request.url.path in {
                "/macro-actual-v7243/rescore",
                "/macro-actual-v7243.json",
                "/calendar",
                "/calendar.json",
            }:
                v72436b_apply_macro_actual_match_fix()
        except Exception as _v72436b_exc:
            print("[V7243.6B] macro match fix middleware error:", _v72436b_exc)
        return response

    print("[V7243.6B] Macro actual match fix middleware installed.")
except Exception as exc:
    print("[V7243.6B] install failed:", exc)
# === END V7243.6B MACRO ACTUAL MATCH FIX MIDDLEWARE ===




# === V7243.9 STABILITY PACK ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7243_9_stability_pack import v72439_stability_payload, v72439_stability_html

    @app.get("/stability-v7243.json")
    def v72439_stability_json(token: str = ""):
        return JSONResponse(v72439_stability_payload())

    @app.get("/stability-v7243", response_class=HTMLResponse)
    def v72439_stability_page(token: str = ""):
        return HTMLResponse(v72439_stability_html())

    print("[V7243.9] Stability Pack routes installed.")
except Exception as exc:
    print("[V7243.9] install failed:", exc)
# === END V7243.9 STABILITY PACK ROUTES ===


# V7243.9D MOBILE COMPACT STABILITY STATUS installed




# === V7244A LIVE DECISION QUALITY ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7244_live_decision_quality import v7244_live_quality_payload, v7244_live_quality_html

    @app.get("/v7244-live-quality.json")
    def v7244_live_quality_json(token: str = "", limit: int = 80):
        return JSONResponse(v7244_live_quality_payload(limit=limit))

    @app.get("/v7244-live-quality", response_class=HTMLResponse)
    def v7244_live_quality_page(token: str = ""):
        return HTMLResponse(v7244_live_quality_html())

    print("[V7244A] Live Decision Quality routes installed.")
except Exception as exc:
    print("[V7244A] install failed:", exc)
# === END V7244A LIVE DECISION QUALITY ROUTES ===




# === V7244C EXPLAIN COMPARE ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7244c_explain_compare import v7244c_explain_payload, v7244c_explain_html

    @app.get("/v7244-explain.json")
    def v7244c_explain_json(token: str = "", limit: int = 80):
        return JSONResponse(v7244c_explain_payload(limit=limit))

    @app.get("/v7244-explain", response_class=HTMLResponse)
    def v7244c_explain_page(token: str = ""):
        return HTMLResponse(v7244c_explain_html())

    print("[V7244C] Explain Compare routes installed.")
except Exception as exc:
    print("[V7244C] install failed:", exc)
# === END V7244C EXPLAIN COMPARE ROUTES ===




# === V7244D NEXT LIVE REQUIREMENT ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7244d_next_live_requirement import v7244d_next_live_payload, v7244d_next_live_html

    @app.get("/v7244d-next-live.json")
    def v7244d_next_live_json(token: str = "", limit: int = 80):
        return JSONResponse(v7244d_next_live_payload(limit=limit))

    @app.get("/v7244d-next-live", response_class=HTMLResponse)
    def v7244d_next_live_page(token: str = ""):
        return HTMLResponse(v7244d_next_live_html())

    print("[V7244D] Next Live Requirement routes installed.")
except Exception as exc:
    print("[V7244D] install failed:", exc)
# === END V7244D NEXT LIVE REQUIREMENT ROUTES ===




# === V7244E THRESHOLD SIMULATOR ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7244e_threshold_simulator import v7244e_threshold_payload, v7244e_threshold_html

    @app.get("/v7244e-threshold-sim.json")
    def v7244e_threshold_json(token: str = "", limit: int = 80):
        return JSONResponse(v7244e_threshold_payload(limit=limit))

    @app.get("/v7244e-threshold-sim", response_class=HTMLResponse)
    def v7244e_threshold_page(token: str = ""):
        return HTMLResponse(v7244e_threshold_html())

    print("[V7244E] Threshold Simulator routes installed.")
except Exception as exc:
    print("[V7244E] install failed:", exc)
# === END V7244E THRESHOLD SIMULATOR ROUTES ===




# === V7244F RECOMMENDED PRESET ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7244f_recommended_preset import v7244f_recommended_preset_payload, v7244f_recommended_preset_html

    @app.get("/v7244f-recommended-preset.json")
    def v7244f_recommended_preset_json(token: str = "", limit: int = 80):
        return JSONResponse(v7244f_recommended_preset_payload(limit=limit))

    @app.get("/v7244f-recommended-preset", response_class=HTMLResponse)
    def v7244f_recommended_preset_page(token: str = ""):
        return HTMLResponse(v7244f_recommended_preset_html())

    print("[V7244F] Recommended Preset routes installed.")
except Exception as exc:
    print("[V7244F] install failed:", exc)
# === END V7244F RECOMMENDED PRESET ROUTES ===




# === V7245A MFE MAE EXCURSION ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7245a_mfe_mae_recorder import v7245a_payload, v7245a_html, v7245a_update_excursions

    @app.get("/v7245-excursion/update")
    def v7245a_excursion_update(token: str = ""):
        return JSONResponse(v7245a_update_excursions())

    @app.get("/v7245-excursion.json")
    def v7245a_excursion_json(token: str = "", limit: int = 80):
        return JSONResponse(v7245a_payload(limit=limit))

    @app.get("/v7245-excursion", response_class=HTMLResponse)
    def v7245a_excursion_page(token: str = ""):
        return HTMLResponse(v7245a_html())

    print("[V7245A] MFE/MAE Excursion routes installed.")
except Exception as exc:
    print("[V7245A] install failed:", exc)
# === END V7245A MFE MAE EXCURSION ROUTES ===




# === V7245B TP OPTIMIZER ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7245b_tp_optimizer import v7245b_payload, v7245b_html

    @app.get("/v7245b-tp-optimizer.json")
    def v7245b_tp_optimizer_json(token: str = "", limit: int = 80):
        return JSONResponse(v7245b_payload(limit=limit))

    @app.get("/v7245b-tp-optimizer", response_class=HTMLResponse)
    def v7245b_tp_optimizer_page(token: str = ""):
        return HTMLResponse(v7245b_html())

    print("[V7245B] TP Optimizer routes installed.")
except Exception as exc:
    print("[V7245B] install failed:", exc)
# === END V7245B TP OPTIMIZER ROUTES ===




# === V7245C RECOMMENDATION ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7245c_recommendation_board import v7245c_payload, v7245c_html

    @app.get("/v7245c-recommendations.json")
    def v7245c_recommendations_json(token: str = "", limit: int = 80):
        return JSONResponse(v7245c_payload(limit=limit))

    @app.get("/v7245c-recommendations", response_class=HTMLResponse)
    def v7245c_recommendations_page(token: str = ""):
        return HTMLResponse(v7245c_html())

    print("[V7245C] Recommendation routes installed.")
except Exception as exc:
    print("[V7245C] install failed:", exc)
# === END V7245C RECOMMENDATION ROUTES ===


# === V7301 COMPLETE AUDIT UPDATE ===
try:
    from app.v7301_complete_update import mount as v7301_mount
    v7301_mount(app)
    print("[V7301] Complete audit update mounted (observe-only).")
except Exception as exc:
    print("[V7301] mount failed:", exc)
# === END V7301 COMPLETE AUDIT UPDATE ===


# === V7302 NEWS DATA REPAIR ===
try:
    from app.v7302_news_data_repair import mount as v7302_mount
    v7302_mount(app)
    print("[V7302] News data repair watchdog mounted.")
except Exception as exc:
    print("[V7302] mount failed:", exc)
# === END V7302 NEWS DATA REPAIR ===


# === V7400 AI EVOLUTION OBSERVE ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7400_ai_evolution import v7400_payload, v7400_html

    @app.get("/v7400-ai-evolution.json")
    def v7400_ai_evolution_json(limit: int = 30):
        return JSONResponse(v7400_payload(limit=limit))

    @app.get("/v7400-ai-evolution", response_class=HTMLResponse)
    def v7400_ai_evolution_page():
        return HTMLResponse(v7400_html())

    print("[V7400] AI Evolution observe routes installed.")
except Exception as exc:
    print("[V7400] install failed:", exc)
# === END V7400 AI EVOLUTION OBSERVE ROUTES ===


# === V7500 AI DECISION ENGINE OBSERVE ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7500_ai_decision_engine import v7500_payload, v7500_html

    @app.get("/v7500-ai-decision.json")
    def v7500_ai_decision_json(limit: int = 50):
        return JSONResponse(v7500_payload(limit=limit))

    @app.get("/v7500-ai-decision", response_class=HTMLResponse)
    def v7500_ai_decision_page():
        return HTMLResponse(v7500_html())

    print("[V7500] AI Decision Engine observe routes installed.")
except Exception as exc:
    print("[V7500] install failed:", exc)
# === END V7500 AI DECISION ENGINE OBSERVE ROUTES ===

# === V7600 INTELLIGENCE DASHBOARD ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7600_intelligence_dashboard import v7600_payload, v7600_html

    @app.get("/v7600-intelligence.json")
    def v7600_intelligence_json():
        return JSONResponse(v7600_payload())

    @app.get("/v7600-intelligence", response_class=HTMLResponse)
    def v7600_intelligence_page():
        return HTMLResponse(v7600_html())

    print("[V7600] Intelligence Dashboard routes installed.")
except Exception as exc:
    print("[V7600] install failed:", exc)
# === END V7600 INTELLIGENCE DASHBOARD ROUTES ===

# === V7601 DEEP INTELLIGENCE ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7601_deep_intelligence import v7601_payload, v7601_html

    @app.get("/v7601-deep-intelligence.json")
    def v7601_deep_intelligence_json():
        return JSONResponse(v7601_payload())

    @app.get("/v7601-deep-intelligence", response_class=HTMLResponse)
    def v7601_deep_intelligence_page():
        return HTMLResponse(v7601_html())

    print("[V7601] Deep Intelligence routes installed.")
except Exception as exc:
    print("[V7601] install failed:", exc)
# === END V7601 DEEP INTELLIGENCE ROUTES ===

# === V7602-WEAKNESS-AUDIT OBSERVE ONLY START ===
# Version: V7602-WEAKNESS-AUDIT-OBSERVE-ONLY
import os as _v7602_os
import glob as _v7602_glob
import sqlite3 as _v7602_sqlite3
from collections import defaultdict as _v7602_defaultdict
from statistics import median as _v7602_median
from fastapi.responses import HTMLResponse as _V7602_HTMLResponse, JSONResponse as _V7602_JSONResponse

V7602_VERSION = "V7602-WEAKNESS-AUDIT-OBSERVE-ONLY"
V7602_TABLE = "v7245a_trade_excursions"

def _v7602_db_candidates():
    candidates = []
    for k in ("TRADINGBOT_DB", "DATABASE_PATH", "DB_PATH", "SQLITE_PATH", "V7000_DB", "V7245_DB"):
        v = _v7602_os.getenv(k)
        if v:
            candidates.append(v)

    candidates += [
        "/app/data/tradingbot.db",
        "/app/tradingbot.db",
        "/app/db/tradingbot.db",
        "/data/tradingbot.db",
        "/data/v7000.db",
        "/opt/tradingbot_v6000/data/tradingbot.db",
        "/opt/tradingbot_v6000/tradingbot.db",
        "/opt/tradingbot_v6000/app/tradingbot.db",
    ]

    for pattern in ("/app/**/*.db", "/data/**/*.db", "/opt/tradingbot_v6000/**/*.db"):
        try:
            candidates.extend(_v7602_glob.glob(pattern, recursive=True))
        except Exception:
            pass

    seen, out = set(), []
    for p in candidates:
        if p and p not in seen and _v7602_os.path.exists(p):
            seen.add(p)
            out.append(p)
    return out

def _v7602_connect_ro(path):
    return _v7602_sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)

def _v7602_find_db():
    checked = []
    for p in _v7602_db_candidates():
        try:
            con = _v7602_connect_ro(p)
            cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (V7602_TABLE,))
            ok = cur.fetchone() is not None
            con.close()
            checked.append({"path": p, "has_table": bool(ok)})
            if ok:
                return p, checked
        except Exception as e:
            checked.append({"path": p, "error": str(e)[:160]})
    return None, checked

def _v7602_num(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    if not s or s.lower() in ("none", "null", "nan", "na", "n/a"):
        return None
    try:
        return float(s)
    except Exception:
        return None

def _v7602_pick(cols, names):
    lower = {c.lower(): c for c in cols}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    for c in cols:
        cl = c.lower()
        for n in names:
            if n.lower() in cl:
                return c
    return None

def _v7602_val(row, col, default="UNKNOWN"):
    if not col:
        return default
    v = row.get(col)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()

def _v7602_bool_hit(row, hit_col, mfe):
    if hit_col and row.get(hit_col) is not None:
        s = str(row.get(hit_col)).strip().lower()
        if s in ("1", "true", "yes", "y", "hit"):
            return True
        if s in ("0", "false", "no", "n", "miss"):
            return False
        n = _v7602_num(row.get(hit_col))
        if n is not None:
            return n > 0
    return bool(mfe is not None and mfe >= 1.0)

def _v7602_status_is_open(row, status_col):
    if not status_col:
        return None
    s = str(row.get(status_col, "")).lower()
    if any(x in s for x in ("open", "running", "active", "live_now")):
        return True
    if any(x in s for x in ("closed", "done", "exit", "exited", "win", "loss", "finished")):
        return False
    return None

def _v7602_group_stats(items):
    n = len(items)
    r_vals = [x["r"] for x in items if x["r"] is not None]
    mfe_vals = [x["mfe"] for x in items if x["mfe"] is not None]
    mae_vals = [x["mae"] for x in items if x["mae"] is not None]

    def avg(vals):
        return round(sum(vals) / len(vals), 4) if vals else None

    loss_rate = round(sum(1 for x in r_vals if x < 0) / len(r_vals), 4) if r_vals else None
    win_rate = round(sum(1 for x in r_vals if x > 0) / len(r_vals), 4) if r_vals else None
    hit1_rate = round(sum(1 for x in items if x["hit1"]) / n, 4) if n else None
    stop_risk_rate = round(sum(1 for x in items if x["mae"] is not None and x["mae"] <= -1.0) / n, 4) if n else None
    giveback_rate = round(sum(1 for x in items if x["mfe"] is not None and x["r"] is not None and x["mfe"] >= 1.0 and x["r"] < 0.25) / n, 4) if n else None
    dead_trade_rate = round(sum(1 for x in items if x["mfe"] is not None and x["mae"] is not None and x["mfe"] < 0.35 and x["mae"] <= -0.50) / n, 4) if n else None

    avg_r = avg(r_vals)
    weakness_score = 0.0

    if avg_r is not None:
        weakness_score += max(0.0, -avg_r) * 2.0
    if loss_rate is not None:
        weakness_score += loss_rate * 1.2
    if stop_risk_rate is not None:
        weakness_score += stop_risk_rate * 1.3
    if giveback_rate is not None:
        weakness_score += giveback_rate * 0.9
    if hit1_rate is not None:
        weakness_score -= hit1_rate * 0.35

    severity = "LOW_SAMPLE" if n < 3 else "NEUTRAL"
    if n >= 3:
        if (avg_r is not None and avg_r <= -0.35) or (stop_risk_rate is not None and stop_risk_rate >= 0.55) or weakness_score >= 2.0:
            severity = "CRITICAL_WEAKNESS"
        elif (avg_r is not None and avg_r < 0) or (loss_rate is not None and loss_rate >= 0.55) or (stop_risk_rate is not None and stop_risk_rate >= 0.35):
            severity = "WEAK"
        elif (avg_r is not None and avg_r > 0.25) and (hit1_rate is not None and hit1_rate >= 0.35):
            severity = "STRONG"

    return {
        "n": n,
        "avg_r": avg_r,
        "median_r": round(_v7602_median(r_vals), 4) if r_vals else None,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "avg_mfe_r": avg(mfe_vals),
        "avg_mae_r": avg(mae_vals),
        "hit_1r_rate": hit1_rate,
        "mae_le_minus_1r_rate": stop_risk_rate,
        "giveback_after_1r_rate": giveback_rate,
        "dead_trade_rate": dead_trade_rate,
        "weakness_score": round(weakness_score, 4),
        "severity": severity,
    }

def _v7602_build():
    db_path, checked = _v7602_find_db()

    if not db_path:
        return {
            "version": V7602_VERSION,
            "mode": "observe_only_read_only",
            "ok": False,
            "error": f"Table {V7602_TABLE} not found in discovered sqlite db files.",
            "checked": checked,
        }

    con = _v7602_connect_ro(db_path)
    con.row_factory = _v7602_sqlite3.Row
    cur = con.cursor()

    cur.execute(f"PRAGMA table_info({V7602_TABLE})")
    cols = [r[1] for r in cur.fetchall()]

    symbol_col = _v7602_pick(cols, ["symbol", "market", "instrument", "ticker", "pair"])
    source_col = _v7602_pick(cols, ["source", "trade_source", "origin", "mode", "kind"])
    side_col = _v7602_pick(cols, ["side", "direction", "trade_side", "dir"])
    setup_col = _v7602_pick(cols, ["setup", "strategy", "signal_name", "signal_type", "model", "route"])
    session_col = _v7602_pick(cols, ["session", "entry_session", "market_session"])
    status_col = _v7602_pick(cols, ["status", "state", "trade_status"])
    r_col = _v7602_pick(cols, ["final_r", "realized_r", "result_r", "pnl_r", "closed_r", "r_result", "current_r"])
    mfe_col = _v7602_pick(cols, ["mfe_r", "max_favorable_r", "max_r", "best_r"])
    mae_col = _v7602_pick(cols, ["mae_r", "max_adverse_r", "min_r", "worst_r"])
    hit1_col = _v7602_pick(cols, ["hit_1r", "hit1r", "reached_1r"])
    time_col = _v7602_pick(cols, ["created_at", "entry_time", "opened_at", "timestamp", "ts", "time"])

    cur.execute(f"SELECT * FROM {V7602_TABLE} LIMIT 50000")
    raw_rows = [dict(r) for r in cur.fetchall()]
    con.close()

    items = []
    source_dist, status_dist = {}, {}

    for row in raw_rows:
        r = _v7602_num(row.get(r_col)) if r_col else None
        mfe = _v7602_num(row.get(mfe_col)) if mfe_col else None
        mae = _v7602_num(row.get(mae_col)) if mae_col else None

        source = _v7602_val(row, source_col)
        status = _v7602_val(row, status_col)

        source_dist[source] = source_dist.get(source, 0) + 1
        status_dist[status] = status_dist.get(status, 0) + 1

        open_state = _v7602_status_is_open(row, status_col)

        items.append({
            "symbol": _v7602_val(row, symbol_col),
            "source": source,
            "side": _v7602_val(row, side_col),
            "setup": _v7602_val(row, setup_col),
            "session": _v7602_val(row, session_col),
            "status": status,
            "is_open": open_state,
            "r": r,
            "mfe": mfe,
            "mae": mae,
            "hit1": _v7602_bool_hit(row, hit1_col, mfe),
            "time": _v7602_val(row, time_col, ""),
        })

    def top_by(key_fn, min_n=3, limit=12):
        groups = _v7602_defaultdict(list)
        for x in items:
            groups[key_fn(x)].append(x)

        out = []
        for k, arr in groups.items():
            st = _v7602_group_stats(arr)
            st["key"] = k
            if st["n"] >= min_n:
                out.append(st)

        out.sort(key=lambda z: (z.get("weakness_score") or 0, -(z.get("avg_r") or 0)), reverse=True)
        return out[:limit]

    data_quality = []
    for label, col in {
        "symbol/market": symbol_col,
        "source": source_col,
        "side": side_col,
        "setup/strategy": setup_col,
        "session": session_col,
        "R result/current_r": r_col,
        "MFE R": mfe_col,
        "MAE R": mae_col,
    }.items():
        if not col:
            data_quality.append(f"missing_or_unknown_column:{label}")

    if len(raw_rows) >= 50000:
        data_quality.append("row_limit_reached_50000")

    return {
        "version": V7602_VERSION,
        "mode": "observe_only_read_only",
        "ok": True,
        "db_path": db_path,
        "table": V7602_TABLE,
        "rows_seen": len(raw_rows),
        "columns_seen": cols,
        "columns_used": {
            "symbol": symbol_col,
            "source": source_col,
            "side": side_col,
            "setup": setup_col,
            "session": session_col,
            "status": status_col,
            "r": r_col,
            "mfe": mfe_col,
            "mae": mae_col,
            "hit_1r": hit1_col,
            "time": time_col,
        },
        "distribution": {
            "source": source_dist,
            "status": status_dist,
        },
        "overall": _v7602_group_stats(items) if items else {},
        "weakest_markets": top_by(lambda x: x["symbol"]),
        "weakest_sources": top_by(lambda x: x["source"]),
        "weakest_sides": top_by(lambda x: x["side"]),
        "weakest_market_side": top_by(lambda x: f'{x["symbol"]} {x["side"]}'),
        "weakest_setups": top_by(lambda x: x["setup"]),
        "weakest_sessions": top_by(lambda x: x["session"]),
        "weakest_source_market_side": top_by(lambda x: f'{x["source"]} | {x["symbol"]} | {x["side"]}'),
        "data_quality_findings": data_quality,
        "observe_only_next_actions": [
            "Do not change live rules from this output.",
            "Use CRITICAL_WEAKNESS groups as candidates for deeper manual review.",
            "Compare weak groups with V7601 before any AI module gets authority.",
            "Only after enough closed samples should filter suggestions be promoted to review.",
        ],
    }

def _v7602_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows):
        if not rows:
            return "<p>No rows.</p>"

        keys = [
            "key", "n", "avg_r", "win_rate", "loss_rate",
            "avg_mfe_r", "avg_mae_r", "hit_1r_rate",
            "mae_le_minus_1r_rate", "giveback_after_1r_rate",
            "weakness_score", "severity"
        ]

        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = ""

        for r in rows:
            body += "<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in keys) + "</tr>"

        return f"<table border='1' cellspacing='0' cellpadding='5'><tr>{head}</tr>{body}</table>"

    if not data.get("ok"):
        return f"<h1>V7602 Weakness Audit</h1><pre>{esc(data)}</pre>"

    return f"""
    <html>
    <head><title>V7602 Weakness Audit</title></head>
    <body style="font-family:Arial, sans-serif; padding:20px;">
    <h1>V7602 Weakness Audit</h1>

    <p>
    <b>Version:</b> {esc(data.get("version"))}<br>
    <b>Mode:</b> {esc(data.get("mode"))}<br>
    <b>Table:</b> {esc(data.get("table"))}<br>
    <b>Rows seen:</b> {esc(data.get("rows_seen"))}
    </p>

    <h2>Overall</h2>
    <pre>{esc(data.get("overall"))}</pre>

    <h2>Columns used</h2>
    <pre>{esc(data.get("columns_used"))}</pre>

    <h2>Data Quality Findings</h2>
    <pre>{esc(data.get("data_quality_findings"))}</pre>

    <h2>Weakest Source / Market / Side</h2>
    {table(data.get("weakest_source_market_side", []))}

    <h2>Weakest Markets</h2>
    {table(data.get("weakest_markets", []))}

    <h2>Weakest Market + Side</h2>
    {table(data.get("weakest_market_side", []))}

    <h2>Weakest Setups</h2>
    {table(data.get("weakest_setups", []))}

    <h2>Weakest Sessions</h2>
    {table(data.get("weakest_sessions", []))}
    </body>
    </html>
    """

@app.get("/v7602-weakness-audit.json")
def v7602_weakness_audit_json():
    return _V7602_JSONResponse(_v7602_build())

@app.get("/v7602-weakness-audit")
def v7602_weakness_audit_html():
    return _V7602_HTMLResponse(_v7602_html(_v7602_build()))

# === V7602-WEAKNESS-AUDIT OBSERVE ONLY END ===

# === V7602.1-DB-LOCATOR-FIX OBSERVE ONLY START ===
# Overrides only DB discovery for V7602. No writes. No live-rule changes.
V7602_LOCATOR_FIX_VERSION = "V7602.1-DB-LOCATOR-FIX-OBSERVE-ONLY"

def _v7602_db_candidates():
    candidates = []

    # 1) Environment variables
    for k in (
        "TRADINGBOT_DB",
        "DATABASE_PATH",
        "DB_PATH",
        "SQLITE_PATH",
        "V7000_DB",
        "V7245_DB",
        "APP_DB_PATH",
        "DATABASE_URL",
    ):
        try:
            v = _v7602_os.getenv(k)
            if v:
                if str(v).startswith("sqlite:///"):
                    v = str(v).replace("sqlite:///", "/", 1)
                candidates.append(v)
        except Exception:
            pass

    # 2) Known container and project paths
    candidates += [
        "tradingbot.db",
        "tradingbot.sqlite",
        "tradingbot.sqlite3",
        "v7000.db",
        "v7000.sqlite",
        "v7000.sqlite3",
        "bot.db",
        "bot.sqlite",
        "bot.sqlite3",

        "/app/tradingbot.db",
        "/app/tradingbot.sqlite",
        "/app/tradingbot.sqlite3",
        "/app/v7000.db",
        "/app/v7000.sqlite",
        "/app/v7000.sqlite3",

        "/app/data/tradingbot.db",
        "/app/data/tradingbot.sqlite",
        "/app/data/tradingbot.sqlite3",
        "/app/data/v7000.db",
        "/app/data/v7000.sqlite",
        "/app/data/v7000.sqlite3",

        "/app/app/tradingbot.db",
        "/app/app/tradingbot.sqlite",
        "/app/app/tradingbot.sqlite3",
        "/app/app/data/tradingbot.db",
        "/app/app/data/tradingbot.sqlite",
        "/app/app/data/tradingbot.sqlite3",

        "/data/tradingbot.db",
        "/data/tradingbot.sqlite",
        "/data/tradingbot.sqlite3",
        "/data/v7000.db",
        "/data/v7000.sqlite",
        "/data/v7000.sqlite3",

        "/opt/tradingbot_v6000/tradingbot.db",
        "/opt/tradingbot_v6000/tradingbot.sqlite",
        "/opt/tradingbot_v6000/tradingbot.sqlite3",
        "/opt/tradingbot_v6000/data/tradingbot.db",
        "/opt/tradingbot_v6000/data/tradingbot.sqlite",
        "/opt/tradingbot_v6000/data/tradingbot.sqlite3",
    ]

    # 3) Recursive DB search in safe project/container folders
    for base in (".", "/app", "/app/app", "/app/data", "/data", "/opt/tradingbot_v6000"):
        for ext in ("*.db", "*.sqlite", "*.sqlite3"):
            try:
                candidates.extend(_v7602_glob.glob(f"{base}/**/{ext}", recursive=True))
            except Exception:
                pass

    seen = set()
    out = []

    for p in candidates:
        try:
            if not p:
                continue
            p = str(p).strip()
            if not p or p in seen:
                continue
            if _v7602_os.path.exists(p) and _v7602_os.path.isfile(p):
                seen.add(p)
                out.append(p)
        except Exception:
            pass

    return out

def _v7602_find_db():
    checked = []
    candidates = _v7602_db_candidates()

    for p in candidates:
        try:
            con = _v7602_connect_ro(p)
            cur = con.cursor()

            cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [r[0] for r in cur.fetchall()]

            has_target = V7602_TABLE in tables

            checked.append({
                "path": p,
                "has_table": bool(has_target),
                "tables_seen_sample": tables[:30],
                "table_count": len(tables),
            })

            con.close()

            if has_target:
                return p, checked

        except Exception as e:
            checked.append({
                "path": p,
                "error": str(e)[:220],
            })

    return None, checked

# === V7602.1-DB-LOCATOR-FIX OBSERVE ONLY END ===

# === V7603-CANDIDATE-BLOCKLIST-REVIEW OBSERVE ONLY START ===
# Review-only candidate engine.
# Does NOT change live rules.
# Does NOT block trades.
# Does NOT write to DB.
# Reads V7602 output only.

V7603_VERSION = "V7603-CANDIDATE-BLOCKLIST-REVIEW-OBSERVE-ONLY"

def _v7603_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default

def _v7603_int(v, default=0):
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default

def _v7603_candidate_from_row(row, category):
    n = _v7603_int(row.get("n"), 0)
    avg_r = _v7603_float(row.get("avg_r"), 0.0)
    median_r = _v7603_float(row.get("median_r"), 0.0)
    win_rate = _v7603_float(row.get("win_rate"), 0.0)
    loss_rate = _v7603_float(row.get("loss_rate"), 0.0)
    avg_mfe = _v7603_float(row.get("avg_mfe_r"), 0.0)
    avg_mae = _v7603_float(row.get("avg_mae_r"), 0.0)
    hit1 = _v7603_float(row.get("hit_1r_rate"), 0.0)
    mae_stop = _v7603_float(row.get("mae_le_minus_1r_rate"), 0.0)
    giveback = _v7603_float(row.get("giveback_after_1r_rate"), 0.0)
    dead = _v7603_float(row.get("dead_trade_rate"), 0.0)
    score = _v7603_float(row.get("weakness_score"), 0.0)

    reasons = []

    if n < 5:
        reasons.append("LOW_SAMPLE_UNDER_5")
    elif n < 10:
        reasons.append("SMALL_SAMPLE_5_TO_9")
    else:
        reasons.append("SAMPLE_OK_10_PLUS")

    if avg_r <= -1.0:
        reasons.append("AVG_R_BELOW_MINUS_1R")
    elif avg_r < 0:
        reasons.append("AVG_R_NEGATIVE")

    if median_r < 0:
        reasons.append("MEDIAN_R_NEGATIVE")

    if loss_rate >= 0.80:
        reasons.append("LOSS_RATE_EXTREME_80_PLUS")
    elif loss_rate >= 0.65:
        reasons.append("LOSS_RATE_HIGH_65_PLUS")
    elif loss_rate >= 0.55:
        reasons.append("LOSS_RATE_ELEVATED_55_PLUS")

    if mae_stop >= 0.80:
        reasons.append("MAE_STOP_RISK_EXTREME_80_PLUS")
    elif mae_stop >= 0.60:
        reasons.append("MAE_STOP_RISK_HIGH_60_PLUS")
    elif mae_stop >= 0.40:
        reasons.append("MAE_STOP_RISK_ELEVATED_40_PLUS")

    if hit1 <= 0.10:
        reasons.append("HIT_1R_VERY_LOW_10_OR_LESS")
    elif hit1 <= 0.25:
        reasons.append("HIT_1R_LOW_25_OR_LESS")

    if avg_mfe < 0.35:
        reasons.append("LOW_MFE_NO_FOLLOW_THROUGH")

    if avg_mae <= -2.0:
        reasons.append("AVG_MAE_WORSE_THAN_MINUS_2R")

    if giveback >= 0.30:
        reasons.append("GIVEBACK_AFTER_1R_HIGH")

    if dead >= 0.50:
        reasons.append("DEAD_TRADE_RATE_HIGH_50_PLUS")

    # Review tier. This is NOT an active block.
    tier = "WATCH_ONLY"
    review_action = "MONITOR_MORE_DATA"

    if n >= 8 and avg_r <= -1.0 and loss_rate >= 0.70 and (mae_stop >= 0.60 or hit1 <= 0.25):
        tier = "BLOCKLIST_CANDIDATE_STRONG_REVIEW_ONLY"
        review_action = "MANUAL_REVIEW_FOR_POSSIBLE_FUTURE_BLOCK"
    elif n >= 5 and avg_r <= -0.75 and loss_rate >= 0.60:
        tier = "BLOCKLIST_CANDIDATE_REVIEW_ONLY"
        review_action = "MANUAL_REVIEW_REQUIRED"
    elif n >= 10 and avg_r < 0 and loss_rate >= 0.55:
        tier = "WEAKNESS_WATCH_REVIEW_ONLY"
        review_action = "WATCH_NEXT_SAMPLES"
    elif n < 5:
        tier = "LOW_SAMPLE_REVIEW_ONLY"
        review_action = "WAIT_FOR_MORE_SAMPLES"

    confidence = "LOW"
    if n >= 30:
        confidence = "HIGH"
    elif n >= 12:
        confidence = "MEDIUM"
    elif n >= 5:
        confidence = "LOW_MEDIUM"

    return {
        "category": category,
        "key": row.get("key"),
        "n": n,
        "avg_r": avg_r,
        "median_r": median_r,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "avg_mfe_r": avg_mfe,
        "avg_mae_r": avg_mae,
        "hit_1r_rate": hit1,
        "mae_le_minus_1r_rate": mae_stop,
        "giveback_after_1r_rate": giveback,
        "dead_trade_rate": dead,
        "weakness_score": score,
        "source_severity": row.get("severity"),
        "candidate_tier": tier,
        "confidence": confidence,
        "review_action": review_action,
        "reasons": reasons,
        "active_effect": "NONE_REVIEW_ONLY",
    }

def _v7603_sort_key(x):
    tier_rank = {
        "BLOCKLIST_CANDIDATE_STRONG_REVIEW_ONLY": 5,
        "BLOCKLIST_CANDIDATE_REVIEW_ONLY": 4,
        "WEAKNESS_WATCH_REVIEW_ONLY": 3,
        "LOW_SAMPLE_REVIEW_ONLY": 2,
        "WATCH_ONLY": 1,
    }.get(x.get("candidate_tier"), 0)

    return (
        tier_rank,
        _v7603_float(x.get("weakness_score"), 0.0),
        -_v7603_float(x.get("avg_r"), 0.0),
        _v7603_int(x.get("n"), 0),
    )

def _v7603_build():
    base = _v7602_build()

    if not base.get("ok"):
        return {
            "version": V7603_VERSION,
            "mode": "observe_only_review_only_no_live_changes",
            "ok": False,
            "error": "V7602 base build failed. V7603 cannot review candidates.",
            "v7602": base,
        }

    candidates = []

    group_map = {
        "source_market_side": base.get("weakest_source_market_side") or [],
        "market_side": base.get("weakest_market_side") or [],
        "market": base.get("weakest_markets") or [],
        "setup": base.get("weakest_setups") or [],
        "side": base.get("weakest_sides") or [],
        "source": base.get("weakest_sources") or [],
        "session": base.get("weakest_sessions") or [],
    }

    for category, rows in group_map.items():
        for row in rows:
            candidates.append(_v7603_candidate_from_row(row, category))

    candidates.sort(key=_v7603_sort_key, reverse=True)

    strong = [x for x in candidates if x.get("candidate_tier") == "BLOCKLIST_CANDIDATE_STRONG_REVIEW_ONLY"]
    review = [x for x in candidates if x.get("candidate_tier") == "BLOCKLIST_CANDIDATE_REVIEW_ONLY"]
    watch = [x for x in candidates if x.get("candidate_tier") == "WEAKNESS_WATCH_REVIEW_ONLY"]
    low_sample = [x for x in candidates if x.get("candidate_tier") == "LOW_SAMPLE_REVIEW_ONLY"]

    # Extra diagnosis patterns
    jpy_long_candidates = [
        x for x in candidates
        if "JPY" in str(x.get("key", "")).upper()
        and "LONG" in str(x.get("key", "")).upper()
        and x.get("category") in ("source_market_side", "market_side")
    ]

    bullish_setup_candidates = [
        x for x in candidates
        if x.get("category") == "setup"
        and any(s in str(x.get("key", "")).upper() for s in ("BULL", "LONG", "MSS", "BOS", "BPR"))
        and _v7603_float(x.get("avg_r"), 0.0) < 0
    ]

    data_quality_notes = []

    for finding in base.get("data_quality_findings") or []:
        data_quality_notes.append({
            "finding": finding,
            "impact": "Limits session/time-of-day weakness attribution.",
            "review_action": "Build derived session audit before making time filters.",
            "active_effect": "NONE_REVIEW_ONLY",
        })

    if (base.get("columns_used") or {}).get("session") is None:
        data_quality_notes.append({
            "finding": "NO_SESSION_COLUMN_MAPPED",
            "impact": "V7602 can only show UNKNOWN session.",
            "review_action": "Create observe-only derived session mapper from opened_at.",
            "active_effect": "NONE_REVIEW_ONLY",
        })

    overall = base.get("overall") or {}

    return {
        "version": V7603_VERSION,
        "mode": "observe_only_review_only_no_live_changes",
        "ok": True,
        "active_effect": "NONE",
        "live_rule_changes": False,
        "auto_blocking": False,
        "auto_filtering": False,
        "db_writes": False,
        "source_table": base.get("table"),
        "db_path": base.get("db_path"),
        "rows_seen": base.get("rows_seen"),
        "distribution": base.get("distribution"),
        "overall": overall,
        "summary": {
            "strong_blocklist_review_candidates": len(strong),
            "blocklist_review_candidates": len(review),
            "weakness_watch_candidates": len(watch),
            "low_sample_review_candidates": len(low_sample),
            "total_candidates_seen": len(candidates),
        },
        "top_strong_review_candidates": strong[:25],
        "top_blocklist_review_candidates": review[:25],
        "top_watch_candidates": watch[:25],
        "low_sample_candidates": low_sample[:25],
        "jpy_long_diagnosis": {
            "finding": "JPY_LONG_CLUSTER_WEAKNESS_REVIEW_ONLY",
            "candidates": jpy_long_candidates[:20],
            "interpretation": "Multiple JPY long groups show severe negative expectancy in the current shadow sample. This is a candidate for deeper review, not an active block.",
            "active_effect": "NONE_REVIEW_ONLY",
        },
        "bullish_setup_diagnosis": {
            "finding": "BULLISH_SETUP_FAMILY_WEAKNESS_REVIEW_ONLY",
            "candidates": bullish_setup_candidates[:25],
            "interpretation": "Several bullish MSS/BOS/BPR-style setup families are weak in current samples. Needs V7601 comparison before any future decision.",
            "active_effect": "NONE_REVIEW_ONLY",
        },
        "data_quality_review": data_quality_notes,
        "safe_next_steps": [
            "Do not change live trading rules.",
            "Compare V7603 candidates with V7601 Deep Intelligence.",
            "Build V7604 derived session/time audit because session is missing.",
            "Only after larger closed samples create review-only proposed filters.",
        ],
    }

def _v7603_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows):
        if not rows:
            return "<p>No rows.</p>"

        keys = [
            "category", "key", "candidate_tier", "confidence", "n",
            "avg_r", "median_r", "loss_rate", "hit_1r_rate",
            "mae_le_minus_1r_rate", "dead_trade_rate", "weakness_score",
            "review_action", "active_effect"
        ]

        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = ""

        for r in rows:
            body += "<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in keys) + "</tr>"

        return f"<table border='1' cellspacing='0' cellpadding='5'><tr>{head}</tr>{body}</table>"

    if not data.get("ok"):
        return f"<h1>V7603 Candidate Blocklist Review</h1><pre>{esc(data)}</pre>"

    return f"""
    <html>
    <head><title>V7603 Candidate Blocklist Review</title></head>
    <body style="font-family:Arial, sans-serif; padding:20px;">
      <h1>V7603 Candidate Blocklist Review</h1>

      <p>
        <b>Version:</b> {esc(data.get("version"))}<br>
        <b>Mode:</b> {esc(data.get("mode"))}<br>
        <b>Active effect:</b> {esc(data.get("active_effect"))}<br>
        <b>Live rule changes:</b> {esc(data.get("live_rule_changes"))}<br>
        <b>Auto blocking:</b> {esc(data.get("auto_blocking"))}<br>
        <b>DB writes:</b> {esc(data.get("db_writes"))}<br>
        <b>Rows seen:</b> {esc(data.get("rows_seen"))}
      </p>

      <h2>Summary</h2>
      <pre>{esc(data.get("summary"))}</pre>

      <h2>Overall</h2>
      <pre>{esc(data.get("overall"))}</pre>

      <h2>Strong Review Candidates</h2>
      {table(data.get("top_strong_review_candidates", []))}

      <h2>Blocklist Review Candidates</h2>
      {table(data.get("top_blocklist_review_candidates", []))}

      <h2>Watch Candidates</h2>
      {table(data.get("top_watch_candidates", []))}

      <h2>JPY Long Diagnosis</h2>
      <pre>{esc(data.get("jpy_long_diagnosis"))}</pre>

      <h2>Bullish Setup Diagnosis</h2>
      <pre>{esc(data.get("bullish_setup_diagnosis"))}</pre>

      <h2>Data Quality Review</h2>
      <pre>{esc(data.get("data_quality_review"))}</pre>
    </body>
    </html>
    """

@app.get("/v7603-candidate-blocklist-review.json")
def v7603_candidate_blocklist_review_json():
    return _V7602_JSONResponse(_v7603_build())

@app.get("/v7603-candidate-blocklist-review")
def v7603_candidate_blocklist_review_html():
    return _V7602_HTMLResponse(_v7603_html(_v7603_build()))

# === V7603-CANDIDATE-BLOCKLIST-REVIEW OBSERVE ONLY END ===

# === V7604-DERIVED-SESSION-AUDIT OBSERVE ONLY START ===
# Reads v7245a_trade_excursions only.
# Derives session/time buckets from opened_at.
# Does NOT write to DB.
# Does NOT change live rules.
# Does NOT block/filter trades.

V7604_VERSION = "V7604-DERIVED-SESSION-AUDIT-OBSERVE-ONLY"

import datetime as _v7604_datetime
import re as _v7604_re

try:
    from zoneinfo import ZoneInfo as _v7604_ZoneInfo
except Exception:
    _v7604_ZoneInfo = None

def _v7604_parse_dt(value):
    if value is None:
        return None, "missing"

    s = str(value).strip()
    if not s:
        return None, "empty"

    try:
        # epoch seconds / milliseconds
        if _v7604_re.fullmatch(r"\d+(\.\d+)?", s):
            n = float(s)
            if n > 100000000000:
                n = n / 1000.0
            dt = _v7604_datetime.datetime.fromtimestamp(
                n,
                tz=_v7604_datetime.timezone.utc
            )
            return dt, "epoch_assumed_utc"

        iso = s.replace("Z", "+00:00")
        dt = _v7604_datetime.datetime.fromisoformat(iso)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_v7604_datetime.timezone.utc)
            return dt.astimezone(_v7604_datetime.timezone.utc), "naive_assumed_utc"

        return dt.astimezone(_v7604_datetime.timezone.utc), "timezone_aware"

    except Exception as e:
        return None, f"parse_error:{str(e)[:80]}"

def _v7604_to_berlin(dt_utc):
    if not dt_utc:
        return None

    try:
        if _v7604_ZoneInfo:
            return dt_utc.astimezone(_v7604_ZoneInfo("Europe/Berlin"))
    except Exception:
        pass

    return dt_utc

def _v7604_session_berlin(dt_berlin):
    if not dt_berlin:
        return "UNKNOWN_TIME"

    hm = dt_berlin.hour + dt_berlin.minute / 60.0

    if 0 <= hm < 7:
        return "ASIA_OVERNIGHT"
    if 7 <= hm < 8:
        return "EU_PRE_OPEN"
    if 8 <= hm < 11:
        return "LONDON_OPEN"
    if 11 <= hm < 13.5:
        return "LONDON_MID"
    if 13.5 <= hm < 16.5:
        return "NY_OPEN_OVERLAP"
    if 16.5 <= hm < 19:
        return "NY_MID"
    if 19 <= hm < 22:
        return "NY_AFTERNOON"
    return "ROLLOVER_OFFHOURS"

def _v7604_hour_bucket(dt, prefix):
    if not dt:
        return f"{prefix}_UNKNOWN"
    return f"{prefix}_{dt.hour:02d}:00"

def _v7604_weekday_bucket(dt):
    if not dt:
        return "UNKNOWN_WEEKDAY"
    names = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    return f"{dt.weekday()}_{names[dt.weekday()]}"

def _v7604_group(items, key_fn, min_n=3, limit=25, strongest=False):
    groups = _v7602_defaultdict(list)

    for x in items:
        groups[key_fn(x)].append(x)

    out = []

    for k, arr in groups.items():
        st = _v7602_group_stats(arr)
        st["key"] = k
        if st.get("n", 0) >= min_n:
            out.append(st)

    if strongest:
        out.sort(
            key=lambda z: (
                z.get("avg_r") if z.get("avg_r") is not None else -999,
                z.get("hit_1r_rate") if z.get("hit_1r_rate") is not None else 0,
                -(z.get("mae_le_minus_1r_rate") if z.get("mae_le_minus_1r_rate") is not None else 1),
                z.get("n", 0),
            ),
            reverse=True,
        )
    else:
        out.sort(
            key=lambda z: (
                z.get("weakness_score") or 0,
                -(z.get("avg_r") or 0),
                z.get("n", 0),
            ),
            reverse=True,
        )

    return out[:limit]

def _v7604_build():
    db_path, checked = _v7602_find_db()

    if not db_path:
        return {
            "version": V7604_VERSION,
            "mode": "observe_only_read_only_no_live_changes",
            "ok": False,
            "error": "DB/table not found via V7602 locator.",
            "checked": checked,
        }

    con = _v7602_connect_ro(db_path)
    con.row_factory = _v7602_sqlite3.Row
    cur = con.cursor()

    cur.execute(f"PRAGMA table_info({V7602_TABLE})")
    cols = [r[1] for r in cur.fetchall()]

    market_col = _v7602_pick(cols, ["market", "symbol", "instrument", "ticker", "pair"])
    source_col = _v7602_pick(cols, ["source", "trade_source", "origin", "mode", "kind"])
    side_col = _v7602_pick(cols, ["direction", "side", "trade_side", "dir"])
    setup_col = _v7602_pick(cols, ["setup_name", "setup", "strategy", "signal_name", "signal_type", "model", "route"])
    status_col = _v7602_pick(cols, ["status", "state", "trade_status"])
    opened_col = _v7602_pick(cols, ["opened_at", "entry_time", "created_at", "timestamp", "ts", "time"])
    r_col = _v7602_pick(cols, ["final_r", "realized_r", "result_r", "pnl_r", "closed_r", "r_result", "current_r"])
    mfe_col = _v7602_pick(cols, ["max_favorable_r", "mfe_r", "max_r", "best_r"])
    mae_col = _v7602_pick(cols, ["max_adverse_r", "mae_r", "min_r", "worst_r"])
    hit1_col = _v7602_pick(cols, ["hit_1r", "hit1r", "reached_1r"])

    cur.execute(f"SELECT * FROM {V7602_TABLE} LIMIT 50000")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()

    items = []
    parse_notes = {}
    raw_session_counts = {}
    closed_count = 0
    open_count = 0

    for row in rows:
        opened_raw = row.get(opened_col) if opened_col else None
        dt_utc, parse_note = _v7604_parse_dt(opened_raw)
        dt_berlin = _v7604_to_berlin(dt_utc)

        parse_notes[parse_note] = parse_notes.get(parse_note, 0) + 1

        session = _v7604_session_berlin(dt_berlin)
        raw_session_counts[session] = raw_session_counts.get(session, 0) + 1

        status = _v7602_val(row, status_col)
        status_upper = status.upper()
        is_closed = "CLOSED" in status_upper or "DONE" in status_upper or "EXIT" in status_upper
        is_open = "OPEN" in status_upper or "RUNNING" in status_upper or "ACTIVE" in status_upper

        if is_closed:
            closed_count += 1
        elif is_open:
            open_count += 1

        mfe = _v7602_num(row.get(mfe_col)) if mfe_col else None
        mae = _v7602_num(row.get(mae_col)) if mae_col else None

        item = {
            "market": _v7602_val(row, market_col),
            "source": _v7602_val(row, source_col),
            "side": _v7602_val(row, side_col),
            "setup": _v7602_val(row, setup_col),
            "status": status,
            "is_closed": is_closed,
            "is_open": is_open,
            "r": _v7602_num(row.get(r_col)) if r_col else None,
            "mfe": mfe,
            "mae": mae,
            "hit1": _v7602_bool_hit(row, hit1_col, mfe),
            "opened_raw": opened_raw,
            "parse_note": parse_note,
            "session_berlin": session,
            "hour_utc": _v7604_hour_bucket(dt_utc, "UTC"),
            "hour_berlin": _v7604_hour_bucket(dt_berlin, "BERLIN"),
            "weekday_berlin": _v7604_weekday_bucket(dt_berlin),
        }

        item["session_market_side"] = f'{item["session_berlin"]} | {item["market"]} | {item["side"]}'
        item["session_setup"] = f'{item["session_berlin"]} | {item["setup"]}'
        item["session_side"] = f'{item["session_berlin"]} | {item["side"]}'
        item["hour_market_side"] = f'{item["hour_berlin"]} | {item["market"]} | {item["side"]}'

        items.append(item)

    closed_items = [x for x in items if x.get("is_closed")]
    if not closed_items:
        closed_items = [x for x in items if x.get("r") is not None]

    all_items = items

    data_quality = []

    if not opened_col:
        data_quality.append("missing_opened_at_or_time_column")

    if parse_notes.get("parse_error", 0):
        data_quality.append("timestamp_parse_errors_detected")

    if parse_notes.get("missing", 0) or parse_notes.get("empty", 0):
        data_quality.append("missing_timestamp_values_detected")

    if parse_notes.get("naive_assumed_utc", 0):
        data_quality.append("some_timestamps_are_naive_assumed_utc")

    return {
        "version": V7604_VERSION,
        "mode": "observe_only_read_only_no_live_changes",
        "ok": True,
        "active_effect": "NONE",
        "live_rule_changes": False,
        "auto_blocking": False,
        "auto_filtering": False,
        "db_writes": False,
        "db_path": db_path,
        "table": V7602_TABLE,
        "rows_seen": len(rows),
        "closed_items_used": len(closed_items),
        "open_items_seen": open_count,
        "closed_status_seen": closed_count,
        "timezone_policy": {
            "source_timestamp_column": opened_col,
            "parse_policy": "timezone-aware timestamps converted to UTC; naive timestamps assumed UTC",
            "derived_timezone": "Europe/Berlin",
            "session_source": "derived_from_opened_at_only",
        },
        "columns_used": {
            "market": market_col,
            "source": source_col,
            "side": side_col,
            "setup": setup_col,
            "status": status_col,
            "opened_at": opened_col,
            "r": r_col,
            "mfe": mfe_col,
            "mae": mae_col,
            "hit_1r": hit1_col,
        },
        "timestamp_parse_notes": parse_notes,
        "raw_session_counts": raw_session_counts,
        "data_quality_findings": data_quality,
        "overall_closed_or_r_available": _v7602_group_stats(closed_items) if closed_items else {},
        "weakest_sessions_closed": _v7604_group(closed_items, lambda x: x["session_berlin"]),
        "strongest_sessions_closed": _v7604_group(closed_items, lambda x: x["session_berlin"], strongest=True),
        "weakest_hours_berlin_closed": _v7604_group(closed_items, lambda x: x["hour_berlin"]),
        "strongest_hours_berlin_closed": _v7604_group(closed_items, lambda x: x["hour_berlin"], strongest=True),
        "weakest_weekdays_berlin_closed": _v7604_group(closed_items, lambda x: x["weekday_berlin"]),
        "weakest_session_side_closed": _v7604_group(closed_items, lambda x: x["session_side"]),
        "weakest_session_market_side_closed": _v7604_group(closed_items, lambda x: x["session_market_side"]),
        "weakest_session_setup_closed": _v7604_group(closed_items, lambda x: x["session_setup"]),
        "weakest_hour_market_side_closed": _v7604_group(closed_items, lambda x: x["hour_market_side"]),
        "all_items_session_overview": _v7604_group(all_items, lambda x: x["session_berlin"]),
        "review_only_interpretation": [
            "If one derived session is much worse, it becomes a candidate for deeper review only.",
            "No session/time filter should be activated from this module alone.",
            "Compare session weakness with V7603 candidate clusters before any future proposal.",
            "If timestamps were naive, verify whether opened_at is truly UTC before trusting exact Berlin session labels.",
        ],
        "safe_next_steps": [
            "Review weakest_sessions_closed and weakest_session_market_side_closed.",
            "If timestamp policy is uncertain, inspect sample opened_at values.",
            "Then build V7605 V7601-V7603-V7604 cross-check matrix.",
        ],
    }

def _v7604_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows):
        if not rows:
            return "<p>No rows.</p>"

        keys = [
            "key", "n", "avg_r", "median_r", "win_rate", "loss_rate",
            "avg_mfe_r", "avg_mae_r", "hit_1r_rate",
            "mae_le_minus_1r_rate", "giveback_after_1r_rate",
            "dead_trade_rate", "weakness_score", "severity"
        ]

        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = ""

        for r in rows:
            body += "<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in keys) + "</tr>"

        return f"<table border='1' cellspacing='0' cellpadding='5'><tr>{head}</tr>{body}</table>"

    if not data.get("ok"):
        return f"<h1>V7604 Derived Session Audit</h1><pre>{esc(data)}</pre>"

    return f"""
    <html>
    <head><title>V7604 Derived Session Audit</title></head>
    <body style="font-family:Arial, sans-serif; padding:20px;">
      <h1>V7604 Derived Session Audit</h1>

      <p>
        <b>Version:</b> {esc(data.get("version"))}<br>
        <b>Mode:</b> {esc(data.get("mode"))}<br>
        <b>Active effect:</b> {esc(data.get("active_effect"))}<br>
        <b>Live rule changes:</b> {esc(data.get("live_rule_changes"))}<br>
        <b>Auto blocking:</b> {esc(data.get("auto_blocking"))}<br>
        <b>DB writes:</b> {esc(data.get("db_writes"))}<br>
        <b>Rows seen:</b> {esc(data.get("rows_seen"))}<br>
        <b>Closed items used:</b> {esc(data.get("closed_items_used"))}
      </p>

      <h2>Timezone Policy</h2>
      <pre>{esc(data.get("timezone_policy"))}</pre>

      <h2>Timestamp Parse Notes</h2>
      <pre>{esc(data.get("timestamp_parse_notes"))}</pre>

      <h2>Overall Closed / R Available</h2>
      <pre>{esc(data.get("overall_closed_or_r_available"))}</pre>

      <h2>Weakest Sessions Closed</h2>
      {table(data.get("weakest_sessions_closed", []))}

      <h2>Strongest Sessions Closed</h2>
      {table(data.get("strongest_sessions_closed", []))}

      <h2>Weakest Berlin Hours Closed</h2>
      {table(data.get("weakest_hours_berlin_closed", []))}

      <h2>Weakest Session + Market + Side Closed</h2>
      {table(data.get("weakest_session_market_side_closed", []))}

      <h2>Weakest Session + Setup Closed</h2>
      {table(data.get("weakest_session_setup_closed", []))}

      <h2>Data Quality Findings</h2>
      <pre>{esc(data.get("data_quality_findings"))}</pre>
    </body>
    </html>
    """

@app.get("/v7604-derived-session-audit.json")
def v7604_derived_session_audit_json():
    return _V7602_JSONResponse(_v7604_build())

@app.get("/v7604-derived-session-audit")
def v7604_derived_session_audit_html():
    return _V7602_HTMLResponse(_v7604_html(_v7604_build()))

# === V7604-DERIVED-SESSION-AUDIT OBSERVE ONLY END ===

# === V7605-CROSS-CHECK-MATRIX OBSERVE ONLY START ===
# Cross-checks V7603 candidates with V7604 derived session weakness.
# Does NOT write to DB.
# Does NOT change live rules.
# Does NOT block/filter trades.

V7605_VERSION = "V7605-CROSS-CHECK-MATRIX-OBSERVE-ONLY"

def _v7605_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default

def _v7605_int(v, default=0):
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default

def _v7605_parse_pipe_key(key):
    parts = [p.strip() for p in str(key or "").split("|")]
    return parts

def _v7605_norm(s):
    return str(s or "").strip().upper()

def _v7605_v7603_candidates(v7603):
    rows = []
    for k in (
        "top_strong_review_candidates",
        "top_blocklist_review_candidates",
        "top_watch_candidates",
        "low_sample_candidates",
    ):
        rows.extend(v7603.get(k) or [])
    return rows

def _v7605_index_v7603(rows):
    idx = {
        "market_side": {},
        "source_market_side": {},
        "market": {},
        "setup": {},
        "side": {},
        "all": [],
    }

    for r in rows:
        cat = r.get("category")
        key = str(r.get("key") or "")
        idx["all"].append(r)

        if cat == "market_side":
            idx["market_side"][_v7605_norm(key)] = r

        elif cat == "source_market_side":
            parts = _v7605_parse_pipe_key(key)
            if len(parts) >= 3:
                market_side = f"{parts[-2]} {parts[-1]}"
                idx["market_side"][_v7605_norm(market_side)] = r
            idx["source_market_side"][_v7605_norm(key)] = r

        elif cat == "market":
            idx["market"][_v7605_norm(key)] = r

        elif cat == "setup":
            idx["setup"][_v7605_norm(key)] = r

        elif cat == "side":
            idx["side"][_v7605_norm(key)] = r

    return idx

def _v7605_tier_weight(tier):
    return {
        "BLOCKLIST_CANDIDATE_STRONG_REVIEW_ONLY": 4.0,
        "BLOCKLIST_CANDIDATE_REVIEW_ONLY": 3.0,
        "WEAKNESS_WATCH_REVIEW_ONLY": 2.0,
        "LOW_SAMPLE_REVIEW_ONLY": 1.0,
        "WATCH_ONLY": 0.5,
    }.get(str(tier or ""), 0.0)

def _v7605_confidence_from_n(n):
    n = _v7605_int(n)
    if n >= 50:
        return "HIGH"
    if n >= 20:
        return "MEDIUM_HIGH"
    if n >= 10:
        return "MEDIUM"
    if n >= 5:
        return "LOW_MEDIUM"
    return "LOW"

def _v7605_matrix_score(v7604_row, v7603_match=None):
    avg_r = _v7605_float(v7604_row.get("avg_r"))
    loss = _v7605_float(v7604_row.get("loss_rate"))
    mae_stop = _v7605_float(v7604_row.get("mae_le_minus_1r_rate"))
    dead = _v7605_float(v7604_row.get("dead_trade_rate"))
    weakness = _v7605_float(v7604_row.get("weakness_score"))

    score = 0.0
    score += max(0.0, -avg_r) * 2.0
    score += loss * 1.2
    score += mae_stop * 1.4
    score += dead * 0.9
    score += weakness * 0.75

    if v7603_match:
        score += _v7605_tier_weight(v7603_match.get("candidate_tier"))

    return round(score, 4)

def _v7605_make_signal(kind, v7604_row, v7603_match=None, parsed=None):
    parsed = parsed or {}

    n = _v7605_int(v7604_row.get("n"))
    avg_r = _v7605_float(v7604_row.get("avg_r"))
    loss = _v7605_float(v7604_row.get("loss_rate"))
    hit1 = _v7605_float(v7604_row.get("hit_1r_rate"))
    mae_stop = _v7605_float(v7604_row.get("mae_le_minus_1r_rate"))
    dead = _v7605_float(v7604_row.get("dead_trade_rate"))

    reasons = []

    if v7603_match:
        reasons.append("CONFIRMED_BY_V7603_CANDIDATE")

    if n < 5:
        reasons.append("LOW_SAMPLE_UNDER_5")
    elif n < 10:
        reasons.append("SMALL_SAMPLE_5_TO_9")
    else:
        reasons.append("SAMPLE_OK_10_PLUS")

    if avg_r <= -1.0:
        reasons.append("SESSION_AVG_R_BELOW_MINUS_1R")
    elif avg_r < 0:
        reasons.append("SESSION_AVG_R_NEGATIVE")

    if loss >= 0.8:
        reasons.append("SESSION_LOSS_RATE_EXTREME_80_PLUS")
    elif loss >= 0.65:
        reasons.append("SESSION_LOSS_RATE_HIGH_65_PLUS")
    elif loss >= 0.55:
        reasons.append("SESSION_LOSS_RATE_ELEVATED_55_PLUS")

    if hit1 <= 0.10:
        reasons.append("SESSION_HIT_1R_VERY_LOW")
    elif hit1 <= 0.25:
        reasons.append("SESSION_HIT_1R_LOW")

    if mae_stop >= 0.8:
        reasons.append("SESSION_MAE_STOP_RISK_EXTREME")
    elif mae_stop >= 0.6:
        reasons.append("SESSION_MAE_STOP_RISK_HIGH")
    elif mae_stop >= 0.4:
        reasons.append("SESSION_MAE_STOP_RISK_ELEVATED")

    if dead >= 0.5:
        reasons.append("SESSION_DEAD_TRADE_RATE_HIGH")

    tier = "WATCH_REVIEW_ONLY"
    action = "WATCH_NEXT_SAMPLES"

    if v7603_match and n >= 5 and avg_r <= -1.0 and loss >= 0.65:
        tier = "CROSS_CONFIRMED_STRONG_REVIEW_ONLY"
        action = "MANUAL_REVIEW_TOP_PRIORITY"
    elif v7603_match and n >= 5 and avg_r < 0 and loss >= 0.55:
        tier = "CROSS_CONFIRMED_REVIEW_ONLY"
        action = "MANUAL_REVIEW"
    elif n >= 10 and avg_r < 0 and loss >= 0.55:
        tier = "SESSION_WEAKNESS_REVIEW_ONLY"
        action = "REVIEW_SESSION_PATTERN"
    elif n < 5:
        tier = "LOW_SAMPLE_REVIEW_ONLY"
        action = "WAIT_FOR_MORE_SAMPLES"

    return {
        "kind": kind,
        "key": v7604_row.get("key"),
        "session": parsed.get("session"),
        "market": parsed.get("market"),
        "side": parsed.get("side"),
        "setup": parsed.get("setup"),
        "n": n,
        "avg_r": avg_r,
        "median_r": _v7605_float(v7604_row.get("median_r")),
        "win_rate": _v7605_float(v7604_row.get("win_rate")),
        "loss_rate": loss,
        "avg_mfe_r": _v7605_float(v7604_row.get("avg_mfe_r")),
        "avg_mae_r": _v7605_float(v7604_row.get("avg_mae_r")),
        "hit_1r_rate": hit1,
        "mae_le_minus_1r_rate": mae_stop,
        "giveback_after_1r_rate": _v7605_float(v7604_row.get("giveback_after_1r_rate")),
        "dead_trade_rate": dead,
        "v7604_severity": v7604_row.get("severity"),
        "v7604_weakness_score": _v7605_float(v7604_row.get("weakness_score")),
        "v7603_match": {
            "category": v7603_match.get("category"),
            "key": v7603_match.get("key"),
            "candidate_tier": v7603_match.get("candidate_tier"),
            "confidence": v7603_match.get("confidence"),
            "review_action": v7603_match.get("review_action"),
        } if v7603_match else None,
        "cross_matrix_score": _v7605_matrix_score(v7604_row, v7603_match),
        "cross_tier": tier,
        "confidence": _v7605_confidence_from_n(n),
        "review_action": action,
        "reasons": reasons,
        "active_effect": "NONE_REVIEW_ONLY",
    }

def _v7605_build():
    v7602 = _v7602_build()
    v7603 = _v7603_build()
    v7604 = _v7604_build()

    ok = bool(v7602.get("ok") and v7603.get("ok") and v7604.get("ok"))

    if not ok:
        return {
            "version": V7605_VERSION,
            "mode": "observe_only_cross_check_no_live_changes",
            "ok": False,
            "active_effect": "NONE",
            "live_rule_changes": False,
            "auto_blocking": False,
            "auto_filtering": False,
            "db_writes": False,
            "errors": {
                "v7602_ok": v7602.get("ok"),
                "v7603_ok": v7603.get("ok"),
                "v7604_ok": v7604.get("ok"),
            },
        }

    v7603_rows = _v7605_v7603_candidates(v7603)
    idx = _v7605_index_v7603(v7603_rows)

    signals = []

    # 1) Session + Market + Side confirmation
    for r in v7604.get("weakest_session_market_side_closed") or []:
        parts = _v7605_parse_pipe_key(r.get("key"))
        if len(parts) >= 3:
            session, market, side = parts[0], parts[1], parts[2]
            ms_key = _v7605_norm(f"{market} {side}")
            match = idx["market_side"].get(ms_key) or idx["market"].get(_v7605_norm(market)) or idx["side"].get(_v7605_norm(side))
            signals.append(_v7605_make_signal(
                "SESSION_MARKET_SIDE",
                r,
                match,
                {"session": session, "market": market, "side": side}
            ))

    # 2) Session + Side confirmation
    for r in v7604.get("weakest_session_side_closed") or []:
        parts = _v7605_parse_pipe_key(r.get("key"))
        if len(parts) >= 2:
            session, side = parts[0], parts[1]
            match = idx["side"].get(_v7605_norm(side))
            signals.append(_v7605_make_signal(
                "SESSION_SIDE",
                r,
                match,
                {"session": session, "side": side}
            ))

    # 3) Session + Setup confirmation
    for r in v7604.get("weakest_session_setup_closed") or []:
        parts = _v7605_parse_pipe_key(r.get("key"))
        if len(parts) >= 2:
            session = parts[0]
            setup = parts[1]
            match = idx["setup"].get(_v7605_norm(setup))
            signals.append(_v7605_make_signal(
                "SESSION_SETUP",
                r,
                match,
                {"session": session, "setup": setup}
            ))

    # 4) Hour + Market + Side confirmation
    for r in v7604.get("weakest_hour_market_side_closed") or []:
        parts = _v7605_parse_pipe_key(r.get("key"))
        if len(parts) >= 3:
            hour, market, side = parts[0], parts[1], parts[2]
            ms_key = _v7605_norm(f"{market} {side}")
            match = idx["market_side"].get(ms_key) or idx["market"].get(_v7605_norm(market)) or idx["side"].get(_v7605_norm(side))
            signals.append(_v7605_make_signal(
                "HOUR_MARKET_SIDE",
                r,
                match,
                {"session": hour, "market": market, "side": side}
            ))

    signals.sort(
        key=lambda x: (
            x.get("cross_matrix_score", 0),
            _v7605_int(x.get("n")),
            -_v7605_float(x.get("avg_r")),
        ),
        reverse=True,
    )

    jpy_long = [
        x for x in signals
        if "JPY" in _v7605_norm(x.get("market"))
        and _v7605_norm(x.get("side")) == "LONG"
    ]

    long_session = [
        x for x in signals
        if _v7605_norm(x.get("side")) == "LONG"
        and x.get("kind") in ("SESSION_SIDE", "SESSION_MARKET_SIDE", "HOUR_MARKET_SIDE")
    ]

    bullish_setup = [
        x for x in signals
        if x.get("setup")
        and any(token in _v7605_norm(x.get("setup")) for token in ("BULL", "LONG", "MSS", "BOS", "BPR"))
    ]

    confirmed_strong = [x for x in signals if x.get("cross_tier") == "CROSS_CONFIRMED_STRONG_REVIEW_ONLY"]
    confirmed = [x for x in signals if x.get("cross_tier") == "CROSS_CONFIRMED_REVIEW_ONLY"]
    session_only = [x for x in signals if x.get("cross_tier") == "SESSION_WEAKNESS_REVIEW_ONLY"]

    return {
        "version": V7605_VERSION,
        "mode": "observe_only_cross_check_no_live_changes",
        "ok": True,
        "active_effect": "NONE",
        "live_rule_changes": False,
        "auto_blocking": False,
        "auto_filtering": False,
        "db_writes": False,
        "source_tables": {
            "v7602_table": v7602.get("table"),
            "v7602_rows": v7602.get("rows_seen"),
            "v7603_rows": v7603.get("rows_seen"),
            "v7604_rows": v7604.get("rows_seen"),
            "v7604_closed_items_used": v7604.get("closed_items_used"),
        },
        "safety": {
            "decision_authority": "NONE_REVIEW_ONLY",
            "can_block_trades": False,
            "can_change_live_rules": False,
            "can_write_db": False,
            "requires_manual_review_before_any_future_filter": True,
        },
        "upstream_summary": {
            "v7602_overall": v7602.get("overall"),
            "v7603_summary": v7603.get("summary"),
            "v7604_overall_closed": v7604.get("overall_closed_or_r_available"),
            "v7604_raw_session_counts": v7604.get("raw_session_counts"),
        },
        "summary": {
            "signals_total": len(signals),
            "cross_confirmed_strong_review_only": len(confirmed_strong),
            "cross_confirmed_review_only": len(confirmed),
            "session_only_review_only": len(session_only),
            "jpy_long_cross_signals": len(jpy_long),
            "long_session_cross_signals": len(long_session),
            "bullish_setup_cross_signals": len(bullish_setup),
        },
        "top_cross_confirmed_risks": signals[:40],
        "jpy_long_cross_matrix": jpy_long[:25],
        "long_session_cross_matrix": long_session[:25],
        "bullish_setup_cross_matrix": bullish_setup[:25],
        "session_ranking_from_v7604": {
            "weakest_sessions_closed": v7604.get("weakest_sessions_closed"),
            "strongest_sessions_closed": v7604.get("strongest_sessions_closed"),
        },
        "interpretation": [
            "Cross-confirmed means V7604 session weakness overlaps with V7603 candidate weakness.",
            "This is still review-only evidence and has no active effect.",
            "JPY LONG and LONG-by-session weakness should be manually reviewed first.",
            "Do not activate filters until sample size and V7601 deep intelligence agree.",
        ],
        "safe_next_steps": [
            "Review top_cross_confirmed_risks.",
            "If stable, build V7606 review-only proposed filter report.",
            "Keep all proposed filters disabled by default.",
            "Only after manual approval and larger samples should any rule be considered.",
        ],
    }

def _v7605_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows):
        if not rows:
            return "<p>No rows.</p>"

        keys = [
            "kind", "key", "cross_tier", "confidence", "n",
            "avg_r", "median_r", "loss_rate", "hit_1r_rate",
            "mae_le_minus_1r_rate", "dead_trade_rate",
            "cross_matrix_score", "review_action", "active_effect"
        ]

        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = ""

        for r in rows:
            body += "<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in keys) + "</tr>"

        return f"<table border='1' cellspacing='0' cellpadding='5'><tr>{head}</tr>{body}</table>"

    if not data.get("ok"):
        return f"<h1>V7605 Cross Check Matrix</h1><pre>{esc(data)}</pre>"

    return f"""
    <html>
    <head><title>V7605 Cross Check Matrix</title></head>
    <body style="font-family:Arial, sans-serif; padding:20px;">
      <h1>V7605 Cross Check Matrix</h1>

      <p>
        <b>Version:</b> {esc(data.get("version"))}<br>
        <b>Mode:</b> {esc(data.get("mode"))}<br>
        <b>Active effect:</b> {esc(data.get("active_effect"))}<br>
        <b>Live rule changes:</b> {esc(data.get("live_rule_changes"))}<br>
        <b>Auto blocking:</b> {esc(data.get("auto_blocking"))}<br>
        <b>DB writes:</b> {esc(data.get("db_writes"))}
      </p>

      <h2>Summary</h2>
      <pre>{esc(data.get("summary"))}</pre>

      <h2>Safety</h2>
      <pre>{esc(data.get("safety"))}</pre>

      <h2>Top Cross Confirmed Risks</h2>
      {table(data.get("top_cross_confirmed_risks", []))}

      <h2>JPY Long Cross Matrix</h2>
      {table(data.get("jpy_long_cross_matrix", []))}

      <h2>Long Session Cross Matrix</h2>
      {table(data.get("long_session_cross_matrix", []))}

      <h2>Bullish Setup Cross Matrix</h2>
      {table(data.get("bullish_setup_cross_matrix", []))}
    </body>
    </html>
    """

@app.get("/v7605-cross-check-matrix.json")
def v7605_cross_check_matrix_json():
    return _V7602_JSONResponse(_v7605_build())

@app.get("/v7605-cross-check-matrix")
def v7605_cross_check_matrix_html():
    return _V7602_HTMLResponse(_v7605_html(_v7605_build()))

# === V7605-CROSS-CHECK-MATRIX OBSERVE ONLY END ===

# === V7606-PROPOSED-FILTER-REPORT OBSERVE ONLY START ===
# Converts V7605 cross-check risks into disabled review-only filter proposals.
# Does NOT write to DB.
# Does NOT change live rules.
# Does NOT block/filter trades.
# All proposals are disabled by default.

V7606_VERSION = "V7606-PROPOSED-FILTER-REPORT-OBSERVE-ONLY"

def _v7606_norm(s):
    return str(s or "").strip().upper()

def _v7606_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default

def _v7606_int(v, default=0):
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default

def _v7606_proposal_type(signal):
    kind = signal.get("kind")
    market = signal.get("market")
    side = signal.get("side")
    setup = signal.get("setup")
    session = signal.get("session")

    if kind == "SESSION_MARKET_SIDE" and market and side:
        return "SESSION_MARKET_SIDE_FILTER_PROPOSAL"

    if kind == "SESSION_SIDE" and session and side:
        return "SESSION_SIDE_FILTER_PROPOSAL"

    if kind == "SESSION_SETUP" and setup:
        return "SESSION_SETUP_FILTER_PROPOSAL"

    if kind == "HOUR_MARKET_SIDE" and market and side:
        return "HOUR_MARKET_SIDE_FILTER_PROPOSAL"

    return "GENERAL_REVIEW_PROPOSAL"

def _v7606_scope(signal):
    return {
        "session_or_hour": signal.get("session"),
        "market": signal.get("market"),
        "side": signal.get("side"),
        "setup": signal.get("setup"),
        "source_key": signal.get("key"),
        "signal_kind": signal.get("kind"),
    }

def _v7606_severity(signal):
    n = _v7606_int(signal.get("n"))
    avg_r = _v7606_float(signal.get("avg_r"))
    loss = _v7606_float(signal.get("loss_rate"))
    mae_stop = _v7606_float(signal.get("mae_le_minus_1r_rate"))
    hit1 = _v7606_float(signal.get("hit_1r_rate"))
    score = _v7606_float(signal.get("cross_matrix_score"))

    if n >= 5 and avg_r <= -1.0 and loss >= 0.75 and mae_stop >= 0.75:
        return "SEVERE_REVIEW_PRIORITY"

    if n >= 10 and avg_r <= -1.0 and loss >= 0.60:
        return "HIGH_REVIEW_PRIORITY"

    if score >= 12:
        return "HIGH_REVIEW_PRIORITY"

    if avg_r < 0 and loss >= 0.55:
        return "MEDIUM_REVIEW_PRIORITY"

    if n < 5:
        return "LOW_SAMPLE_REVIEW_PRIORITY"

    if hit1 <= 0.25 and loss >= 0.65:
        return "MEDIUM_REVIEW_PRIORITY"

    return "WATCH_REVIEW_PRIORITY"

def _v7606_sample_warning(signal):
    n = _v7606_int(signal.get("n"))

    if n < 5:
        return "LOW_SAMPLE_DO_NOT_USE_FOR_FILTER_DECISION"
    if n < 10:
        return "SMALL_SAMPLE_NEEDS_MORE_CONFIRMATION"
    if n < 30:
        return "MEDIUM_SAMPLE_REVIEW_REQUIRED"
    return "LARGER_SAMPLE_STILL_REVIEW_ONLY"

def _v7606_filter_sentence(signal):
    proposal_type = _v7606_proposal_type(signal)
    scope = _v7606_scope(signal)

    session = scope.get("session_or_hour")
    market = scope.get("market")
    side = scope.get("side")
    setup = scope.get("setup")

    if proposal_type == "SESSION_MARKET_SIDE_FILTER_PROPOSAL":
        return f"Review whether {market} {side} should be avoided during {session}."

    if proposal_type == "SESSION_SIDE_FILTER_PROPOSAL":
        return f"Review whether {side} trades should be avoided during {session}."

    if proposal_type == "SESSION_SETUP_FILTER_PROPOSAL":
        return f"Review whether setup {setup} should be avoided during {session}."

    if proposal_type == "HOUR_MARKET_SIDE_FILTER_PROPOSAL":
        return f"Review whether {market} {side} should be avoided around {session}."

    return "Review this weakness cluster manually before any future rule proposal."

def _v7606_make_proposal(signal, rank):
    v7603 = signal.get("v7603_match") or {}

    proposal = {
        "rank": rank,
        "proposal_id": f"V7606-PROP-{rank:03d}",
        "proposal_type": _v7606_proposal_type(signal),
        "proposal_text": _v7606_filter_sentence(signal),
        "scope": _v7606_scope(signal),
        "evidence": {
            "n": _v7606_int(signal.get("n")),
            "avg_r": _v7606_float(signal.get("avg_r")),
            "median_r": _v7606_float(signal.get("median_r")),
            "win_rate": _v7606_float(signal.get("win_rate")),
            "loss_rate": _v7606_float(signal.get("loss_rate")),
            "hit_1r_rate": _v7606_float(signal.get("hit_1r_rate")),
            "mae_le_minus_1r_rate": _v7606_float(signal.get("mae_le_minus_1r_rate")),
            "dead_trade_rate": _v7606_float(signal.get("dead_trade_rate")),
            "cross_matrix_score": _v7606_float(signal.get("cross_matrix_score")),
            "cross_tier": signal.get("cross_tier"),
            "confidence": signal.get("confidence"),
            "v7603_match": v7603,
            "reasons": signal.get("reasons") or [],
        },
        "review_priority": _v7606_severity(signal),
        "sample_warning": _v7606_sample_warning(signal),
        "proposed_filter_enabled": False,
        "active_effect": "NONE_REVIEW_ONLY",
        "requires_manual_approval": True,
        "requires_larger_sample": _v7606_int(signal.get("n")) < 10,
        "requires_v7601_confirmation": True,
        "allowed_action_now": "REVIEW_ONLY",
        "forbidden_actions_now": [
            "DO_NOT_BLOCK_TRADES",
            "DO_NOT_CHANGE_LIVE_RULES",
            "DO_NOT_AUTO_FILTER",
            "DO_NOT_WRITE_DB",
        ],
    }

    return proposal

def _v7606_dedupe(proposals):
    seen = set()
    out = []

    for p in proposals:
        scope = p.get("scope") or {}
        key = (
            p.get("proposal_type"),
            scope.get("session_or_hour"),
            scope.get("market"),
            scope.get("side"),
            scope.get("setup"),
        )

        if key in seen:
            continue

        seen.add(key)
        out.append(p)

    for i, p in enumerate(out, 1):
        p["rank"] = i
        p["proposal_id"] = f"V7606-PROP-{i:03d}"

    return out

def _v7606_build():
    base = _v7605_build()

    if not base.get("ok"):
        return {
            "version": V7606_VERSION,
            "mode": "observe_only_disabled_filter_proposals",
            "ok": False,
            "active_effect": "NONE",
            "live_rule_changes": False,
            "auto_blocking": False,
            "auto_filtering": False,
            "db_writes": False,
            "error": "V7605 base build failed. Cannot create review-only proposals.",
            "v7605_ok": base.get("ok"),
        }

    signals = base.get("top_cross_confirmed_risks") or []

    eligible = []

    for s in signals:
        tier = str(s.get("cross_tier") or "")
        n = _v7606_int(s.get("n"))
        avg_r = _v7606_float(s.get("avg_r"))
        loss = _v7606_float(s.get("loss_rate"))
        score = _v7606_float(s.get("cross_matrix_score"))

        if "CROSS_CONFIRMED" in tier:
            eligible.append(s)
        elif score >= 12 and avg_r < 0:
            eligible.append(s)
        elif n >= 10 and avg_r < 0 and loss >= 0.55:
            eligible.append(s)

    proposals = [_v7606_make_proposal(s, i + 1) for i, s in enumerate(eligible[:60])]
    proposals = _v7606_dedupe(proposals)

    severe = [p for p in proposals if p.get("review_priority") == "SEVERE_REVIEW_PRIORITY"]
    high = [p for p in proposals if p.get("review_priority") == "HIGH_REVIEW_PRIORITY"]
    medium = [p for p in proposals if p.get("review_priority") == "MEDIUM_REVIEW_PRIORITY"]
    low_sample = [p for p in proposals if "LOW_SAMPLE" in str(p.get("sample_warning"))]

    jpy_long = [
        p for p in proposals
        if "JPY" in _v7606_norm((p.get("scope") or {}).get("market"))
        and _v7606_norm((p.get("scope") or {}).get("side")) == "LONG"
    ]

    long_side = [
        p for p in proposals
        if _v7606_norm((p.get("scope") or {}).get("side")) == "LONG"
    ]

    bullish_setup = [
        p for p in proposals
        if any(
            token in _v7606_norm((p.get("scope") or {}).get("setup"))
            for token in ("BULL", "LONG", "MSS", "BOS", "BPR")
        )
    ]

    return {
        "version": V7606_VERSION,
        "mode": "observe_only_disabled_filter_proposals",
        "ok": True,
        "active_effect": "NONE",
        "live_rule_changes": False,
        "auto_blocking": False,
        "auto_filtering": False,
        "db_writes": False,
        "source": {
            "based_on": "V7605 cross-check matrix",
            "v7605_summary": base.get("summary"),
            "source_tables": base.get("source_tables"),
        },
        "safety": {
            "all_filters_disabled": True,
            "proposed_filter_enabled_default": False,
            "decision_authority": "NONE_REVIEW_ONLY",
            "requires_manual_approval": True,
            "requires_v7601_confirmation": True,
            "can_block_trades": False,
            "can_change_live_rules": False,
            "can_write_db": False,
        },
        "summary": {
            "proposals_total": len(proposals),
            "severe_review_priority": len(severe),
            "high_review_priority": len(high),
            "medium_review_priority": len(medium),
            "low_sample_warning": len(low_sample),
            "jpy_long_proposals": len(jpy_long),
            "long_side_proposals": len(long_side),
            "bullish_setup_proposals": len(bullish_setup),
        },
        "top_proposals": proposals[:40],
        "severe_priority_proposals": severe[:25],
        "high_priority_proposals": high[:25],
        "jpy_long_proposals": jpy_long[:25],
        "long_side_proposals": long_side[:25],
        "bullish_setup_proposals": bullish_setup[:25],
        "manual_review_checklist": [
            "Verify V7601 Deep Intelligence agrees with each proposal.",
            "Verify sample size is not too small.",
            "Check whether losses come from bad direction, bad session, bad setup, or exit/giveback.",
            "Do not activate any filter from V7606 automatically.",
            "Only create a future active rule after manual approval and larger closed sample.",
        ],
        "safe_next_steps": [
            "Review top_proposals.",
            "Run a compact sample-detail audit for the top 5 proposal clusters.",
            "Build V7607 Top Cluster Trade Detail Viewer, still observe-only.",
        ],
    }

def _v7606_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows):
        if not rows:
            return "<p>No rows.</p>"

        keys = [
            "rank", "proposal_id", "proposal_type", "proposal_text",
            "review_priority", "sample_warning", "proposed_filter_enabled",
            "active_effect", "requires_manual_approval"
        ]

        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = ""

        for r in rows:
            body += "<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in keys) + "</tr>"

        return f"<table border='1' cellspacing='0' cellpadding='5'><tr>{head}</tr>{body}</table>"

    if not data.get("ok"):
        return f"<h1>V7606 Proposed Filter Report</h1><pre>{esc(data)}</pre>"

    return f"""
    <html>
    <head><title>V7606 Proposed Filter Report</title></head>
    <body style="font-family:Arial, sans-serif; padding:20px;">
      <h1>V7606 Proposed Filter Report</h1>

      <p>
        <b>Version:</b> {esc(data.get("version"))}<br>
        <b>Mode:</b> {esc(data.get("mode"))}<br>
        <b>Active effect:</b> {esc(data.get("active_effect"))}<br>
        <b>Live rule changes:</b> {esc(data.get("live_rule_changes"))}<br>
        <b>Auto blocking:</b> {esc(data.get("auto_blocking"))}<br>
        <b>DB writes:</b> {esc(data.get("db_writes"))}
      </p>

      <h2>Safety</h2>
      <pre>{esc(data.get("safety"))}</pre>

      <h2>Summary</h2>
      <pre>{esc(data.get("summary"))}</pre>

      <h2>Top Disabled Proposals</h2>
      {table(data.get("top_proposals", []))}

      <h2>JPY Long Proposals</h2>
      {table(data.get("jpy_long_proposals", []))}

      <h2>Long Side Proposals</h2>
      {table(data.get("long_side_proposals", []))}

      <h2>Bullish Setup Proposals</h2>
      {table(data.get("bullish_setup_proposals", []))}

      <h2>Manual Review Checklist</h2>
      <pre>{esc(data.get("manual_review_checklist"))}</pre>
    </body>
    </html>
    """

@app.get("/v7606-proposed-filter-report.json")
def v7606_proposed_filter_report_json():
    return _V7602_JSONResponse(_v7606_build())

@app.get("/v7606-proposed-filter-report")
def v7606_proposed_filter_report_html():
    return _V7602_HTMLResponse(_v7606_html(_v7606_build()))

# === V7606-PROPOSED-FILTER-REPORT OBSERVE ONLY END ===

# === V7607-TOP-CLUSTER-TRADE-DETAIL-VIEWER OBSERVE ONLY START ===
# Shows individual trade/excursion rows behind V7606 top proposals.
# Does NOT write to DB.
# Does NOT change live rules.
# Does NOT block/filter trades.

V7607_VERSION = "V7607-TOP-CLUSTER-TRADE-DETAIL-VIEWER-OBSERVE-ONLY"

def _v7607_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default

def _v7607_int(v, default=0):
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default

def _v7607_norm(s):
    return str(s or "").strip().upper()

def _v7607_short(v, max_len=140):
    s = str(v or "")
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s

def _v7607_status_closed(status):
    s = _v7607_norm(status)
    return "CLOSED" in s or "DONE" in s or "EXIT" in s or "WIN" in s or "LOSS" in s

def _v7607_derive_time(row, opened_col):
    raw = row.get(opened_col) if opened_col else None
    dt_utc, parse_note = _v7604_parse_dt(raw)
    dt_berlin = _v7604_to_berlin(dt_utc)

    return {
        "opened_raw": raw,
        "parse_note": parse_note,
        "session_berlin": _v7604_session_berlin(dt_berlin),
        "hour_berlin": _v7604_hour_bucket(dt_berlin, "BERLIN"),
        "weekday_berlin": _v7604_weekday_bucket(dt_berlin),
    }

def _v7607_row_to_item(row, cols):
    t = _v7607_derive_time(row, cols.get("opened_at"))

    item = {
        "id": row.get(cols.get("id")) if cols.get("id") else None,
        "source": _v7602_val(row, cols.get("source")),
        "trade_key": row.get(cols.get("trade_key")) if cols.get("trade_key") else None,
        "source_id": row.get(cols.get("source_id")) if cols.get("source_id") else None,
        "client_trade_id": row.get(cols.get("client_trade_id")) if cols.get("client_trade_id") else None,

        "market": _v7602_val(row, cols.get("market")),
        "side": _v7602_val(row, cols.get("side")),
        "setup": _v7602_val(row, cols.get("setup")),
        "status": _v7602_val(row, cols.get("status")),

        "opened_at": t.get("opened_raw"),
        "session_berlin": t.get("session_berlin"),
        "hour_berlin": t.get("hour_berlin"),
        "weekday_berlin": t.get("weekday_berlin"),
        "timestamp_parse_note": t.get("parse_note"),

        "entry": _v7607_float(row.get(cols.get("entry"))) if cols.get("entry") else None,
        "sl": _v7607_float(row.get(cols.get("sl"))) if cols.get("sl") else None,
        "tp1": _v7607_float(row.get(cols.get("tp1"))) if cols.get("tp1") else None,
        "risk_points": _v7607_float(row.get(cols.get("risk_points"))) if cols.get("risk_points") else None,
        "current_price": _v7607_float(row.get(cols.get("current_price"))) if cols.get("current_price") else None,

        "current_r": _v7607_float(row.get(cols.get("current_r"))) if cols.get("current_r") else None,
        "final_r": _v7607_float(row.get(cols.get("final_r"))) if cols.get("final_r") else None,
        "max_favorable_r": _v7607_float(row.get(cols.get("mfe"))) if cols.get("mfe") else None,
        "max_adverse_r": _v7607_float(row.get(cols.get("mae"))) if cols.get("mae") else None,

        "mfe_price": _v7607_float(row.get(cols.get("mfe_price"))) if cols.get("mfe_price") else None,
        "mae_price": _v7607_float(row.get(cols.get("mae_price"))) if cols.get("mae_price") else None,
        "mfe_at": row.get(cols.get("mfe_at")) if cols.get("mfe_at") else None,
        "mae_at": row.get(cols.get("mae_at")) if cols.get("mae_at") else None,

        "hit_0_5r": row.get(cols.get("hit_0_5r")) if cols.get("hit_0_5r") else None,
        "hit_1r": row.get(cols.get("hit_1r")) if cols.get("hit_1r") else None,
        "hit_1_5r": row.get(cols.get("hit_1_5r")) if cols.get("hit_1_5r") else None,
        "hit_2r": row.get(cols.get("hit_2r")) if cols.get("hit_2r") else None,
        "hit_3r": row.get(cols.get("hit_3r")) if cols.get("hit_3r") else None,

        "final_status": row.get(cols.get("final_status")) if cols.get("final_status") else None,
        "first_seen_at": row.get(cols.get("first_seen_at")) if cols.get("first_seen_at") else None,
        "last_update_at": row.get(cols.get("last_update_at")) if cols.get("last_update_at") else None,
        "updates": row.get(cols.get("updates")) if cols.get("updates") else None,
    }

    item["is_closed"] = _v7607_status_closed(item.get("status"))
    item["r_for_stats"] = item.get("final_r") if item.get("final_r") is not None else item.get("current_r")

    return item

def _v7607_match_scope(item, scope):
    session_or_hour = scope.get("session_or_hour")
    market = scope.get("market")
    side = scope.get("side")
    setup = scope.get("setup")

    if session_or_hour:
        so = str(session_or_hour)
        if so.startswith("BERLIN_"):
            if _v7607_norm(item.get("hour_berlin")) != _v7607_norm(so):
                return False
        else:
            if _v7607_norm(item.get("session_berlin")) != _v7607_norm(so):
                return False

    if market and _v7607_norm(item.get("market")) != _v7607_norm(market):
        return False

    if side and _v7607_norm(item.get("side")) != _v7607_norm(side):
        return False

    if setup and _v7607_norm(item.get("setup")) != _v7607_norm(setup):
        return False

    return True

def _v7607_cluster_stats(items):
    if not items:
        return {}

    r_vals = [x.get("r_for_stats") for x in items if x.get("r_for_stats") is not None]
    mfe_vals = [x.get("max_favorable_r") for x in items if x.get("max_favorable_r") is not None]
    mae_vals = [x.get("max_adverse_r") for x in items if x.get("max_adverse_r") is not None]

    def avg(vals):
        return round(sum(vals) / len(vals), 4) if vals else None

    n = len(items)

    hit1_count = 0
    giveback_count = 0
    dead_count = 0
    stop_risk_count = 0
    no_follow_count = 0

    for x in items:
        mfe = x.get("max_favorable_r")
        mae = x.get("max_adverse_r")
        r = x.get("r_for_stats")

        hit1 = False
        try:
            hit1 = bool(int(x.get("hit_1r") or 0))
        except Exception:
            hit1 = bool(mfe is not None and mfe >= 1.0)

        if hit1:
            hit1_count += 1

        if mae is not None and mae <= -1.0:
            stop_risk_count += 1

        if mfe is not None and mfe < 0.35:
            no_follow_count += 1

        if mfe is not None and mae is not None and mfe < 0.35 and mae <= -0.50:
            dead_count += 1

        if mfe is not None and r is not None and mfe >= 1.0 and r < 0.25:
            giveback_count += 1

    return {
        "n": n,
        "closed_n": sum(1 for x in items if x.get("is_closed")),
        "avg_r": avg(r_vals),
        "min_r": round(min(r_vals), 4) if r_vals else None,
        "max_r": round(max(r_vals), 4) if r_vals else None,
        "loss_rate": round(sum(1 for x in r_vals if x < 0) / len(r_vals), 4) if r_vals else None,
        "win_rate": round(sum(1 for x in r_vals if x > 0) / len(r_vals), 4) if r_vals else None,
        "avg_mfe_r": avg(mfe_vals),
        "avg_mae_r": avg(mae_vals),
        "hit_1r_rate": round(hit1_count / n, 4) if n else None,
        "mae_le_minus_1r_rate": round(stop_risk_count / n, 4) if n else None,
        "no_follow_through_rate": round(no_follow_count / n, 4) if n else None,
        "dead_trade_rate": round(dead_count / n, 4) if n else None,
        "giveback_after_1r_rate": round(giveback_count / n, 4) if n else None,
    }

def _v7607_root_cause(stats):
    causes = []

    if not stats:
        return ["NO_MATCHING_TRADES_FOUND"]

    if stats.get("mae_le_minus_1r_rate") is not None and stats.get("mae_le_minus_1r_rate") >= 0.75:
        causes.append("STOP_RISK_TOO_HIGH")

    if stats.get("hit_1r_rate") is not None and stats.get("hit_1r_rate") <= 0.20:
        causes.append("NO_1R_FOLLOW_THROUGH")

    if stats.get("no_follow_through_rate") is not None and stats.get("no_follow_through_rate") >= 0.50:
        causes.append("LOW_MFE_NO_FOLLOW_THROUGH")

    if stats.get("dead_trade_rate") is not None and stats.get("dead_trade_rate") >= 0.50:
        causes.append("DEAD_TRADE_PATTERN")

    if stats.get("giveback_after_1r_rate") is not None and stats.get("giveback_after_1r_rate") >= 0.40:
        causes.append("GIVEBACK_AFTER_1R")

    if stats.get("loss_rate") is not None and stats.get("loss_rate") >= 0.80:
        causes.append("DIRECTION_OR_CONTEXT_FAILURE")

    if stats.get("avg_r") is not None and stats.get("avg_r") <= -1.0:
        causes.append("NEGATIVE_EXPECTANCY_SEVERE")

    if not causes:
        causes.append("MIXED_WEAKNESS_REVIEW_REQUIRED")

    return causes

def _v7607_build():
    v7606 = _v7606_build()

    if not v7606.get("ok"):
        return {
            "version": V7607_VERSION,
            "mode": "observe_only_trade_detail_viewer",
            "ok": False,
            "active_effect": "NONE",
            "live_rule_changes": False,
            "auto_blocking": False,
            "auto_filtering": False,
            "db_writes": False,
            "error": "V7606 base build failed. Cannot build detail viewer.",
        }

    db_path, checked = _v7602_find_db()

    if not db_path:
        return {
            "version": V7607_VERSION,
            "mode": "observe_only_trade_detail_viewer",
            "ok": False,
            "active_effect": "NONE",
            "live_rule_changes": False,
            "auto_blocking": False,
            "auto_filtering": False,
            "db_writes": False,
            "error": "DB/table not found.",
            "checked": checked,
        }

    con = _v7602_connect_ro(db_path)
    con.row_factory = _v7602_sqlite3.Row
    cur = con.cursor()

    cur.execute(f"PRAGMA table_info({V7602_TABLE})")
    raw_cols = [r[1] for r in cur.fetchall()]

    cols = {
        "id": _v7602_pick(raw_cols, ["id"]),
        "source": _v7602_pick(raw_cols, ["source"]),
        "trade_key": _v7602_pick(raw_cols, ["trade_key"]),
        "source_id": _v7602_pick(raw_cols, ["source_id"]),
        "client_trade_id": _v7602_pick(raw_cols, ["client_trade_id"]),

        "market": _v7602_pick(raw_cols, ["market", "symbol", "instrument", "ticker", "pair"]),
        "side": _v7602_pick(raw_cols, ["direction", "side", "trade_side", "dir"]),
        "setup": _v7602_pick(raw_cols, ["setup_name", "setup", "strategy", "signal_name", "signal_type"]),
        "status": _v7602_pick(raw_cols, ["status", "state", "trade_status"]),
        "opened_at": _v7602_pick(raw_cols, ["opened_at", "entry_time", "created_at", "timestamp", "ts", "time"]),

        "entry": _v7602_pick(raw_cols, ["entry"]),
        "sl": _v7602_pick(raw_cols, ["sl", "stop", "stop_loss"]),
        "tp1": _v7602_pick(raw_cols, ["tp1", "target", "take_profit"]),
        "risk_points": _v7602_pick(raw_cols, ["risk_points", "risk"]),
        "current_price": _v7602_pick(raw_cols, ["current_price", "price"]),

        "current_r": _v7602_pick(raw_cols, ["current_r"]),
        "final_r": _v7602_pick(raw_cols, ["final_r", "realized_r", "result_r", "pnl_r", "closed_r"]),
        "mfe": _v7602_pick(raw_cols, ["max_favorable_r", "mfe_r", "max_r", "best_r"]),
        "mae": _v7602_pick(raw_cols, ["max_adverse_r", "mae_r", "min_r", "worst_r"]),

        "mfe_price": _v7602_pick(raw_cols, ["mfe_price"]),
        "mae_price": _v7602_pick(raw_cols, ["mae_price"]),
        "mfe_at": _v7602_pick(raw_cols, ["mfe_at"]),
        "mae_at": _v7602_pick(raw_cols, ["mae_at"]),

        "hit_0_5r": _v7602_pick(raw_cols, ["hit_0_5r"]),
        "hit_1r": _v7602_pick(raw_cols, ["hit_1r", "hit1r"]),
        "hit_1_5r": _v7602_pick(raw_cols, ["hit_1_5r"]),
        "hit_2r": _v7602_pick(raw_cols, ["hit_2r"]),
        "hit_3r": _v7602_pick(raw_cols, ["hit_3r"]),

        "final_status": _v7602_pick(raw_cols, ["final_status"]),
        "first_seen_at": _v7602_pick(raw_cols, ["first_seen_at"]),
        "last_update_at": _v7602_pick(raw_cols, ["last_update_at"]),
        "updates": _v7602_pick(raw_cols, ["updates"]),
    }

    cur.execute(f"SELECT * FROM {V7602_TABLE} LIMIT 50000")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()

    items = [_v7607_row_to_item(row, cols) for row in rows]

    proposals = v7606.get("top_proposals") or []
    clusters = []

    for p in proposals[:15]:
        scope = p.get("scope") or {}
        matches = [x for x in items if _v7607_match_scope(x, scope)]

        matches_sorted = sorted(
            matches,
            key=lambda x: (
                x.get("r_for_stats") if x.get("r_for_stats") is not None else 999,
                x.get("opened_at") or "",
            )
        )

        stats = _v7607_cluster_stats(matches_sorted)
        root_causes = _v7607_root_cause(stats)

        compact_trades = []

        for x in matches_sorted[:30]:
            compact_trades.append({
                "id": x.get("id"),
                "source": x.get("source"),
                "trade_key": _v7607_short(x.get("trade_key"), 90),
                "market": x.get("market"),
                "side": x.get("side"),
                "setup": x.get("setup"),
                "status": x.get("status"),
                "opened_at": x.get("opened_at"),
                "session_berlin": x.get("session_berlin"),
                "hour_berlin": x.get("hour_berlin"),
                "entry": x.get("entry"),
                "sl": x.get("sl"),
                "tp1": x.get("tp1"),
                "risk_points": x.get("risk_points"),
                "current_r": x.get("current_r"),
                "final_r": x.get("final_r"),
                "r_for_stats": x.get("r_for_stats"),
                "max_favorable_r": x.get("max_favorable_r"),
                "max_adverse_r": x.get("max_adverse_r"),
                "hit_0_5r": x.get("hit_0_5r"),
                "hit_1r": x.get("hit_1r"),
                "hit_2r": x.get("hit_2r"),
                "hit_3r": x.get("hit_3r"),
                "mfe_at": x.get("mfe_at"),
                "mae_at": x.get("mae_at"),
                "last_update_at": x.get("last_update_at"),
                "updates": x.get("updates"),
            })

        clusters.append({
            "proposal_id": p.get("proposal_id"),
            "rank": p.get("rank"),
            "proposal_type": p.get("proposal_type"),
            "proposal_text": p.get("proposal_text"),
            "review_priority": p.get("review_priority"),
            "sample_warning": p.get("sample_warning"),
            "proposed_filter_enabled": p.get("proposed_filter_enabled"),
            "active_effect": "NONE_REVIEW_ONLY",
            "scope": scope,
            "proposal_evidence": p.get("evidence"),
            "matched_trades": len(matches_sorted),
            "cluster_stats": stats,
            "root_cause_hypothesis": root_causes,
            "worst_trades_first": compact_trades,
        })

    return {
        "version": V7607_VERSION,
        "mode": "observe_only_trade_detail_viewer",
        "ok": True,
        "active_effect": "NONE",
        "live_rule_changes": False,
        "auto_blocking": False,
        "auto_filtering": False,
        "db_writes": False,
        "db_path": db_path,
        "table": V7602_TABLE,
        "rows_seen": len(rows),
        "columns_used": cols,
        "source": {
            "based_on": "V7606 disabled review-only proposals",
            "v7606_summary": v7606.get("summary"),
        },
        "safety": {
            "read_only": True,
            "trade_details_only": True,
            "can_block_trades": False,
            "can_change_live_rules": False,
            "can_write_db": False,
            "all_filter_proposals_still_disabled": True,
        },
        "clusters_reviewed": len(clusters),
        "top_cluster_details": clusters,
        "interpretation": [
            "This module shows the individual rows behind proposed filters.",
            "Root-cause labels are hypotheses from MFE/MAE/R only.",
            "Use this to decide whether weakness is direction, timing, setup, stop-risk, no-follow-through, or giveback.",
            "No proposal is activated by this module.",
        ],
        "safe_next_steps": [
            "Review top_cluster_details for repeated patterns.",
            "Compare root_cause_hypothesis with V7601 Deep Intelligence.",
            "If the same cause repeats, build V7608 Root Cause Summary, still observe-only.",
        ],
    }

def _v7607_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def cluster_block(c):
        trades = c.get("worst_trades_first") or []

        rows = ""
        for t in trades[:20]:
            rows += (
                "<tr>"
                f"<td>{esc(t.get('id'))}</td>"
                f"<td>{esc(t.get('market'))}</td>"
                f"<td>{esc(t.get('side'))}</td>"
                f"<td>{esc(t.get('setup'))}</td>"
                f"<td>{esc(t.get('opened_at'))}</td>"
                f"<td>{esc(t.get('session_berlin'))}</td>"
                f"<td>{esc(t.get('hour_berlin'))}</td>"
                f"<td>{esc(t.get('final_r'))}</td>"
                f"<td>{esc(t.get('current_r'))}</td>"
                f"<td>{esc(t.get('max_favorable_r'))}</td>"
                f"<td>{esc(t.get('max_adverse_r'))}</td>"
                f"<td>{esc(t.get('hit_1r'))}</td>"
                f"<td>{esc(t.get('status'))}</td>"
                "</tr>"
            )

        return f"""
        <h2>{esc(c.get("proposal_id"))}: {esc(c.get("proposal_text"))}</h2>
        <p>
          <b>Priority:</b> {esc(c.get("review_priority"))}<br>
          <b>Warning:</b> {esc(c.get("sample_warning"))}<br>
          <b>Enabled:</b> {esc(c.get("proposed_filter_enabled"))}<br>
          <b>Active effect:</b> {esc(c.get("active_effect"))}<br>
          <b>Matched trades:</b> {esc(c.get("matched_trades"))}
        </p>
        <h3>Cluster Stats</h3>
        <pre>{esc(c.get("cluster_stats"))}</pre>
        <h3>Root Cause Hypothesis</h3>
        <pre>{esc(c.get("root_cause_hypothesis"))}</pre>
        <h3>Worst Trades First</h3>
        <table border='1' cellspacing='0' cellpadding='5'>
          <tr>
            <th>id</th><th>market</th><th>side</th><th>setup</th><th>opened_at</th>
            <th>session</th><th>hour</th><th>final_r</th><th>current_r</th>
            <th>MFE</th><th>MAE</th><th>hit_1r</th><th>status</th>
          </tr>
          {rows}
        </table>
        """

    if not data.get("ok"):
        return f"<h1>V7607 Top Cluster Trade Detail Viewer</h1><pre>{esc(data)}</pre>"

    body = "".join(cluster_block(c) for c in data.get("top_cluster_details", []))

    return f"""
    <html>
    <head><title>V7607 Top Cluster Trade Detail Viewer</title></head>
    <body style="font-family:Arial, sans-serif; padding:20px;">
      <h1>V7607 Top Cluster Trade Detail Viewer</h1>

      <p>
        <b>Version:</b> {esc(data.get("version"))}<br>
        <b>Mode:</b> {esc(data.get("mode"))}<br>
        <b>Active effect:</b> {esc(data.get("active_effect"))}<br>
        <b>Live rule changes:</b> {esc(data.get("live_rule_changes"))}<br>
        <b>Auto blocking:</b> {esc(data.get("auto_blocking"))}<br>
        <b>DB writes:</b> {esc(data.get("db_writes"))}<br>
        <b>Rows seen:</b> {esc(data.get("rows_seen"))}
      </p>

      <h2>Safety</h2>
      <pre>{esc(data.get("safety"))}</pre>

      {body}
    </body>
    </html>
    """

@app.get("/v7607-top-cluster-trade-detail.json")
def v7607_top_cluster_trade_detail_json():
    return _V7602_JSONResponse(_v7607_build())

@app.get("/v7607-top-cluster-trade-detail")
def v7607_top_cluster_trade_detail_html():
    return _V7602_HTMLResponse(_v7607_html(_v7607_build()))

# === V7607-TOP-CLUSTER-TRADE-DETAIL-VIEWER OBSERVE ONLY END ===

# === V7608-ROOT-CAUSE-SUMMARY OBSERVE ONLY START ===
# Aggregates V7607 root-cause hypotheses across reviewed clusters.
# Does NOT write to DB.
# Does NOT change live rules.
# Does NOT block/filter trades.

V7608_VERSION = "V7608-ROOT-CAUSE-SUMMARY-OBSERVE-ONLY"

def _v7608_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default

def _v7608_int(v, default=0):
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default

def _v7608_norm(s):
    return str(s or "").strip().upper()

def _v7608_avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None

def _v7608_make_bucket(rows, key_name):
    groups = {}

    for c in rows:
        key = c.get(key_name) or "UNKNOWN"
        if key not in groups:
            groups[key] = []
        groups[key].append(c)

    out = []

    for key, arr in groups.items():
        stats = [x.get("cluster_stats") or {} for x in arr]

        out.append({
            "key": key,
            "clusters": len(arr),
            "total_matched_trades": sum(_v7608_int(x.get("matched_trades")) for x in arr),
            "avg_cluster_r": _v7608_avg([_v7608_float(s.get("avg_r"), None) for s in stats]),
            "avg_loss_rate": _v7608_avg([_v7608_float(s.get("loss_rate"), None) for s in stats]),
            "avg_hit_1r_rate": _v7608_avg([_v7608_float(s.get("hit_1r_rate"), None) for s in stats]),
            "avg_mae_le_minus_1r_rate": _v7608_avg([_v7608_float(s.get("mae_le_minus_1r_rate"), None) for s in stats]),
            "avg_dead_trade_rate": _v7608_avg([_v7608_float(s.get("dead_trade_rate"), None) for s in stats]),
            "avg_giveback_after_1r_rate": _v7608_avg([_v7608_float(s.get("giveback_after_1r_rate"), None) for s in stats]),
            "examples": [
                {
                    "proposal_id": x.get("proposal_id"),
                    "proposal_text": x.get("proposal_text"),
                    "scope": x.get("scope"),
                    "matched_trades": x.get("matched_trades"),
                    "root_cause_hypothesis": x.get("root_cause_hypothesis"),
                }
                for x in arr[:8]
            ],
            "active_effect": "NONE_REVIEW_ONLY",
        })

    out.sort(
        key=lambda x: (
            x.get("clusters", 0),
            x.get("total_matched_trades", 0),
            -(_v7608_float(x.get("avg_cluster_r"), 0.0)),
        ),
        reverse=True,
    )

    return out

def _v7608_build():
    base = _v7607_build()

    if not base.get("ok"):
        return {
            "version": V7608_VERSION,
            "mode": "observe_only_root_cause_summary",
            "ok": False,
            "active_effect": "NONE",
            "live_rule_changes": False,
            "auto_blocking": False,
            "auto_filtering": False,
            "db_writes": False,
            "error": "V7607 base build failed. Cannot summarize root causes.",
        }

    clusters = base.get("top_cluster_details") or []

    cause_counts = {}
    cause_examples = {}

    expanded = []

    for c in clusters:
        scope = c.get("scope") or {}
        stats = c.get("cluster_stats") or {}
        causes = c.get("root_cause_hypothesis") or []

        row = {
            "proposal_id": c.get("proposal_id"),
            "proposal_text": c.get("proposal_text"),
            "review_priority": c.get("review_priority"),
            "sample_warning": c.get("sample_warning"),
            "matched_trades": c.get("matched_trades"),
            "session_or_hour": scope.get("session_or_hour"),
            "market": scope.get("market"),
            "side": scope.get("side"),
            "setup": scope.get("setup"),
            "signal_kind": scope.get("signal_kind"),
            "cluster_stats": stats,
            "root_cause_hypothesis": causes,
            "active_effect": "NONE_REVIEW_ONLY",
        }

        expanded.append(row)

        for cause in causes:
            cause_counts[cause] = cause_counts.get(cause, 0) + 1
            cause_examples.setdefault(cause, []).append(row)

    root_cause_summary = []

    for cause, count in cause_counts.items():
        arr = cause_examples.get(cause) or []
        stats = [x.get("cluster_stats") or {} for x in arr]

        root_cause_summary.append({
            "root_cause": cause,
            "cluster_count": count,
            "total_matched_trades": sum(_v7608_int(x.get("matched_trades")) for x in arr),
            "avg_cluster_r": _v7608_avg([_v7608_float(s.get("avg_r"), None) for s in stats]),
            "avg_loss_rate": _v7608_avg([_v7608_float(s.get("loss_rate"), None) for s in stats]),
            "avg_hit_1r_rate": _v7608_avg([_v7608_float(s.get("hit_1r_rate"), None) for s in stats]),
            "avg_mae_le_minus_1r_rate": _v7608_avg([_v7608_float(s.get("mae_le_minus_1r_rate"), None) for s in stats]),
            "avg_no_follow_through_rate": _v7608_avg([_v7608_float(s.get("no_follow_through_rate"), None) for s in stats]),
            "avg_dead_trade_rate": _v7608_avg([_v7608_float(s.get("dead_trade_rate"), None) for s in stats]),
            "avg_giveback_after_1r_rate": _v7608_avg([_v7608_float(s.get("giveback_after_1r_rate"), None) for s in stats]),
            "example_clusters": [
                {
                    "proposal_id": x.get("proposal_id"),
                    "proposal_text": x.get("proposal_text"),
                    "session_or_hour": x.get("session_or_hour"),
                    "market": x.get("market"),
                    "side": x.get("side"),
                    "setup": x.get("setup"),
                    "matched_trades": x.get("matched_trades"),
                    "avg_r": (x.get("cluster_stats") or {}).get("avg_r"),
                    "loss_rate": (x.get("cluster_stats") or {}).get("loss_rate"),
                }
                for x in arr[:10]
            ],
            "active_effect": "NONE_REVIEW_ONLY",
        })

    root_cause_summary.sort(
        key=lambda x: (
            x.get("cluster_count", 0),
            x.get("total_matched_trades", 0),
            -(_v7608_float(x.get("avg_cluster_r"), 0.0)),
        ),
        reverse=True,
    )

    market_rows = []
    session_rows = []
    side_rows = []
    setup_rows = []

    for x in expanded:
        market_rows.append({**x, "market_key": x.get("market") or "ALL_MARKETS"})
        session_rows.append({**x, "session_key": x.get("session_or_hour") or "UNKNOWN_SESSION"})
        side_rows.append({**x, "side_key": x.get("side") or "ALL_SIDES"})
        setup_rows.append({**x, "setup_key": x.get("setup") or "ALL_SETUPS"})

    # Main diagnosis
    diagnosis = []

    if cause_counts.get("DIRECTION_OR_CONTEXT_FAILURE", 0) >= 5:
        diagnosis.append({
            "finding": "DIRECTION_OR_CONTEXT_FAILURE_IS_REPEATING",
            "meaning": "Many top clusters fail immediately with negative R and weak MFE. This points to bad direction/context, not just poor exit management.",
            "active_effect": "NONE_REVIEW_ONLY",
        })

    if cause_counts.get("NO_1R_FOLLOW_THROUGH", 0) >= 5 or cause_counts.get("LOW_MFE_NO_FOLLOW_THROUGH", 0) >= 5:
        diagnosis.append({
            "finding": "NO_FOLLOW_THROUGH_IS_REPEATING",
            "meaning": "Many clusters do not reach +1R or have very low MFE. Entry context/filter quality should be reviewed before exit logic.",
            "active_effect": "NONE_REVIEW_ONLY",
        })

    if cause_counts.get("STOP_RISK_TOO_HIGH", 0) >= 5:
        diagnosis.append({
            "finding": "STOP_RISK_TOO_HIGH_IS_REPEATING",
            "meaning": "MAE frequently reaches below -1R. This suggests entries are often on the wrong side of pressure or stops are placed against strong movement.",
            "active_effect": "NONE_REVIEW_ONLY",
        })

    if cause_counts.get("GIVEBACK_AFTER_1R", 0) >= 2:
        diagnosis.append({
            "finding": "GIVEBACK_EXISTS_BUT_IS_NOT_MAIN_CAUSE",
            "meaning": "Some clusters give back after +1R, but the dominant issue is worse: many trades never reach +1R at all.",
            "active_effect": "NONE_REVIEW_ONLY",
        })

    return {
        "version": V7608_VERSION,
        "mode": "observe_only_root_cause_summary",
        "ok": True,
        "active_effect": "NONE",
        "live_rule_changes": False,
        "auto_blocking": False,
        "auto_filtering": False,
        "db_writes": False,
        "source": {
            "based_on": "V7607 top cluster trade detail viewer",
            "v7607_rows_seen": base.get("rows_seen"),
            "v7607_clusters_reviewed": base.get("clusters_reviewed"),
            "v7606_summary": (base.get("source") or {}).get("v7606_summary"),
        },
        "safety": {
            "read_only": True,
            "root_cause_summary_only": True,
            "can_block_trades": False,
            "can_change_live_rules": False,
            "can_write_db": False,
            "all_filter_proposals_still_disabled": True,
        },
        "summary": {
            "clusters_analyzed": len(clusters),
            "unique_root_causes": len(root_cause_summary),
            "top_root_cause": root_cause_summary[0]["root_cause"] if root_cause_summary else None,
            "direction_or_context_failure_clusters": cause_counts.get("DIRECTION_OR_CONTEXT_FAILURE", 0),
            "no_1r_follow_through_clusters": cause_counts.get("NO_1R_FOLLOW_THROUGH", 0),
            "low_mfe_no_follow_through_clusters": cause_counts.get("LOW_MFE_NO_FOLLOW_THROUGH", 0),
            "stop_risk_too_high_clusters": cause_counts.get("STOP_RISK_TOO_HIGH", 0),
            "dead_trade_pattern_clusters": cause_counts.get("DEAD_TRADE_PATTERN", 0),
            "giveback_after_1r_clusters": cause_counts.get("GIVEBACK_AFTER_1R", 0),
            "negative_expectancy_severe_clusters": cause_counts.get("NEGATIVE_EXPECTANCY_SEVERE", 0),
        },
        "root_cause_summary": root_cause_summary,
        "market_root_cause_summary": _v7608_make_bucket(market_rows, "market_key"),
        "session_root_cause_summary": _v7608_make_bucket(session_rows, "session_key"),
        "side_root_cause_summary": _v7608_make_bucket(side_rows, "side_key"),
        "setup_root_cause_summary": _v7608_make_bucket(setup_rows, "setup_key"),
        "diagnosis": diagnosis,
        "review_only_conclusion": [
            "The repeated top-cluster issue appears to be direction/context and no-follow-through, not only exit management.",
            "JPY LONG and general LONG clusters should stay review-only candidates until larger sample confirms.",
            "Session timing matters, especially NY_OPEN_OVERLAP, NY_MID, LONDON_MID, ASIA_OVERNIGHT and ROLLOVER_OFFHOURS.",
            "Do not activate filters from this module.",
        ],
        "safe_next_steps": [
            "Build V7609 V7601 confirmation bridge to compare these root causes with Deep Intelligence.",
            "Keep all proposed filters disabled.",
            "Only after V7601 confirms and sample size grows should a disabled rule config be drafted.",
        ],
    }

def _v7608_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows):
        if not rows:
            return "<p>No rows.</p>"

        keys = [
            "root_cause", "cluster_count", "total_matched_trades",
            "avg_cluster_r", "avg_loss_rate", "avg_hit_1r_rate",
            "avg_mae_le_minus_1r_rate", "avg_no_follow_through_rate",
            "avg_dead_trade_rate", "avg_giveback_after_1r_rate",
            "active_effect"
        ]

        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = ""

        for r in rows:
            body += "<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in keys) + "</tr>"

        return f"<table border='1' cellspacing='0' cellpadding='5'><tr>{head}</tr>{body}</table>"

    def bucket_table(rows):
        if not rows:
            return "<p>No rows.</p>"

        keys = [
            "key", "clusters", "total_matched_trades", "avg_cluster_r",
            "avg_loss_rate", "avg_hit_1r_rate",
            "avg_mae_le_minus_1r_rate", "avg_dead_trade_rate",
            "avg_giveback_after_1r_rate", "active_effect"
        ]

        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = ""

        for r in rows:
            body += "<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in keys) + "</tr>"

        return f"<table border='1' cellspacing='0' cellpadding='5'><tr>{head}</tr>{body}</table>"

    if not data.get("ok"):
        return f"<h1>V7608 Root Cause Summary</h1><pre>{esc(data)}</pre>"

    return f"""
    <html>
    <head><title>V7608 Root Cause Summary</title></head>
    <body style="font-family:Arial, sans-serif; padding:20px;">
      <h1>V7608 Root Cause Summary</h1>

      <p>
        <b>Version:</b> {esc(data.get("version"))}<br>
        <b>Mode:</b> {esc(data.get("mode"))}<br>
        <b>Active effect:</b> {esc(data.get("active_effect"))}<br>
        <b>Live rule changes:</b> {esc(data.get("live_rule_changes"))}<br>
        <b>Auto blocking:</b> {esc(data.get("auto_blocking"))}<br>
        <b>DB writes:</b> {esc(data.get("db_writes"))}
      </p>

      <h2>Safety</h2>
      <pre>{esc(data.get("safety"))}</pre>

      <h2>Summary</h2>
      <pre>{esc(data.get("summary"))}</pre>

      <h2>Diagnosis</h2>
      <pre>{esc(data.get("diagnosis"))}</pre>

      <h2>Root Cause Summary</h2>
      {table(data.get("root_cause_summary", []))}

      <h2>Market Root Cause Summary</h2>
      {bucket_table(data.get("market_root_cause_summary", []))}

      <h2>Session Root Cause Summary</h2>
      {bucket_table(data.get("session_root_cause_summary", []))}

      <h2>Side Root Cause Summary</h2>
      {bucket_table(data.get("side_root_cause_summary", []))}

      <h2>Setup Root Cause Summary</h2>
      {bucket_table(data.get("setup_root_cause_summary", []))}

      <h2>Review-only Conclusion</h2>
      <pre>{esc(data.get("review_only_conclusion"))}</pre>
    </body>
    </html>
    """

@app.get("/v7608-root-cause-summary.json")
def v7608_root_cause_summary_json():
    return _V7602_JSONResponse(_v7608_build())

@app.get("/v7608-root-cause-summary")
def v7608_root_cause_summary_html():
    return _V7602_HTMLResponse(_v7608_html(_v7608_build()))

# === V7608-ROOT-CAUSE-SUMMARY OBSERVE ONLY END ===

# === V7609-V7601-CONFIRMATION-BRIDGE OBSERVE ONLY START ===
# Compares V7608 root causes with V7601 Deep Intelligence if discoverable.
# Does NOT write to DB.
# Does NOT change live rules.
# Does NOT block/filter trades.

V7609_VERSION = "V7609-V7601-CONFIRMATION-BRIDGE-OBSERVE-ONLY"

import json as _v7609_json
import inspect as _v7609_inspect

def _v7609_norm(s):
    return str(s or "").strip().upper()

def _v7609_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default

def _v7609_int(v, default=0):
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default

def _v7609_response_to_payload(obj):
    try:
        if isinstance(obj, dict):
            return obj

        if hasattr(obj, "body"):
            body = obj.body
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="ignore")
            return _v7609_json.loads(body)

        if isinstance(obj, str):
            return _v7609_json.loads(obj)

    except Exception as e:
        return {
            "ok": False,
            "extract_error": str(e)[:200],
            "raw_type": str(type(obj)),
        }

    return {
        "ok": False,
        "extract_error": "unsupported_payload_type",
        "raw_type": str(type(obj)),
    }

def _v7609_try_call_noarg(fn):
    try:
        sig = _v7609_inspect.signature(fn)
        required = [
            p for p in sig.parameters.values()
            if p.default is p.empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]

        if required:
            return None, "requires_args"

        obj = fn()
        return _v7609_response_to_payload(obj), None

    except Exception as e:
        return None, str(e)[:220]

def _v7609_discover_v7601():
    candidates = []

    for name, obj in globals().items():
        lname = str(name).lower()

        if "v7601" not in lname:
            continue

        if not callable(obj):
            continue

        if "html" in lname:
            continue

        candidates.append((name, obj))

    preferred = []

    for name, obj in candidates:
        lname = name.lower()
        score = 0

        if "build" in lname:
            score += 5
        if "json" in lname:
            score += 4
        if "deep" in lname:
            score += 3
        if "intelligence" in lname:
            score += 3
        if lname.startswith("_"):
            score += 1

        preferred.append((score, name, obj))

    preferred.sort(reverse=True)

    attempts = []

    for score, name, fn in preferred:
        payload, err = _v7609_try_call_noarg(fn)
        attempts.append({
            "name": name,
            "score": score,
            "error": err,
            "payload_ok": bool(isinstance(payload, dict) and payload.get("ok") is not False),
            "payload_keys": list(payload.keys())[:30] if isinstance(payload, dict) else [],
        })

        if isinstance(payload, dict) and payload.get("ok") is not False:
            return {
                "found": True,
                "function_name": name,
                "payload": payload,
                "attempts": attempts,
            }

    return {
        "found": False,
        "function_name": None,
        "payload": None,
        "attempts": attempts,
    }

def _v7609_flatten_text(obj, limit=200000):
    chunks = []

    def walk(x):
        if len(" ".join(chunks)) > limit:
            return

        if isinstance(x, dict):
            for k, v in x.items():
                chunks.append(str(k))
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        else:
            chunks.append(str(x))

    try:
        walk(obj)
    except Exception:
        pass

    text = " ".join(chunks)
    return text[:limit]

def _v7609_extract_watch_terms(v7608):
    terms = []

    # From root cause examples
    for group_key in (
        "market_root_cause_summary",
        "session_root_cause_summary",
        "side_root_cause_summary",
        "setup_root_cause_summary",
    ):
        for row in v7608.get(group_key) or []:
            key = row.get("key")
            if key and key not in ("ALL_MARKETS", "ALL_SIDES", "ALL_SETUPS", "UNKNOWN"):
                terms.append(str(key))

    # Important known clusters from V7608/V7607 chain
    terms += [
        "EURJPY",
        "AUDJPY",
        "USDJPY",
        "GBPJPY",
        "CADJPY",
        "JPY",
        "LONG",
        "NY_OPEN_OVERLAP",
        "NY_MID",
        "LONDON_MID",
        "ASIA_OVERNIGHT",
        "ROLLOVER_OFFHOURS",
        "BERLIN_16:00",
        "MSS_BULL_RECENT",
        "BOS_BULL_RECENT",
        "INTERNAL_MSS_BULL_RECENT",
        "REVERSAL_LONG_MSS_RECLAIM",
        "BPR_RETEST",
        "NO_1R_FOLLOW_THROUGH",
        "LOW_MFE",
        "DEAD_TRADE",
        "STOP_RISK",
        "DIRECTION",
        "CONTEXT",
    ]

    out = []
    seen = set()

    for t in terms:
        nt = _v7609_norm(t)
        if not nt or nt in seen:
            continue
        seen.add(nt)
        out.append(t)

    return out[:80]

def _v7609_keyword_confirmation(v7601_payload, terms):
    text = _v7609_norm(_v7609_flatten_text(v7601_payload))
    rows = []

    for term in terms:
        nt = _v7609_norm(term)
        count = text.count(nt)

        rows.append({
            "term": term,
            "found_in_v7601_payload": count > 0,
            "occurrences": count,
        })

    rows.sort(key=lambda x: (x.get("found_in_v7601_payload"), x.get("occurrences", 0)), reverse=True)

    found = [x for x in rows if x.get("found_in_v7601_payload")]

    return {
        "terms_checked": len(rows),
        "terms_found": len(found),
        "confirmation_ratio": round(len(found) / len(rows), 4) if rows else 0,
        "top_found_terms": found[:40],
        "missing_terms": [x for x in rows if not x.get("found_in_v7601_payload")][:40],
    }

def _v7609_confirm_clusters(v7608, v7601_payload):
    terms = _v7609_extract_watch_terms(v7608)
    keyword = _v7609_keyword_confirmation(v7601_payload, terms)

    root_causes = v7608.get("root_cause_summary") or []
    markets = v7608.get("market_root_cause_summary") or []
    sessions = v7608.get("session_root_cause_summary") or []
    sides = v7608.get("side_root_cause_summary") or []
    setups = v7608.get("setup_root_cause_summary") or []

    text = _v7609_norm(_v7609_flatten_text(v7601_payload))

    def mark_rows(rows, label):
        out = []

        for r in rows[:25]:
            key = str(r.get("key") or r.get("root_cause") or "")
            nk = _v7609_norm(key)

            found = bool(nk and nk in text)

            out.append({
                "category": label,
                "key": key,
                "found_in_v7601": found,
                "v7608_clusters": r.get("clusters") or r.get("cluster_count"),
                "v7608_total_matched_trades": r.get("total_matched_trades"),
                "v7608_avg_r": r.get("avg_cluster_r"),
                "v7608_avg_loss_rate": r.get("avg_loss_rate"),
                "v7608_avg_hit_1r_rate": r.get("avg_hit_1r_rate"),
                "v7608_avg_mae_le_minus_1r_rate": r.get("avg_mae_le_minus_1r_rate"),
                "active_effect": "NONE_REVIEW_ONLY",
            })

        return out

    evidence_rows = []
    evidence_rows += mark_rows(root_causes, "root_cause")
    evidence_rows += mark_rows(markets, "market")
    evidence_rows += mark_rows(sessions, "session")
    evidence_rows += mark_rows(sides, "side")
    evidence_rows += mark_rows(setups, "setup")

    confirmed = [x for x in evidence_rows if x.get("found_in_v7601")]

    return {
        "keyword_confirmation": keyword,
        "evidence_rows": evidence_rows,
        "confirmed_rows": confirmed,
        "confirmation_summary": {
            "evidence_rows_total": len(evidence_rows),
            "evidence_rows_confirmed_by_v7601_text": len(confirmed),
            "confirmation_ratio": round(len(confirmed) / len(evidence_rows), 4) if evidence_rows else 0,
        },
    }

def _v7609_build():
    v7608 = _v7608_build()

    if not v7608.get("ok"):
        return {
            "version": V7609_VERSION,
            "mode": "observe_only_v7601_confirmation_bridge",
            "ok": False,
            "active_effect": "NONE",
            "live_rule_changes": False,
            "auto_blocking": False,
            "auto_filtering": False,
            "db_writes": False,
            "error": "V7608 base build failed. Cannot compare to V7601.",
        }

    discovery = _v7609_discover_v7601()

    if not discovery.get("found"):
        return {
            "version": V7609_VERSION,
            "mode": "observe_only_v7601_confirmation_bridge",
            "ok": True,
            "active_effect": "NONE",
            "live_rule_changes": False,
            "auto_blocking": False,
            "auto_filtering": False,
            "db_writes": False,
            "v7601_access": {
                "found": False,
                "function_name": None,
                "attempts": discovery.get("attempts"),
            },
            "v7608_summary": v7608.get("summary"),
            "status": "V7601_NOT_DISCOVERED_AUTOMATICALLY",
            "interpretation": [
                "V7608 is valid, but V7609 could not discover a callable V7601 JSON/build function automatically.",
                "No live rules are changed.",
                "Use route/log inspection to identify the exact V7601 function or route name.",
            ],
            "safe_next_steps": [
                "Run route/function discovery for V7601.",
                "Then install a V7609.1 bridge with the exact V7601 function name if needed.",
            ],
        }

    v7601_payload = discovery.get("payload") or {}
    confirmation = _v7609_confirm_clusters(v7608, v7601_payload)

    conf_ratio = (confirmation.get("confirmation_summary") or {}).get("confirmation_ratio", 0)

    if conf_ratio >= 0.50:
        status = "V7601_TEXT_CONFIRMATION_STRONG"
    elif conf_ratio >= 0.25:
        status = "V7601_TEXT_CONFIRMATION_PARTIAL"
    else:
        status = "V7601_TEXT_CONFIRMATION_WEAK"

    return {
        "version": V7609_VERSION,
        "mode": "observe_only_v7601_confirmation_bridge",
        "ok": True,
        "active_effect": "NONE",
        "live_rule_changes": False,
        "auto_blocking": False,
        "auto_filtering": False,
        "db_writes": False,
        "v7601_access": {
            "found": True,
            "function_name": discovery.get("function_name"),
            "attempts": discovery.get("attempts"),
            "payload_keys": list(v7601_payload.keys())[:50],
        },
        "v7608_summary": v7608.get("summary"),
        "confirmation_status": status,
        "confirmation": confirmation,
        "review_only_conclusion": [
            "This bridge only compares V7608 evidence with V7601 text/payload presence.",
            "Strong confirmation does not activate any filter.",
            "Weak confirmation may mean V7601 uses different naming, not that V7608 is wrong.",
            "Any future rule must remain disabled until manual approval.",
        ],
        "safe_next_steps": [
            "If confirmation is strong or partial, build V7610 disabled rule-draft board.",
            "If confirmation is weak, inspect V7601 payload structure and add exact field mapping.",
            "Keep all proposals disabled.",
        ],
    }

def _v7609_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows):
        if not rows:
            return "<p>No rows.</p>"

        keys = [
            "category", "key", "found_in_v7601",
            "v7608_clusters", "v7608_total_matched_trades",
            "v7608_avg_r", "v7608_avg_loss_rate",
            "v7608_avg_hit_1r_rate",
            "v7608_avg_mae_le_minus_1r_rate",
            "active_effect"
        ]

        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = ""

        for r in rows:
            body += "<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in keys) + "</tr>"

        return f"<table border='1' cellspacing='0' cellpadding='5'><tr>{head}</tr>{body}</table>"

    if not data.get("ok"):
        return f"<h1>V7609 V7601 Confirmation Bridge</h1><pre>{esc(data)}</pre>"

    confirmation = data.get("confirmation") or {}

    return f"""
    <html>
    <head><title>V7609 V7601 Confirmation Bridge</title></head>
    <body style="font-family:Arial, sans-serif; padding:20px;">
      <h1>V7609 V7601 Confirmation Bridge</h1>

      <p>
        <b>Version:</b> {esc(data.get("version"))}<br>
        <b>Mode:</b> {esc(data.get("mode"))}<br>
        <b>Active effect:</b> {esc(data.get("active_effect"))}<br>
        <b>Live rule changes:</b> {esc(data.get("live_rule_changes"))}<br>
        <b>Auto blocking:</b> {esc(data.get("auto_blocking"))}<br>
        <b>DB writes:</b> {esc(data.get("db_writes"))}<br>
        <b>Confirmation status:</b> {esc(data.get("confirmation_status"))}
      </p>

      <h2>V7601 Access</h2>
      <pre>{esc(data.get("v7601_access"))}</pre>

      <h2>V7608 Summary</h2>
      <pre>{esc(data.get("v7608_summary"))}</pre>

      <h2>Confirmation Summary</h2>
      <pre>{esc(confirmation.get("confirmation_summary"))}</pre>

      <h2>Keyword Confirmation</h2>
      <pre>{esc(confirmation.get("keyword_confirmation"))}</pre>

      <h2>Evidence Rows</h2>
      {table(confirmation.get("evidence_rows", []))}

      <h2>Review-only Conclusion</h2>
      <pre>{esc(data.get("review_only_conclusion"))}</pre>
    </body>
    </html>
    """

@app.get("/v7609-v7601-confirmation-bridge.json")
def v7609_v7601_confirmation_bridge_json():
    return _V7602_JSONResponse(_v7609_build())

@app.get("/v7609-v7601-confirmation-bridge")
def v7609_v7601_confirmation_bridge_html():
    return _V7602_HTMLResponse(_v7609_html(_v7609_build()))

# === V7609-V7601-CONFIRMATION-BRIDGE OBSERVE ONLY END ===

# === V7610-DISABLED-RULE-DRAFT-BOARD OBSERVE ONLY START ===
# Drafts disabled rule candidates from V7606/V7608/V7609 evidence.
# Does NOT write to DB.
# Does NOT change live rules.
# Does NOT block/filter trades.
# All rules are disabled drafts only.

V7610_VERSION = "V7610-DISABLED-RULE-DRAFT-BOARD-OBSERVE-ONLY"

def _v7610_norm(s):
    return str(s or "").strip().upper()

def _v7610_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default

def _v7610_int(v, default=0):
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default

def _v7610_rule_type_from_proposal(p):
    pt = str(p.get("proposal_type") or "")

    if "SESSION_MARKET_SIDE" in pt:
        return "AVOID_SESSION_MARKET_SIDE_DRAFT"

    if "SESSION_SIDE" in pt:
        return "AVOID_SESSION_SIDE_DRAFT"

    if "SESSION_SETUP" in pt:
        return "AVOID_SESSION_SETUP_DRAFT"

    if "HOUR_MARKET_SIDE" in pt:
        return "AVOID_HOUR_MARKET_SIDE_DRAFT"

    return "GENERAL_REVIEW_RULE_DRAFT"

def _v7610_risk_score(p):
    ev = p.get("evidence") or {}

    n = _v7610_int(ev.get("n"))
    avg_r = _v7610_float(ev.get("avg_r"))
    loss = _v7610_float(ev.get("loss_rate"))
    mae_stop = _v7610_float(ev.get("mae_le_minus_1r_rate"))
    hit1 = _v7610_float(ev.get("hit_1r_rate"))
    score = _v7610_float(ev.get("cross_matrix_score"))

    risk = 0.0
    risk += max(0.0, -avg_r) * 2.0
    risk += loss * 3.0
    risk += mae_stop * 2.0
    risk += max(0.0, 1.0 - hit1) * 1.5
    risk += min(score / 10.0, 5.0)

    if n >= 10:
        risk += 2.0
    elif n >= 5:
        risk += 1.0
    else:
        risk -= 1.0

    return round(risk, 4)

def _v7610_sample_gate(p):
    ev = p.get("evidence") or {}
    n = _v7610_int(ev.get("n"))

    if n < 5:
        return {
            "sample_gate": "FAIL_LOW_SAMPLE",
            "allowed_status": "WATCH_ONLY",
            "reason": "n_under_5",
        }

    if n < 10:
        return {
            "sample_gate": "PASS_DRAFT_ONLY_SMALL_SAMPLE",
            "allowed_status": "DISABLED_DRAFT_ONLY",
            "reason": "n_5_to_9_needs_more_confirmation",
        }

    return {
        "sample_gate": "PASS_DRAFT_ONLY_SAMPLE_OK",
        "allowed_status": "DISABLED_DRAFT_ONLY",
        "reason": "n_10_plus_but_still_requires_manual_approval",
    }

def _v7610_v7601_gate(p, v7609):
    conf = v7609.get("confirmation") or {}
    rows = conf.get("evidence_rows") or []

    scope = p.get("scope") or {}
    market = _v7610_norm(scope.get("market"))
    side = _v7610_norm(scope.get("side"))
    setup = _v7610_norm(scope.get("setup"))
    session = _v7610_norm(scope.get("session_or_hour"))

    confirmed_terms = []

    for r in rows:
        key = _v7610_norm(r.get("key"))
        if not r.get("found_in_v7601"):
            continue

        if key and key in (market, side, setup, session):
            confirmed_terms.append(r.get("key"))

    status = v7609.get("confirmation_status") or v7609.get("status") or "UNKNOWN"

    return {
        "v7601_bridge_status": status,
        "confirmed_terms": confirmed_terms,
        "confirmed_by_v7601": bool(confirmed_terms),
        "note": "V7601 text confirmation is supportive only; it does not activate any rule.",
    }

def _v7610_make_rule(p, rank, v7608, v7609):
    scope = p.get("scope") or {}
    ev = p.get("evidence") or {}
    sample_gate = _v7610_sample_gate(p)
    v7601_gate = _v7610_v7601_gate(p, v7609)

    rule_type = _v7610_rule_type_from_proposal(p)

    enabled = False
    status = "DISABLED_DRAFT_ONLY"

    if sample_gate.get("sample_gate") == "FAIL_LOW_SAMPLE":
        status = "WATCH_ONLY_LOW_SAMPLE"

    rule = {
        "rank": rank,
        "rule_id": f"V7610-DRAFT-{rank:03d}",
        "rule_type": rule_type,
        "title": p.get("proposal_text"),
        "enabled": enabled,
        "status": status,
        "active_effect": "NONE",
        "decision_authority": "NONE_REVIEW_ONLY",
        "requires_manual_approval": True,
        "requires_v7601_confirmation": True,
        "requires_larger_sample": _v7610_int(ev.get("n")) < 10,
        "draft_scope": {
            "session_or_hour": scope.get("session_or_hour"),
            "market": scope.get("market"),
            "side": scope.get("side"),
            "setup": scope.get("setup"),
            "signal_kind": scope.get("signal_kind"),
        },
        "trigger_logic_draft": {
            "description": "If enabled in the future after manual approval, this draft would avoid or flag matching trades.",
            "match_session_or_hour": scope.get("session_or_hour"),
            "match_market": scope.get("market"),
            "match_side": scope.get("side"),
            "match_setup": scope.get("setup"),
            "current_action": "NO_ACTION_REVIEW_ONLY",
        },
        "evidence": {
            "n": ev.get("n"),
            "avg_r": ev.get("avg_r"),
            "median_r": ev.get("median_r"),
            "loss_rate": ev.get("loss_rate"),
            "hit_1r_rate": ev.get("hit_1r_rate"),
            "mae_le_minus_1r_rate": ev.get("mae_le_minus_1r_rate"),
            "dead_trade_rate": ev.get("dead_trade_rate"),
            "cross_matrix_score": ev.get("cross_matrix_score"),
            "cross_tier": ev.get("cross_tier"),
            "confidence": ev.get("confidence"),
            "review_priority": p.get("review_priority"),
            "sample_warning": p.get("sample_warning"),
        },
        "risk_score": _v7610_risk_score(p),
        "sample_gate": sample_gate,
        "v7601_gate": v7601_gate,
        "safety_lock": {
            "can_execute": False,
            "can_block": False,
            "can_filter": False,
            "can_modify_live_rules": False,
            "can_write_db": False,
            "must_remain_disabled": True,
        },
        "manual_review_questions": [
            "Does V7601 agree with the same market/side/setup weakness?",
            "Is the sample size large enough?",
            "Is the weakness caused by direction/context rather than exit only?",
            "Does this rule risk blocking good trades in stronger sessions?",
            "Should it become a warning-only rule before any hard block?",
        ],
    }

    return rule

def _v7610_build():
    v7606 = _v7606_build()
    v7608 = _v7608_build()
    v7609 = _v7609_build()

    if not v7606.get("ok") or not v7608.get("ok") or not v7609.get("ok"):
        return {
            "version": V7610_VERSION,
            "mode": "observe_only_disabled_rule_draft_board",
            "ok": False,
            "active_effect": "NONE",
            "live_rule_changes": False,
            "auto_blocking": False,
            "auto_filtering": False,
            "db_writes": False,
            "errors": {
                "v7606_ok": v7606.get("ok"),
                "v7608_ok": v7608.get("ok"),
                "v7609_ok": v7609.get("ok"),
            },
        }

    proposals = v7606.get("top_proposals") or []

    # Keep only meaningful proposals; still all disabled.
    candidates = []

    for p in proposals:
        ev = p.get("evidence") or {}

        n = _v7610_int(ev.get("n"))
        avg_r = _v7610_float(ev.get("avg_r"))
        loss = _v7610_float(ev.get("loss_rate"))
        mae_stop = _v7610_float(ev.get("mae_le_minus_1r_rate"))
        score = _v7610_float(ev.get("cross_matrix_score"))

        if score >= 10 or (avg_r < 0 and loss >= 0.65) or (n >= 10 and avg_r < 0):
            candidates.append(p)

    rules = [_v7610_make_rule(p, i + 1, v7608, v7609) for i, p in enumerate(candidates[:40])]

    rules.sort(key=lambda r: (r.get("risk_score", 0), r.get("evidence", {}).get("n") or 0), reverse=True)

    for i, r in enumerate(rules, 1):
        r["rank"] = i
        r["rule_id"] = f"V7610-DRAFT-{i:03d}"

    severe = [r for r in rules if r.get("evidence", {}).get("review_priority") == "SEVERE_REVIEW_PRIORITY"]
    high = [r for r in rules if r.get("evidence", {}).get("review_priority") == "HIGH_REVIEW_PRIORITY"]
    low_sample = [r for r in rules if r.get("status") == "WATCH_ONLY_LOW_SAMPLE"]
    v7601_confirmed = [r for r in rules if (r.get("v7601_gate") or {}).get("confirmed_by_v7601")]

    jpy_long = [
        r for r in rules
        if "JPY" in _v7610_norm((r.get("draft_scope") or {}).get("market"))
        and _v7610_norm((r.get("draft_scope") or {}).get("side")) == "LONG"
    ]

    long_side = [
        r for r in rules
        if _v7610_norm((r.get("draft_scope") or {}).get("side")) == "LONG"
    ]

    setup_rules = [
        r for r in rules
        if (r.get("draft_scope") or {}).get("setup")
    ]

    return {
        "version": V7610_VERSION,
        "mode": "observe_only_disabled_rule_draft_board",
        "ok": True,
        "active_effect": "NONE",
        "live_rule_changes": False,
        "auto_blocking": False,
        "auto_filtering": False,
        "db_writes": False,
        "safety": {
            "all_rules_enabled": False,
            "all_rules_disabled": True,
            "draft_only": True,
            "can_execute": False,
            "can_block_trades": False,
            "can_filter_trades": False,
            "can_change_live_rules": False,
            "can_write_db": False,
            "requires_manual_approval": True,
        },
        "source": {
            "based_on": [
                "V7606 disabled proposal report",
                "V7608 root cause summary",
                "V7609 V7601 confirmation bridge",
            ],
            "v7606_summary": v7606.get("summary"),
            "v7608_summary": v7608.get("summary"),
            "v7609_status": v7609.get("confirmation_status") or v7609.get("status"),
        },
        "summary": {
            "draft_rules_total": len(rules),
            "severe_priority_rules": len(severe),
            "high_priority_rules": len(high),
            "watch_only_low_sample_rules": len(low_sample),
            "v7601_confirmed_rules": len(v7601_confirmed),
            "jpy_long_rules": len(jpy_long),
            "long_side_rules": len(long_side),
            "setup_rules": len(setup_rules),
            "enabled_rules": sum(1 for r in rules if r.get("enabled")),
        },
        "disabled_rule_drafts": rules[:40],
        "v7601_confirmed_rule_drafts": v7601_confirmed[:25],
        "jpy_long_rule_drafts": jpy_long[:25],
        "long_side_rule_drafts": long_side[:25],
        "setup_rule_drafts": setup_rules[:25],
        "review_only_conclusion": [
            "This board drafts possible future rules, but every rule is disabled.",
            "The strongest drafts are mainly JPY LONG and LONG-by-session problems.",
            "V7601 partially confirms markets/sides/setups, but not the derived session labels.",
            "No rule should become active without manual approval and more samples.",
            "Warning-only review should be preferred before any future hard block.",
        ],
        "safe_next_steps": [
            "Review disabled_rule_drafts.",
            "Build V7611 warning-only simulation board before any active filter.",
            "Keep all drafts disabled.",
        ],
    }

def _v7610_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows):
        if not rows:
            return "<p>No rows.</p>"

        keys = [
            "rank", "rule_id", "rule_type", "title", "enabled",
            "status", "risk_score", "active_effect",
            "requires_manual_approval", "requires_larger_sample"
        ]

        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = ""

        for r in rows:
            body += "<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in keys) + "</tr>"

        return f"<table border='1' cellspacing='0' cellpadding='5'><tr>{head}</tr>{body}</table>"

    if not data.get("ok"):
        return f"<h1>V7610 Disabled Rule Draft Board</h1><pre>{esc(data)}</pre>"

    return f"""
    <html>
    <head><title>V7610 Disabled Rule Draft Board</title></head>
    <body style="font-family:Arial, sans-serif; padding:20px;">
      <h1>V7610 Disabled Rule Draft Board</h1>

      <p>
        <b>Version:</b> {esc(data.get("version"))}<br>
        <b>Mode:</b> {esc(data.get("mode"))}<br>
        <b>Active effect:</b> {esc(data.get("active_effect"))}<br>
        <b>Live rule changes:</b> {esc(data.get("live_rule_changes"))}<br>
        <b>Auto blocking:</b> {esc(data.get("auto_blocking"))}<br>
        <b>DB writes:</b> {esc(data.get("db_writes"))}
      </p>

      <h2>Safety</h2>
      <pre>{esc(data.get("safety"))}</pre>

      <h2>Summary</h2>
      <pre>{esc(data.get("summary"))}</pre>

      <h2>Disabled Rule Drafts</h2>
      {table(data.get("disabled_rule_drafts", []))}

      <h2>V7601 Confirmed Rule Drafts</h2>
      {table(data.get("v7601_confirmed_rule_drafts", []))}

      <h2>JPY Long Rule Drafts</h2>
      {table(data.get("jpy_long_rule_drafts", []))}

      <h2>Long Side Rule Drafts</h2>
      {table(data.get("long_side_rule_drafts", []))}

      <h2>Setup Rule Drafts</h2>
      {table(data.get("setup_rule_drafts", []))}

      <h2>Review-only Conclusion</h2>
      <pre>{esc(data.get("review_only_conclusion"))}</pre>
    </body>
    </html>
    """

@app.get("/v7610-disabled-rule-draft-board.json")
def v7610_disabled_rule_draft_board_json():
    return _V7602_JSONResponse(_v7610_build())

@app.get("/v7610-disabled-rule-draft-board")
def v7610_disabled_rule_draft_board_html():
    return _V7602_HTMLResponse(_v7610_html(_v7610_build()))

# === V7610-DISABLED-RULE-DRAFT-BOARD OBSERVE ONLY END ===

# === V7611-WARNING-ONLY-SIMULATION-BOARD OBSERVE ONLY START ===
# Simulates what disabled V7610 draft rules would have warned on historically.
# Does NOT write to DB.
# Does NOT change live rules.
# Does NOT block/filter trades.
# Warning-only simulation, no active effect.

V7611_VERSION = "V7611-WARNING-ONLY-SIMULATION-BOARD-OBSERVE-ONLY"

def _v7611_norm(s):
    return str(s or "").strip().upper()

def _v7611_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default

def _v7611_status_closed(status):
    s = _v7611_norm(status)
    return "CLOSED" in s or "DONE" in s or "EXIT" in s or "WIN" in s or "LOSS" in s

def _v7611_derive_time(row, opened_col):
    raw = row.get(opened_col) if opened_col else None
    dt_utc, parse_note = _v7604_parse_dt(raw)
    dt_berlin = _v7604_to_berlin(dt_utc)

    return {
        "opened_raw": raw,
        "parse_note": parse_note,
        "session_berlin": _v7604_session_berlin(dt_berlin),
        "hour_berlin": _v7604_hour_bucket(dt_berlin, "BERLIN"),
        "weekday_berlin": _v7604_weekday_bucket(dt_berlin),
    }

def _v7611_row_to_item(row, cols):
    t = _v7611_derive_time(row, cols.get("opened_at"))

    item = {
        "id": row.get(cols.get("id")) if cols.get("id") else None,
        "source": _v7602_val(row, cols.get("source")),
        "trade_key": row.get(cols.get("trade_key")) if cols.get("trade_key") else None,
        "market": _v7602_val(row, cols.get("market")),
        "side": _v7602_val(row, cols.get("side")),
        "setup": _v7602_val(row, cols.get("setup")),
        "status": _v7602_val(row, cols.get("status")),
        "opened_at": t.get("opened_raw"),
        "session_berlin": t.get("session_berlin"),
        "hour_berlin": t.get("hour_berlin"),
        "entry": _v7611_float(row.get(cols.get("entry"))) if cols.get("entry") else None,
        "sl": _v7611_float(row.get(cols.get("sl"))) if cols.get("sl") else None,
        "tp1": _v7611_float(row.get(cols.get("tp1"))) if cols.get("tp1") else None,
        "risk_points": _v7611_float(row.get(cols.get("risk_points"))) if cols.get("risk_points") else None,
        "current_r": _v7611_float(row.get(cols.get("current_r"))) if cols.get("current_r") else None,
        "final_r": _v7611_float(row.get(cols.get("final_r"))) if cols.get("final_r") else None,
        "mfe": _v7611_float(row.get(cols.get("mfe"))) if cols.get("mfe") else None,
        "mae": _v7611_float(row.get(cols.get("mae"))) if cols.get("mae") else None,
        "hit_1r": row.get(cols.get("hit_1r")) if cols.get("hit_1r") else None,
        "updates": row.get(cols.get("updates")) if cols.get("updates") else None,
    }

    item["is_closed"] = _v7611_status_closed(item.get("status"))
    item["r_for_stats"] = item.get("final_r") if item.get("final_r") is not None else item.get("current_r")

    return item

def _v7611_rule_matches_trade(rule, item):
    scope = rule.get("draft_scope") or {}

    session_or_hour = scope.get("session_or_hour")
    market = scope.get("market")
    side = scope.get("side")
    setup = scope.get("setup")

    if session_or_hour:
        so = str(session_or_hour)
        if so.startswith("BERLIN_"):
            if _v7611_norm(item.get("hour_berlin")) != _v7611_norm(so):
                return False
        else:
            if _v7611_norm(item.get("session_berlin")) != _v7611_norm(so):
                return False

    if market and _v7611_norm(item.get("market")) != _v7611_norm(market):
        return False

    if side and _v7611_norm(item.get("side")) != _v7611_norm(side):
        return False

    if setup and _v7611_norm(item.get("setup")) != _v7611_norm(setup):
        return False

    return True

def _v7611_stats(items):
    if not items:
        return {
            "n": 0,
            "closed_n": 0,
            "avg_r": None,
            "loss_rate": None,
            "win_rate": None,
            "avg_mfe_r": None,
            "avg_mae_r": None,
            "hit_1r_rate": None,
            "mae_le_minus_1r_rate": None,
            "dead_trade_rate": None,
            "giveback_after_1r_rate": None,
        }

    r_vals = [x.get("r_for_stats") for x in items if x.get("r_for_stats") is not None]
    mfe_vals = [x.get("mfe") for x in items if x.get("mfe") is not None]
    mae_vals = [x.get("mae") for x in items if x.get("mae") is not None]

    def avg(vals):
        return round(sum(vals) / len(vals), 4) if vals else None

    hit1 = 0
    mae_stop = 0
    dead = 0
    giveback = 0

    for x in items:
        mfe = x.get("mfe")
        mae = x.get("mae")
        r = x.get("r_for_stats")

        try:
            h1 = bool(int(x.get("hit_1r") or 0))
        except Exception:
            h1 = bool(mfe is not None and mfe >= 1.0)

        if h1:
            hit1 += 1

        if mae is not None and mae <= -1.0:
            mae_stop += 1

        if mfe is not None and mae is not None and mfe < 0.35 and mae <= -0.50:
            dead += 1

        if mfe is not None and r is not None and mfe >= 1.0 and r < 0.25:
            giveback += 1

    n = len(items)

    return {
        "n": n,
        "closed_n": sum(1 for x in items if x.get("is_closed")),
        "avg_r": avg(r_vals),
        "loss_rate": round(sum(1 for x in r_vals if x < 0) / len(r_vals), 4) if r_vals else None,
        "win_rate": round(sum(1 for x in r_vals if x > 0) / len(r_vals), 4) if r_vals else None,
        "avg_mfe_r": avg(mfe_vals),
        "avg_mae_r": avg(mae_vals),
        "hit_1r_rate": round(hit1 / n, 4) if n else None,
        "mae_le_minus_1r_rate": round(mae_stop / n, 4) if n else None,
        "dead_trade_rate": round(dead / n, 4) if n else None,
        "giveback_after_1r_rate": round(giveback / n, 4) if n else None,
    }

def _v7611_build():
    v7610 = _v7610_build()

    if not v7610.get("ok"):
        return {
            "version": V7611_VERSION,
            "mode": "observe_only_warning_simulation",
            "ok": False,
            "active_effect": "NONE",
            "live_rule_changes": False,
            "auto_blocking": False,
            "auto_filtering": False,
            "db_writes": False,
            "error": "V7610 build failed",
        }

    db_path, checked = _v7602_find_db()

    if not db_path:
        return {
            "version": V7611_VERSION,
            "mode": "observe_only_warning_simulation",
            "ok": False,
            "active_effect": "NONE",
            "live_rule_changes": False,
            "auto_blocking": False,
            "auto_filtering": False,
            "db_writes": False,
            "error": "DB not found",
            "checked": checked,
        }

    con = _v7602_connect_ro(db_path)
    con.row_factory = _v7602_sqlite3.Row
    cur = con.cursor()

    cur.execute(f"PRAGMA table_info({V7602_TABLE})")
    raw_cols = [r[1] for r in cur.fetchall()]

    cols = {
        "id": _v7602_pick(raw_cols, ["id"]),
        "source": _v7602_pick(raw_cols, ["source"]),
        "trade_key": _v7602_pick(raw_cols, ["trade_key"]),
        "market": _v7602_pick(raw_cols, ["market", "symbol", "instrument", "ticker", "pair"]),
        "side": _v7602_pick(raw_cols, ["direction", "side", "trade_side", "dir"]),
        "setup": _v7602_pick(raw_cols, ["setup_name", "setup", "strategy", "signal_name", "signal_type"]),
        "status": _v7602_pick(raw_cols, ["status", "state", "trade_status"]),
        "opened_at": _v7602_pick(raw_cols, ["opened_at", "entry_time", "created_at", "timestamp", "ts", "time"]),
        "entry": _v7602_pick(raw_cols, ["entry"]),
        "sl": _v7602_pick(raw_cols, ["sl", "stop", "stop_loss"]),
        "tp1": _v7602_pick(raw_cols, ["tp1", "target", "take_profit"]),
        "risk_points": _v7602_pick(raw_cols, ["risk_points", "risk"]),
        "current_r": _v7602_pick(raw_cols, ["current_r"]),
        "final_r": _v7602_pick(raw_cols, ["final_r", "realized_r", "result_r", "pnl_r", "closed_r"]),
        "mfe": _v7602_pick(raw_cols, ["max_favorable_r", "mfe_r", "max_r", "best_r"]),
        "mae": _v7602_pick(raw_cols, ["max_adverse_r", "mae_r", "min_r", "worst_r"]),
        "hit_1r": _v7602_pick(raw_cols, ["hit_1r", "hit1r"]),
        "updates": _v7602_pick(raw_cols, ["updates"]),
    }

    cur.execute(f"SELECT * FROM {V7602_TABLE} LIMIT 50000")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()

    items = [_v7611_row_to_item(row, cols) for row in rows]
    closed_items = [x for x in items if x.get("is_closed")]
    rules = v7610.get("disabled_rule_drafts") or []

    simulations = []
    warned_trade_ids = set()

    for rule in rules:
        matches = [x for x in items if _v7611_rule_matches_trade(rule, x)]
        closed_matches = [x for x in matches if x.get("is_closed")]

        for x in matches:
            if x.get("id") is not None:
                warned_trade_ids.add(x.get("id"))

        simulations.append({
            "rule_id": rule.get("rule_id"),
            "rank": rule.get("rank"),
            "title": rule.get("title"),
            "rule_type": rule.get("rule_type"),
            "rule_status": rule.get("status"),
            "rule_enabled": rule.get("enabled"),
            "risk_score": rule.get("risk_score"),
            "draft_scope": rule.get("draft_scope"),
            "v7601_confirmed": (rule.get("v7601_gate") or {}).get("confirmed_by_v7601"),
            "sample_gate": (rule.get("sample_gate") or {}).get("sample_gate"),
            "would_warn_trades": len(matches),
            "would_warn_closed_trades": len(closed_matches),
            "matched_all_stats": _v7611_stats(matches),
            "matched_closed_stats": _v7611_stats(closed_matches),
            "current_action": "NO_ACTION_WARNING_SIMULATION_ONLY",
            "active_effect": "NONE",
            "enabled": False,
        })

    warned_items = [x for x in items if x.get("id") in warned_trade_ids]
    warned_closed_items = [x for x in warned_items if x.get("is_closed")]
    un_warned_items = [x for x in items if x.get("id") not in warned_trade_ids]
    un_warned_closed_items = [x for x in un_warned_items if x.get("is_closed")]

    simulations.sort(
        key=lambda x: (
            x.get("risk_score") or 0,
            x.get("would_warn_closed_trades") or 0
        ),
        reverse=True
    )

    top_events = []

    for sim in simulations[:20]:
        matches = [
            x for x in items
            if _v7611_rule_matches_trade({"draft_scope": sim.get("draft_scope")}, x)
        ]

        matches = sorted(
            matches,
            key=lambda x: (
                x.get("r_for_stats") if x.get("r_for_stats") is not None else 999,
                str(x.get("opened_at") or "")
            )
        )

        top_events.append({
            "rule_id": sim.get("rule_id"),
            "title": sim.get("title"),
            "worst_matching_trades": [
                {
                    "id": x.get("id"),
                    "market": x.get("market"),
                    "side": x.get("side"),
                    "setup": x.get("setup"),
                    "status": x.get("status"),
                    "opened_at": x.get("opened_at"),
                    "session_berlin": x.get("session_berlin"),
                    "hour_berlin": x.get("hour_berlin"),
                    "r": x.get("r_for_stats"),
                    "mfe": x.get("mfe"),
                    "mae": x.get("mae"),
                    "hit_1r": x.get("hit_1r"),
                    "updates": x.get("updates"),
                    "active_effect": "NONE_WARNING_SIMULATION_ONLY",
                }
                for x in matches[:10]
            ],
        })

    return {
        "version": V7611_VERSION,
        "mode": "observe_only_warning_simulation",
        "ok": True,
        "active_effect": "NONE",
        "live_rule_changes": False,
        "auto_blocking": False,
        "auto_filtering": False,
        "db_writes": False,
        "safety": {
            "warning_only": True,
            "simulation_only": True,
            "all_rules_disabled": True,
            "can_execute": False,
            "can_block_trades": False,
            "can_filter_trades": False,
            "can_change_live_rules": False,
            "can_write_db": False,
        },
        "source": {
            "based_on": "V7610 disabled rule drafts",
            "v7610_summary": v7610.get("summary"),
        },
        "rows_seen": len(items),
        "closed_rows_seen": len(closed_items),
        "rules_simulated": len(rules),
        "summary": {
            "unique_trades_that_would_receive_warning": len(warned_items),
            "unique_closed_trades_that_would_receive_warning": len(warned_closed_items),
            "warned_closed_stats": _v7611_stats(warned_closed_items),
            "unwarned_closed_stats": _v7611_stats(un_warned_closed_items),
            "enabled_rules": 0,
            "blocked_trades": 0,
            "filtered_trades": 0,
            "live_effect": "NONE",
        },
        "rule_warning_simulations": simulations[:40],
        "top_warning_trade_examples": top_events,
        "review_only_conclusion": [
            "This simulates warning-only labels that disabled draft rules would have produced.",
            "No trade is blocked or filtered.",
            "Compare warned_closed_stats vs unwarned_closed_stats to judge whether warnings identify weaker trades.",
            "If warning-only separation is strong, the next step is still only a warning dashboard, not active blocking.",
        ],
        "safe_next_steps": [
            "Review whether warned trades are clearly worse than unwarned trades.",
            "If yes, build V7612 warning dashboard.",
            "Keep every rule disabled and warning-only.",
        ],
    }

def _v7611_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows):
        if not rows:
            return "<p>No rows.</p>"

        keys = [
            "rank", "rule_id", "title", "rule_enabled", "rule_status",
            "risk_score", "would_warn_trades", "would_warn_closed_trades",
            "current_action", "active_effect"
        ]

        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = ""

        for r in rows:
            body += "<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in keys) + "</tr>"

        return f"<table border='1' cellspacing='0' cellpadding='5'><tr>{head}</tr>{body}</table>"

    if not data.get("ok"):
        return f"<h1>V7611 Warning Only Simulation Board</h1><pre>{esc(data)}</pre>"

    return f"""
    <html>
    <head><title>V7611 Warning Only Simulation Board</title></head>
    <body style="font-family:Arial, sans-serif; padding:20px;">
      <h1>V7611 Warning Only Simulation Board</h1>

      <p>
        <b>Version:</b> {esc(data.get("version"))}<br>
        <b>Mode:</b> {esc(data.get("mode"))}<br>
        <b>Active effect:</b> {esc(data.get("active_effect"))}<br>
        <b>Live rule changes:</b> {esc(data.get("live_rule_changes"))}<br>
        <b>Auto blocking:</b> {esc(data.get("auto_blocking"))}<br>
        <b>DB writes:</b> {esc(data.get("db_writes"))}
      </p>

      <h2>Safety</h2>
      <pre>{esc(data.get("safety"))}</pre>

      <h2>Summary</h2>
      <pre>{esc(data.get("summary"))}</pre>

      <h2>Rule Warning Simulations</h2>
      {table(data.get("rule_warning_simulations", []))}

      <h2>Review-only Conclusion</h2>
      <pre>{esc(data.get("review_only_conclusion"))}</pre>
    </body>
    </html>
    """

@app.get("/v7611-warning-only-simulation-board.json")
def v7611_warning_only_simulation_board_json():
    return _V7602_JSONResponse(_v7611_build())

@app.get("/v7611-warning-only-simulation-board")
def v7611_warning_only_simulation_board_html():
    return _V7602_HTMLResponse(_v7611_html(_v7611_build()))

# === V7611-WARNING-ONLY-SIMULATION-BOARD OBSERVE ONLY END ===

# === V7612-WARNING-DASHBOARD OBSERVE ONLY START ===
# Dashboard for V7611 warning-only simulation.
# Does NOT write to DB.
# Does NOT change live rules.
# Does NOT block/filter trades.
# Shows warning tiers only.

V7612_VERSION = "V7612-WARNING-DASHBOARD-OBSERVE-ONLY"

def _v7612_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default

def _v7612_int(v, default=0):
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default

def _v7612_tier(rule):
    closed = rule.get("matched_closed_stats") or {}
    n = _v7612_int(closed.get("n"))
    avg_r = _v7612_float(closed.get("avg_r"))
    loss = _v7612_float(closed.get("loss_rate"))
    hit1 = _v7612_float(closed.get("hit_1r_rate"))
    mae = _v7612_float(closed.get("mae_le_minus_1r_rate"))
    risk = _v7612_float(rule.get("risk_score"))

    if n >= 5 and avg_r <= -2.0 and loss >= 0.80 and mae >= 0.75:
        return "RED_WARNING_STRONG_REVIEW_ONLY"

    if n >= 5 and avg_r <= -1.0 and loss >= 0.65:
        return "ORANGE_WARNING_REVIEW_ONLY"

    if n >= 10 and avg_r < 0 and loss >= 0.55:
        return "YELLOW_WARNING_REVIEW_ONLY"

    if n < 5:
        return "LOW_SAMPLE_WATCH_ONLY"

    if risk >= 10 and avg_r < 0:
        return "WATCH_REVIEW_ONLY"

    if hit1 >= 0.60 and avg_r > -1.0:
        return "MIXED_DO_NOT_ESCALATE"

    return "INFO_REVIEW_ONLY"

def _v7612_score(rule):
    closed = rule.get("matched_closed_stats") or {}
    n = _v7612_int(closed.get("n"))
    avg_r = _v7612_float(closed.get("avg_r"))
    loss = _v7612_float(closed.get("loss_rate"))
    mae = _v7612_float(closed.get("mae_le_minus_1r_rate"))
    risk = _v7612_float(rule.get("risk_score"))

    score = 0.0
    score += max(0.0, -avg_r) * 2.5
    score += loss * 3.0
    score += mae * 2.0
    score += min(risk / 8.0, 4.0)

    if n >= 10:
        score += 1.5
    elif n >= 5:
        score += 0.75
    else:
        score -= 1.0

    return round(score, 4)

def _v7612_extract_family(rule):
    scope = rule.get("draft_scope") or {}
    market = str(scope.get("market") or "")
    side = str(scope.get("side") or "")
    setup = str(scope.get("setup") or "")
    session = str(scope.get("session_or_hour") or "")

    if "JPY" in market and side == "LONG":
        return "JPY_LONG_WARNING"
    if side == "LONG":
        return "LONG_SESSION_WARNING"
    if setup:
        return "SETUP_SESSION_WARNING"
    if side == "SHORT":
        return "SHORT_SESSION_WARNING"
    if session:
        return "SESSION_WARNING"

    return "GENERAL_WARNING"

def _v7612_build():
    base = _v7611_build()

    if not base.get("ok"):
        return {
            "version": V7612_VERSION,
            "mode": "observe_only_warning_dashboard",
            "ok": False,
            "active_effect": "NONE",
            "live_rule_changes": False,
            "auto_blocking": False,
            "auto_filtering": False,
            "db_writes": False,
            "error": "V7611 build failed. Cannot build warning dashboard.",
        }

    summary = base.get("summary") or {}
    warned = summary.get("warned_closed_stats") or {}
    unwarned = summary.get("unwarned_closed_stats") or {}

    warned_avg = _v7612_float(warned.get("avg_r"))
    unwarned_avg = _v7612_float(unwarned.get("avg_r"))
    warned_loss = _v7612_float(warned.get("loss_rate"))
    unwarned_loss = _v7612_float(unwarned.get("loss_rate"))
    warned_hit1 = _v7612_float(warned.get("hit_1r_rate"))
    unwarned_hit1 = _v7612_float(unwarned.get("hit_1r_rate"))
    warned_mae = _v7612_float(warned.get("avg_mae_r"))
    unwarned_mae = _v7612_float(unwarned.get("avg_mae_r"))

    separation = {
        "warned_closed_avg_r": warned.get("avg_r"),
        "unwarned_closed_avg_r": unwarned.get("avg_r"),
        "avg_r_delta_warned_minus_unwarned": round(warned_avg - unwarned_avg, 4),
        "warned_loss_rate": warned.get("loss_rate"),
        "unwarned_loss_rate": unwarned.get("loss_rate"),
        "loss_rate_delta_warned_minus_unwarned": round(warned_loss - unwarned_loss, 4),
        "warned_hit_1r_rate": warned.get("hit_1r_rate"),
        "unwarned_hit_1r_rate": unwarned.get("hit_1r_rate"),
        "hit_1r_delta_warned_minus_unwarned": round(warned_hit1 - unwarned_hit1, 4),
        "warned_avg_mae_r": warned.get("avg_mae_r"),
        "unwarned_avg_mae_r": unwarned.get("avg_mae_r"),
        "avg_mae_delta_warned_minus_unwarned": round(warned_mae - unwarned_mae, 4),
    }

    rules = []

    for r in base.get("rule_warning_simulations") or []:
        closed = r.get("matched_closed_stats") or {}
        scope = r.get("draft_scope") or {}

        row = {
            "rank": r.get("rank"),
            "rule_id": r.get("rule_id"),
            "title": r.get("title"),
            "warning_tier": _v7612_tier(r),
            "warning_score": _v7612_score(r),
            "warning_family": _v7612_extract_family(r),
            "enabled": False,
            "active_effect": "NONE_WARNING_DASHBOARD_ONLY",
            "current_action": "DISPLAY_WARNING_ONLY",
            "would_warn_trades": r.get("would_warn_trades"),
            "would_warn_closed_trades": r.get("would_warn_closed_trades"),
            "closed_avg_r": closed.get("avg_r"),
            "closed_loss_rate": closed.get("loss_rate"),
            "closed_hit_1r_rate": closed.get("hit_1r_rate"),
            "closed_mae_le_minus_1r_rate": closed.get("mae_le_minus_1r_rate"),
            "closed_dead_trade_rate": closed.get("dead_trade_rate"),
            "closed_giveback_after_1r_rate": closed.get("giveback_after_1r_rate"),
            "scope": {
                "session_or_hour": scope.get("session_or_hour"),
                "market": scope.get("market"),
                "side": scope.get("side"),
                "setup": scope.get("setup"),
                "signal_kind": scope.get("signal_kind"),
            },
            "sample_gate": r.get("sample_gate"),
            "v7601_confirmed": r.get("v7601_confirmed"),
        }

        rules.append(row)

    rules.sort(
        key=lambda x: (
            x.get("warning_score") or 0,
            x.get("would_warn_closed_trades") or 0
        ),
        reverse=True
    )

    for i, r in enumerate(rules, 1):
        r["dashboard_rank"] = i

    red = [r for r in rules if r.get("warning_tier") == "RED_WARNING_STRONG_REVIEW_ONLY"]
    orange = [r for r in rules if r.get("warning_tier") == "ORANGE_WARNING_REVIEW_ONLY"]
    yellow = [r for r in rules if r.get("warning_tier") == "YELLOW_WARNING_REVIEW_ONLY"]
    low_sample = [r for r in rules if r.get("warning_tier") == "LOW_SAMPLE_WATCH_ONLY"]

    family = {}
    for r in rules:
        f = r.get("warning_family") or "UNKNOWN"
        family.setdefault(f, []).append(r)

    family_summary = []
    for f, arr in family.items():
        family_summary.append({
            "family": f,
            "rules": len(arr),
            "total_warn_closed": sum(_v7612_int(x.get("would_warn_closed_trades")) for x in arr),
            "avg_warning_score": round(sum(_v7612_float(x.get("warning_score")) for x in arr) / len(arr), 4) if arr else None,
            "top_rule": arr[0].get("title") if arr else None,
            "active_effect": "NONE_WARNING_DASHBOARD_ONLY",
        })

    family_summary.sort(
        key=lambda x: (
            x.get("avg_warning_score") or 0,
            x.get("total_warn_closed") or 0
        ),
        reverse=True
    )

    if separation.get("avg_r_delta_warned_minus_unwarned") is not None and separation.get("avg_r_delta_warned_minus_unwarned") <= -2.0:
        separation_quality = "STRONG_WARNING_SEPARATION"
    elif separation.get("avg_r_delta_warned_minus_unwarned") is not None and separation.get("avg_r_delta_warned_minus_unwarned") <= -1.0:
        separation_quality = "GOOD_WARNING_SEPARATION"
    elif separation.get("avg_r_delta_warned_minus_unwarned") is not None and separation.get("avg_r_delta_warned_minus_unwarned") < 0:
        separation_quality = "WEAK_WARNING_SEPARATION"
    else:
        separation_quality = "NO_WARNING_EDGE"

    return {
        "version": V7612_VERSION,
        "mode": "observe_only_warning_dashboard",
        "ok": True,
        "active_effect": "NONE",
        "live_rule_changes": False,
        "auto_blocking": False,
        "auto_filtering": False,
        "db_writes": False,
        "safety": {
            "dashboard_only": True,
            "warning_only": True,
            "all_rules_disabled": True,
            "can_execute": False,
            "can_block_trades": False,
            "can_filter_trades": False,
            "can_change_live_rules": False,
            "can_write_db": False,
        },
        "source": {
            "based_on": "V7611 warning-only simulation board",
            "v7611_rows_seen": base.get("rows_seen"),
            "v7611_closed_rows_seen": base.get("closed_rows_seen"),
            "v7611_rules_simulated": base.get("rules_simulated"),
            "v7611_summary": base.get("summary"),
        },
        "summary": {
            "warning_rules_total": len(rules),
            "red_warning_rules": len(red),
            "orange_warning_rules": len(orange),
            "yellow_warning_rules": len(yellow),
            "low_sample_watch_rules": len(low_sample),
            "enabled_rules": 0,
            "blocked_trades": 0,
            "filtered_trades": 0,
            "separation_quality": separation_quality,
        },
        "warning_separation": separation,
        "top_warning_rules": rules[:40],
        "red_warning_rules": red[:25],
        "orange_warning_rules": orange[:25],
        "yellow_warning_rules": yellow[:25],
        "low_sample_watch_rules": low_sample[:25],
        "warning_family_summary": family_summary,
        "review_only_conclusion": [
            "The warning-only simulation separates weaker trades from stronger unwarned trades.",
            "This dashboard is still display-only and has no live effect.",
            "No rule is enabled, no trade is blocked, and no trade is filtered.",
            "The next safe step is a live-style warning display that marks risk but does not prevent trades.",
        ],
        "safe_next_steps": [
            "Review top_warning_rules and red_warning_rules.",
            "Build V7613 live-style warning preview if desired.",
            "Keep warning-only and disabled status until much larger sample confirms.",
        ],
    }

def _v7612_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows):
        if not rows:
            return "<p>No rows.</p>"

        keys = [
            "dashboard_rank", "rule_id", "warning_tier", "warning_score",
            "warning_family", "title", "enabled", "would_warn_closed_trades",
            "closed_avg_r", "closed_loss_rate", "closed_hit_1r_rate",
            "closed_mae_le_minus_1r_rate", "active_effect"
        ]

        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = ""

        for r in rows:
            body += "<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in keys) + "</tr>"

        return f"<table border='1' cellspacing='0' cellpadding='5'><tr>{head}</tr>{body}</table>"

    if not data.get("ok"):
        return f"<h1>V7612 Warning Dashboard</h1><pre>{esc(data)}</pre>"

    return f"""
    <html>
    <head><title>V7612 Warning Dashboard</title></head>
    <body style="font-family:Arial, sans-serif; padding:20px;">
      <h1>V7612 Warning Dashboard</h1>

      <p>
        <b>Version:</b> {esc(data.get("version"))}<br>
        <b>Mode:</b> {esc(data.get("mode"))}<br>
        <b>Active effect:</b> {esc(data.get("active_effect"))}<br>
        <b>Live rule changes:</b> {esc(data.get("live_rule_changes"))}<br>
        <b>Auto blocking:</b> {esc(data.get("auto_blocking"))}<br>
        <b>DB writes:</b> {esc(data.get("db_writes"))}
      </p>

      <h2>Safety</h2>
      <pre>{esc(data.get("safety"))}</pre>

      <h2>Summary</h2>
      <pre>{esc(data.get("summary"))}</pre>

      <h2>Warning Separation</h2>
      <pre>{esc(data.get("warning_separation"))}</pre>

      <h2>Top Warning Rules</h2>
      {table(data.get("top_warning_rules", []))}

      <h2>Red Warning Rules</h2>
      {table(data.get("red_warning_rules", []))}

      <h2>Warning Family Summary</h2>
      <pre>{esc(data.get("warning_family_summary"))}</pre>

      <h2>Review-only Conclusion</h2>
      <pre>{esc(data.get("review_only_conclusion"))}</pre>
    </body>
    </html>
    """

@app.get("/v7612-warning-dashboard.json")
def v7612_warning_dashboard_json():
    return _V7602_JSONResponse(_v7612_build())

@app.get("/v7612-warning-dashboard")
def v7612_warning_dashboard_html():
    return _V7602_HTMLResponse(_v7612_html(_v7612_build()))

# === V7612-WARNING-DASHBOARD OBSERVE ONLY END ===

# === V7612M-MASTER-AI-REVIEW-MENU START ===
# Navigation-only master menu for AI Review / Warning boards.
# Does NOT write to DB.
# Does NOT change live rules.
# Does NOT block/filter trades.

V7612M_VERSION = "V7612M-MASTER-AI-REVIEW-MENU"

from fastapi.responses import HTMLResponse as _V7612M_HTMLResponse
from fastapi.responses import JSONResponse as _V7612M_JSONResponse
from starlette.responses import Response as _V7612M_Response

def _v7612m_links():
    return [
        {
            "group": "Deep Intelligence",
            "label": "V7601 Deep Intelligence",
            "html": "/v7601-deep-intelligence",
            "json": "/v7601-deep-intelligence.json",
            "purpose": "Main deep intelligence overview.",
        },
        {
            "group": "Weakness Discovery",
            "label": "V7602 Weakness Audit",
            "html": "/v7602-weakness-audit",
            "json": "/v7602-weakness-audit.json",
            "purpose": "Find weak markets/sides/setups.",
        },
        {
            "group": "Weakness Discovery",
            "label": "V7603 Candidate Blocklist Review",
            "html": "/v7603-candidate-blocklist-review",
            "json": "/v7603-candidate-blocklist-review.json",
            "purpose": "Review-only candidate list, no blocking.",
        },
        {
            "group": "Session Intelligence",
            "label": "V7604 Derived Session Audit",
            "html": "/v7604-derived-session-audit",
            "json": "/v7604-derived-session-audit.json",
            "purpose": "Berlin-session based weakness audit.",
        },
        {
            "group": "Cross Confirmation",
            "label": "V7605 Cross Check Matrix",
            "html": "/v7605-cross-check-matrix",
            "json": "/v7605-cross-check-matrix.json",
            "purpose": "Cross-check weakness across source/session/setup.",
        },
        {
            "group": "Proposed Filters",
            "label": "V7606 Proposed Filter Report",
            "html": "/v7606-proposed-filter-report",
            "json": "/v7606-proposed-filter-report.json",
            "purpose": "Disabled proposal list only.",
        },
        {
            "group": "Trade Details",
            "label": "V7607 Top Cluster Trade Detail",
            "html": "/v7607-top-cluster-trade-detail",
            "json": "/v7607-top-cluster-trade-detail.json",
            "purpose": "Individual trades behind weak clusters.",
        },
        {
            "group": "Root Cause",
            "label": "V7608 Root Cause Summary",
            "html": "/v7608-root-cause-summary",
            "json": "/v7608-root-cause-summary.json",
            "purpose": "Aggregated root-cause analysis.",
        },
        {
            "group": "Confirmation",
            "label": "V7609 V7601 Confirmation Bridge",
            "html": "/v7609-v7601-confirmation-bridge",
            "json": "/v7609-v7601-confirmation-bridge.json",
            "purpose": "Compare V7608 with V7601.",
        },
        {
            "group": "Disabled Drafts",
            "label": "V7610 Disabled Rule Draft Board",
            "html": "/v7610-disabled-rule-draft-board",
            "json": "/v7610-disabled-rule-draft-board.json",
            "purpose": "Disabled rule drafts only.",
        },
        {
            "group": "Warning Simulation",
            "label": "V7611 Warning-Only Simulation Board",
            "html": "/v7611-warning-only-simulation-board",
            "json": "/v7611-warning-only-simulation-board.json",
            "purpose": "Historical warning-only simulation.",
        },
        {
            "group": "Warning Dashboard",
            "label": "V7612 Warning Dashboard",
            "html": "/v7612-warning-dashboard",
            "json": "/v7612-warning-dashboard.json",
            "purpose": "Warning tiers and separation quality.",
        },
    ]

def _v7612m_build():
    return {
        "version": V7612M_VERSION,
        "ok": True,
        "mode": "navigation_only_master_menu",
        "active_effect": "NONE",
        "live_rule_changes": False,
        "auto_blocking": False,
        "auto_filtering": False,
        "db_writes": False,
        "safety": {
            "navigation_only": True,
            "can_execute": False,
            "can_block_trades": False,
            "can_filter_trades": False,
            "can_change_live_rules": False,
            "can_write_db": False,
        },
        "menu_title": "AI Review / Warning Boards",
        "links_total": len(_v7612m_links()),
        "links": _v7612m_links(),
        "recommended_next": {
            "next_module": "V7613 live-style warning preview",
            "status": "not_installed_yet",
            "rule": "warning display only, no blocking, no filtering",
        },
    }

def _v7612m_html(data):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    groups = {}

    for link in data.get("links") or []:
        groups.setdefault(link.get("group") or "Other", []).append(link)

    blocks = ""

    for group, links in groups.items():
        rows = ""

        for l in links:
            rows += f"""
            <tr>
              <td><b>{esc(l.get("label"))}</b></td>
              <td><a href="{esc(l.get("html"))}">HTML öffnen</a></td>
              <td><a href="{esc(l.get("json"))}">JSON öffnen</a></td>
              <td>{esc(l.get("purpose"))}</td>
            </tr>
            """

        blocks += f"""
        <h2>{esc(group)}</h2>
        <table border="1" cellspacing="0" cellpadding="7" style="border-collapse:collapse; width:100%;">
          <tr>
            <th>Board</th>
            <th>HTML</th>
            <th>JSON</th>
            <th>Zweck</th>
          </tr>
          {rows}
        </table>
        """

    return f"""
    <html>
    <head>
      <title>AI Review / Warning Boards</title>
    </head>
    <body style="font-family:Arial, sans-serif; padding:20px; max-width:1200px;">
      <h1>AI Review / Warning Boards</h1>

      <p>
        <b>Version:</b> {esc(data.get("version"))}<br>
        <b>Mode:</b> {esc(data.get("mode"))}<br>
        <b>Active effect:</b> {esc(data.get("active_effect"))}<br>
        <b>Live rule changes:</b> {esc(data.get("live_rule_changes"))}<br>
        <b>Auto blocking:</b> {esc(data.get("auto_blocking"))}<br>
        <b>Auto filtering:</b> {esc(data.get("auto_filtering"))}<br>
        <b>DB writes:</b> {esc(data.get("db_writes"))}
      </p>

      <div style="padding:12px; border:1px solid #999; background:#f7f7f7; margin-bottom:20px;">
        <b>Sicherheit:</b> Dieses Menü ist nur Navigation. Es aktiviert keine Regel, blockiert keine Trades,
        filtert keine Signale und schreibt nichts in die Datenbank.
      </div>

      {blocks}

      <h2>Nächster Schritt</h2>
      <pre>{esc(data.get("recommended_next"))}</pre>

      <p>
        <a href="/pine-master">Zurück zum Pine Master</a>
      </p>
    </body>
    </html>
    """

@app.get("/master-ai-review.json")
def v7612m_master_ai_review_json():
    return _V7612M_JSONResponse(_v7612m_build())

@app.get("/master-ai-review")
def v7612m_master_ai_review_html():
    return _V7612M_HTMLResponse(_v7612m_html(_v7612m_build()))

# Inject one menu link into common master pages.
@app.middleware("http")
async def _v7612m_master_menu_link_injector(request, call_next):
    response = await call_next(request)

    try:
        path = request.url.path

        if path not in ["/pine-master", "/pine-master-review", "/intel-links", "/"]:
            return response

        content_type = response.headers.get("content-type", "")

        if "text/html" not in content_type.lower():
            return response

        body = b""

        async for chunk in response.body_iterator:
            body += chunk

        html = body.decode("utf-8", errors="ignore")

        marker = "V7612M-AI-REVIEW-MENU-LINK"

        if marker not in html:
            inject = f"""
            <div id="{marker}" style="margin:16px 0; padding:14px; border:2px solid #444; background:#f3f3f3;">
              <h2 style="margin-top:0;">AI Review / Warning Boards</h2>
              <p>Review-, Root-Cause-, Disabled-Draft- und Warning-Dashboards.</p>
              <p>
                <a href="/master-ai-review" style="font-weight:bold;">AI Review / Warning Boards öffnen</a>
              </p>
              <small>Navigation only · no live rule changes · no blocking · no filtering · no DB writes</small>
            </div>
            """

            if "</body>" in html:
                html = html.replace("</body>", inject + "</body>")
            else:
                html += inject

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return _V7612M_Response(
            content=html,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )

    except Exception:
        return response

# === V7612M-MASTER-AI-REVIEW-MENU END ===




# === V7246B ENTRY SCHEMA ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7246b_entry_schema_board import v7246b_payload, v7246b_html

    @app.get("/v7246b-entry-schema.json")
    def v7246b_entry_schema_json(token: str = "", limit: int = 100):
        return JSONResponse(v7246b_payload(limit=limit))

    @app.get("/v7246b-entry-schema", response_class=HTMLResponse)
    def v7246b_entry_schema_page(token: str = ""):
        return HTMLResponse(v7246b_html())

    print("[V7246B] Entry Schema Board routes installed.")
except Exception as exc:
    print("[V7246B] install failed:", exc)
# === END V7246B ENTRY SCHEMA ROUTES ===




# === V7246C ENTRY OVERLAY ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7246c_entry_schema_overlay import v7246c_payload, v7246c_html, score_entry_schema

    @app.get("/v7246c-entry-overlay.json")
    def v7246c_entry_overlay_json(token: str = "", setup_name: str = "", direction: str = "", market: str = ""):
        return JSONResponse(v7246c_payload(setup_name=setup_name, direction=direction, market=market))

    @app.get("/v7246c-entry-overlay", response_class=HTMLResponse)
    def v7246c_entry_overlay_page(token: str = ""):
        return HTMLResponse(v7246c_html())

    print("[V7246C] Entry Schema Score Overlay routes installed.")
except Exception as exc:
    print("[V7246C] install failed:", exc)
# === END V7246C ENTRY OVERLAY ROUTES ===




# === V7246D SIGNAL OVERLAY ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7246d_signal_overlay_integration import v7246d_payload, v7246d_html

    @app.get("/v7246d-signal-overlay.json")
    def v7246d_signal_overlay_json(token: str = "", limit: int = 80):
        return JSONResponse(v7246d_payload(limit=limit))

    @app.get("/v7246d-signal-overlay", response_class=HTMLResponse)
    def v7246d_signal_overlay_page(token: str = ""):
        return HTMLResponse(v7246d_html())

    print("[V7246D] Signal Overlay Integration routes installed.")
except Exception as exc:
    print("[V7246D] install failed:", exc)
# === END V7246D SIGNAL OVERLAY ROUTES ===




# === V7246E ENTRY UPGRADE SIM ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7246e_entry_upgrade_simulator import v7246e_payload, v7246e_html

    @app.get("/v7246e-entry-upgrade-sim.json")
    def v7246e_entry_upgrade_sim_json(token: str = "", limit: int = 100):
        return JSONResponse(v7246e_payload(limit=limit))

    @app.get("/v7246e-entry-upgrade-sim", response_class=HTMLResponse)
    def v7246e_entry_upgrade_sim_page(token: str = ""):
        return HTMLResponse(v7246e_html())

    print("[V7246E] Entry Upgrade Simulator routes installed.")
except Exception as exc:
    print("[V7246E] install failed:", exc)
# === END V7246E ENTRY UPGRADE SIM ROUTES ===




# === V7246F CLEAN UPGRADE CANDIDATE ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7246f_clean_upgrade_candidates import v7246f_payload, v7246f_html

    @app.get("/v7246f-clean-upgrade-candidates.json")
    def v7246f_clean_upgrade_candidates_json(token: str = "", limit: int = 160):
        return JSONResponse(v7246f_payload(limit=limit))

    @app.get("/v7246f-clean-upgrade-candidates", response_class=HTMLResponse)
    def v7246f_clean_upgrade_candidates_page(token: str = ""):
        return HTMLResponse(v7246f_html())

    print("[V7246F] Clean Upgrade Candidate Board routes installed.")
except Exception as exc:
    print("[V7246F] install failed:", exc)
# === END V7246F CLEAN UPGRADE CANDIDATE ROUTES ===




# === V7246G ENTRY GUARD ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v7246g_entry_guard_recommendation import v7246g_payload, v7246g_html

    @app.get("/v7246g-entry-guard.json")
    def v7246g_entry_guard_json(token: str = "", limit: int = 220):
        return JSONResponse(v7246g_payload(limit=limit))

    @app.get("/v7246g-entry-guard", response_class=HTMLResponse)
    def v7246g_entry_guard_page(token: str = ""):
        return HTMLResponse(v7246g_html())

    print("[V7246G] Entry Guard Recommendation routes installed.")
except Exception as exc:
    print("[V7246G] install failed:", exc)
# === END V7246G ENTRY GUARD ROUTES ===




# === V8000 DUAL PLAYBOOK ROUTES ===
try:
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.v8000_dual_playbook_engine import (
        v8000_master_payload, v8000_master_html,
        v8000_portfolio_payload, v8000_portfolio_html,
        v8000_playbooks_payload, v8000_playbooks_html,
        v8000_a_plus_payload, v8000_a_plus_html,
    )

    @app.get("/v8000/master.json")
    def v8000_master_json(token: str = "", limit: int = 220):
        return JSONResponse(v8000_master_payload(limit=limit))

    @app.get("/v8000/master", response_class=HTMLResponse)
    def v8000_master_page(token: str = ""):
        return HTMLResponse(v8000_master_html())

    @app.get("/v8000.json")
    def v8000_short_json(token: str = "", limit: int = 220):
        return JSONResponse(v8000_master_payload(limit=limit))

    @app.get("/v8000", response_class=HTMLResponse)
    def v8000_short_page(token: str = ""):
        return HTMLResponse(v8000_master_html())

    @app.get("/v8000/portfolio.json")
    def v8000_portfolio_json(token: str = "", limit: int = 220):
        return JSONResponse(v8000_portfolio_payload(limit=limit))

    @app.get("/v8000/portfolio", response_class=HTMLResponse)
    def v8000_portfolio_page(token: str = ""):
        return HTMLResponse(v8000_portfolio_html())

    @app.get("/v8000/playbooks.json")
    def v8000_playbooks_json(token: str = "", limit: int = 220):
        return JSONResponse(v8000_playbooks_payload(limit=limit))

    @app.get("/v8000/playbooks", response_class=HTMLResponse)
    def v8000_playbooks_page(token: str = ""):
        return HTMLResponse(v8000_playbooks_html())

    @app.get("/v8000/a-plus.json")
    def v8000_a_plus_json(token: str = "", limit: int = 220):
        return JSONResponse(v8000_a_plus_payload(limit=limit))

    @app.get("/v8000/a-plus", response_class=HTMLResponse)
    def v8000_a_plus_page(token: str = ""):
        return HTMLResponse(v8000_a_plus_html())

    print("[V8000] Dual Playbook routes installed.")
except Exception as exc:
    print("[V8000] install failed:", exc)
# === END V8000 DUAL PLAYBOOK ROUTES ===

# V8001_V8002_INSTALL_BEGIN
try:
    from app.v8002_news_window_policy import install_v8002_news_window_policy
    install_v8002_news_window_policy(app)
except Exception as exc:
    print(f"[V8002] install error: {exc}")

try:
    from app.v8001_pine_enrichment import install_v8001_pine_enrichment
    install_v8001_pine_enrichment(app)
except Exception as exc:
    print(f"[V8001] install error: {exc}")
# V8001_V8002_INSTALL_END

# V8003_MASTER_NAVIGATION_INSTALL_BEGIN
try:
    from app.v8003_master_navigation import install_v8003_master_navigation
    install_v8003_master_navigation(app)
except Exception as exc:
    print(f"[V8003] install error: {exc}")
# V8003_MASTER_NAVIGATION_INSTALL_END


# V8004_QUALITY_ROUTER_INSTALL_BEGIN
try:
    from app.v8004_quality_router import install_v8004_quality_router
    install_v8004_quality_router(app)
except Exception as exc:
    print(f"[V8004] install error: {exc}")
# V8004_QUALITY_ROUTER_INSTALL_END
