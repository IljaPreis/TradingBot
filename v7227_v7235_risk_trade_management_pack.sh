#!/usr/bin/env bash
set -euo pipefail

cd /opt/tradingbot_v6000 || exit 1

TS="$(date -u +%Y%m%d_%H%M%S)"
TOKEN_DEFAULT="eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg"

echo "== V7227-V7235 RISK & TRADE MANAGEMENT PACK =="
echo "Mode: OBSERVE-ONLY. No trades, no blocks, no auto actions."

mkdir -p backups app data ops

echo "== Backup =="
tar -czf "backups/v7227_v7235_risk_management_before_${TS}.tar.gz" \
  --exclude='backups' \
  --exclude='__pycache__' \
  --exclude='.git' \
  . || true

echo "== Config =="
cat > data/v7227_v7235_risk_management_config.json <<'JSON'
{
  "version": "V7227-V7235",
  "enabled": true,
  "observe_only": true,
  "stale_heartbeat_seconds": 600,
  "profit_protect_r": 0.5,
  "take_partial_r": 1.0,
  "danger_negative_r": -0.5,
  "near_tp_ratio": 0.25,
  "near_sl_ratio": 0.25,
  "max_cluster_open_trades": 2,
  "note": "Risk management pack is observe-only. It does not execute, block, close, or modify trades."
}
JSON

echo "== Module =="
cat > app/v7227_v7235_risk_trade_management_pack.py <<'PY'
import json
import html
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict
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
        "version": "V7227-V7235",
        "enabled": True,
        "observe_only": True,
        "stale_heartbeat_seconds": 600,
        "profit_protect_r": 0.5,
        "take_partial_r": 1.0,
        "danger_negative_r": -0.5,
        "near_tp_ratio": 0.25,
        "near_sl_ratio": 0.25,
        "max_cluster_open_trades": 2,
        "note": "Risk management pack is observe-only. It does not execute, block, close, or modify trades.",
    }
    c.update(_read_json("v7227_v7235_risk_management_config.json", {}))
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
            "events": [],
            "load_error": str(exc),
        }


def _control_status():
    try:
        from app.v7207_master_compact_control_center import _status
        return _status()
    except Exception as exc:
        return {
            "safe_state": False,
            "risky_on": True,
            "safety": {},
            "event_risk": _event_data(),
            "load_error": str(exc),
        }


def _decision_explain(market):
    try:
        from app.v7209_v7212_decision_suite import _explain_market
        return _explain_market(market)
    except Exception:
        return {"market": market, "decision": "UNKNOWN", "readiness": {"state": "UNKNOWN"}, "quality": {}}


def _daily_intelligence():
    try:
        from app.v7213_v7218_intelligence_pack import _daily_report
        return _daily_report(write_file=False)
    except Exception:
        return _read_json("v7218_daily_intelligence_report_last.json", {})


def _performance_summary():
    try:
        from app.v7219_v7226_performance_learning_pack import _summary
        return _summary()
    except Exception:
        return _read_json("v7226_daily_performance_report_last.json", {}).get("performance_summary", {})


def _num(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(str(x).replace(",", "."))
    except Exception:
        return default


def _parse_dt(x):
    if not x:
        return None
    s = str(x).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _now():
    return datetime.now(timezone.utc)


def _sqlite_files():
    d = _root() / "data"
    if not d.exists():
        return []
    return sorted(d.glob("*.sqlite3"))


def _pick(low, names):
    for n in names:
        if n in low:
            return low[n]
    return None


def _latest_heartbeats():
    rows = []
    for db in _sqlite_files():
        try:
            con = sqlite3.connect(str(db))
            con.row_factory = sqlite3.Row
        except Exception:
            continue

        try:
            tables = con.execute("select name from sqlite_master where type='table'").fetchall()
            for tr in tables:
                table = tr[0]
                tl = table.lower()
                if not any(x in tl for x in ["heartbeat", "market", "price"]):
                    continue

                try:
                    info = con.execute(f'pragma table_info("{table}")').fetchall()
                except Exception:
                    continue

                cols = [x[1] for x in info]
                low = {str(c).lower(): c for c in cols}

                market_col = _pick(low, ["market", "symbol", "instrument", "ticker"])
                close_col = _pick(low, ["close", "price", "last_price"])
                time_col = _pick(low, ["received_at", "timestamp", "created_at", "time"])
                tf_col = _pick(low, ["timeframe", "tf"])

                if not market_col or not close_col:
                    continue

                order = f'order by "{time_col}" desc' if time_col else "order by rowid desc"

                try:
                    for r in con.execute(f'select * from "{table}" {order} limit 500').fetchall():
                        d = dict(r)
                        m = str(d.get(market_col, "")).upper().strip()
                        if not m:
                            continue
                        rows.append({
                            "market": m,
                            "close": _num(d.get(close_col), None),
                            "received_at": d.get(time_col) if time_col else None,
                            "timeframe": d.get(tf_col) if tf_col else None,
                            "source_db": db.name,
                            "source_table": table,
                        })
                except Exception:
                    continue
        finally:
            try:
                con.close()
            except Exception:
                pass

    latest = {}
    for r in rows:
        if r["market"] not in latest:
            latest[r["market"]] = r
    return latest


def _scan_open_trades():
    rows = []
    for db in _sqlite_files():
        try:
            con = sqlite3.connect(str(db))
            con.row_factory = sqlite3.Row
        except Exception:
            continue

        try:
            tables = con.execute("select name from sqlite_master where type='table'").fetchall()
            for tr in tables:
                table = tr[0]
                tl = table.lower()
                if not any(x in tl for x in ["trade", "position", "order"]):
                    continue

                try:
                    info = con.execute(f'pragma table_info("{table}")').fetchall()
                except Exception:
                    continue

                cols = [x[1] for x in info]
                low = {str(c).lower(): c for c in cols}

                market_col = _pick(low, ["market", "symbol", "instrument", "ticker"])
                side_col = _pick(low, ["direction", "side", "action"])
                status_col = _pick(low, ["status", "state"])
                setup_col = _pick(low, ["setup_name", "setup", "strategy", "signal_name"])
                entry_col = _pick(low, ["entry", "entry_price", "price"])
                sl_col = _pick(low, ["sl", "stop_loss", "stop"])
                tp_col = _pick(low, ["tp1", "tp", "take_profit", "target"])
                opened_col = _pick(low, ["opened_at", "created_at", "timestamp", "time"])
                id_col = _pick(low, ["client_trade_id", "trade_id", "id", "signal_id", "decision_id"])

                if not market_col or not entry_col:
                    continue

                order = f'order by "{opened_col}" desc' if opened_col else "order by rowid desc"

                try:
                    raw = con.execute(f'select * from "{table}" {order} limit 1000').fetchall()
                except Exception:
                    continue

                for rr in raw:
                    d = dict(rr)
                    status = str(d.get(status_col, "") or "").upper().strip() if status_col else ""
                    if status and status not in {"OPEN", "ACTIVE", "LIVE", "RUNNING"}:
                        continue

                    market = str(d.get(market_col, "")).upper().strip()
                    if not market:
                        continue

                    side = str(d.get(side_col, "") or "").upper().strip()
                    if side in {"BUY", "LONG"}:
                        side = "LONG"
                    elif side in {"SELL", "SHORT"}:
                        side = "SHORT"

                    entry = _num(d.get(entry_col), None)
                    sl = _num(d.get(sl_col), None) if sl_col else None
                    tp1 = _num(d.get(tp_col), None) if tp_col else None

                    if entry is None:
                        continue

                    rows.append({
                        "source_db": db.name,
                        "source_table": table,
                        "id": d.get(id_col) if id_col else None,
                        "market": market,
                        "side": side,
                        "setup_name": str(d.get(setup_col, "") or "UNKNOWN_SETUP"),
                        "entry": entry,
                        "sl": sl,
                        "tp1": tp1,
                        "status": status or "OPEN",
                        "opened_at": str(d.get(opened_col)) if opened_col and d.get(opened_col) is not None else None,
                        "raw": {k: d.get(k) for k in d.keys() if k in {market_col, side_col, status_col, setup_col, entry_col, sl_col, tp_col, opened_col, id_col}},
                    })
        finally:
            try:
                con.close()
            except Exception:
                pass

    seen = set()
    clean = []
    for r in rows:
        key = (r.get("source_db"), r.get("source_table"), str(r.get("id")), r.get("market"), r.get("side"), r.get("entry"), r.get("opened_at"))
        if key in seen:
            continue
        seen.add(key)
        clean.append(r)
    return clean


def _calc_trade(trade, hb=None):
    cfg = _cfg()
    hb = hb or {}
    close = _num(hb.get("close"), None)
    entry = _num(trade.get("entry"), None)
    sl = _num(trade.get("sl"), None)
    tp = _num(trade.get("tp1"), None)
    side = str(trade.get("side", "")).upper()

    out = dict(trade)
    out["close"] = close
    out["heartbeat_received_at"] = hb.get("received_at")
    out["heartbeat_ok"] = False
    out["heartbeat_age_seconds"] = None
    out["r_now"] = None
    out["to_tp"] = None
    out["to_sl"] = None
    out["progress_to_tp"] = None
    out["risk_flags"] = []

    hb_dt = _parse_dt(hb.get("received_at"))
    if hb_dt:
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=timezone.utc)
        age = max(0, int((_now() - hb_dt).total_seconds()))
        out["heartbeat_age_seconds"] = age
        out["heartbeat_ok"] = age <= int(cfg.get("stale_heartbeat_seconds", 600))

    if not out["heartbeat_ok"]:
        out["risk_flags"].append("STALE_OR_MISSING_HEARTBEAT")

    if close is None or entry is None or sl is None:
        out["risk_flags"].append("MISSING_PRICE_OR_SL")
        return out

    if side == "SHORT":
        risk = sl - entry
        r_now = (entry - close) / risk if risk > 0 else None
        out["to_tp"] = close - tp if tp is not None else None
        out["to_sl"] = sl - close
        if tp is not None and entry != tp:
            out["progress_to_tp"] = (entry - close) / (entry - tp)
    else:
        risk = entry - sl
        r_now = (close - entry) / risk if risk > 0 else None
        out["to_tp"] = tp - close if tp is not None else None
        out["to_sl"] = close - sl
        if tp is not None and tp != entry:
            out["progress_to_tp"] = (close - entry) / (tp - entry)

    if r_now is not None:
        out["r_now"] = round(r_now, 4)

    if out["r_now"] is not None:
        if out["r_now"] <= float(cfg.get("danger_negative_r", -0.5)):
            out["risk_flags"].append("DANGER_NEGATIVE_R")
        if out["r_now"] >= float(cfg.get("profit_protect_r", 0.5)):
            out["risk_flags"].append("PROFIT_PROTECT_ZONE")
        if out["r_now"] >= float(cfg.get("take_partial_r", 1.0)):
            out["risk_flags"].append("PARTIAL_PROFIT_ZONE")

    if out.get("progress_to_tp") is not None:
        pr = float(out["progress_to_tp"])
        out["progress_to_tp"] = round(pr, 4)
        if pr >= (1 - float(cfg.get("near_tp_ratio", 0.25))):
            out["risk_flags"].append("NEAR_TP")
        if pr <= float(cfg.get("near_sl_ratio", 0.25)):
            out["risk_flags"].append("NEAR_ENTRY_OR_SL_ZONE")

    return out


def _open_trades_enriched():
    heartbeats = _latest_heartbeats()
    trades = []
    for t in _scan_open_trades():
        trades.append(_calc_trade(t, heartbeats.get(t.get("market"))))
    trades.sort(key=lambda x: (x.get("r_now") is not None, x.get("r_now") or -999), reverse=True)
    return trades


def _family(market):
    m = str(market).upper()
    if m in {"US30", "US100", "US500", "GER40", "FRA40", "FTSE100", "ASX200"}:
        return "INDEX"
    if m in {"XAUUSD", "UKOILSPOT", "BRENT"}:
        return "COMMODITY"
    if any(ccy in m for ccy in ["EUR", "GBP", "USD", "JPY", "AUD", "NZD", "CAD", "CHF"]):
        return "FX"
    return "OTHER"


def _cluster(market):
    m = str(market).upper()
    if m in {"US30", "US100", "US500"}:
        return "US_INDEX"
    if m in {"GER40", "FRA40", "FTSE100"}:
        return "EU_INDEX"
    if m in {"ASX200"}:
        return "ASIA_INDEX"
    if m in {"XAUUSD"}:
        return "GOLD"
    if m in {"UKOILSPOT", "BRENT"}:
        return "OIL"
    if "JPY" in m:
        return "JPY_FX"
    if "USD" in m:
        return "USD_FX"
    return _family(m)


def _position_overview():
    trades = _open_trades_enriched()
    total_r = round(sum(float(t.get("r_now") or 0) for t in trades), 4)
    flags = Counter()
    for t in trades:
        for f in t.get("risk_flags", []):
            flags[f] += 1
    return {
        "version": "V7227",
        "mode": "POSITION_OVERVIEW_BOARD",
        "now_utc": _now().isoformat(),
        "open_trade_count": len(trades),
        "total_open_r": total_r,
        "flag_counts": dict(flags),
        "open_trades": trades,
        "observe_only": True,
    }


def _open_trade_risk():
    trades = _open_trades_enriched()
    out = []
    for t in trades:
        flags = t.get("risk_flags", [])
        score = 0
        if "STALE_OR_MISSING_HEARTBEAT" in flags:
            score += 35
        if "MISSING_PRICE_OR_SL" in flags:
            score += 40
        if "DANGER_NEGATIVE_R" in flags:
            score += 30
        if "NEAR_ENTRY_OR_SL_ZONE" in flags:
            score += 10
        if "PROFIT_PROTECT_ZONE" in flags:
            score += 5
        if score >= 60:
            level = "HIGH"
        elif score >= 25:
            level = "MEDIUM"
        else:
            level = "LOW"
        item = dict(t)
        item["risk_score"] = score
        item["risk_level"] = level
        out.append(item)
    out.sort(key=lambda x: x.get("risk_score", 0), reverse=True)
    return {
        "version": "V7228",
        "mode": "OPEN_TRADE_RISK_ANALYZER",
        "now_utc": _now().isoformat(),
        "open_trade_count": len(out),
        "risk_counts": dict(Counter(x.get("risk_level") for x in out)),
        "trades": out,
        "observe_only": True,
    }


def _exit_readiness():
    trades = _open_trades_enriched()
    out = []
    for t in trades:
        r = t.get("r_now")
        flags = t.get("risk_flags", [])
        if "STALE_OR_MISSING_HEARTBEAT" in flags:
            state = "CHECK_DATA_FIRST"
            action = "No management decision until heartbeat is fresh."
        elif "PARTIAL_PROFIT_ZONE" in flags:
            state = "PARTIAL_READY"
            action = "Consider partial profit or tighter management."
        elif "NEAR_TP" in flags:
            state = "TP_CLOSE"
            action = "Monitor closely near target."
        elif "PROFIT_PROTECT_ZONE" in flags:
            state = "PROTECT_PROFIT"
            action = "Consider protecting profit; no auto action."
        elif "DANGER_NEGATIVE_R" in flags:
            state = "DANGER_REVIEW"
            action = "Review invalidation and exit logic."
        elif r is not None and r > 0:
            state = "HOLD_WITH_MANAGEMENT"
            action = "Trade positive; keep monitoring."
        else:
            state = "WAIT"
            action = "No exit readiness signal."
        item = dict(t)
        item["exit_readiness"] = state
        item["management_note"] = action
        out.append(item)
    return {
        "version": "V7229",
        "mode": "EXIT_READINESS_ENGINE",
        "now_utc": _now().isoformat(),
        "open_trade_count": len(out),
        "exit_counts": dict(Counter(x.get("exit_readiness") for x in out)),
        "trades": out,
        "observe_only": True,
    }


def _sl_tp_review():
    trades = _open_trades_enriched()
    out = []
    for t in trades:
        issues = []
        side = str(t.get("side", "")).upper()
        entry = _num(t.get("entry"), None)
        sl = _num(t.get("sl"), None)
        tp = _num(t.get("tp1"), None)
        if entry is None:
            issues.append("MISSING_ENTRY")
        if sl is None:
            issues.append("MISSING_SL")
        if tp is None:
            issues.append("MISSING_TP")
        if entry is not None and sl is not None:
            if side == "LONG" and sl >= entry:
                issues.append("INVALID_LONG_SL")
            if side == "SHORT" and sl <= entry:
                issues.append("INVALID_SHORT_SL")
        if entry is not None and tp is not None:
            if side == "LONG" and tp <= entry:
                issues.append("INVALID_LONG_TP")
            if side == "SHORT" and tp >= entry:
                issues.append("INVALID_SHORT_TP")
        item = dict(t)
        item["sl_tp_issues"] = issues
        item["sl_tp_ok"] = not issues
        out.append(item)
    return {
        "version": "V7230",
        "mode": "SL_TP_REVIEW",
        "now_utc": _now().isoformat(),
        "open_trade_count": len(out),
        "invalid_count": sum(1 for x in out if not x.get("sl_tp_ok")),
        "trades": out,
        "observe_only": True,
    }


def _cluster_exposure():
    trades = _open_trades_enriched()
    groups = defaultdict(list)
    for t in trades:
        t["cluster"] = _cluster(t.get("market"))
        groups[t["cluster"]].append(t)
    rows = []
    for name, items in groups.items():
        total_r = round(sum(float(x.get("r_now") or 0) for x in items), 4)
        sides = Counter(str(x.get("side")) for x in items)
        markets = sorted(set(str(x.get("market")) for x in items))
        max_allowed = int(_cfg().get("max_cluster_open_trades", 2))
        warnings = []
        if len(items) > max_allowed:
            warnings.append("CLUSTER_OVEREXPOSED")
        if len(sides) > 1:
            warnings.append("MIXED_DIRECTION")
        rows.append({
            "cluster": name,
            "count": len(items),
            "markets": markets,
            "sides": dict(sides),
            "total_open_r": total_r,
            "warnings": warnings,
            "trades": items,
        })
    rows.sort(key=lambda x: x.get("count", 0), reverse=True)
    return {
        "version": "V7231",
        "mode": "CLUSTER_EXPOSURE_BOARD",
        "now_utc": _now().isoformat(),
        "cluster_count": len(rows),
        "clusters": rows,
        "observe_only": True,
    }


def _news_risk_open_trades():
    trades = _open_trades_enriched()
    ev = _event_data()
    out = []
    for t in trades:
        market = t.get("market")
        ex = _decision_explain(market)
        event = (ex.get("event") or {}).get("market_event") or (ex.get("quality") or {}).get("event") or {}
        item = dict(t)
        item["global_event_risk"] = {
            "risk_level": ev.get("risk_level"),
            "cooldown_active": ev.get("cooldown_active"),
            "upcoming_count": ev.get("upcoming_count"),
            "active_count": ev.get("active_count"),
        }
        item["market_event"] = event
        item["decision_readiness"] = ex.get("readiness", {})
        risk = "LOW"
        if ev.get("cooldown_active"):
            risk = "HIGH"
        elif event and str(event.get("impact", "")).upper() == "HIGH":
            risk = "MEDIUM"
        elif ev.get("upcoming_count", 0):
            risk = "WATCH"
        item["open_trade_news_risk"] = risk
        out.append(item)
    return {
        "version": "V7232",
        "mode": "NEWS_RISK_FOR_OPEN_TRADES",
        "now_utc": _now().isoformat(),
        "open_trade_count": len(out),
        "risk_counts": dict(Counter(x.get("open_trade_news_risk") for x in out)),
        "trades": out,
        "observe_only": True,
    }


def _recommendations():
    risk = _open_trade_risk().get("trades", [])
    exits = {str(x.get("id")): x for x in _exit_readiness().get("trades", [])}
    news = {str(x.get("id")): x for x in _news_risk_open_trades().get("trades", [])}
    sltp = {str(x.get("id")): x for x in _sl_tp_review().get("trades", [])}

    out = []
    for t in risk:
        key = str(t.get("id"))
        notes = []
        priority = "LOW"

        if not sltp.get(key, {}).get("sl_tp_ok", True):
            notes.append("SL/TP invalid or incomplete; review immediately.")
            priority = "HIGH"

        if t.get("risk_level") == "HIGH":
            notes.append("High trade risk score; review position.")
            priority = "HIGH"
        elif t.get("risk_level") == "MEDIUM":
            notes.append("Medium trade risk; monitor closely.")
            priority = "MEDIUM"

        er = exits.get(key, {})
        if er.get("exit_readiness") in {"PARTIAL_READY", "TP_CLOSE", "PROTECT_PROFIT"}:
            notes.append(er.get("management_note"))
            if priority == "LOW":
                priority = "MEDIUM"

        nr = news.get(key, {})
        if nr.get("open_trade_news_risk") in {"HIGH", "MEDIUM"}:
            notes.append(f"News risk {nr.get('open_trade_news_risk')}; avoid adding, manage only.")
            priority = "HIGH" if nr.get("open_trade_news_risk") == "HIGH" else priority

        if not notes:
            notes.append("No urgent management action. Continue monitoring.")

        item = dict(t)
        item["recommendation_priority"] = priority
        item["recommendations"] = notes
        out.append(item)

    out.sort(key=lambda x: {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(x.get("recommendation_priority"), 0), reverse=True)

    return {
        "version": "V7233",
        "mode": "TRADE_MANAGEMENT_RECOMMENDATIONS",
        "now_utc": _now().isoformat(),
        "open_trade_count": len(out),
        "priority_counts": dict(Counter(x.get("recommendation_priority") for x in out)),
        "trades": out,
        "observe_only": True,
        "note": "Recommendations only. No auto execution.",
    }


def _log_path():
    return _data("v7234_trade_management_log.jsonl")


def _append_log(record):
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _read_log(limit=500):
    p = _log_path()
    if not p.exists():
        return []
    rows = []
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in reversed(lines[-5000:]):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
            if len(rows) >= limit:
                break
    except Exception:
        pass
    return rows


def _management_log(limit=500, write_snapshot=False):
    if write_snapshot:
        snap = {
            "version": "V7234",
            "logged_utc": _now().isoformat(),
            "position_overview": _position_overview(),
            "recommendations": _recommendations(),
            "observe_only": True,
        }
        _append_log(snap)
    rows = _read_log(limit)
    return {
        "version": "V7234",
        "mode": "TRADE_MANAGEMENT_LOG",
        "now_utc": _now().isoformat(),
        "rows_total": len(rows),
        "latest": rows[:100],
        "log_file": str(_log_path()),
        "observe_only": True,
    }


def _daily_risk_report(write_file=False):
    control = _control_status()
    report = {
        "version": "V7235",
        "mode": "DAILY_RISK_MANAGEMENT_REPORT",
        "generated_utc": _now().isoformat(),
        "safe_state": control.get("safe_state"),
        "risky_on": control.get("risky_on"),
        "position_overview": _position_overview(),
        "open_trade_risk": _open_trade_risk(),
        "exit_readiness": _exit_readiness(),
        "sl_tp_review": _sl_tp_review(),
        "cluster_exposure": _cluster_exposure(),
        "news_risk_open_trades": _news_risk_open_trades(),
        "recommendations": _recommendations(),
        "observe_only": True,
        "note": "Daily risk management report is observe-only.",
    }
    if write_file:
        _write_json("v7235_daily_risk_management_report_last.json", report)
        _management_log(limit=100, write_snapshot=True)
    return report


def _risk_suite():
    return {
        "version": "V7227-V7235",
        "mode": "RISK_TRADE_MANAGEMENT_SUITE",
        "now_utc": _now().isoformat(),
        "config": _cfg(),
        "position_overview": _position_overview(),
        "open_trade_risk": _open_trade_risk(),
        "exit_readiness": _exit_readiness(),
        "sl_tp_review": _sl_tp_review(),
        "cluster_exposure": _cluster_exposure(),
        "news_risk_open_trades": _news_risk_open_trades(),
        "recommendations": _recommendations(),
        "observe_only": True,
    }


def _links(token):
    links = [
        ("Risk Suite", "/risk-management"),
        ("Positions", "/position-overview"),
        ("Trade Risk", "/open-trade-risk"),
        ("Exit", "/exit-readiness"),
        ("SL/TP", "/sl-tp-review"),
        ("Cluster", "/cluster-exposure"),
        ("News Risk", "/open-trade-news-risk"),
        ("Recommendations", "/trade-management-recommendations"),
        ("Log", "/trade-management-log"),
        ("Daily Risk", "/daily-risk-report"),
        ("Performance", "/performance-learning"),
        ("Intel", "/daily-intelligence"),
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
pre{{white-space:pre-wrap;background:#0f1721;border:1px solid #263447;border-radius:10px;padding:12px}}
</style></head><body><h1>{_esc(title)}</h1><div class="card">{_links(token)}</div>{body}</body></html>"""


def _trade_rows(rows, mode="risk"):
    out = ""
    for r in rows:
        extra = ""
        if "risk_level" in r:
            extra = r.get("risk_level")
        elif "exit_readiness" in r:
            extra = r.get("exit_readiness")
        elif "sl_tp_ok" in r:
            extra = "OK" if r.get("sl_tp_ok") else "INVALID"
        elif "open_trade_news_risk" in r:
            extra = r.get("open_trade_news_risk")
        elif "recommendation_priority" in r:
            extra = r.get("recommendation_priority")
        out += f"""<tr>
<td>{_esc(r.get("market"))}</td><td>{_esc(r.get("side"))}</td><td>{_esc(r.get("setup_name"))}</td>
<td>{_esc(r.get("entry"))}</td><td>{_esc(r.get("sl"))}</td><td>{_esc(r.get("tp1"))}</td><td>{_esc(r.get("close"))}</td>
<td>{_esc(r.get("r_now"))}</td><td>{_esc(extra)}</td><td>{_esc(r.get("risk_flags") or r.get("recommendations") or r.get("sl_tp_issues"))}</td>
</tr>"""
    return out or "<tr><td colspan='10'>Keine offenen Trades.</td></tr>"


def _trade_table(title, rows):
    return f"""<div class="card"><h2>{_esc(title)}</h2><table>
<tr><th>Market</th><th>Side</th><th>Setup</th><th>Entry</th><th>SL</th><th>TP1</th><th>Close</th><th>R now</th><th>Status</th><th>Notes</th></tr>
{_trade_rows(rows)}</table></div>"""


def _cluster_rows(rows):
    out = ""
    for r in rows:
        out += f"""<tr><td>{_esc(r.get("cluster"))}</td><td>{_esc(r.get("count"))}</td><td>{_esc(r.get("markets"))}</td>
<td>{_esc(r.get("sides"))}</td><td>{_esc(r.get("total_open_r"))}</td><td>{_esc(r.get("warnings"))}</td></tr>"""
    return out or "<tr><td colspan='6'>Keine Cluster.</td></tr>"


def _log_rows(rows):
    out = ""
    for r in rows:
        po = r.get("position_overview", {})
        out += f"""<tr><td>{_esc(r.get("logged_utc"))}</td><td>{_esc(po.get("open_trade_count"))}</td>
<td>{_esc(po.get("total_open_r"))}</td><td>{_esc((r.get("recommendations") or {}).get("priority_counts"))}</td></tr>"""
    return out or "<tr><td colspan='4'>Noch kein Management Log.</td></tr>"


def install_v7227_v7235_risk_trade_management_pack(app):
    if getattr(app.state, "v7227_v7235_risk_pack_installed", False):
        return

    @app.get("/risk-management", response_class=HTMLResponse)
    def risk_management_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        s = _risk_suite()
        po = s.get("position_overview", {})
        rec = s.get("recommendations", {})
        body = f"""<div class="card"><span class="badge">V7227-V7235 RISK MANAGEMENT</span>
<p class="muted">Open Trades: <b>{_esc(po.get("open_trade_count"))}</b> | Open R: <b>{_esc(po.get("total_open_r"))}</b> | Priority: <b>{_esc(rec.get("priority_counts"))}</b></p>
<a href="/risk-management.json?token={_esc(request.query_params.get("token",""))}">JSON</a></div>
{_trade_table("Open Positions", po.get("open_trades", []))}
{_trade_table("Recommendations", rec.get("trades", []))}"""
        return HTMLResponse(_html_page("TradingBot V7227-V7235 - Risk Management", body, request))

    @app.get("/risk-management.json")
    def risk_management_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_risk_suite())

    @app.get("/risk-management-config.json")
    def risk_management_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    @app.get("/position-overview", response_class=HTMLResponse)
    def position_overview_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _position_overview()
        return HTMLResponse(_html_page("TradingBot V7227 - Position Overview", _trade_table("Open Positions", r.get("open_trades", [])), request))

    @app.get("/position-overview.json")
    def position_overview_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_position_overview())

    @app.get("/open-trade-risk", response_class=HTMLResponse)
    def open_trade_risk_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _open_trade_risk()
        body = f'<div class="card"><span class="badge">V7228 OPEN TRADE RISK</span><p class="muted">{_esc(r.get("risk_counts"))}</p></div>' + _trade_table("Trade Risk", r.get("trades", []))
        return HTMLResponse(_html_page("TradingBot V7228 - Open Trade Risk", body, request))

    @app.get("/open-trade-risk.json")
    def open_trade_risk_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_open_trade_risk())

    @app.get("/exit-readiness", response_class=HTMLResponse)
    def exit_readiness_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _exit_readiness()
        body = f'<div class="card"><span class="badge">V7229 EXIT READINESS</span><p class="muted">{_esc(r.get("exit_counts"))}</p></div>' + _trade_table("Exit Readiness", r.get("trades", []))
        return HTMLResponse(_html_page("TradingBot V7229 - Exit Readiness", body, request))

    @app.get("/exit-readiness.json")
    def exit_readiness_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_exit_readiness())

    @app.get("/sl-tp-review", response_class=HTMLResponse)
    def sl_tp_review_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _sl_tp_review()
        body = f'<div class="card"><span class="badge">V7230 SL/TP REVIEW</span><p class="muted">Invalid: <b>{_esc(r.get("invalid_count"))}</b></p></div>' + _trade_table("SL TP Review", r.get("trades", []))
        return HTMLResponse(_html_page("TradingBot V7230 - SL TP Review", body, request))

    @app.get("/sl-tp-review.json")
    def sl_tp_review_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_sl_tp_review())

    @app.get("/cluster-exposure", response_class=HTMLResponse)
    def cluster_exposure_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _cluster_exposure()
        body = f"""<div class="card"><span class="badge">V7231 CLUSTER EXPOSURE</span></div>
<div class="card"><table><tr><th>Cluster</th><th>Count</th><th>Markets</th><th>Sides</th><th>Open R</th><th>Warnings</th></tr>{_cluster_rows(r.get("clusters", []))}</table></div>"""
        return HTMLResponse(_html_page("TradingBot V7231 - Cluster Exposure", body, request))

    @app.get("/cluster-exposure.json")
    def cluster_exposure_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cluster_exposure())

    @app.get("/open-trade-news-risk", response_class=HTMLResponse)
    def open_trade_news_risk_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _news_risk_open_trades()
        body = f'<div class="card"><span class="badge">V7232 NEWS RISK OPEN TRADES</span><p class="muted">{_esc(r.get("risk_counts"))}</p></div>' + _trade_table("News Risk", r.get("trades", []))
        return HTMLResponse(_html_page("TradingBot V7232 - News Risk Open Trades", body, request))

    @app.get("/open-trade-news-risk.json")
    def open_trade_news_risk_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_news_risk_open_trades())

    @app.get("/trade-management-recommendations", response_class=HTMLResponse)
    def trade_management_recommendations_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _recommendations()
        body = f'<div class="card"><span class="badge">V7233 MANAGEMENT RECOMMENDATIONS</span><p class="muted">{_esc(r.get("priority_counts"))}</p></div>' + _trade_table("Recommendations", r.get("trades", []))
        return HTMLResponse(_html_page("TradingBot V7233 - Trade Management Recommendations", body, request))

    @app.get("/trade-management-recommendations.json")
    def trade_management_recommendations_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_recommendations())

    @app.get("/trade-management-log", response_class=HTMLResponse)
    def trade_management_log_page(request: Request, write: Optional[int] = 0, limit: Optional[int] = 500):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _management_log(limit=int(limit), write_snapshot=bool(int(write)))
        body = f"""<div class="card"><span class="badge">V7234 MANAGEMENT LOG</span><p class="muted">Rows: <b>{_esc(r.get("rows_total"))}</b></p></div>
<div class="card"><table><tr><th>UTC</th><th>Open Trades</th><th>Open R</th><th>Priorities</th></tr>{_log_rows(r.get("latest", []))}</table></div>"""
        return HTMLResponse(_html_page("TradingBot V7234 - Trade Management Log", body, request))

    @app.get("/trade-management-log.json")
    def trade_management_log_json(request: Request, write: Optional[int] = 0, limit: Optional[int] = 500):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_management_log(limit=int(limit), write_snapshot=bool(int(write))))

    @app.get("/daily-risk-report", response_class=HTMLResponse)
    def daily_risk_report_page(request: Request, write: Optional[int] = 1):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        r = _daily_risk_report(write_file=bool(int(write)))
        po = r.get("position_overview", {})
        rec = r.get("recommendations", {})
        body = f"""<div class="card"><span class="badge">V7235 DAILY RISK REPORT</span>
<p class="muted">Safe: <b>{_esc(r.get("safe_state"))}</b> | Risky: <b>{_esc(r.get("risky_on"))}</b> | Open: <b>{_esc(po.get("open_trade_count"))}</b> | Open R: <b>{_esc(po.get("total_open_r"))}</b></p>
<a href="/daily-risk-report.json?token={_esc(request.query_params.get("token",""))}">JSON</a></div>
{_trade_table("Recommendations", rec.get("trades", []))}"""
        return HTMLResponse(_html_page("TradingBot V7235 - Daily Risk Report", body, request))

    @app.get("/daily-risk-report.json")
    def daily_risk_report_json(request: Request, write: Optional[int] = 1):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_daily_risk_report(write_file=bool(int(write))))

    app.state.v7227_v7235_risk_pack_installed = True
    print("[V7227-V7235] Risk & Trade Management Pack installed")
PY

echo "== Header targets =="
python3 - <<'PY'
from pathlib import Path

p = Path("app/v7208_single_header_mode.py")
if p.exists():
    s = p.read_text(encoding="utf-8")
    routes = [
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
        ]
        rest = sorted(existing - set(ordered))
        new = "TARGET_PATHS = {\n" + "\n".join(f'    "{r}",' for r in ordered + rest) + "\n}"
        s = s[:start] + new + s[end:]
    except Exception:
        pass

    if '/risk-management?token=' not in s:
        s = s.replace(
            '<a href="/performance-learning?token={_esc(token)}">Performance</a> |\\n    <a href="/single-header?token={_esc(token)}">V7208</a>',
            '<a href="/performance-learning?token={_esc(token)}">Performance</a> |\\n    <a href="/risk-management?token={_esc(token)}">Risk</a> |\\n    <a href="/single-header?token={_esc(token)}">V7208</a>'
        )
    p.write_text(s, encoding="utf-8")
PY

echo "== main hook =="
if ! grep -q "V7227 V7235 RISK TRADE MANAGEMENT PACK INSTALL" app/main.py; then
cat >> app/main.py <<'PY'

# === V7227 V7235 RISK TRADE MANAGEMENT PACK INSTALL ===
try:
    from app.v7227_v7235_risk_trade_management_pack import install_v7227_v7235_risk_trade_management_pack
    install_v7227_v7235_risk_trade_management_pack(app)
except Exception as exc:
    print("[V7227-V7235] Risk Trade Management Pack install failed:", exc)
# === END V7227 V7235 RISK TRADE MANAGEMENT PACK INSTALL ===
PY
fi

echo "== ops daily risk report =="
cat > ops/v7235_daily_risk_management_report.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
TOKEN="${1:-eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg}"
BASE_URL="${BASE_URL:-http://127.0.0.1}"
cd /opt/tradingbot_v6000 || exit 1
mkdir -p data
TMP="$(mktemp /tmp/v7235_daily_risk.XXXXXX.json)"
trap 'rm -f "$TMP"' EXIT
curl -fsS "${BASE_URL}/daily-risk-report.json?token=${TOKEN}&write=0" > "$TMP"
python3 -m json.tool "$TMP" >/dev/null
mv "$TMP" data/v7235_daily_risk_management_report_last.json
trap - EXIT
echo "v7235 daily risk ok $(date -u +%Y-%m-%dT%H:%M:%SZ)"
SH
chmod +x ops/v7235_daily_risk_management_report.sh

echo "== cron =="
cat > /etc/cron.d/tradingbot_v7235_daily_risk_management <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
19,49 * * * * root cd /opt/tradingbot_v6000 && /opt/tradingbot_v6000/ops/v7235_daily_risk_management_report.sh >> /opt/tradingbot_v6000/data/v7235_daily_risk_cron.log 2>&1
CRON
chmod 0644 /etc/cron.d/tradingbot_v7235_daily_risk_management
if command -v systemctl >/dev/null 2>&1; then systemctl restart cron >/dev/null 2>&1 || true; else service cron restart || true; fi

echo "== check script =="
CHECK_FILE="ops/v7000_check.sh"
if [ -f "$CHECK_FILE" ] && ! grep -q "V7227-V7235 RISK TRADE MANAGEMENT PACK ROUTES" "$CHECK_FILE"; then
cat >> "$CHECK_FILE" <<'EOF'

echo ""
echo "===== V7227-V7235 RISK TRADE MANAGEMENT PACK ROUTES ====="
TOKEN_FOR_V7235="eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg"
for U in risk-management risk-management.json risk-management-config.json position-overview position-overview.json open-trade-risk open-trade-risk.json exit-readiness exit-readiness.json sl-tp-review sl-tp-review.json cluster-exposure cluster-exposure.json open-trade-news-risk open-trade-news-risk.json trade-management-recommendations trade-management-recommendations.json trade-management-log trade-management-log.json daily-risk-report daily-risk-report.json; do
  echo "/${U}?token=*** -> $(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/${U}?token=${TOKEN_FOR_V7235}")"
done
echo "daily_risk_cron -> $(test -f /etc/cron.d/tradingbot_v7235_daily_risk_management && echo OK || echo MISSING)"
echo "daily_risk_file -> $(test -f data/v7235_daily_risk_management_report_last.json && echo OK || echo MISSING)"
for R in risk-management position-overview open-trade-risk exit-readiness sl-tp-review cluster-exposure open-trade-news-risk trade-management-recommendations trade-management-log daily-risk-report master markets single-header performance-learning daily-intelligence; do
  TMP="/tmp/v7235_${R}.html"
  CODE="$(curl -s -o "$TMP" -w "%{http_code}" "http://127.0.0.1/${R}?token=${TOKEN_FOR_V7235}")"
  HEADER="$(grep -c 'id="v7208_single_header_bar"' "$TMP" || true)"
  RISK="$(grep -c '/risk-management' "$TMP" || true)"
  echo "/${R}?token=*** -> ${CODE} | single_header_hits=${HEADER} | risk_link_hits=${RISK}"
done
EOF
chmod +x "$CHECK_FILE"
fi

echo "== syntax =="
python3 -m py_compile app/v7227_v7235_risk_trade_management_pack.py app/v7208_single_header_mode.py app/main.py

echo "== docker rebuild =="
docker compose up -d --build tradingbot || docker restart tradingbot
sleep 6

TOKEN_TEST="${1:-$TOKEN_DEFAULT}"

echo "== Route Test =="
for U in risk-management risk-management.json risk-management-config.json position-overview position-overview.json open-trade-risk open-trade-risk.json exit-readiness exit-readiness.json sl-tp-review sl-tp-review.json cluster-exposure cluster-exposure.json open-trade-news-risk open-trade-news-risk.json trade-management-recommendations trade-management-recommendations.json trade-management-log trade-management-log.json daily-risk-report daily-risk-report.json; do
  echo "${U}_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/${U}?token=${TOKEN_TEST}")"
done

echo ""
echo "== Header Link Test =="
for R in risk-management position-overview open-trade-risk exit-readiness sl-tp-review cluster-exposure open-trade-news-risk trade-management-recommendations trade-management-log daily-risk-report master markets single-header performance-learning daily-intelligence; do
  TMP="/tmp/v7235_${R}.html"
  CODE="$(curl -s -o "$TMP" -w "%{http_code}" "http://127.0.0.1/${R}?token=${TOKEN_TEST}")"
  HEADER="$(grep -c 'id="v7208_single_header_bar"' "$TMP" || true)"
  RISK="$(grep -c '/risk-management' "$TMP" || true)"
  echo "/${R}?token=*** -> ${CODE} | single_header=${HEADER} | risk_link=${RISK}"
done

echo ""
echo "== Daily Risk Snapshot =="
bash ops/v7235_daily_risk_management_report.sh "$TOKEN_TEST" || true
echo "daily_risk_file=$(test -f data/v7235_daily_risk_management_report_last.json && echo OK || echo MISSING)"
echo "daily_risk_cron=$(test -f /etc/cron.d/tradingbot_v7235_daily_risk_management && echo OK || echo MISSING)"

echo ""
echo "== Risk Preview =="
curl -s "http://127.0.0.1/risk-management.json?token=${TOKEN_TEST}" \
  | python3 -m json.tool \
  | grep -E '"open_trade_count"|"total_open_r"|"risk_counts"|"exit_counts"|"invalid_count"|"cluster_count"|"priority_counts"|"safe_state"|"risky_on"|"observe_only"' \
  | head -n 120 || true

echo ""
echo "== Smoke Existing =="
echo "master_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/master?token=${TOKEN_TEST}")"
echo "markets_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/markets?token=${TOKEN_TEST}")"
echo "performance_learning_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/performance-learning?token=${TOKEN_TEST}")"
echo "daily_intelligence_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/daily-intelligence?token=${TOKEN_TEST}")"

echo ""
echo "== V7227-V7235 DONE =="
