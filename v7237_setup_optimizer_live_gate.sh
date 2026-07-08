#!/usr/bin/env bash
set -euo pipefail

cd /opt/tradingbot_v6000 || exit 1

TS="$(date -u +%Y%m%d_%H%M%S)"
TOKEN_DEFAULT="eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg"

echo "== V7237 SETUP OPTIMIZER & LIVE GATE PACK =="
echo "Mode: OBSERVE-ONLY by default. No live blocking unless config apply_to_live=true is set later."

mkdir -p backups app data ops

echo "== Backup =="
tar -czf "backups/v7237_setup_optimizer_before_${TS}.tar.gz" \
  --exclude='backups' \
  --exclude='__pycache__' \
  --exclude='.git' \
  . || true

echo "== Config =="
cat > data/v7237_setup_optimizer_config.json <<'JSON'
{
  "version": "V7237",
  "enabled": true,
  "apply_to_live": false,
  "observe_only": true,
  "min_samples_setup": 3,
  "min_samples_market_setup": 2,
  "long_reversal_min_confidence": 75.0,
  "short_reversal_min_confidence": 65.0,
  "default_min_confidence": 62.0,
  "block_wait_grade_live": true,
  "block_event_high_or_hardblock_live": true,
  "shadow_only_setups": [
    "BPR_RETEST",
    "BEAR_FVG_RETEST"
  ],
  "long_reversal_block_premium": true,
  "short_reversal_block_discount": true,
  "notes": "Observe-only optimizer. It reports what should be shadow-only or blocked, but does not change execution while apply_to_live=false."
}
JSON

echo "== Module =="
cat > app/v7237_setup_optimizer_live_gate.py <<'PY'
import json
import html
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, Counter
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
        return {"load_error": str(exc)}
    return default


def _write_json(name, obj):
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _cfg():
    c = {
        "version": "V7237",
        "enabled": True,
        "apply_to_live": False,
        "observe_only": True,
        "min_samples_setup": 3,
        "min_samples_market_setup": 2,
        "long_reversal_min_confidence": 75.0,
        "short_reversal_min_confidence": 65.0,
        "default_min_confidence": 62.0,
        "block_wait_grade_live": True,
        "block_event_high_or_hardblock_live": True,
        "shadow_only_setups": ["BPR_RETEST", "BEAR_FVG_RETEST"],
        "long_reversal_block_premium": True,
        "short_reversal_block_discount": True,
        "notes": "Observe-only optimizer. It reports what should be shadow-only or blocked, but does not change execution while apply_to_live=false.",
    }
    c.update(_read_json("v7237_setup_optimizer_config.json", {}))
    return c


def _token_ok(request: Request):
    try:
        from app.v7200_event_risk import _token_ok as real_token_ok
        return real_token_ok(request)
    except Exception:
        return bool(request.query_params.get("token", ""))


def _safe_event():
    try:
        from app.v7200_event_risk import _event_data
        return _event_data()
    except Exception as exc:
        return {"risk_level": "ERROR", "cooldown_active": False, "upcoming_count": 0, "load_error": str(exc)}


def _safe_control():
    try:
        from app.v7207_master_compact_control_center import _status
        return _status()
    except Exception as exc:
        return {"safe_state": False, "risky_on": True, "load_error": str(exc)}


def _connect():
    p = _data("v7000_learning.sqlite3")
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    return con


def _num(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(str(x).replace(",", "."))
    except Exception:
        return default


def _parse_notes(notes):
    out = {}
    if not notes:
        return out
    txt = str(notes)
    for part in txt.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _json_loads(x):
    try:
        if not x:
            return {}
        y = json.loads(x)
        return y if isinstance(y, dict) else {}
    except Exception:
        return {}


def _nested_get(d, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def _session_from_dt(s):
    if not s:
        return "UNKNOWN"
    try:
        t = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        h = dt.hour
    except Exception:
        return "UNKNOWN"
    if 0 <= h < 6:
        return "ASIA"
    if 6 <= h < 12:
        return "LONDON"
    if 12 <= h < 17:
        return "NY"
    if 17 <= h < 21:
        return "NY_LATE"
    return "AFTER_HOURS"


def _load_trade_outcomes():
    rows = []
    try:
        con = _connect()
        q = "select * from trade_outcomes order by id asc"
        for r in con.execute(q).fetchall():
            d = dict(r)
            n = _parse_notes(d.get("notes"))
            result = str(d.get("result", "")).upper()
            pnl_r = _num(d.get("pnl_r"), None)
            rows.append({
                "source": "LIVE",
                "id": d.get("id"),
                "decision_id": d.get("decision_id"),
                "client_trade_id": n.get("client_trade_id"),
                "market": str(n.get("market", "UNKNOWN")).upper(),
                "direction": str(n.get("direction", "UNKNOWN")).upper(),
                "setup_name": n.get("setup", "UNKNOWN"),
                "result": result,
                "pnl_r": pnl_r if pnl_r is not None else (1.6 if result == "WIN" else -1.0 if result == "LOSS" else 0.0),
                "entry": _num(n.get("entry"), None),
                "sl": _num(n.get("sl"), None),
                "tp1": _num(n.get("tp1"), None),
                "exit_price": _num(d.get("exit_price"), None),
                "closed_at": d.get("closed_at"),
                "session": _session_from_dt(d.get("closed_at")),
                "notes": d.get("notes", ""),
            })
    except Exception as exc:
        rows.append({"source": "ERROR", "error": str(exc), "market": "ERROR", "direction": "ERROR", "setup_name": "ERROR", "pnl_r": 0.0})
    finally:
        try:
            con.close()
        except Exception:
            pass
    return rows


def _load_shadow_outcomes():
    rows = []
    try:
        con = _connect()
        q = "select * from shadow_outcomes order by id asc"
        for r in con.execute(q).fetchall():
            d = dict(r)
            result = str(d.get("result", "")).upper()
            pnl_r = _num(d.get("pnl_r"), None)
            n = _parse_notes(d.get("notes"))
            rows.append({
                "source": "SHADOW",
                "id": d.get("id"),
                "decision_id": None,
                "client_trade_id": d.get("client_trade_id"),
                "market": str(d.get("market", "UNKNOWN")).upper(),
                "direction": str(d.get("direction", "UNKNOWN")).upper(),
                "setup_name": d.get("setup_name", "UNKNOWN"),
                "result": result,
                "pnl_r": pnl_r if pnl_r is not None else (1.6 if result == "WIN" else -1.0 if result == "LOSS" else 0.0),
                "entry": _num(n.get("entry"), None),
                "sl": _num(n.get("sl"), None),
                "tp1": _num(n.get("tp1"), None),
                "exit_price": _num(d.get("exit_price"), None),
                "closed_at": d.get("closed_at"),
                "session": _session_from_dt(d.get("closed_at")),
                "notes": d.get("notes", ""),
            })
    except Exception:
        pass
    finally:
        try:
            con.close()
        except Exception:
            pass
    return rows


def _load_decisions():
    rows = []
    try:
        con = _connect()
        for r in con.execute("select * from setup_decisions order by id asc").fetchall():
            d = dict(r)
            f = _json_loads(d.get("features_json"))
            v6000 = f.get("v6000_decision", {}) if isinstance(f.get("v6000_decision"), dict) else {}
            news = f.get("news_bias_snapshot", {}) if isinstance(f.get("news_bias_snapshot"), dict) else {}
            market_news = {}
            try:
                market_news = news.get("markets", {}).get(str(d.get("market")).upper(), {})
            except Exception:
                market_news = {}
            rows.append({
                "id": d.get("id"),
                "market": str(d.get("market", "UNKNOWN")).upper(),
                "direction": str(d.get("direction", "UNKNOWN")).upper(),
                "setup_name": d.get("setup_name", "UNKNOWN"),
                "session": d.get("session"),
                "timeframe": d.get("timeframe"),
                "entry": _num(d.get("entry"), None),
                "sl": _num(d.get("sl"), None),
                "tp1": _num(d.get("tp1"), None),
                "technical_score": _num(d.get("technical_score"), None),
                "news_score": _num(d.get("news_score"), 0.0),
                "risk_r": _num(d.get("risk_r"), 0.0),
                "grade": v6000.get("grade"),
                "decision": v6000.get("decision"),
                "v6000_score": _num(v6000.get("score"), None),
                "trend_state": f.get("trend_state"),
                "volume_state": f.get("volume_state"),
                "vwap_state": f.get("vwap_state"),
                "structure_state": f.get("structure_state"),
                "smc_score": _num(f.get("smc_score"), None),
                "vp_score": _num(f.get("vp_score"), None),
                "delta_score": _num(f.get("delta_score"), None),
                "premium_discount_state": _nested_get(f, "signal", "premium_discount_state", default=None) or f.get("premium_discount_state"),
                "htf_bias": _nested_get(f, "signal", "htf_bias", default=None) or f.get("htf_bias"),
                "trade_bias": _nested_get(f, "signal", "trade_bias", default=None) or f.get("trade_bias"),
                "calendar_hard_block": bool(market_news.get("calendar_hard_block", False)),
                "calendar_phase": market_news.get("calendar_phase"),
                "event_risk": market_news.get("event_risk"),
                "created_at": d.get("created_at"),
            })
    except Exception as exc:
        rows.append({"id": None, "market": "ERROR", "error": str(exc)})
    finally:
        try:
            con.close()
        except Exception:
            pass
    return rows


def _load_audit():
    rows = []
    try:
        con = _connect()
        for r in con.execute("select * from signal_audit order by id asc").fetchall():
            d = dict(r)
            raw = _json_loads(d.get("raw_json"))
            signal = raw.get("signal", {}) if isinstance(raw.get("signal"), dict) else {}
            v6000 = raw.get("v6000", {}) if isinstance(raw.get("v6000"), dict) else {}
            rows.append({
                "id": d.get("id"),
                "created_at": d.get("created_at"),
                "client_trade_id": d.get("client_trade_id"),
                "market": str(d.get("market", "UNKNOWN")).upper(),
                "direction": str(d.get("direction", "UNKNOWN")).upper(),
                "setup_name": d.get("trigger", "UNKNOWN"),
                "timeframe": d.get("timeframe"),
                "entry": _num(d.get("entry"), None),
                "technical_score": _num(d.get("technical_score"), None),
                "confidence": _num(d.get("confidence"), None),
                "news_score": _num(d.get("news_score"), 0.0),
                "event_risk": d.get("event_risk"),
                "risk_r": _num(d.get("risk_r"), 0.0),
                "allow_trade": int(d.get("allow_trade") or 0),
                "reason": d.get("reason"),
                "bias_gate": d.get("bias_gate"),
                "entry_state": d.get("entry_state"),
                "chase_state": d.get("chase_state"),
                "impulse_state": d.get("impulse_state"),
                "grade": v6000.get("grade"),
                "decision": v6000.get("decision"),
                "setup_quality": signal.get("setup_quality"),
                "trade_bias": signal.get("trade_bias"),
                "htf_bias": signal.get("htf_bias"),
                "premium_discount_state": signal.get("premium_discount_state"),
                "delta_state": signal.get("delta_state"),
                "cvd_state": signal.get("cvd_state"),
                "rel_volume": _num(signal.get("rel_volume"), None),
            })
    except Exception:
        pass
    finally:
        try:
            con.close()
        except Exception:
            pass
    return rows


def _all_closed_trades(include_shadow=True):
    rows = _load_trade_outcomes()
    if include_shadow:
        rows += _load_shadow_outcomes()
    for r in rows:
        r["side_setup"] = f'{r.get("direction")} {r.get("setup_name")}'
        r["market_setup"] = f'{r.get("market")} {r.get("setup_name")}'
    return rows


def _agg(rows, keys):
    g = defaultdict(list)
    for r in rows:
        k = tuple(r.get(x, "-") for x in keys)
        g[k].append(r)
    out = []
    for key, items in g.items():
        n = len(items)
        wins = sum(1 for x in items if float(x.get("pnl_r") or 0) > 0)
        losses = sum(1 for x in items if float(x.get("pnl_r") or 0) < 0)
        total_r = round(sum(float(x.get("pnl_r") or 0) for x in items), 4)
        wr = round(wins / n * 100, 2) if n else 0.0
        avg_r = round(total_r / n, 4) if n else 0.0
        row = {
            "count": n,
            "wins": wins,
            "losses": losses,
            "winrate": wr,
            "avg_r": avg_r,
            "total_r": total_r,
            "latest": items[-5:],
        }
        for i, kk in enumerate(keys):
            row[kk] = key[i]
        if n >= 3 and total_r > 0 and wr >= 50:
            row["edge_state"] = "POSITIVE"
        elif n >= 2 and (total_r < 0 or wr < 45):
            row["edge_state"] = "WEAK"
        elif n < 3:
            row["edge_state"] = "SMALL_SAMPLE"
        else:
            row["edge_state"] = "NEUTRAL"
        out.append(row)
    out.sort(key=lambda x: (x.get("total_r", 0), x.get("winrate", 0), x.get("count", 0)), reverse=True)
    return out


def _threshold_for(setup, direction):
    cfg = _cfg()
    s = str(setup or "").upper()
    d = str(direction or "").upper()
    if "REVERSAL_LONG" in s or (d == "LONG" and "REVERSAL" in s):
        return float(cfg.get("long_reversal_min_confidence", 75.0))
    if "REVERSAL_SHORT" in s or (d == "SHORT" and "REVERSAL" in s):
        return float(cfg.get("short_reversal_min_confidence", 65.0))
    return float(cfg.get("default_min_confidence", 62.0))


def _grade_live_gate(row):
    cfg = _cfg()
    reasons = []
    setup = str(row.get("setup_name", "")).upper()
    direction = str(row.get("direction", "")).upper()
    conf = _num(row.get("confidence"), _num(row.get("technical_score"), 0.0)) or 0.0
    threshold = _threshold_for(setup, direction)

    if setup in [str(x).upper() for x in cfg.get("shadow_only_setups", [])]:
        reasons.append("SETUP_SHADOW_ONLY")

    if cfg.get("block_wait_grade_live", True):
        if str(row.get("grade", "")).upper() == "WAIT" or str(row.get("decision", "")).upper() == "WAIT":
            reasons.append("WAIT_GRADE")

    if conf < threshold:
        reasons.append(f"CONFIDENCE_BELOW_{threshold:g}")

    er = str(row.get("event_risk", "")).upper()
    hard = bool(row.get("calendar_hard_block"))
    if cfg.get("block_event_high_or_hardblock_live", True) and (er == "HIGH" or hard or str(row.get("calendar_phase", "")).upper() == "HARD_BLOCK"):
        reasons.append("EVENT_HIGH_OR_HARDBLOCK")

    pd = str(row.get("premium_discount_state", "")).lower()
    if direction == "LONG" and cfg.get("long_reversal_block_premium", True) and "premium" in pd:
        reasons.append("LONG_IN_PREMIUM")
    if direction == "SHORT" and cfg.get("short_reversal_block_discount", True) and "discount" in pd:
        reasons.append("SHORT_IN_DISCOUNT")

    action = "LIVE_OK" if not reasons else "SHADOW_ONLY"
    severity = "OK" if not reasons else ("HIGH" if any(x in reasons for x in ["EVENT_HIGH_OR_HARDBLOCK", "WAIT_GRADE", "SETUP_SHADOW_ONLY"]) else "MEDIUM")

    return {
        "action": action,
        "severity": severity,
        "reasons": reasons,
        "confidence_used": round(conf, 2),
        "threshold": threshold,
    }


def _performance_report():
    rows_live = _load_trade_outcomes()
    rows_shadow = _load_shadow_outcomes()
    rows_all = rows_live + rows_shadow

    total_r = round(sum(float(x.get("pnl_r") or 0) for x in rows_all), 4)
    wins = sum(1 for x in rows_all if float(x.get("pnl_r") or 0) > 0)
    losses = sum(1 for x in rows_all if float(x.get("pnl_r") or 0) < 0)
    count = len(rows_all)

    return {
        "version": "V7237",
        "mode": "SETUP_OPTIMIZER_PERFORMANCE",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "total": {
            "count": count,
            "wins": wins,
            "losses": losses,
            "winrate": round(wins / count * 100, 2) if count else 0,
            "total_r": total_r,
            "avg_r": round(total_r / count, 4) if count else 0,
        },
        "live_total": {
            "count": len(rows_live),
            "total_r": round(sum(float(x.get("pnl_r") or 0) for x in rows_live), 4),
        },
        "shadow_total": {
            "count": len(rows_shadow),
            "total_r": round(sum(float(x.get("pnl_r") or 0) for x in rows_shadow), 4),
        },
        "by_setup": _agg(rows_all, ["setup_name"]),
        "by_direction_setup": _agg(rows_all, ["direction", "setup_name"]),
        "by_market_setup": _agg(rows_all, ["market", "setup_name"]),
        "by_source_setup": _agg(rows_all, ["source", "setup_name"]),
        "latest": rows_all[-30:],
        "observe_only": not bool(_cfg().get("apply_to_live")),
    }


def _entry_quality_report():
    audit = _load_audit()
    decisions = _load_decisions()

    reviewed = []
    for r in audit[-300:]:
        gate = _grade_live_gate(r)
        item = dict(r)
        item["optimizer_gate"] = gate
        reviewed.append(item)

    blocked_but_good = [x for x in reviewed if x.get("allow_trade") == 0 and x.get("optimizer_gate", {}).get("action") == "LIVE_OK"]
    allowed_but_bad = [x for x in reviewed if x.get("allow_trade") == 1 and x.get("optimizer_gate", {}).get("action") != "LIVE_OK"]

    wait_decisions = [x for x in decisions if str(x.get("grade", "")).upper() == "WAIT" or str(x.get("decision", "")).upper() == "WAIT"]
    hardblock_decisions = [x for x in decisions if x.get("calendar_hard_block") or str(x.get("event_risk", "")).upper() == "HIGH"]

    return {
        "version": "V7237",
        "mode": "ENTRY_QUALITY_REPORT",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "audit_rows": len(audit),
        "reviewed_rows": len(reviewed),
        "allowed_but_optimizer_shadow_only": allowed_but_bad[-100:],
        "blocked_but_optimizer_live_ok": blocked_but_good[-100:],
        "wait_decision_count": len(wait_decisions),
        "hardblock_or_high_event_decision_count": len(hardblock_decisions),
        "latest_reviewed": reviewed[-100:],
        "observe_only": not bool(_cfg().get("apply_to_live")),
    }


def _event_leak_report():
    decisions = _load_decisions()
    hard = [x for x in decisions if x.get("calendar_hard_block") or str(x.get("event_risk", "")).upper() == "HIGH"]
    live = _load_trade_outcomes()
    live_ids = {str(x.get("decision_id")): x for x in live if x.get("decision_id") is not None}
    leaks = []
    for d in hard:
        lid = str(d.get("id"))
        if lid in live_ids:
            item = dict(d)
            item["closed_trade"] = live_ids[lid]
            leaks.append(item)

    return {
        "version": "V7237",
        "mode": "EVENT_LEAK_REPORT",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "high_or_hardblock_decisions": len(hard),
        "live_trades_during_high_or_hardblock": len(leaks),
        "leaks": leaks[-100:],
        "observe_only": not bool(_cfg().get("apply_to_live")),
    }


def _weak_combo_report():
    perf = _performance_report()
    weak_setup = [x for x in perf.get("by_setup", []) if x.get("edge_state") == "WEAK" or str(x.get("setup_name")).upper() in [str(s).upper() for s in _cfg().get("shadow_only_setups", [])]]
    weak_dir_setup = [x for x in perf.get("by_direction_setup", []) if x.get("edge_state") == "WEAK"]
    weak_market_setup = [x for x in perf.get("by_market_setup", []) if x.get("edge_state") == "WEAK"]

    return {
        "version": "V7237",
        "mode": "WEAK_COMBO_REPORT",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "shadow_only_setups_config": _cfg().get("shadow_only_setups", []),
        "weak_setups": weak_setup,
        "weak_direction_setups": weak_dir_setup,
        "weak_market_setups": weak_market_setup,
        "recommendations": [
            "BPR_RETEST and BEAR_FVG_RETEST remain shadow-only until enough positive sample exists.",
            "REVERSAL_LONG_MSS_RECLAIM needs higher confidence and no high event risk.",
            "REVERSAL_SHORT_MSS_RECLAIM can stay candidate, but still needs confidence and event filters.",
            "Do not reduce TP below current R structure; improve entry gating first."
        ],
        "observe_only": not bool(_cfg().get("apply_to_live")),
    }


def _rules():
    cfg = _cfg()
    return {
        "version": "V7237",
        "mode": "SETUP_OPTIMIZER_RULES",
        "apply_to_live": bool(cfg.get("apply_to_live")),
        "observe_only": not bool(cfg.get("apply_to_live")),
        "rules": [
            {
                "name": "WAIT_TO_SHADOW",
                "enabled": cfg.get("block_wait_grade_live", True),
                "logic": "If grade/decision is WAIT, recommend SHADOW_ONLY.",
            },
            {
                "name": "EVENT_HIGH_HARDBLOCK_TO_SHADOW",
                "enabled": cfg.get("block_event_high_or_hardblock_live", True),
                "logic": "If event_risk HIGH or calendar_hard_block/hard_block phase, recommend SHADOW_ONLY.",
            },
            {
                "name": "SETUP_SHADOW_ONLY",
                "enabled": True,
                "setups": cfg.get("shadow_only_setups", []),
            },
            {
                "name": "LONG_REVERSAL_CONFIDENCE",
                "min_confidence": cfg.get("long_reversal_min_confidence", 75.0),
                "logic": "Long reversal needs stronger threshold.",
            },
            {
                "name": "SHORT_REVERSAL_CONFIDENCE",
                "min_confidence": cfg.get("short_reversal_min_confidence", 65.0),
                "logic": "Short reversal can use lower threshold but still no event hardblock.",
            },
            {
                "name": "PREMIUM_DISCOUNT_FILTER",
                "long_reversal_block_premium": cfg.get("long_reversal_block_premium", True),
                "short_reversal_block_discount": cfg.get("short_reversal_block_discount", True),
            },
        ],
    }


def _optimizer_summary(write_file=False):
    perf = _performance_report()
    entry = _entry_quality_report()
    leaks = _event_leak_report()
    weak = _weak_combo_report()
    control = _safe_control()
    ev = _safe_event()

    out = {
        "version": "V7237",
        "mode": "SETUP_OPTIMIZER_SUMMARY",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "safe_state": control.get("safe_state"),
        "risky_on": control.get("risky_on"),
        "event_risk": {
            "risk_level": ev.get("risk_level"),
            "cooldown_active": ev.get("cooldown_active"),
            "upcoming_count": ev.get("upcoming_count"),
        },
        "performance_total": perf.get("total"),
        "best_direction_setups": perf.get("by_direction_setup", [])[:5],
        "weak_direction_setups": weak.get("weak_direction_setups", [])[:10],
        "weak_setups": weak.get("weak_setups", [])[:10],
        "event_leaks": leaks.get("live_trades_during_high_or_hardblock"),
        "allowed_but_optimizer_shadow_only": len(entry.get("allowed_but_optimizer_shadow_only", [])),
        "rules": _rules(),
        "observe_only": not bool(_cfg().get("apply_to_live")),
    }
    if write_file:
        _write_json("v7237_setup_optimizer_report_last.json", out)
    return out


def _links(token):
    links = [
        ("Optimizer", "/setup-optimizer"),
        ("Live Gate", "/live-gate-review"),
        ("Weak Combos", "/weak-combo-report"),
        ("Entry Quality", "/entry-quality-report"),
        ("Event Leaks", "/event-leak-report"),
        ("Rules", "/setup-optimizer-rules.json"),
        ("Performance", "/performance-learning"),
        ("Risk", "/risk-management"),
        ("Master", "/master"),
    ]
    return " · ".join(f'<a href="{url}?token={_esc(token)}">{_esc(label)}</a>' for label, url in links)


def _html_page(title, body, request):
    token = request.query_params.get("token", "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>
body{{background:#0b0f14;color:#e8eef5;font-family:Arial,sans-serif;margin:24px}}
a{{color:#8cc8ff;text-decoration:none}}.card{{background:#121923;border:1px solid #263447;border-radius:12px;padding:16px;margin-bottom:18px}}
.badge{{display:inline-block;padding:7px 11px;border-radius:999px;color:white;font-weight:bold;background:#1f6f3d}}
.warn{{background:#8a6a1f}}.danger{{background:#8a1f1f}}table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}}
th,td{{border-bottom:1px solid #263447;padding:8px;text-align:left;vertical-align:top}}th{{color:#a9bfd6}}.muted{{color:#9fb0c0}}
.good{{color:#39d06f}}.bad{{color:#ff5c67}}.yellow{{color:#f3c747}}
pre{{white-space:pre-wrap;background:#0f1721;border:1px solid #263447;border-radius:10px;padding:12px}}
@media(max-width:760px){{body{{margin:14px}}table{{font-size:12px}}th,td{{padding:6px}}}}
</style></head><body><h1>{_esc(title)}</h1><div class="card">{_links(token)}</div>{body}</body></html>"""


def _perf_table(title, rows, keys):
    trs = ""
    for r in rows:
        name = " / ".join(str(r.get(k, "-")) for k in keys)
        cls = "good" if r.get("total_r", 0) > 0 else ("bad" if r.get("total_r", 0) < 0 else "")
        trs += f"""<tr><td>{_esc(name)}</td><td>{_esc(r.get("count"))}</td><td>{_esc(r.get("wins"))}</td><td>{_esc(r.get("losses"))}</td>
<td>{_esc(r.get("winrate"))}%</td><td class="{cls}">{_esc(r.get("total_r"))}</td><td>{_esc(r.get("avg_r"))}</td><td>{_esc(r.get("edge_state"))}</td></tr>"""
    if not trs:
        trs = "<tr><td colspan='8'>Keine Daten.</td></tr>"
    return f"""<div class="card"><h2>{_esc(title)}</h2><table>
<tr><th>Name</th><th>N</th><th>W</th><th>L</th><th>WR</th><th>Total R</th><th>Avg R</th><th>State</th></tr>{trs}</table></div>"""


def _gate_table(title, rows):
    trs = ""
    for r in rows:
        gate = r.get("optimizer_gate", {})
        cls = "good" if gate.get("action") == "LIVE_OK" else "yellow"
        trs += f"""<tr><td>{_esc(r.get("created_at"))}</td><td>{_esc(r.get("market"))}</td><td>{_esc(r.get("direction"))}</td>
<td>{_esc(r.get("setup_name"))}</td><td>{_esc(r.get("confidence"))}</td><td>{_esc(r.get("grade"))}</td>
<td>{_esc(r.get("event_risk"))}</td><td class="{cls}">{_esc(gate.get("action"))}</td><td>{_esc(gate.get("reasons"))}</td></tr>"""
    if not trs:
        trs = "<tr><td colspan='9'>Keine Daten.</td></tr>"
    return f"""<div class="card"><h2>{_esc(title)}</h2><table>
<tr><th>Time</th><th>Market</th><th>Dir</th><th>Setup</th><th>Conf</th><th>Grade</th><th>Event</th><th>Gate</th><th>Reasons</th></tr>{trs}</table></div>"""


def install_v7237_setup_optimizer_live_gate(app):
    if getattr(app.state, "v7237_setup_optimizer_installed", False):
        return

    @app.get("/setup-optimizer", response_class=HTMLResponse)
    def setup_optimizer_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        s = _optimizer_summary(write_file=True)
        p = _performance_report()
        body = f"""<div class="card"><span class="badge">V7237 SETUP OPTIMIZER</span>
<p class="muted">Observe-only: <b>{_esc(s.get("observe_only"))}</b> | Event leaks: <b>{_esc(s.get("event_leaks"))}</b> | Allowed but optimizer shadow-only: <b>{_esc(s.get("allowed_but_optimizer_shadow_only"))}</b></p>
<a href="/setup-optimizer.json?token={_esc(request.query_params.get("token",""))}">JSON</a></div>
{_perf_table("Direction + Setup", p.get("by_direction_setup", []), ["direction", "setup_name"])}
{_perf_table("Market + Setup", p.get("by_market_setup", []), ["market", "setup_name"])}
{_perf_table("Setup", p.get("by_setup", []), ["setup_name"])}"""
        return HTMLResponse(_html_page("TradingBot V7237 - Setup Optimizer", body, request))

    @app.get("/setup-optimizer.json")
    def setup_optimizer_json(request: Request, write: Optional[int] = 1):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_optimizer_summary(write_file=bool(int(write))))

    @app.get("/setup-optimizer-config.json")
    def setup_optimizer_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    @app.get("/setup-optimizer-rules.json")
    def setup_optimizer_rules_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_rules())

    @app.get("/live-gate-review", response_class=HTMLResponse)
    def live_gate_review_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _entry_quality_report()
        body = f"""<div class="card"><span class="badge warn">LIVE GATE REVIEW</span>
<p class="muted">Allowed but optimizer says shadow-only: <b>{_esc(len(r.get("allowed_but_optimizer_shadow_only", [])))}</b> | Wait decisions: <b>{_esc(r.get("wait_decision_count"))}</b> | High/hardblock decisions: <b>{_esc(r.get("hardblock_or_high_event_decision_count"))}</b></p></div>
{_gate_table("Latest Gate Review", r.get("latest_reviewed", [])[-80:])}"""
        return HTMLResponse(_html_page("TradingBot V7237 - Live Gate Review", body, request))

    @app.get("/live-gate-review.json")
    def live_gate_review_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_entry_quality_report())

    @app.get("/weak-combo-report", response_class=HTMLResponse)
    def weak_combo_report_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _weak_combo_report()
        body = _perf_table("Weak Setups", r.get("weak_setups", []), ["setup_name"])
        body += _perf_table("Weak Direction + Setup", r.get("weak_direction_setups", []), ["direction", "setup_name"])
        body += _perf_table("Weak Market + Setup", r.get("weak_market_setups", []), ["market", "setup_name"])
        return HTMLResponse(_html_page("TradingBot V7237 - Weak Combo Report", body, request))

    @app.get("/weak-combo-report.json")
    def weak_combo_report_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_weak_combo_report())

    @app.get("/entry-quality-report", response_class=HTMLResponse)
    def entry_quality_report_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _entry_quality_report()
        body = _gate_table("Allowed but Optimizer says Shadow-only", r.get("allowed_but_optimizer_shadow_only", []))
        body += _gate_table("Blocked but Optimizer says Live OK", r.get("blocked_but_optimizer_live_ok", []))
        return HTMLResponse(_html_page("TradingBot V7237 - Entry Quality Report", body, request))

    @app.get("/entry-quality-report.json")
    def entry_quality_report_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_entry_quality_report())

    @app.get("/event-leak-report", response_class=HTMLResponse)
    def event_leak_report_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _event_leak_report()
        rows = []
        for x in r.get("leaks", []):
            t = x.get("closed_trade", {})
            rows.append({
                "created_at": x.get("created_at"),
                "market": x.get("market"),
                "direction": x.get("direction"),
                "setup_name": x.get("setup_name"),
                "confidence": x.get("technical_score"),
                "grade": x.get("grade"),
                "event_risk": x.get("event_risk"),
                "optimizer_gate": {"action": "EVENT_LEAK", "reasons": ["LIVE_DURING_HIGH_OR_HARDBLOCK", t.get("result"), t.get("pnl_r")]},
            })
        body = f"""<div class="card"><span class="badge danger">EVENT LEAK REPORT</span>
<p class="muted">High/hardblock decisions: <b>{_esc(r.get("high_or_hardblock_decisions"))}</b> | Live leaks: <b>{_esc(r.get("live_trades_during_high_or_hardblock"))}</b></p></div>"""
        body += _gate_table("Live Trades During High/Hardblock", rows)
        return HTMLResponse(_html_page("TradingBot V7237 - Event Leak Report", body, request))

    @app.get("/event-leak-report.json")
    def event_leak_report_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_event_leak_report())

    app.state.v7237_setup_optimizer_installed = True
    print("[V7237] Setup Optimizer & Live Gate installed")
PY

echo "== Header targets =="
python3 - <<'PY'
from pathlib import Path

p = Path("app/v7208_single_header_mode.py")
if p.exists():
    s = p.read_text(encoding="utf-8")
    routes = [
        "/setup-optimizer", "/live-gate-review", "/weak-combo-report",
        "/entry-quality-report", "/event-leak-report",
    ]
    try:
        start = s.index("TARGET_PATHS = {")
        end = s.index("}", start) + 1
        block = s[start:end]
        existing = set()
        for line in block.splitlines():
            line = line.strip().strip(",")
            if line.startswith('"') and line.endswith('"'):
                existing.add(line.strip('"'))
        existing.update(routes)
        ordered = [
            "/master", "/markets", "/event-risk", "/trade-protection", "/pre-news-manager",
            "/entry-scoring", "/signal-quality", "/ranking-snapshot", "/control-center",
            "/decision-suite", "/decision-explain", "/decision-playbook", "/decision-review",
            "/market-regime", "/session-bias", "/mtf-confirmation", "/rotation-board",
            "/candidate-inbox", "/daily-intelligence", "/performance-learning",
            "/setup-performance", "/market-session-performance", "/news-performance",
            "/shadow-edge", "/best-times", "/weak-setups", "/daily-performance-report",
            "/risk-management", "/position-overview", "/open-trade-risk", "/exit-readiness",
            "/sl-tp-review", "/cluster-exposure", "/open-trade-news-risk",
            "/trade-management-recommendations", "/trade-management-log", "/daily-risk-report",
            "/setup-optimizer", "/live-gate-review", "/weak-combo-report",
            "/entry-quality-report", "/event-leak-report",
        ]
        rest = sorted(existing - set(ordered))
        new = "TARGET_PATHS = {\n" + "\n".join(f'    "{r}",' for r in ordered + rest) + "\n}"
        s = s[:start] + new + s[end:]
    except Exception:
        pass

    if '/setup-optimizer?token=' not in s:
        s = s.replace(
            '<a href="/risk-management?token={_esc(token)}">Risk</a> |\\n    <a href="/single-header?token={_esc(token)}">V7208</a>',
            '<a href="/risk-management?token={_esc(token)}">Risk</a> |\\n    <a href="/setup-optimizer?token={_esc(token)}">Optimizer</a> |\\n    <a href="/single-header?token={_esc(token)}">V7208</a>'
        )
    p.write_text(s, encoding="utf-8")
PY

echo "== main hook =="
if ! grep -q "V7237 SETUP OPTIMIZER LIVE GATE INSTALL" app/main.py; then
cat >> app/main.py <<'PY'

# === V7237 SETUP OPTIMIZER LIVE GATE INSTALL ===
try:
    from app.v7237_setup_optimizer_live_gate import install_v7237_setup_optimizer_live_gate
    install_v7237_setup_optimizer_live_gate(app)
except Exception as exc:
    print("[V7237] Setup Optimizer Live Gate install failed:", exc)
# === END V7237 SETUP OPTIMIZER LIVE GATE INSTALL ===
PY
fi

echo "== ops report =="
cat > ops/v7237_setup_optimizer_report.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
TOKEN="${1:-eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg}"
BASE_URL="${BASE_URL:-http://127.0.0.1}"
cd /opt/tradingbot_v6000 || exit 1
mkdir -p data
TMP="$(mktemp /tmp/v7237_setup_optimizer.XXXXXX.json)"
trap 'rm -f "$TMP"' EXIT
curl -fsS "${BASE_URL}/setup-optimizer.json?token=${TOKEN}&write=1" > "$TMP"
python3 -m json.tool "$TMP" >/dev/null
mv "$TMP" data/v7237_setup_optimizer_report_last.json
trap - EXIT
echo "v7237 setup optimizer ok $(date -u +%Y-%m-%dT%H:%M:%SZ)"
SH
chmod +x ops/v7237_setup_optimizer_report.sh

echo "== cron =="
cat > /etc/cron.d/tradingbot_v7237_setup_optimizer <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
21,51 * * * * root cd /opt/tradingbot_v6000 && /opt/tradingbot_v6000/ops/v7237_setup_optimizer_report.sh >> /opt/tradingbot_v6000/data/v7237_setup_optimizer_cron.log 2>&1
CRON
chmod 0644 /etc/cron.d/tradingbot_v7237_setup_optimizer
if command -v systemctl >/dev/null 2>&1; then systemctl restart cron >/dev/null 2>&1 || true; else service cron restart || true; fi

echo "== check script =="
CHECK_FILE="ops/v7000_check.sh"
if [ -f "$CHECK_FILE" ] && ! grep -q "V7237 SETUP OPTIMIZER LIVE GATE ROUTES" "$CHECK_FILE"; then
cat >> "$CHECK_FILE" <<'EOF'

echo ""
echo "===== V7237 SETUP OPTIMIZER LIVE GATE ROUTES ====="
TOKEN_FOR_V7237="eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg"
for U in setup-optimizer setup-optimizer.json setup-optimizer-config.json setup-optimizer-rules.json live-gate-review live-gate-review.json weak-combo-report weak-combo-report.json entry-quality-report entry-quality-report.json event-leak-report event-leak-report.json; do
  echo "/${U}?token=*** -> $(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/${U}?token=${TOKEN_FOR_V7237}")"
done
echo "setup_optimizer_cron -> $(test -f /etc/cron.d/tradingbot_v7237_setup_optimizer && echo OK || echo MISSING)"
echo "setup_optimizer_file -> $(test -f data/v7237_setup_optimizer_report_last.json && echo OK || echo MISSING)"
for R in setup-optimizer live-gate-review weak-combo-report entry-quality-report event-leak-report master markets single-header performance-learning risk-management; do
  TMP="/tmp/v7237_${R}.html"
  CODE="$(curl -s -o "$TMP" -w "%{http_code}" "http://127.0.0.1/${R}?token=${TOKEN_FOR_V7237}")"
  HEADER="$(grep -c 'id="v7208_single_header_bar"' "$TMP" || true)"
  OPT="$(grep -c '/setup-optimizer' "$TMP" || true)"
  echo "/${R}?token=*** -> ${CODE} | single_header_hits=${HEADER} | optimizer_link_hits=${OPT}"
done
EOF
chmod +x "$CHECK_FILE"
fi

echo "== syntax =="
python3 -m py_compile app/v7237_setup_optimizer_live_gate.py app/v7208_single_header_mode.py app/main.py

echo "== docker rebuild =="
docker compose up -d --build tradingbot || docker restart tradingbot
sleep 6

TOKEN_TEST="${1:-$TOKEN_DEFAULT}"

echo "== Route Test =="
for U in setup-optimizer setup-optimizer.json setup-optimizer-config.json setup-optimizer-rules.json live-gate-review live-gate-review.json weak-combo-report weak-combo-report.json entry-quality-report entry-quality-report.json event-leak-report event-leak-report.json; do
  echo "${U}_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/${U}?token=${TOKEN_TEST}")"
done

echo ""
echo "== Header Link Test =="
for R in setup-optimizer live-gate-review weak-combo-report entry-quality-report event-leak-report master markets single-header performance-learning risk-management; do
  TMP="/tmp/v7237_${R}.html"
  CODE="$(curl -s -o "$TMP" -w "%{http_code}" "http://127.0.0.1/${R}?token=${TOKEN_TEST}")"
  HEADER="$(grep -c 'id="v7208_single_header_bar"' "$TMP" || true)"
  OPT="$(grep -c '/setup-optimizer' "$TMP" || true)"
  echo "/${R}?token=*** -> ${CODE} | single_header=${HEADER} | optimizer_link=${OPT}"
done

echo ""
echo "== Optimizer Snapshot =="
bash ops/v7237_setup_optimizer_report.sh "$TOKEN_TEST" || true
echo "setup_optimizer_file=$(test -f data/v7237_setup_optimizer_report_last.json && echo OK || echo MISSING)"
echo "setup_optimizer_cron=$(test -f /etc/cron.d/tradingbot_v7237_setup_optimizer && echo OK || echo MISSING)"

echo ""
echo "== Optimizer Preview =="
curl -s "http://127.0.0.1/setup-optimizer.json?token=${TOKEN_TEST}" \
  | python3 -m json.tool \
  | grep -E '"safe_state"|"risky_on"|"performance_total"|"best_direction_setups"|"weak_direction_setups"|"event_leaks"|"allowed_but_optimizer_shadow_only"|"observe_only"|"apply_to_live"' \
  | head -n 120 || true

echo ""
echo "== Event Leak Preview =="
curl -s "http://127.0.0.1/event-leak-report.json?token=${TOKEN_TEST}" \
  | python3 -m json.tool \
  | grep -E '"high_or_hardblock_decisions"|"live_trades_during_high_or_hardblock"|"market"|"setup_name"|"result"|"pnl_r"' \
  | head -n 80 || true

echo ""
echo "== Smoke Existing =="
echo "master_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/master?token=${TOKEN_TEST}")"
echo "markets_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/markets?token=${TOKEN_TEST}")"
echo "performance_learning_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/performance-learning?token=${TOKEN_TEST}")"
echo "risk_management_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/risk-management?token=${TOKEN_TEST}")"

echo ""
echo "== V7237 SETUP OPTIMIZER DONE =="
