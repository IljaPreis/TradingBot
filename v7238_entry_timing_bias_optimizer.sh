#!/usr/bin/env bash
set -euo pipefail

cd /opt/tradingbot_v6000 || exit 1

TS="$(date -u +%Y%m%d_%H%M%S)"
TOKEN_DEFAULT="eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg"

echo "== V7238 ENTRY TIMING & BIAS OPTIMIZER PACK =="
echo "Mode: OBSERVE-ONLY. It measures bias/timing quality and gives soft live recommendations."
echo "No live blocking, no trade execution changes, no SL/TP changes."

mkdir -p backups app data ops

echo "== Backup =="
tar -czf "backups/v7238_entry_timing_bias_before_${TS}.tar.gz" \
  --exclude='backups' \
  --exclude='__pycache__' \
  --exclude='.git' \
  . || true

echo "== Config =="
cat > data/v7238_entry_timing_bias_config.json <<'JSON'
{
  "version": "V7238",
  "enabled": true,
  "apply_to_live": false,
  "observe_only": true,
  "min_sample_for_edge": 3,
  "min_confidence_absolute_shadow": 50.0,
  "soft_confidence_floor": 55.0,
  "preferred_htf_bias": ["neutral"],
  "weak_htf_bias": ["mixed_bear"],
  "weak_bias_gates": ["countertrend_watch"],
  "preferred_trade_bias": ["structure_long", "structure_short"],
  "weak_trade_bias": ["pullback_in_downtrend", "trend_long", "trend_short"],
  "premium_discount_enforce": false,
  "use_confidence_as_secondary_only": true,
  "notes": "Do not hard-block all WAIT or all low confidence. Bias/trade_bias/HTF context has priority; confidence is secondary."
}
JSON

echo "== Module =="
cat > app/v7238_entry_timing_bias_optimizer.py <<'PY'
import json
import html
import sqlite3
import math
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
            obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(obj, dict):
                return obj
    except Exception as exc:
        return {"load_error": str(exc)}
    return default


def _write_json(name, obj):
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _cfg():
    c = {
        "version": "V7238",
        "enabled": True,
        "apply_to_live": False,
        "observe_only": True,
        "min_sample_for_edge": 3,
        "min_confidence_absolute_shadow": 50.0,
        "soft_confidence_floor": 55.0,
        "preferred_htf_bias": ["neutral"],
        "weak_htf_bias": ["mixed_bear"],
        "weak_bias_gates": ["countertrend_watch"],
        "preferred_trade_bias": ["structure_long", "structure_short"],
        "weak_trade_bias": ["pullback_in_downtrend", "trend_long", "trend_short"],
        "premium_discount_enforce": False,
        "use_confidence_as_secondary_only": True,
        "notes": "Do not hard-block all WAIT or all low confidence. Bias/trade_bias/HTF context has priority; confidence is secondary.",
    }
    c.update(_read_json("v7238_entry_timing_bias_config.json", {}))
    return c


def _token_ok(request: Request):
    try:
        from app.v7200_event_risk import _token_ok as real_token_ok
        return real_token_ok(request)
    except Exception:
        return bool(request.query_params.get("token", ""))


def _safe_control():
    try:
        from app.v7207_master_compact_control_center import _status
        return _status()
    except Exception as exc:
        return {"safe_state": False, "risky_on": True, "load_error": str(exc)}


def _safe_event():
    try:
        from app.v7200_event_risk import _event_data
        return _event_data()
    except Exception as exc:
        return {"risk_level": "ERROR", "cooldown_active": False, "upcoming_count": 0, "load_error": str(exc)}


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
    for part in str(notes or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _json_loads(x):
    try:
        y = json.loads(x or "{}")
        return y if isinstance(y, dict) else {}
    except Exception:
        return {}


def _session_from_dt(s):
    if not s:
        return "UNKNOWN"
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
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


def _load_live_outcomes():
    out = {}
    rows = []
    try:
        con = _connect()
        for r in con.execute("select * from trade_outcomes order by id asc").fetchall():
            d = dict(r)
            n = _parse_notes(d.get("notes"))
            cid = n.get("client_trade_id")
            result = str(d.get("result", "")).upper()
            pnl_r = _num(d.get("pnl_r"), 0.0)
            item = {
                "source": "LIVE",
                "outcome_id": d.get("id"),
                "decision_id": d.get("decision_id"),
                "client_trade_id": cid,
                "market": str(n.get("market", "UNKNOWN")).upper(),
                "direction": str(n.get("direction", "UNKNOWN")).upper(),
                "setup_name": n.get("setup", "UNKNOWN"),
                "entry": _num(n.get("entry"), None),
                "sl": _num(n.get("sl"), None),
                "tp1": _num(n.get("tp1"), None),
                "exit_price": _num(d.get("exit_price"), None),
                "result": result,
                "pnl_r": pnl_r,
                "closed_at": d.get("closed_at"),
                "session": _session_from_dt(d.get("closed_at")),
                "notes": d.get("notes", ""),
            }
            rows.append(item)
            if cid:
                out[cid] = item
    except Exception as exc:
        rows.append({"source": "ERROR", "error": str(exc)})
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out, rows


def _load_signal_audit():
    rows = []
    try:
        con = _connect()
        for r in con.execute("select * from signal_audit order by id asc").fetchall():
            d = dict(r)
            raw = _json_loads(d.get("raw_json"))
            sig = raw.get("signal", {}) if isinstance(raw.get("signal"), dict) else {}
            v6000 = raw.get("v6000", {}) if isinstance(raw.get("v6000"), dict) else {}
            item = {
                "id": d.get("id"),
                "created_at": d.get("created_at"),
                "client_trade_id": d.get("client_trade_id"),
                "market": str(d.get("market", "UNKNOWN")).upper(),
                "direction": str(d.get("direction", "UNKNOWN")).upper(),
                "setup_name": d.get("trigger", "UNKNOWN"),
                "timeframe": str(d.get("timeframe", "")),
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
                "setup_quality": sig.get("setup_quality"),
                "trade_bias": sig.get("trade_bias"),
                "htf_bias": sig.get("htf_bias"),
                "premium_discount_state": sig.get("premium_discount_state"),
                "delta_state": sig.get("delta_state"),
                "cvd_state": sig.get("cvd_state"),
                "absorption_state": sig.get("absorption_state"),
                "reversal_state": sig.get("reversal_state"),
                "liquidity_state": sig.get("liquidity_state"),
                "bpr_state": sig.get("bpr_state"),
                "rel_volume": _num(sig.get("rel_volume"), None),
                "zone_score": _num(sig.get("zone_score"), None),
                "htf_score": _num(sig.get("htf_score"), None),
                "atr": _num(sig.get("atr"), None),
                "smc_score": _num(sig.get("smc_score"), None),
                "vp_score": _num(sig.get("vp_score"), None),
                "delta_score": _num(sig.get("delta_score"), None),
                "grade": v6000.get("grade"),
                "decision": v6000.get("decision"),
                "v6000_score": _num(v6000.get("score"), None),
                "v6000_tech": _num(v6000.get("tech"), None),
                "v6000_smc": _num(v6000.get("smc"), None),
                "v6000_vp": _num(v6000.get("vp"), None),
                "v6000_delta": _num(v6000.get("delta"), None),
            }
            rows.append(item)
    except Exception:
        pass
    finally:
        try:
            con.close()
        except Exception:
            pass
    return rows


def _rr_metrics(row):
    entry = _num(row.get("entry"), None)
    sl = _num(row.get("sl"), None)
    tp1 = _num(row.get("tp1"), None)
    if entry is None or sl is None or tp1 is None:
        return {"risk_abs": None, "reward_abs": None, "rr": None, "sl_distance_pct": None, "tp_distance_pct": None}
    risk = abs(entry - sl)
    reward = abs(tp1 - entry)
    rr = reward / risk if risk else None
    pct_risk = risk / abs(entry) * 100 if entry else None
    pct_reward = reward / abs(entry) * 100 if entry else None
    return {
        "risk_abs": round(risk, 10),
        "reward_abs": round(reward, 10),
        "rr": round(rr, 4) if rr is not None else None,
        "sl_distance_pct": round(pct_risk, 4) if pct_risk is not None else None,
        "tp_distance_pct": round(pct_reward, 4) if pct_reward is not None else None,
    }


def _recommend_signal(row):
    cfg = _cfg()
    conf = _num(row.get("confidence"), 0.0) or 0.0
    bias_gate = str(row.get("bias_gate") or "").lower()
    trade_bias = str(row.get("trade_bias") or "").lower()
    htf_bias = str(row.get("htf_bias") or "").lower()
    setup = str(row.get("setup_name") or "").upper()
    event = str(row.get("event_risk") or "").lower()
    grade = str(row.get("grade") or "").upper()
    pd = str(row.get("premium_discount_state") or "").lower()
    direction = str(row.get("direction") or "").upper()

    weak_gates = {x.lower() for x in cfg.get("weak_bias_gates", [])}
    weak_tb = {x.lower() for x in cfg.get("weak_trade_bias", [])}
    weak_htf = {x.lower() for x in cfg.get("weak_htf_bias", [])}
    pref_tb = {x.lower() for x in cfg.get("preferred_trade_bias", [])}
    pref_htf = {x.lower() for x in cfg.get("preferred_htf_bias", [])}

    score = 50.0
    reasons = []

    if trade_bias in pref_tb:
        score += 18
        reasons.append("TRADE_BIAS_STRUCTURE_OK")
    if trade_bias in weak_tb:
        score -= 22
        reasons.append("WEAK_TRADE_BIAS")

    if htf_bias in pref_htf:
        score += 16
        reasons.append("HTF_NEUTRAL_EDGE")
    if htf_bias in weak_htf:
        score -= 22
        reasons.append("WEAK_HTF_BIAS")

    if bias_gate in weak_gates:
        score -= 18
        reasons.append("COUNTERTREND_WATCH")
    elif "trend_aligned" in bias_gate:
        score += 8
        reasons.append("BIAS_GATE_ALIGNED")

    if setup in {"BPR_RETEST", "BEAR_FVG_RETEST"}:
        score -= 14
        reasons.append("SETUP_STILL_SHADOW_REVIEW")

    if event in {"high", "hard_block"}:
        score -= 25
        reasons.append("EVENT_RISK_HIGH")

    if conf < float(cfg.get("min_confidence_absolute_shadow", 50.0)):
        score -= 18
        reasons.append("CONFIDENCE_TOO_LOW")
    elif conf < float(cfg.get("soft_confidence_floor", 55.0)):
        score -= 6
        reasons.append("CONFIDENCE_SOFT_LOW")
    elif conf >= 60:
        score += 4
        reasons.append("CONFIDENCE_OK_SOFT")

    if grade == "WAIT":
        score -= 3
        reasons.append("WAIT_GRADE_SOFT_ONLY")

    # premium/discount is only a soft warning by default.
    if cfg.get("premium_discount_enforce", False):
        if direction == "LONG" and "premium" in pd:
            score -= 12
            reasons.append("LONG_IN_PREMIUM_ENFORCED")
        if direction == "SHORT" and "discount" in pd:
            score -= 12
            reasons.append("SHORT_IN_DISCOUNT_ENFORCED")
    else:
        if direction == "LONG" and "premium" in pd:
            reasons.append("LONG_IN_PREMIUM_WARNING")
        if direction == "SHORT" and "discount" in pd:
            reasons.append("SHORT_IN_DISCOUNT_WARNING")

    score = max(0, min(100, round(score, 2)))

    if score >= 72:
        action = "LIVE_CANDIDATE"
        severity = "GOOD"
    elif score >= 58:
        action = "LIVE_SMALL_OR_SHADOW_REVIEW"
        severity = "CAUTION"
    else:
        action = "SHADOW_ONLY_RECOMMENDED"
        severity = "WEAK"

    return {"score": score, "action": action, "severity": severity, "reasons": reasons}


def _matched_live_rows():
    outcome_map, outcomes = _load_live_outcomes()
    audit = _load_signal_audit()
    rows = []
    for a in audit:
        cid = a.get("client_trade_id")
        if cid not in outcome_map:
            continue
        o = outcome_map[cid]
        item = dict(a)
        item.update({
            "result": o.get("result"),
            "pnl_r": _num(o.get("pnl_r"), 0.0),
            "closed_at": o.get("closed_at"),
            "outcome_session": o.get("session"),
            "sl": o.get("sl"),
            "tp1": o.get("tp1"),
            "exit_price": o.get("exit_price"),
        })
        item.update(_rr_metrics(o))
        item["soft_gate"] = _recommend_signal(item)
        rows.append(item)
    return rows


def _aggregate(rows, keys, min_count=1):
    g = defaultdict(list)
    for r in rows:
        k = tuple(r.get(x, "-") for x in keys)
        g[k].append(r)
    out = []
    for key, items in g.items():
        n = len(items)
        if n < min_count:
            continue
        wins = sum(1 for x in items if float(x.get("pnl_r") or 0) > 0)
        losses = sum(1 for x in items if float(x.get("pnl_r") or 0) < 0)
        total_r = round(sum(float(x.get("pnl_r") or 0) for x in items), 4)
        winrate = round(wins / n * 100, 2) if n else 0
        avg_r = round(total_r / n, 4) if n else 0
        edge = "SMALL_SAMPLE"
        if n >= int(_cfg().get("min_sample_for_edge", 3)):
            if total_r > 0 and winrate >= 50:
                edge = "POSITIVE_EDGE"
            elif total_r < 0 or winrate < 40:
                edge = "WEAK_EDGE"
            else:
                edge = "NEUTRAL_EDGE"
        row = {
            "count": n,
            "wins": wins,
            "losses": losses,
            "winrate": winrate,
            "total_r": total_r,
            "avg_r": avg_r,
            "edge_state": edge,
            "latest": items[-5:],
        }
        for i, name in enumerate(keys):
            row[name] = key[i]
        out.append(row)
    out.sort(key=lambda x: (x.get("total_r", 0), x.get("winrate", 0), x.get("count", 0)), reverse=True)
    return out


def _bias_report():
    rows = _matched_live_rows()
    for r in rows:
        r["bias_combo"] = f'{r.get("bias_gate")} | {r.get("trade_bias")} | {r.get("htf_bias")}'
        r["direction_setup"] = f'{r.get("direction")} {r.get("setup_name")}'
        r["timing_combo"] = f'{r.get("entry_state")} | {r.get("chase_state")} | {r.get("impulse_state")}'
    total_r = round(sum(float(x.get("pnl_r") or 0) for x in rows), 4)
    wins = sum(1 for x in rows if float(x.get("pnl_r") or 0) > 0)
    losses = sum(1 for x in rows if float(x.get("pnl_r") or 0) < 0)
    n = len(rows)
    return {
        "version": "V7238",
        "mode": "ENTRY_TIMING_BIAS_REPORT",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "matched_live_trades": n,
        "total": {
            "count": n,
            "wins": wins,
            "losses": losses,
            "winrate": round(wins / n * 100, 2) if n else 0,
            "total_r": total_r,
            "avg_r": round(total_r / n, 4) if n else 0,
        },
        "by_bias_gate": _aggregate(rows, ["bias_gate"]),
        "by_trade_bias": _aggregate(rows, ["trade_bias"]),
        "by_htf_bias": _aggregate(rows, ["htf_bias"]),
        "by_bias_combo": _aggregate(rows, ["bias_combo"]),
        "by_direction_setup": _aggregate(rows, ["direction", "setup_name"]),
        "by_timing_combo": _aggregate(rows, ["timing_combo"]),
        "by_premium_discount": _aggregate(rows, ["premium_discount_state"]),
        "by_market_direction_setup": _aggregate(rows, ["market", "direction", "setup_name"]),
        "latest_trades": rows[-50:],
        "observe_only": not bool(_cfg().get("apply_to_live")),
    }


def _latest_signal_review(limit=120):
    audit = _load_signal_audit()
    rows = []
    for a in audit[-limit:]:
        item = dict(a)
        item["soft_gate"] = _recommend_signal(item)
        rows.append(item)
    actions = Counter(x.get("soft_gate", {}).get("action") for x in rows)
    reasons = Counter()
    for x in rows:
        for r in x.get("soft_gate", {}).get("reasons", []):
            reasons[r] += 1
    return {
        "version": "V7238",
        "mode": "LATEST_SIGNAL_BIAS_TIMING_REVIEW",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "rows": len(rows),
        "action_counts": dict(actions),
        "top_reasons": dict(reasons.most_common(20)),
        "latest": rows,
        "observe_only": not bool(_cfg().get("apply_to_live")),
    }


def _soft_gate_backtest():
    rows = _matched_live_rows()
    buckets = defaultdict(list)
    for r in rows:
        action = r.get("soft_gate", {}).get("action", "UNKNOWN")
        buckets[action].append(r)
    out = []
    for action, items in buckets.items():
        n = len(items)
        wins = sum(1 for x in items if float(x.get("pnl_r") or 0) > 0)
        losses = sum(1 for x in items if float(x.get("pnl_r") or 0) < 0)
        total_r = round(sum(float(x.get("pnl_r") or 0) for x in items), 4)
        out.append({
            "soft_action": action,
            "count": n,
            "wins": wins,
            "losses": losses,
            "winrate": round(wins / n * 100, 2) if n else 0,
            "total_r": total_r,
            "avg_r": round(total_r / n, 4) if n else 0,
            "latest": items[-10:],
        })
    out.sort(key=lambda x: x.get("total_r", 0), reverse=True)

    avoid = [x for x in rows if x.get("soft_gate", {}).get("action") == "SHADOW_ONLY_RECOMMENDED"]
    avoided_r = round(sum(float(x.get("pnl_r") or 0) for x in avoid), 4)
    live_candidate = [x for x in rows if x.get("soft_gate", {}).get("action") in {"LIVE_CANDIDATE", "LIVE_SMALL_OR_SHADOW_REVIEW"}]
    live_r = round(sum(float(x.get("pnl_r") or 0) for x in live_candidate), 4)

    return {
        "version": "V7238",
        "mode": "SOFT_GATE_BACKTEST",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "buckets": out,
        "if_shadow_only_removed": {
            "removed_count": len(avoid),
            "removed_total_r": avoided_r,
            "remaining_count": len(live_candidate),
            "remaining_total_r": live_r,
        },
        "observe_only": not bool(_cfg().get("apply_to_live")),
    }


def _summary(write_file=False):
    bias = _bias_report()
    latest = _latest_signal_review(120)
    backtest = _soft_gate_backtest()
    control = _safe_control()
    ev = _safe_event()

    out = {
        "version": "V7238",
        "mode": "ENTRY_TIMING_BIAS_OPTIMIZER_SUMMARY",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "safe_state": control.get("safe_state"),
        "risky_on": control.get("risky_on"),
        "event_risk": {
            "risk_level": ev.get("risk_level"),
            "cooldown_active": ev.get("cooldown_active"),
            "upcoming_count": ev.get("upcoming_count"),
        },
        "matched_live_total": bias.get("total"),
        "best_bias_gates": bias.get("by_bias_gate", [])[:5],
        "best_trade_bias": bias.get("by_trade_bias", [])[:5],
        "best_htf_bias": bias.get("by_htf_bias", [])[:5],
        "weak_bias_gates": [x for x in reversed(bias.get("by_bias_gate", []))][:5],
        "weak_trade_bias": [x for x in reversed(bias.get("by_trade_bias", []))][:5],
        "weak_htf_bias": [x for x in reversed(bias.get("by_htf_bias", []))][:5],
        "latest_action_counts": latest.get("action_counts", {}),
        "top_reasons": latest.get("top_reasons", {}),
        "soft_gate_backtest": backtest.get("if_shadow_only_removed", {}),
        "recommendation": [
            "Do not enforce confidence 75 as a hard rule.",
            "Use confidence as secondary filter only.",
            "Prefer structure_long/structure_short plus neutral HTF.",
            "Send countertrend_watch, pullback_in_downtrend, trend_long/trend_short, mixed_bear to shadow review first.",
            "Premium/discount should remain warning-only until more samples exist."
        ],
        "observe_only": not bool(_cfg().get("apply_to_live")),
        "apply_to_live": bool(_cfg().get("apply_to_live")),
    }
    if write_file:
        _write_json("v7238_entry_timing_bias_report_last.json", out)
    return out


def _links(token):
    links = [
        ("V7238", "/entry-bias-optimizer"),
        ("Bias Report", "/bias-quality-report"),
        ("Signal Review", "/entry-timing-review"),
        ("Soft Backtest", "/soft-gate-backtest"),
        ("V7237", "/setup-optimizer"),
        ("Performance", "/performance-learning"),
        ("Risk", "/risk-management"),
        ("Master", "/master"),
    ]
    return " · ".join(f'<a href="{url}?token={_esc(token)}">{_esc(label)}</a>' for label, url in links)


def _html(title, body, request):
    token = request.query_params.get("token", "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>
body{{background:#09111b;color:#e8eef5;font-family:Arial,sans-serif;margin:22px}}
a{{color:#8cc8ff;text-decoration:none}}.card{{background:#121b27;border:1px solid #27364a;border-radius:14px;padding:15px;margin-bottom:16px}}
.badge{{display:inline-block;background:#1e5d9b;color:white;border-radius:999px;padding:7px 11px;font-weight:bold}}.good{{color:#39d06f}}.bad{{color:#ff5c67}}.yellow{{color:#f3c747}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}}th,td{{border-bottom:1px solid #27364a;padding:8px;text-align:left;vertical-align:top}}th{{color:#a9bfd6}}
pre{{white-space:pre-wrap;background:#0d1621;border:1px solid #27364a;border-radius:10px;padding:12px}}
.muted{{color:#a9b8c9}}@media(max-width:760px){{body{{margin:12px}}table{{font-size:12px}}th,td{{padding:6px}}}}
</style></head><body><h1>{_esc(title)}</h1><div class="card">{_links(token)}</div>{body}</body></html>"""


def _agg_table(title, rows, keys):
    trs = ""
    for r in rows:
        name = " / ".join(str(r.get(k, "-")) for k in keys)
        cls = "good" if float(r.get("total_r") or 0) > 0 else ("bad" if float(r.get("total_r") or 0) < 0 else "")
        trs += f"""<tr><td>{_esc(name)}</td><td>{_esc(r.get("count"))}</td><td>{_esc(r.get("wins"))}</td><td>{_esc(r.get("losses"))}</td><td>{_esc(r.get("winrate"))}%</td><td class="{cls}">{_esc(r.get("total_r"))}</td><td>{_esc(r.get("avg_r"))}</td><td>{_esc(r.get("edge_state"))}</td></tr>"""
    if not trs:
        trs = "<tr><td colspan='8'>Keine Daten.</td></tr>"
    return f"""<div class="card"><h2>{_esc(title)}</h2><table><tr><th>Name</th><th>N</th><th>W</th><th>L</th><th>WR</th><th>TotalR</th><th>AvgR</th><th>Edge</th></tr>{trs}</table></div>"""


def _signal_table(title, rows):
    trs = ""
    for r in rows:
        sg = r.get("soft_gate", {})
        cls = "good" if sg.get("action") == "LIVE_CANDIDATE" else ("yellow" if sg.get("action") == "LIVE_SMALL_OR_SHADOW_REVIEW" else "bad")
        trs += f"""<tr><td>{_esc(r.get("created_at"))}</td><td>{_esc(r.get("market"))}</td><td>{_esc(r.get("direction"))}</td><td>{_esc(r.get("setup_name"))}</td><td>{_esc(r.get("confidence"))}</td><td>{_esc(r.get("bias_gate"))}</td><td>{_esc(r.get("trade_bias"))}</td><td>{_esc(r.get("htf_bias"))}</td><td class="{cls}">{_esc(sg.get("score"))} · {_esc(sg.get("action"))}</td><td>{_esc(sg.get("reasons"))}</td></tr>"""
    if not trs:
        trs = "<tr><td colspan='10'>Keine Daten.</td></tr>"
    return f"""<div class="card"><h2>{_esc(title)}</h2><table><tr><th>Time</th><th>Market</th><th>Dir</th><th>Setup</th><th>Conf</th><th>Bias Gate</th><th>Trade Bias</th><th>HTF</th><th>Soft Gate</th><th>Reasons</th></tr>{trs}</table></div>"""


def install_v7238_entry_timing_bias_optimizer(app):
    if getattr(app.state, "v7238_entry_timing_bias_installed", False):
        return

    @app.get("/entry-bias-optimizer", response_class=HTMLResponse)
    def entry_bias_optimizer_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        s = _summary(write_file=True)
        body = f"""<div class="card"><span class="badge">V7238 ENTRY TIMING & BIAS OPTIMIZER</span>
<p class="muted">Observe-only: <b>{_esc(s.get("observe_only"))}</b> | Apply to live: <b>{_esc(s.get("apply_to_live"))}</b></p>
<p><b>Matched Live:</b> {_esc(s.get("matched_live_total"))}</p>
<p><b>Soft Backtest:</b> {_esc(s.get("soft_gate_backtest"))}</p>
</div>"""
        body += _agg_table("Best / Weak Bias Gates", s.get("best_bias_gates", []) + s.get("weak_bias_gates", []), ["bias_gate"])
        body += _agg_table("Best / Weak Trade Bias", s.get("best_trade_bias", []) + s.get("weak_trade_bias", []), ["trade_bias"])
        body += _agg_table("Best / Weak HTF Bias", s.get("best_htf_bias", []) + s.get("weak_htf_bias", []), ["htf_bias"])
        return HTMLResponse(_html("TradingBot V7238 - Entry Timing & Bias Optimizer", body, request))

    @app.get("/entry-bias-optimizer.json")
    def entry_bias_optimizer_json(request: Request, write: Optional[int] = 1):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_summary(write_file=bool(int(write))))

    @app.get("/entry-bias-optimizer-config.json")
    def entry_bias_optimizer_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    @app.get("/bias-quality-report", response_class=HTMLResponse)
    def bias_quality_report_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _bias_report()
        body = f"""<div class="card"><span class="badge">BIAS QUALITY REPORT</span><p>{_esc(r.get("total"))}</p></div>"""
        body += _agg_table("Bias Gate", r.get("by_bias_gate", []), ["bias_gate"])
        body += _agg_table("Trade Bias", r.get("by_trade_bias", []), ["trade_bias"])
        body += _agg_table("HTF Bias", r.get("by_htf_bias", []), ["htf_bias"])
        body += _agg_table("Bias Combo", r.get("by_bias_combo", []), ["bias_combo"])
        body += _agg_table("Timing Combo", r.get("by_timing_combo", []), ["timing_combo"])
        return HTMLResponse(_html("TradingBot V7238 - Bias Quality Report", body, request))

    @app.get("/bias-quality-report.json")
    def bias_quality_report_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_bias_report())

    @app.get("/entry-timing-review", response_class=HTMLResponse)
    def entry_timing_review_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _latest_signal_review(120)
        body = f"""<div class="card"><span class="badge">LATEST SIGNAL REVIEW</span>
<p><b>Action counts:</b> {_esc(r.get("action_counts"))}</p>
<p><b>Top reasons:</b> {_esc(r.get("top_reasons"))}</p></div>"""
        body += _signal_table("Latest Signals", r.get("latest", [])[-120:])
        return HTMLResponse(_html("TradingBot V7238 - Entry Timing Review", body, request))

    @app.get("/entry-timing-review.json")
    def entry_timing_review_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_latest_signal_review(120))

    @app.get("/soft-gate-backtest", response_class=HTMLResponse)
    def soft_gate_backtest_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _soft_gate_backtest()
        rows = r.get("buckets", [])
        body = f"""<div class="card"><span class="badge">SOFT GATE BACKTEST</span>
<p><b>If shadow-only removed:</b> {_esc(r.get("if_shadow_only_removed"))}</p></div>"""
        body += _agg_table("Soft Gate Buckets", rows, ["soft_action"])
        return HTMLResponse(_html("TradingBot V7238 - Soft Gate Backtest", body, request))

    @app.get("/soft-gate-backtest.json")
    def soft_gate_backtest_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_soft_gate_backtest())

    app.state.v7238_entry_timing_bias_installed = True
    print("[V7238] Entry Timing & Bias Optimizer installed")
PY

echo "== Header targets =="
python3 - <<'PY'
from pathlib import Path

p = Path("app/v7208_single_header_mode.py")
if p.exists():
    s = p.read_text(encoding="utf-8")
    routes = ["/entry-bias-optimizer", "/bias-quality-report", "/entry-timing-review", "/soft-gate-backtest"]
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
            "/entry-bias-optimizer", "/bias-quality-report", "/entry-timing-review", "/soft-gate-backtest",
        ]
        rest = sorted(existing - set(ordered))
        new = "TARGET_PATHS = {\n" + "\n".join(f'    "{r}",' for r in ordered + rest) + "\n}"
        s = s[:start] + new + s[end:]
    except Exception:
        pass

    if '/entry-bias-optimizer?token=' not in s:
        if '<a href="/setup-optimizer?token={_esc(token)}">Optimizer</a>' in s:
            s = s.replace(
                '<a href="/setup-optimizer?token={_esc(token)}">Optimizer</a> |',
                '<a href="/setup-optimizer?token={_esc(token)}">Optimizer</a> |\\n    <a href="/entry-bias-optimizer?token={_esc(token)}">Bias</a> |'
            )
        elif '<a href="/risk-management?token={_esc(token)}">Risk</a>' in s:
            s = s.replace(
                '<a href="/risk-management?token={_esc(token)}">Risk</a> |',
                '<a href="/risk-management?token={_esc(token)}">Risk</a> |\\n    <a href="/entry-bias-optimizer?token={_esc(token)}">Bias</a> |'
            )
        else:
            s = s.replace(
                '<a href="/single-header?token={_esc(token)}">V7208</a>',
                '<a href="/entry-bias-optimizer?token={_esc(token)}">Bias</a> |\\n    <a href="/single-header?token={_esc(token)}">V7208</a>'
            )
    p.write_text(s, encoding="utf-8")
PY

echo "== main hook =="
if ! grep -q "V7238 ENTRY TIMING BIAS OPTIMIZER INSTALL" app/main.py; then
cat >> app/main.py <<'PY'

# === V7238 ENTRY TIMING BIAS OPTIMIZER INSTALL ===
try:
    from app.v7238_entry_timing_bias_optimizer import install_v7238_entry_timing_bias_optimizer
    install_v7238_entry_timing_bias_optimizer(app)
except Exception as exc:
    print("[V7238] Entry Timing Bias Optimizer install failed:", exc)
# === END V7238 ENTRY TIMING BIAS OPTIMIZER INSTALL ===
PY
fi

echo "== ops report =="
cat > ops/v7238_entry_timing_bias_report.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
TOKEN="${1:-eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg}"
BASE_URL="${BASE_URL:-http://127.0.0.1}"
cd /opt/tradingbot_v6000 || exit 1
mkdir -p data
TMP="$(mktemp /tmp/v7238_entry_bias.XXXXXX.json)"
trap 'rm -f "$TMP"' EXIT
curl -fsS "${BASE_URL}/entry-bias-optimizer.json?token=${TOKEN}&write=1" > "$TMP"
python3 -m json.tool "$TMP" >/dev/null
mv "$TMP" data/v7238_entry_timing_bias_report_last.json
trap - EXIT
echo "v7238 entry timing bias ok $(date -u +%Y-%m-%dT%H:%M:%SZ)"
SH
chmod +x ops/v7238_entry_timing_bias_report.sh

echo "== cron =="
cat > /etc/cron.d/tradingbot_v7238_entry_timing_bias <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
24,54 * * * * root cd /opt/tradingbot_v6000 && /opt/tradingbot_v6000/ops/v7238_entry_timing_bias_report.sh >> /opt/tradingbot_v6000/data/v7238_entry_timing_bias_cron.log 2>&1
CRON
chmod 0644 /etc/cron.d/tradingbot_v7238_entry_timing_bias
if command -v systemctl >/dev/null 2>&1; then systemctl restart cron >/dev/null 2>&1 || true; else service cron restart || true; fi

echo "== check script =="
CHECK_FILE="ops/v7000_check.sh"
if [ -f "$CHECK_FILE" ] && ! grep -q "V7238 ENTRY TIMING BIAS ROUTES" "$CHECK_FILE"; then
cat >> "$CHECK_FILE" <<'EOF'

echo ""
echo "===== V7238 ENTRY TIMING BIAS ROUTES ====="
TOKEN_FOR_V7238="eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg"
for U in entry-bias-optimizer entry-bias-optimizer.json entry-bias-optimizer-config.json bias-quality-report bias-quality-report.json entry-timing-review entry-timing-review.json soft-gate-backtest soft-gate-backtest.json; do
  echo "/${U}?token=*** -> $(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/${U}?token=${TOKEN_FOR_V7238}")"
done
echo "entry_bias_cron -> $(test -f /etc/cron.d/tradingbot_v7238_entry_timing_bias && echo OK || echo MISSING)"
echo "entry_bias_file -> $(test -f data/v7238_entry_timing_bias_report_last.json && echo OK || echo MISSING)"
for R in entry-bias-optimizer bias-quality-report entry-timing-review soft-gate-backtest master markets single-header setup-optimizer performance-learning risk-management; do
  TMP="/tmp/v7238_${R}.html"
  CODE="$(curl -s -o "$TMP" -w "%{http_code}" "http://127.0.0.1/${R}?token=${TOKEN_FOR_V7238}")"
  HEADER="$(grep -c 'id="v7208_single_header_bar"' "$TMP" || true)"
  BIAS="$(grep -c '/entry-bias-optimizer' "$TMP" || true)"
  echo "/${R}?token=*** -> ${CODE} | single_header_hits=${HEADER} | bias_link_hits=${BIAS}"
done
EOF
chmod +x "$CHECK_FILE"
fi

echo "== syntax =="
python3 -m py_compile app/v7238_entry_timing_bias_optimizer.py app/v7208_single_header_mode.py app/main.py

echo "== docker rebuild =="
docker compose up -d --build tradingbot || docker restart tradingbot
sleep 6

TOKEN_TEST="${1:-$TOKEN_DEFAULT}"

echo "== Route Test =="
for U in entry-bias-optimizer entry-bias-optimizer.json entry-bias-optimizer-config.json bias-quality-report bias-quality-report.json entry-timing-review entry-timing-review.json soft-gate-backtest soft-gate-backtest.json; do
  echo "${U}_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/${U}?token=${TOKEN_TEST}")"
done

echo ""
echo "== Header Link Test =="
for R in entry-bias-optimizer bias-quality-report entry-timing-review soft-gate-backtest master markets single-header setup-optimizer performance-learning risk-management; do
  TMP="/tmp/v7238_${R}.html"
  CODE="$(curl -s -o "$TMP" -w "%{http_code}" "http://127.0.0.1/${R}?token=${TOKEN_TEST}")"
  HEADER="$(grep -c 'id="v7208_single_header_bar"' "$TMP" || true)"
  BIAS="$(grep -c '/entry-bias-optimizer' "$TMP" || true)"
  echo "/${R}?token=*** -> ${CODE} | single_header=${HEADER} | bias_link=${BIAS}"
done

echo ""
echo "== Snapshot =="
bash ops/v7238_entry_timing_bias_report.sh "$TOKEN_TEST" || true
echo "entry_bias_file=$(test -f data/v7238_entry_timing_bias_report_last.json && echo OK || echo MISSING)"
echo "entry_bias_cron=$(test -f /etc/cron.d/tradingbot_v7238_entry_timing_bias && echo OK || echo MISSING)"

echo ""
echo "== V7238 Summary Preview =="
curl -s "http://127.0.0.1/entry-bias-optimizer.json?token=${TOKEN_TEST}" \
  | python3 -m json.tool \
  | grep -E '"matched_live_total"|"best_bias_gates"|"best_trade_bias"|"best_htf_bias"|"weak_bias_gates"|"weak_trade_bias"|"weak_htf_bias"|"latest_action_counts"|"soft_gate_backtest"|"observe_only"|"apply_to_live"' \
  | head -n 160 || true

echo ""
echo "== Soft Gate Backtest Preview =="
curl -s "http://127.0.0.1/soft-gate-backtest.json?token=${TOKEN_TEST}" \
  | python3 -m json.tool \
  | grep -E '"soft_action"|"count"|"wins"|"losses"|"winrate"|"total_r"|"removed_count"|"removed_total_r"|"remaining_count"|"remaining_total_r"' \
  | head -n 160 || true

echo ""
echo "== Smoke Existing =="
echo "master_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/master?token=${TOKEN_TEST}")"
echo "markets_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/markets?token=${TOKEN_TEST}")"
echo "setup_optimizer_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/setup-optimizer?token=${TOKEN_TEST}")"
echo "performance_learning_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/performance-learning?token=${TOKEN_TEST}")"
echo "risk_management_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/risk-management?token=${TOKEN_TEST}")"

echo ""
echo "== V7238 ENTRY TIMING & BIAS OPTIMIZER DONE =="
