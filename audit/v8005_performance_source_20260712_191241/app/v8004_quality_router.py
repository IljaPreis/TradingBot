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

VERSION = "V8004-LIVE-SHADOW-QUALITY-ROUTER"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": VERSION,
    "enabled": True,
    "mode": "enforce_quality_routing",
    "enforce": True,
    "apply_only_to_v8001": True,
    "non_v8001_behavior": "KEEP_EXISTING",
    "require_existing_gate_allow_for_live": True,
    "broker_auto_execution": False,
    "v8001_observe_only": True,
    "v8002_observe_only": True,
    "v8002_would_block_is_observation_only": True,
    "require_15m": True,
    "require_bar_confirmed": True,
    "require_signal_event_id": True,
    "require_structure_event_id": True,
    "require_zone_id_for_playbook_a": True,
    "require_fresh": True,
    "reject_chase": True,
    "playbook_a": {
        "a_plus_score": 76.0,
        "a_score": 68.0,
        "b_score": 58.0,
        "review_score": 52.0,
    },
    "playbook_b": {
        "a_plus_score": 78.0,
        "a_score": 70.0,
        "b_score": 60.0,
        "review_score": 52.0,
    },
    "a_plus_min_confirmations": 3,
    "a_min_confirmations": 2,
    "a_plus_min_aligned_biases": 3,
    "a_min_aligned_biases": 2,
    "live_grades": ["A_PLUS", "A"],
    "shadow_grades": ["B", "REVIEW"],
    "watch_grades": ["WATCH"],
    "store_shadow": True,
    "store_watch_as_shadow": False,
    "duplicate_route": "NO_TRADE",
    "invalid_route": "NO_TRADE",
}

VALID_PLAYBOOKS = {
    "PLAYBOOK_A_HTF_OB_REACTION",
    "PLAYBOOK_B_SESSION_MOMENTUM",
}


def _root() -> Path:
    for p in (Path("/app"), Path("/opt/tradingbot_v6000"), Path.cwd()):
        try:
            if (p / "data").exists():
                return p
        except Exception:
            pass
    return Path.cwd()


def _data(name: str) -> Path:
    return _root() / "data" / name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(obj, dict):
                return _deep_merge(default, obj)
    except Exception:
        pass
    return json.loads(json.dumps(default))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict[str, Any]:
    return _read_json(_data("v8004_quality_router_config.json"), DEFAULT_CONFIG)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_data("v7000_learning.sqlite3")), timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _init_db(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS v8004_quality_router_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            signal_event_id TEXT,
            client_trade_id TEXT,
            market TEXT,
            direction TEXT,
            setup_name TEXT,
            playbook TEXT,
            pine_score REAL,
            server_confidence REAL,
            grade TEXT,
            route TEXT,
            base_allow INTEGER,
            final_allow INTEGER,
            duplicate INTEGER,
            shadow_stored INTEGER,
            reasons_json TEXT,
            blockers_json TEXT,
            raw_json TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS v8004_seen_signal_ids (
            signal_event_id TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            client_trade_id TEXT,
            market TEXT,
            direction TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_v8004_created ON v8004_quality_router_log(created_at)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_v8004_route ON v8004_quality_router_log(route, grade)")
    con.commit()


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "."))
    except Exception:
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _text(value).lower()
    if text in {"1", "true", "yes", "on", "confirmed"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _obj_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return dict(obj)
    try:
        if hasattr(obj, "model_dump"):
            data = obj.model_dump()
            return data if isinstance(data, dict) else {}
        if hasattr(obj, "dict"):
            data = obj.dict()
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _load_v8001_payload(client_trade_id: str | None, signal_obj: Any) -> tuple[dict[str, Any], str]:
    cid = _text(client_trade_id)
    con = None
    try:
        con = _connect()
        if _table_exists(con, "v8001_pine_enriched_signals"):
            row = None
            if cid:
                row = con.execute(
                    """
                    SELECT raw_json FROM v8001_pine_enriched_signals
                    WHERE client_trade_id=? OR signal_event_id=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (cid, cid),
                ).fetchone()
            if row:
                payload = _json_obj(row["raw_json"])
                if payload:
                    return payload, "v8001_pine_enriched_signals"
    except Exception:
        pass
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass

    fallback = _obj_dict(signal_obj)
    return fallback, "signal_object_fallback"


def _is_v8001(payload: dict[str, Any]) -> bool:
    source = _text(payload.get("source")).lower()
    schema = _upper(payload.get("schema_version"))
    return source.startswith("tradingview_v8001") or schema.startswith("V8001")


def _direction(payload: dict[str, Any], v7: dict[str, Any]) -> str:
    raw = _upper(payload.get("direction") or v7.get("direction") or payload.get("side"))
    if raw in {"BUY", "LONG", "L"}:
        return "LONG"
    if raw in {"SELL", "SHORT", "S"}:
        return "SHORT"
    return raw or "UNKNOWN"


def _timeframe_ok(payload: dict[str, Any]) -> bool:
    value = _upper(payload.get("signal_timeframe") or payload.get("timeframe"))
    value = value.replace("MINUTES", "").replace("MINUTE", "").replace("M", "")
    return value in {"15", "15.0"}


def _bias_value(value: Any) -> int:
    text = _text(value).lower()
    if any(x in text for x in ("bull", "long", "up")):
        return 1
    if any(x in text for x in ("bear", "short", "down")):
        return -1
    return 0


def _bias_counts(payload: dict[str, Any], direction: str) -> tuple[int, int, dict[str, str]]:
    desired = 1 if direction == "LONG" else -1 if direction == "SHORT" else 0
    fields = {
        "bias_15m": _text(payload.get("bias_15m")),
        "tactical_layer_bias": _text(payload.get("tactical_layer_bias")),
        "htf_layer_bias": _text(payload.get("htf_layer_bias")),
        "regime_bias": _text(payload.get("regime_bias")),
    }
    aligned = 0
    opposing = 0
    for value in fields.values():
        b = _bias_value(value)
        if desired and b == desired:
            aligned += 1
        elif desired and b == -desired:
            opposing += 1
    return aligned, opposing, fields


def _confirmations(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("confirmations_csv")
    if isinstance(raw, list):
        values = [_upper(x) for x in raw]
    else:
        text = _text(raw)
        for sep in (";", "|"):
            text = text.replace(sep, ",")
        values = [_upper(x) for x in text.split(",")]
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _server_confidence(v7: dict[str, Any]) -> float:
    learned = v7.get("learned") if isinstance(v7.get("learned"), dict) else {}
    for value in (
        learned.get("adjusted_confidence"),
        v7.get("confidence"),
        v7.get("base_confidence"),
        v7.get("technical_score"),
    ):
        n = _num(value, -1.0)
        if n >= 0:
            return n
    return 0.0


def _claim_signal(payload: dict[str, Any], direction: str, record: bool) -> bool:
    """Returns True for the first processing of an ID, False for a duplicate."""
    if not record:
        return True
    signal_id = _text(payload.get("signal_event_id") or payload.get("client_trade_id"))
    if not signal_id:
        return False
    con = None
    try:
        con = _connect()
        _init_db(con)
        cur = con.execute(
            """
            INSERT OR IGNORE INTO v8004_seen_signal_ids
            (signal_event_id, first_seen_at, client_trade_id, market, direction)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                _now(),
                _text(payload.get("client_trade_id")),
                _upper(payload.get("market")),
                direction,
            ),
        )
        con.commit()
        return int(cur.rowcount or 0) == 1
    except Exception:
        return False
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def evaluate_payload(
    payload: dict[str, Any],
    v7: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    *,
    record: bool = False,
) -> dict[str, Any]:
    """Pure quality evaluation when record=False; no broker execution is performed."""
    cfg = config or load_config()
    gate = v7 if isinstance(v7, dict) else {}
    recognized = _is_v8001(payload)
    base_allow = bool(gate.get("allow_trade"))
    direction = _direction(payload, gate)
    market = _upper(payload.get("market") or gate.get("market"))
    playbook = _upper(payload.get("playbook"))
    setup_name = _upper(payload.get("setup_name") or payload.get("trigger") or gate.get("setup_name"))
    primary = _upper(payload.get("primary_trigger"))
    signal_id = _text(payload.get("signal_event_id") or payload.get("client_trade_id"))
    structure_id = _text(payload.get("structure_event_id"))
    zone_id = _text(payload.get("zone_id"))
    session = _upper(payload.get("session"))
    pine_score = _num(payload.get("confidence") or payload.get("technical_score"), 0.0)
    server_conf = _server_confidence(gate)
    confs = _confirmations(payload)
    aligned, opposing, bias_fields = _bias_counts(payload, direction)
    news = payload.get("v8002_news_policy") if isinstance(payload.get("v8002_news_policy"), dict) else {}

    reasons: list[str] = []
    blockers: list[str] = []

    if not recognized:
        return {
            "ok": True,
            "version": VERSION,
            "applied": False,
            "recognized_v8001": False,
            "grade": "LEGACY",
            "route": "KEEP_EXISTING",
            "base_allow": base_allow,
            "final_allow": base_allow,
            "market": market,
            "direction": direction,
            "playbook": playbook,
            "setup_name": setup_name,
            "reasons": ["V8004_APPLIES_ONLY_TO_V8001"],
            "blockers": [],
            "broker_auto_execution": False,
        }

    if not cfg.get("enabled", True):
        return {
            "ok": True,
            "version": VERSION,
            "applied": False,
            "recognized_v8001": True,
            "grade": "DISABLED",
            "route": "KEEP_EXISTING",
            "base_allow": base_allow,
            "final_allow": base_allow,
            "market": market,
            "direction": direction,
            "playbook": playbook,
            "setup_name": setup_name,
            "reasons": ["V8004_DISABLED"],
            "blockers": [],
            "broker_auto_execution": False,
        }

    if cfg.get("require_15m", True) and not _timeframe_ok(payload):
        blockers.append("V8004_MAIN_SIGNAL_NOT_15M")
    if cfg.get("require_bar_confirmed", True) and not _bool(payload.get("bar_confirmed"), False):
        blockers.append("V8004_SIGNAL_NOT_BAR_CLOSE_CONFIRMED")
    if cfg.get("require_signal_event_id", True) and not signal_id:
        blockers.append("V8004_MISSING_SIGNAL_EVENT_ID")
    if cfg.get("require_structure_event_id", True) and (not structure_id or structure_id in {"0", "0.0", "NONE"}):
        blockers.append("V8004_MISSING_STRUCTURE_EVENT_ID")
    if playbook not in VALID_PLAYBOOKS:
        blockers.append("V8004_INVALID_PLAYBOOK")
    if direction not in {"LONG", "SHORT"} or not market:
        blockers.append("V8004_MISSING_MARKET_OR_DIRECTION")
    if cfg.get("require_fresh", True) and not _bool(payload.get("is_fresh"), True):
        blockers.append("V8004_STALE_SIGNAL")
    if cfg.get("reject_chase", True) and (
        _bool(payload.get("is_chase"), False) or _bool(payload.get("is_late_entry"), False)
    ):
        blockers.append("V8004_CHASE_OR_LATE_ENTRY")
    if "FVG" in primary or "BPR" in primary:
        blockers.append("V8004_FVG_BPR_CANNOT_BE_PRIMARY_TRIGGER")

    if playbook == "PLAYBOOK_A_HTF_OB_REACTION":
        if cfg.get("require_zone_id_for_playbook_a", True) and not zone_id:
            blockers.append("V8004_PLAYBOOK_A_MISSING_ZONE_ID")
        if not _bool(payload.get("reaction_confirmed"), False):
            blockers.append("V8004_PLAYBOOK_A_REACTION_NOT_CONFIRMED")
        touch_count = int(_num(payload.get("zone_touch_count"), 0))
        if touch_count <= 0:
            blockers.append("V8004_PLAYBOOK_A_INVALID_TOUCH_COUNT")
        if touch_count > 2:
            blockers.append("V8004_PLAYBOOK_A_ZONE_OVERUSED")
    elif playbook == "PLAYBOOK_B_SESSION_MOMENTUM":
        if session in {"", "OFF", "NONE", "UNKNOWN"}:
            blockers.append("V8004_PLAYBOOK_B_OUTSIDE_VALID_SESSION")
        directional_structure = (
            (_bool(payload.get("mss_bull")) or _bool(payload.get("bos_bull")))
            if direction == "LONG"
            else (_bool(payload.get("mss_bear")) or _bool(payload.get("bos_bear")))
        )
        if not directional_structure:
            blockers.append("V8004_PLAYBOOK_B_STRUCTURE_NOT_DIRECTIONAL")

    first_process = _claim_signal(payload, direction, record=record)
    duplicate = not first_process
    if duplicate:
        blockers.append("V8004_DUPLICATE_SIGNAL_EVENT_ID")

    grade = "NO_TRADE"
    if not blockers:
        thresholds = cfg.get("playbook_a", {}) if playbook.startswith("PLAYBOOK_A") else cfg.get("playbook_b", {})
        a_plus_score = _num(thresholds.get("a_plus_score"), 76.0)
        a_score = _num(thresholds.get("a_score"), 68.0)
        b_score = _num(thresholds.get("b_score"), 58.0)
        review_score = _num(thresholds.get("review_score"), 52.0)
        min_ap_conf = int(_num(cfg.get("a_plus_min_confirmations"), 3))
        min_a_conf = int(_num(cfg.get("a_min_confirmations"), 2))
        min_ap_bias = int(_num(cfg.get("a_plus_min_aligned_biases"), 3))
        min_a_bias = int(_num(cfg.get("a_min_aligned_biases"), 2))

        playbook_a_premium = (
            playbook == "PLAYBOOK_A_HTF_OB_REACTION"
            and (_bool(payload.get("zone_first_touch"), False) or int(_num(payload.get("zone_touch_count"), 0)) == 1)
        )
        playbook_b_premium = (
            playbook == "PLAYBOOK_B_SESSION_MOMENTUM"
            and ("MSS" in primary or "MSS" in setup_name)
        )
        premium = playbook_a_premium or playbook_b_premium

        if (
            pine_score >= a_plus_score
            and len(confs) >= min_ap_conf
            and aligned >= min_ap_bias
            and opposing == 0
            and premium
        ):
            grade = "A_PLUS"
            reasons.append("V8004_A_PLUS_STRICT")
        elif (
            pine_score >= a_score
            and len(confs) >= min_a_conf
            and aligned >= min_a_bias
            and opposing == 0
        ):
            grade = "A"
            reasons.append("V8004_A_STRICT")
        elif pine_score >= b_score and len(confs) >= 1:
            grade = "B"
            reasons.append("V8004_B_SHADOW_QUALITY")
        elif pine_score >= review_score:
            grade = "REVIEW"
            reasons.append("V8004_REVIEW_SHADOW")
        else:
            grade = "WATCH"
            reasons.append("V8004_WATCH_ONLY")

    live_grades = {_upper(x) for x in cfg.get("live_grades", ["A_PLUS", "A"])}
    shadow_grades = {_upper(x) for x in cfg.get("shadow_grades", ["B", "REVIEW"])}
    watch_grades = {_upper(x) for x in cfg.get("watch_grades", ["WATCH"])}

    if blockers:
        route = str(cfg.get("duplicate_route") if duplicate else cfg.get("invalid_route") or "NO_TRADE").upper()
    elif grade in live_grades:
        if cfg.get("require_existing_gate_allow_for_live", True) and not base_allow:
            route = "SHADOW"
            reasons.append("V8004_STRONG_SIGNAL_DOWNGRADED_BY_EXISTING_SAFETY_GATE")
        else:
            route = "LIVE"
            reasons.append("V8004_EXISTING_SAFETY_GATE_PASS")
    elif grade in shadow_grades:
        route = "SHADOW"
    elif grade in watch_grades:
        route = "WATCH_ONLY"
    else:
        route = "NO_TRADE"

    final_allow = bool(route == "LIVE" and base_allow)
    if not cfg.get("enforce", True):
        final_allow = base_allow
        reasons.append("V8004_OBSERVE_ONLY_CONFIG")

    if news.get("would_hard_block"):
        reasons.append("V8002_WOULD_BLOCK_OBSERVATION_ONLY")
    if news.get("hard_block"):
        blockers.append("V8002_HARD_BLOCK_PRESENT")
        route = "SHADOW"
        final_allow = False

    return {
        "ok": True,
        "version": VERSION,
        "applied": True,
        "recognized_v8001": True,
        "mode": cfg.get("mode"),
        "enforce": bool(cfg.get("enforce", True)),
        "market": market,
        "direction": direction,
        "setup_name": setup_name,
        "playbook": playbook,
        "primary_trigger": primary,
        "signal_event_id": signal_id,
        "structure_event_id": structure_id,
        "zone_id": zone_id,
        "session": session,
        "pine_score": pine_score,
        "server_confidence": server_conf,
        "confirmations": confs,
        "confirmation_count": len(confs),
        "aligned_bias_count": aligned,
        "opposing_bias_count": opposing,
        "biases": bias_fields,
        "grade": grade,
        "route": route,
        "base_allow": base_allow,
        "final_allow": final_allow,
        "duplicate": duplicate,
        "reasons": reasons,
        "blockers": blockers,
        "v8002_observation": {
            "risk": news.get("risk"),
            "would_hard_block": bool(news.get("would_hard_block", False)),
            "hard_block": bool(news.get("hard_block", False)),
            "observe_only": bool(news.get("observe_only", True)),
        },
        "broker_auto_execution": False,
    }


def _store_shadow(payload: dict[str, Any], decision: dict[str, Any], v7: dict[str, Any]) -> bool:
    cid = _text(payload.get("client_trade_id") or decision.get("signal_event_id"))
    if not cid:
        return False
    shadow_id = "SHADOW_" + cid
    con = None
    try:
        con = _connect()
        con.execute(
            """
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
            """
        )
        raw = {
            "source": VERSION,
            "decision": decision,
            "signal": payload,
            "v7000": v7,
        }
        cur = con.execute(
            """
            INSERT OR IGNORE INTO shadow_trades (
                shadow_id, client_trade_id, market, direction, setup_name,
                entry, sl, tp1, confidence, reason, raw_json, status, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (
                shadow_id,
                cid,
                decision.get("market"),
                decision.get("direction"),
                decision.get("setup_name"),
                _num(payload.get("entry") or payload.get("price"), 0.0),
                _num(payload.get("sl") or v7.get("sl"), 0.0),
                _num(payload.get("tp1") or v7.get("tp1"), 0.0),
                _num(decision.get("pine_score"), 0.0),
                f"V8004_{decision.get('grade')}_{decision.get('route')}",
                json.dumps(raw, ensure_ascii=False, default=str),
                _now(),
            ),
        )
        con.commit()
        return int(cur.rowcount or 0) == 1
    except Exception:
        return False
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def _log_decision(payload: dict[str, Any], decision: dict[str, Any], shadow_stored: bool) -> None:
    con = None
    try:
        con = _connect()
        _init_db(con)
        con.execute(
            """
            INSERT INTO v8004_quality_router_log (
                created_at, signal_event_id, client_trade_id, market, direction,
                setup_name, playbook, pine_score, server_confidence, grade, route,
                base_allow, final_allow, duplicate, shadow_stored, reasons_json,
                blockers_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now(),
                decision.get("signal_event_id"),
                _text(payload.get("client_trade_id")),
                decision.get("market"),
                decision.get("direction"),
                decision.get("setup_name"),
                decision.get("playbook"),
                decision.get("pine_score"),
                decision.get("server_confidence"),
                decision.get("grade"),
                decision.get("route"),
                1 if decision.get("base_allow") else 0,
                1 if decision.get("final_allow") else 0,
                1 if decision.get("duplicate") else 0,
                1 if shadow_stored else 0,
                json.dumps(decision.get("reasons") or [], ensure_ascii=False),
                json.dumps(decision.get("blockers") or [], ensure_ascii=False),
                json.dumps({"signal": payload, "decision": decision}, ensure_ascii=False, default=str),
            ),
        )
        con.commit()
    except Exception:
        pass
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def v8004_apply_quality_router(
    signal_obj: Any,
    v7: dict[str, Any],
    *,
    client_trade_id: str | None = None,
    old_d: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(v7, dict):
        return v7
    payload, source = _load_v8001_payload(client_trade_id, signal_obj)
    cfg = load_config()
    decision = evaluate_payload(payload, v7, cfg, record=True)
    decision["payload_source"] = source

    if not decision.get("applied"):
        v7["v8004_quality_router"] = decision
        v7["v8004_route"] = decision.get("route")
        return v7

    previous_reason = _text(v7.get("reason"))
    route = _upper(decision.get("route"))
    grade = _upper(decision.get("grade"))

    if cfg.get("enforce", True):
        v7["allow_trade"] = bool(decision.get("final_allow"))
        if route != "LIVE":
            v7["risk_r"] = 0.0

    shadow_stored = False
    if (
        cfg.get("store_shadow", True)
        and route == "SHADOW"
        and not decision.get("duplicate")
    ):
        shadow_stored = _store_shadow(payload, decision, v7)
    elif cfg.get("store_watch_as_shadow", False) and route == "WATCH_ONLY":
        shadow_stored = _store_shadow(payload, decision, v7)

    decision["shadow_stored"] = shadow_stored
    v7["v8004_quality_router"] = decision
    v7["v8004_grade"] = grade
    v7["v8004_route"] = route
    v7["v8004_live_candidate"] = route == "LIVE"
    v7["v8004_shadow_candidate"] = route == "SHADOW"
    v7["v8004_shadow_stored"] = shadow_stored
    v7["v8004_broker_auto_execution"] = False

    detail = ",".join(decision.get("blockers") or decision.get("reasons") or [])
    prefix = f"V8004 {route} {grade}: score={decision.get('pine_score')}; {detail}"
    v7["reason"] = f"{prefix}. Previous: {previous_reason}" if previous_reason else prefix

    _log_decision(payload, decision, shadow_stored)
    return v7


def v8004_shadow_telegram(signal_obj: Any, v7: dict[str, Any]) -> str:
    decision = v7.get("v8004_quality_router") if isinstance(v7.get("v8004_quality_router"), dict) else {}
    payload = _obj_dict(signal_obj)
    return (
        "🧪 V8004 SHADOW\n"
        f"Market: {decision.get('market') or payload.get('market')} {decision.get('direction') or payload.get('side')}\n"
        f"Setup: {decision.get('setup_name') or payload.get('trigger')}\n"
        f"Playbook: {decision.get('playbook')}\n"
        f"Grade: {decision.get('grade')} | Score: {decision.get('pine_score')}\n"
        f"Shadow stored: {decision.get('shadow_stored')}\n"
        f"Reason: {v7.get('reason')}"
    )


def _summary(limit: int = 150) -> dict[str, Any]:
    cfg = load_config()
    result: dict[str, Any] = {
        "ok": True,
        "version": VERSION,
        "mode": cfg.get("mode"),
        "generated_utc": _now(),
        "config": cfg,
        "safety": {
            "v8001_observe_only": True,
            "v8002_observe_only": True,
            "quality_routing_enforced": bool(cfg.get("enforce", True)),
            "live_signal_routing_changes": bool(cfg.get("enforce", True)),
            "broker_auto_execution": False,
            "promotes_existing_safety_blocks": False,
        },
        "summary": {
            "evaluated": 0,
            "live": 0,
            "shadow": 0,
            "watch_only": 0,
            "no_trade": 0,
            "a_plus": 0,
            "a": 0,
            "b": 0,
            "review": 0,
            "duplicates": 0,
        },
        "latest": [],
    }
    con = None
    try:
        con = _connect()
        _init_db(con)
        rows = con.execute(
            """
            SELECT id, created_at, signal_event_id, client_trade_id, market, direction,
                   setup_name, playbook, pine_score, server_confidence, grade, route,
                   base_allow, final_allow, duplicate, shadow_stored, reasons_json,
                   blockers_json
            FROM v8004_quality_router_log
            ORDER BY id DESC LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        latest = []
        for row in rows:
            d = dict(row)
            d["reasons"] = json.loads(d.pop("reasons_json") or "[]")
            d["blockers"] = json.loads(d.pop("blockers_json") or "[]")
            latest.append(d)
        result["latest"] = latest

        counts = con.execute(
            """
            SELECT
              COUNT(*) AS evaluated,
              SUM(CASE WHEN route='LIVE' THEN 1 ELSE 0 END) AS live,
              SUM(CASE WHEN route='SHADOW' THEN 1 ELSE 0 END) AS shadow,
              SUM(CASE WHEN route='WATCH_ONLY' THEN 1 ELSE 0 END) AS watch_only,
              SUM(CASE WHEN route='NO_TRADE' THEN 1 ELSE 0 END) AS no_trade,
              SUM(CASE WHEN grade='A_PLUS' THEN 1 ELSE 0 END) AS a_plus,
              SUM(CASE WHEN grade='A' THEN 1 ELSE 0 END) AS a,
              SUM(CASE WHEN grade='B' THEN 1 ELSE 0 END) AS b,
              SUM(CASE WHEN grade='REVIEW' THEN 1 ELSE 0 END) AS review,
              SUM(CASE WHEN duplicate=1 THEN 1 ELSE 0 END) AS duplicates
            FROM v8004_quality_router_log
            """
        ).fetchone()
        if counts:
            result["summary"] = {k: int(counts[k] or 0) for k in counts.keys()}
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass
    return result


def _fixture_payloads() -> list[tuple[str, dict[str, Any], bool, str]]:
    common = {
        "source": "tradingview_v8001_dual_playbook_zone_audit",
        "schema_version": "V8001_DUAL_PLAYBOOK_ZONE_AUDIT",
        "market": "US100",
        "direction": "LONG",
        "side": "BUY",
        "signal_timeframe": "15m",
        "timeframe": "15",
        "bar_confirmed": True,
        "is_fresh": True,
        "is_chase": False,
        "is_late_entry": False,
        "structure_event_id": 101,
        "session": "NEW_YORK",
        "bias_15m": "bullish",
        "tactical_layer_bias": "bullish",
        "htf_layer_bias": "bullish",
        "regime_bias": "bullish",
        "v8002_news_policy": {"observe_only": True, "would_hard_block": False, "hard_block": False},
    }
    return [
        (
            "A_PLUS_LIVE",
            {**common, "client_trade_id": "TEST_AP", "signal_event_id": "TEST_AP", "playbook": "PLAYBOOK_A_HTF_OB_REACTION", "setup_name": "BULL_OB_REACTION_CONFIRMED", "primary_trigger": "OB_REACTION_LONG", "confidence": 82, "zone_id": "US100_4H_DEMAND_1", "zone_touch_count": 1, "zone_first_touch": True, "reaction_confirmed": True, "confirmations_csv": "4H_DEMAND_TOUCH,SWEEP_LOW,MSS_BULL,VOLUME_SPIKE"},
            True,
            "LIVE",
        ),
        (
            "A_LIVE",
            {**common, "client_trade_id": "TEST_A", "signal_event_id": "TEST_A", "playbook": "PLAYBOOK_B_SESSION_MOMENTUM", "setup_name": "MSS_BULL", "primary_trigger": "MSS_BULL", "confidence": 72, "mss_bull": True, "confirmations_csv": "MSS_BULL,VOLUME_SPIKE"},
            True,
            "LIVE",
        ),
        (
            "B_SHADOW",
            {**common, "client_trade_id": "TEST_B", "signal_event_id": "TEST_B", "playbook": "PLAYBOOK_B_SESSION_MOMENTUM", "setup_name": "BOS_BULL", "primary_trigger": "BOS_BULL", "confidence": 62, "bos_bull": True, "bias_15m": "bullish", "tactical_layer_bias": "neutral", "htf_layer_bias": "neutral", "confirmations_csv": "BOS_BULL"},
            True,
            "SHADOW",
        ),
        (
            "SAFETY_DOWNGRADE",
            {**common, "client_trade_id": "TEST_SAFE", "signal_event_id": "TEST_SAFE", "playbook": "PLAYBOOK_A_HTF_OB_REACTION", "setup_name": "BULL_OB_REACTION_CONFIRMED", "primary_trigger": "OB_REACTION_LONG", "confidence": 82, "zone_id": "US100_4H_DEMAND_2", "zone_touch_count": 1, "zone_first_touch": True, "reaction_confirmed": True, "confirmations_csv": "4H_DEMAND_TOUCH,SWEEP_LOW,MSS_BULL"},
            False,
            "SHADOW",
        ),
        (
            "INVALID_5M",
            {**common, "client_trade_id": "TEST_5M", "signal_event_id": "TEST_5M", "signal_timeframe": "5m", "timeframe": "5", "playbook": "PLAYBOOK_B_SESSION_MOMENTUM", "setup_name": "MSS_BULL", "primary_trigger": "MSS_BULL", "confidence": 85, "mss_bull": True, "confirmations_csv": "MSS_BULL,VOLUME_SPIKE"},
            True,
            "NO_TRADE",
        ),
    ]


def selftest_payload() -> dict[str, Any]:
    cfg = load_config()
    tests = []
    for name, payload, base_allow, expected_route in _fixture_payloads():
        result = evaluate_payload(payload, {"allow_trade": base_allow, "base_confidence": payload.get("confidence")}, cfg, record=False)
        tests.append({
            "name": name,
            "expected_route": expected_route,
            "actual_route": result.get("route"),
            "grade": result.get("grade"),
            "pass": result.get("route") == expected_route,
            "blockers": result.get("blockers"),
        })
    return {
        "ok": all(t["pass"] for t in tests),
        "version": VERSION,
        "tests": tests,
        "note": "Synthetic weekend test; no signal, shadow or live row is written.",
    }


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _token_ok(request: Request) -> bool:
    try:
        from app.v7200_event_risk import _token_ok as real_token_ok
        return bool(real_token_ok(request))
    except Exception:
        return True


def _page(data: dict[str, Any], token: str = "") -> str:
    s = data.get("summary", {})
    suffix = f"?token={quote(token)}" if token else ""
    rows = "".join(
        "<tr>" + "".join(
            f"<td>{_esc(row.get(k, ''))}</td>" for k in (
                "created_at", "market", "direction", "setup_name", "playbook",
                "pine_score", "server_confidence", "grade", "route", "base_allow",
                "final_allow", "shadow_stored", "duplicate"
            )
        ) + f"<td>{_esc(', '.join(row.get('blockers') or row.get('reasons') or []))}</td></tr>"
        for row in data.get("latest", [])
    ) or "<tr><td colspan='14'>Noch keine V8004-Signale. Am Wochenende ist das normal.</td></tr>"
    return f"""<!doctype html><html lang='de'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>V8004 Quality Router</title><style>
:root{{--bg:#07111e;--panel:#101d2e;--line:#273a52;--text:#eef6ff;--muted:#9caec4;--green:#22c55e;--yellow:#facc15;--red:#f87171;--blue:#93c5fd}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:Arial}}.w{{max-width:1600px;margin:auto;padding:14px}}.hero,.c{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px;margin:10px 0;overflow:auto}}h1{{margin:0 0 8px;font-size:32px}}.sub{{color:var(--muted)}}.pills{{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}}.pill{{padding:6px 9px;border:1px solid var(--line);border-radius:999px;font-size:12px;font-weight:900}}.good{{color:var(--green)}}.warn{{color:var(--yellow)}}.bad{{color:var(--red)}}.stats{{display:grid;grid-template-columns:repeat(8,1fr);gap:8px}}.stat{{background:#0b1728;border:1px solid var(--line);border-radius:13px;padding:11px}}.label{{font-size:10px;color:var(--muted);text-transform:uppercase}}.value{{font-size:25px;font-weight:900;margin-top:4px}}a{{color:var(--blue)}}table{{width:100%;border-collapse:collapse;font-size:11px}}th,td{{padding:7px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}th{{color:#a5d8ff}}@media(max-width:900px){{.stats{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><div class='w'>
<div class='hero'><h1>V8004 Live/Shadow Quality Router</h1><div class='sub'>A+/A + bestehender Safety-Pass → LIVE · B/Review → SHADOW · Watch → WATCH ONLY · ungültig/dupliziert → NO TRADE.</div><div class='pills'><span class='pill good'>QUALITY ROUTING ENFORCED</span><span class='pill good'>V8001 OBSERVE ONLY</span><span class='pill good'>V8002 OBSERVE ONLY</span><span class='pill warn'>BROKER AUTO EXECUTION FALSE</span><span class='pill'>15m / BAR CLOSE</span></div></div>
<div class='c'><a href='/master{suffix}'>Master</a> · <a href='/master-ai-review{suffix}'>AI Review</a> · <a href='/v8004/quality-router.json{suffix}'>JSON</a> · <a href='/v8004/selftest.json{suffix}'>Selftest</a></div>
<div class='stats'>
<div class='stat'><div class='label'>Evaluated</div><div class='value'>{_esc(s.get('evaluated',0))}</div></div>
<div class='stat'><div class='label'>Live</div><div class='value good'>{_esc(s.get('live',0))}</div></div>
<div class='stat'><div class='label'>Shadow</div><div class='value warn'>{_esc(s.get('shadow',0))}</div></div>
<div class='stat'><div class='label'>Watch</div><div class='value warn'>{_esc(s.get('watch_only',0))}</div></div>
<div class='stat'><div class='label'>No Trade</div><div class='value bad'>{_esc(s.get('no_trade',0))}</div></div>
<div class='stat'><div class='label'>A+</div><div class='value good'>{_esc(s.get('a_plus',0))}</div></div>
<div class='stat'><div class='label'>A</div><div class='value good'>{_esc(s.get('a',0))}</div></div>
<div class='stat'><div class='label'>B/Review</div><div class='value warn'>{_esc(int(s.get('b',0))+int(s.get('review',0)))}</div></div>
</div>
<div class='c'><b>Wichtig:</b> LIVE ist die bestehende Server-/Telegram-Live-Pipeline. V8004 sendet keine Broker-Order und hebt keinen bestehenden Safety-Block auf.</div>
<div class='c'><table><tr><th>Zeit</th><th>Markt</th><th>Dir</th><th>Setup</th><th>Playbook</th><th>Pine</th><th>Server</th><th>Grade</th><th>Route</th><th>Base</th><th>Final</th><th>Shadow</th><th>Dup</th><th>Grund</th></tr>{rows}</table></div>
</div></body></html>"""


def _master_card(token: str, ai: bool = False) -> str:
    data = _summary(limit=1)
    s = data.get("summary", {})
    suffix = f"?token={quote(token)}" if token else ""
    title = "V8004 Quality Router"
    note = "A+/A live; B/Review shadow. Bestehende Safety-Blocks werden nie aufgehoben."
    return f"""
<section id='v8004-router' class='section'>
  <div class='sectionhead'><div><h2>{title}</h2><p>{note}</p></div></div>
  <div class='cards'>
    <a class='card' href='/v8004/quality-router{suffix}'>
      <div class='icon'>⚖️</div><div class='cardbody'><div class='cardtop'><h3>Live/Shadow Router</h3><span class='status ok'>AKTIV</span></div>
      <p>15m-Bar-Close · A+/A + Safety Pass → Live · B/Review → Shadow.</p><code>/v8004/quality-router</code></div><div class='arrow'>›</div>
    </a>
    <a class='card' href='/v8004/selftest.json{suffix}'>
      <div class='icon'>🧪</div><div class='cardbody'><div class='cardtop'><h3>V8004 Status</h3><span class='status ok'>ENFORCE</span></div>
      <p>Live {_esc(s.get('live',0))} · Shadow {_esc(s.get('shadow',0))} · Watch {_esc(s.get('watch_only',0))} · No Trade {_esc(s.get('no_trade',0))}</p><code>Broker Auto Execution: FALSE</code></div><div class='arrow'>›</div>
    </a>
  </div>
</section>
"""


def _integrate_master_html(text: str, token: str, ai: bool) -> str:
    if "id='v8004-router'" in text:
        return text
    suffix = f"?token={quote(token)}" if token else ""
    nav_anchor = f"<a class='nav' href='/v8002/news-policy{suffix}'>V8002</a>"
    if nav_anchor in text:
        text = text.replace(nav_anchor, nav_anchor + f"<a class='nav' href='/v8004/quality-router{suffix}'>V8004</a>", 1)

    old_pills = "<span class='pill safe'>OBSERVE ONLY</span><span class='pill safe'>ENFORCE FALSE</span><span class='pill safe'>LIVE RULE CHANGES FALSE</span><span class='pill'>V8003 NAVIGATION ONLY</span>"
    new_pills = "<span class='pill safe'>V8001 OBSERVE ONLY</span><span class='pill safe'>V8002 OBSERVE ONLY</span><span class='pill safe'>V8004 ROUTING ENFORCED</span><span class='pill'>BROKER AUTO EXECUTION FALSE</span><span class='pill'>V8003 NAVIGATION ONLY</span>"
    text = text.replace(old_pills, new_pills, 1)

    text = text.replace(
        "V8000/V8001/V8002 · klare Navigation statt mehrfacher Master-Overlays",
        "V8000–V8004 · klare Navigation · gute V8001-Signale live, Review-Signale shadow",
        1,
    )
    text = text.replace(
        "Observe-only AI-Review-Pipeline · keine automatischen Blocks · keine Live-Regeländerungen",
        "AI-Review bleibt beobachtend · V8004 routet ausschließlich neue V8001-Signale nach Live/Shadow",
        1,
    )

    marker = "<section id='current-inputs' class='section'>" if ai else "<section id='current' class='section'>"
    card = _master_card(token, ai=ai)
    if marker in text:
        text = text.replace(marker, card + marker, 1)
    elif "</body>" in text:
        text = text.replace("</body>", card + "</body>", 1)
    else:
        text += card
    return text


class V8004MasterIntegrationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or _upper(scope.get("method") or "GET") != "GET":
            await self.app(scope, receive, send)
            return
        path = _text(scope.get("path"))
        if path not in {"/master", "/master-ai-review"}:
            await self.app(scope, receive, send)
            return

        start: dict[str, Any] | None = None
        chunks: list[bytes] = []

        async def capture(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = dict(message)
                return
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                if start is None:
                    return
                body = b"".join(chunks)
                headers = list(start.get("headers", []))
                ctype = next((v.decode("latin1") for k, v in headers if k.lower() == b"content-type"), "")
                if start.get("status") == 200 and "text/html" in ctype:
                    query = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="ignore"))
                    token = (query.get("token") or [""])[0]
                    text = body.decode("utf-8", errors="ignore")
                    body = _integrate_master_html(text, token, ai=(path == "/master-ai-review")).encode("utf-8")
                headers = [(k, v) for k, v in headers if k.lower() not in {b"content-length", b"content-encoding"}]
                headers.append((b"content-length", str(len(body)).encode("ascii")))
                start["headers"] = headers
                await send(start)
                await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, capture)


def install_v8004_quality_router(app) -> None:
    if getattr(app.state, "v8004_quality_router_installed", False):
        return

    @app.get("/v8004/quality-router", response_class=HTMLResponse)
    def v8004_page(request: Request, limit: int = 150):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        return HTMLResponse(_page(_summary(limit), request.query_params.get("token", "")))

    @app.get("/v8004/quality-router.json")
    def v8004_json(request: Request, limit: int = 150):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_summary(limit))

    @app.get("/v8004/quality-router-config.json")
    def v8004_config(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        cfg = load_config()
        return JSONResponse({
            "ok": True,
            "version": VERSION,
            "config": cfg,
            "safety": {
                "v8001_observe_only": True,
                "v8002_observe_only": True,
                "quality_routing_enforced": bool(cfg.get("enforce", True)),
                "live_signal_routing_changes": bool(cfg.get("enforce", True)),
                "broker_auto_execution": False,
                "promotes_existing_safety_blocks": False,
            },
        })

    @app.get("/v8004/selftest.json")
    def v8004_selftest(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(selftest_payload())

    app.add_middleware(V8004MasterIntegrationMiddleware)
    app.state.v8004_quality_router_installed = True
    print("[V8004] Live/Shadow quality router installed (V8001 only, broker auto execution false)")
