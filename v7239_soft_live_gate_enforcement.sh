#!/usr/bin/env bash
set -euo pipefail

cd /opt/tradingbot_v6000 || exit 1

TS="$(date -u +%Y%m%d_%H%M%S)"
TOKEN_DEFAULT="eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg"

echo "== V7239 SOFT LIVE GATE ENFORCEMENT PACK =="
echo "Only enforces V7238 action SHADOW_ONLY_RECOMMENDED."
echo "LIVE_CANDIDATE and LIVE_SMALL_OR_SHADOW_REVIEW stay allowed."
echo "Default apply_to_live=true. Toggle scripts included."

mkdir -p backups app data ops

echo "== Backup =="
tar -czf "backups/v7239_soft_live_gate_before_${TS}.tar.gz" \
  --exclude='backups' \
  --exclude='__pycache__' \
  --exclude='.git' \
  . || true

echo "== Config =="
cat > data/v7239_soft_live_gate_config.json <<'JSON'
{
  "version": "V7239",
  "enabled": true,
  "apply_to_live": true,
  "observe_only": false,
  "enforce_action": "SHADOW_ONLY_RECOMMENDED",
  "allow_actions": ["LIVE_CANDIDATE", "LIVE_SMALL_OR_SHADOW_REVIEW"],
  "update_signal_audit": true,
  "log_all_signals": true,
  "shadow_fallback_expected": true,
  "reason_prefix": "V7239_SOFT_GATE",
  "notes": "Blocks only V7238 SHADOW_ONLY_RECOMMENDED from live. It does not block all WAIT and does not use confidence 75 as a hard rule."
}
JSON

echo "== Module =="
cat > app/v7239_soft_live_gate_enforcement.py <<'PY'
import json
import html
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
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
        "version": "V7239",
        "enabled": True,
        "apply_to_live": True,
        "observe_only": False,
        "enforce_action": "SHADOW_ONLY_RECOMMENDED",
        "allow_actions": ["LIVE_CANDIDATE", "LIVE_SMALL_OR_SHADOW_REVIEW"],
        "update_signal_audit": True,
        "log_all_signals": True,
        "shadow_fallback_expected": True,
        "reason_prefix": "V7239_SOFT_GATE",
        "notes": "Blocks only V7238 SHADOW_ONLY_RECOMMENDED from live. It does not block all WAIT and does not use confidence 75 as a hard rule.",
    }
    c.update(_read_json("v7239_soft_live_gate_config.json", {}))
    c["observe_only"] = not bool(c.get("apply_to_live"))
    return c


def _token_ok(request: Request):
    try:
        from app.v7200_event_risk import _token_ok as real_token_ok
        return real_token_ok(request)
    except Exception:
        return bool(request.query_params.get("token", ""))


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


def _json_loads(x):
    try:
        y = json.loads(x or "{}")
        return y if isinstance(y, dict) else {}
    except Exception:
        return {}


def _get(obj, key, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)
    except Exception:
        return default


def _pick(*vals, default=None):
    for v in vals:
        if v is not None and v != "":
            return v
    return default


def _init_log(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS v7239_soft_gate_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT,
        client_trade_id TEXT,
        market TEXT,
        direction TEXT,
        setup_name TEXT,
        confidence REAL,
        grade TEXT,
        bias_gate TEXT,
        trade_bias TEXT,
        htf_bias TEXT,
        event_risk TEXT,
        soft_score REAL,
        soft_action TEXT,
        reasons_json TEXT,
        previous_allow INTEGER,
        final_allow INTEGER,
        applied INTEGER,
        apply_to_live INTEGER,
        previous_reason TEXT,
        final_reason TEXT,
        row_json TEXT
    )
    """)


def _row_from_audit(client_trade_id):
    if not client_trade_id:
        return None
    try:
        con = _connect()
        r = con.execute("""
            SELECT *
            FROM signal_audit
            WHERE client_trade_id=?
            ORDER BY id DESC
            LIMIT 1
        """, (str(client_trade_id),)).fetchone()
        if not r:
            return None
        d = dict(r)
        raw = _json_loads(d.get("raw_json"))
        sig = raw.get("signal", {}) if isinstance(raw.get("signal"), dict) else {}
        v6000 = raw.get("v6000", {}) if isinstance(raw.get("v6000"), dict) else {}
        return {
            "source": "signal_audit",
            "audit_id": d.get("id"),
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
            "bias_gate": d.get("bias_gate") or sig.get("bias_gate"),
            "entry_state": d.get("entry_state") or sig.get("entry_state"),
            "chase_state": d.get("chase_state") or sig.get("chase_state"),
            "impulse_state": d.get("impulse_state") or sig.get("impulse_state"),
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
        }
    except Exception as exc:
        return {"source": "audit_error", "error": str(exc), "client_trade_id": client_trade_id}
    finally:
        try:
            con.close()
        except Exception:
            pass


def _fallback_row(s, v7, client_trade_id=None, old_d=None):
    old_d = old_d if isinstance(old_d, dict) else {}
    v7 = v7 if isinstance(v7, dict) else {}
    direction = _pick(_get(s, "direction"), _get(s, "side"), v7.get("direction"), v7.get("bias"), old_d.get("bias"), default="UNKNOWN")
    return {
        "source": "fallback",
        "client_trade_id": client_trade_id,
        "market": str(_pick(_get(s, "market"), v7.get("market"), old_d.get("market"), default="UNKNOWN")).upper(),
        "direction": "LONG" if str(direction).upper() in {"BUY", "LONG"} else ("SHORT" if str(direction).upper() in {"SELL", "SHORT"} else str(direction).upper()),
        "setup_name": _pick(_get(s, "trigger"), _get(s, "setup_name"), v7.get("trigger"), v7.get("setup_name"), default="UNKNOWN"),
        "confidence": _num(_pick(_get(s, "confidence"), v7.get("confidence"), v7.get("score"), old_d.get("score")), None),
        "technical_score": _num(_pick(_get(s, "technical_score"), v7.get("technical_score"), old_d.get("score")), None),
        "event_risk": _pick(v7.get("event_risk"), _get(s, "event_risk"), default="normal"),
        "allow_trade": 1 if bool(v7.get("allow_trade")) else 0,
        "reason": v7.get("reason"),
        "bias_gate": _pick(v7.get("bias_gate"), _get(s, "bias_gate")),
        "entry_state": _pick(v7.get("entry_state"), _get(s, "entry_state")),
        "chase_state": _pick(v7.get("chase_state"), _get(s, "chase_state")),
        "impulse_state": _pick(v7.get("impulse_state"), _get(s, "impulse_state")),
        "trade_bias": _pick(v7.get("trade_bias"), _get(s, "trade_bias")),
        "htf_bias": _pick(v7.get("htf_bias"), _get(s, "htf_bias")),
        "premium_discount_state": _pick(v7.get("premium_discount_state"), _get(s, "premium_discount_state")),
        "grade": _pick(v7.get("grade"), old_d.get("grade")),
        "decision": _pick(v7.get("decision"), old_d.get("decision")),
    }


def _soft_gate(row):
    try:
        from app.v7238_entry_timing_bias_optimizer import _recommend_signal
        return _recommend_signal(row)
    except Exception as exc:
        score = 50.0
        reasons = [f"V7238_IMPORT_FALLBACK:{exc}"]
        tb = str(row.get("trade_bias") or "").lower()
        htf = str(row.get("htf_bias") or "").lower()
        bg = str(row.get("bias_gate") or "").lower()
        if tb in {"structure_long", "structure_short"}:
            score += 18
            reasons.append("TRADE_BIAS_STRUCTURE_OK")
        if tb in {"pullback_in_downtrend", "trend_long", "trend_short"}:
            score -= 22
            reasons.append("WEAK_TRADE_BIAS")
        if htf == "neutral":
            score += 16
            reasons.append("HTF_NEUTRAL_EDGE")
        if htf == "mixed_bear":
            score -= 22
            reasons.append("WEAK_HTF_BIAS")
        if bg == "countertrend_watch":
            score -= 18
            reasons.append("COUNTERTREND_WATCH")
        score = max(0, min(100, round(score, 2)))
        if score >= 72:
            action = "LIVE_CANDIDATE"
        elif score >= 58:
            action = "LIVE_SMALL_OR_SHADOW_REVIEW"
        else:
            action = "SHADOW_ONLY_RECOMMENDED"
        return {"score": score, "action": action, "severity": "FALLBACK", "reasons": reasons}


def _log(row, gate, previous_allow, final_allow, applied, previous_reason, final_reason, apply_to_live):
    cfg = _cfg()
    if not cfg.get("log_all_signals", True) and not applied:
        return
    try:
        con = _connect()
        _init_log(con)
        con.execute("""
        INSERT INTO v7239_soft_gate_log
        (created_at, client_trade_id, market, direction, setup_name, confidence, grade,
         bias_gate, trade_bias, htf_bias, event_risk, soft_score, soft_action, reasons_json,
         previous_allow, final_allow, applied, apply_to_live, previous_reason, final_reason, row_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            str(row.get("client_trade_id") or ""),
            str(row.get("market") or ""),
            str(row.get("direction") or ""),
            str(row.get("setup_name") or ""),
            _num(row.get("confidence"), None),
            str(row.get("grade") or ""),
            str(row.get("bias_gate") or ""),
            str(row.get("trade_bias") or ""),
            str(row.get("htf_bias") or ""),
            str(row.get("event_risk") or ""),
            _num(gate.get("score"), None),
            str(gate.get("action") or ""),
            json.dumps(gate.get("reasons", []), ensure_ascii=False),
            1 if previous_allow else 0,
            1 if final_allow else 0,
            1 if applied else 0,
            1 if apply_to_live else 0,
            str(previous_reason or ""),
            str(final_reason or ""),
            json.dumps(row, ensure_ascii=False, default=str),
        ))
        con.commit()
    except Exception:
        pass
    finally:
        try:
            con.close()
        except Exception:
            pass


def _update_signal_audit(client_trade_id, final_allow, final_reason):
    if not client_trade_id or not _cfg().get("update_signal_audit", True):
        return
    try:
        con = _connect()
        con.execute("UPDATE signal_audit SET allow_trade=?, reason=? WHERE client_trade_id=?",
                    (1 if final_allow else 0, str(final_reason), str(client_trade_id)))
        con.commit()
    except Exception:
        pass
    finally:
        try:
            con.close()
        except Exception:
            pass


def v7239_apply_soft_gate(s, v7, client_trade_id=None, old_d=None):
    cfg = _cfg()
    if not isinstance(v7, dict):
        return v7
    if not cfg.get("enabled", True):
        v7["v7239_soft_gate_disabled"] = True
        return v7

    cid = client_trade_id or _get(s, "client_trade_id") or _get(s, "trade_id") or _get(s, "id")
    previous_allow = bool(v7.get("allow_trade"))
    previous_reason = str(v7.get("reason") or "")

    row = _row_from_audit(cid) or _fallback_row(s, v7, cid, old_d)
    row["client_trade_id"] = row.get("client_trade_id") or cid
    row["allow_trade"] = 1 if previous_allow else 0

    gate = _soft_gate(row)
    action = str(gate.get("action") or "")
    apply_to_live = bool(cfg.get("apply_to_live", False))
    enforce_action = str(cfg.get("enforce_action", "SHADOW_ONLY_RECOMMENDED"))

    applied = False
    final_allow = previous_allow
    final_reason = previous_reason

    if previous_allow and apply_to_live and action == enforce_action:
        applied = True
        final_allow = False
        reasons_txt = ",".join(str(x) for x in gate.get("reasons", []))
        final_reason = (
            f"{cfg.get('reason_prefix','V7239_SOFT_GATE')}: live disabled -> shadow review. "
            f"soft_action={action}; score={gate.get('score')}; reasons={reasons_txt}. "
            f"Previous: {previous_reason}"
        )
        v7["allow_trade"] = False
        v7["risk_r"] = 0.0
        v7["v7239_soft_gate_blocked_live"] = True
        v7["v7239_shadow_fallback_expected"] = bool(cfg.get("shadow_fallback_expected", True))
        v7["reason"] = final_reason
    else:
        v7["v7239_soft_gate_blocked_live"] = False

    v7["v7239_soft_gate"] = gate
    v7["v7239_apply_to_live"] = apply_to_live
    v7["v7239_enforced_action"] = enforce_action
    v7["v7239_soft_gate_row_source"] = row.get("source")

    _log(row, gate, previous_allow, final_allow, applied, previous_reason, final_reason, apply_to_live)
    if applied:
        _update_signal_audit(cid, final_allow, final_reason)
    return v7


def _logs(limit=250):
    try:
        con = _connect()
        _init_log(con)
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM v7239_soft_gate_log ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()]
        rows.reverse()
        return rows
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        try:
            con.close()
        except Exception:
            pass


def _stats():
    rows = _logs(1000)
    if rows and "error" in rows[0]:
        return {"error": rows[0]["error"], "config": _cfg()}
    action_counts = Counter(str(r.get("soft_action")) for r in rows)
    reason_counts = Counter()
    for r in rows:
        try:
            reasons = json.loads(r.get("reasons_json") or "[]")
        except Exception:
            reasons = []
        for x in reasons:
            reason_counts[str(x)] += 1
    applied = [r for r in rows if int(r.get("applied") or 0) == 1]
    allowed = [r for r in rows if int(r.get("final_allow") or 0) == 1]
    shadow_seen = [r for r in rows if str(r.get("soft_action")) == "SHADOW_ONLY_RECOMMENDED"]
    return {
        "version": "V7239",
        "mode": "SOFT_LIVE_GATE_STATS",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "config": _cfg(),
        "log_rows": len(rows),
        "applied_blocks": len(applied),
        "final_allowed": len(allowed),
        "soft_shadow_only_seen": len(shadow_seen),
        "action_counts": dict(action_counts),
        "top_reasons": dict(reason_counts.most_common(20)),
        "latest_applied_blocks": applied[-50:],
        "latest": rows[-100:],
    }


def _set_apply_to_live(enabled: bool):
    c = _cfg()
    c["apply_to_live"] = bool(enabled)
    c["observe_only"] = not bool(enabled)
    _write_json("v7239_soft_live_gate_config.json", c)
    return c


def _links(token):
    links = [
        ("Soft Gate", "/soft-live-gate"),
        ("Log", "/soft-live-gate-log"),
        ("V7238", "/entry-bias-optimizer"),
        ("V7237", "/setup-optimizer"),
        ("Master", "/master"),
        ("Risk", "/risk-management"),
    ]
    return " · ".join(f'<a href="{url}?token={_esc(token)}">{_esc(label)}</a>' for label, url in links)


def _html(title, body, request):
    token = request.query_params.get("token", "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>
body{{background:#09111b;color:#e8eef5;font-family:Arial,sans-serif;margin:22px}}
a{{color:#8cc8ff;text-decoration:none}}.card{{background:#121b27;border:1px solid #27364a;border-radius:14px;padding:15px;margin-bottom:16px}}
.badge{{display:inline-block;background:#1e5d9b;color:white;border-radius:999px;padding:7px 11px;font-weight:bold}}.on{{background:#1f7a3d}}.off{{background:#8a6a1f}}
.good{{color:#39d06f}}.bad{{color:#ff5c67}}.yellow{{color:#f3c747}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}}th,td{{border-bottom:1px solid #27364a;padding:8px;text-align:left;vertical-align:top}}th{{color:#a9bfd6}}
.muted{{color:#a9b8c9}}@media(max-width:760px){{body{{margin:12px}}table{{font-size:12px}}th,td{{padding:6px}}}}
</style></head><body><h1>{_esc(title)}</h1><div class="card">{_links(token)}</div>{body}</body></html>"""


def _table(rows):
    trs = ""
    for r in rows:
        applied = int(r.get("applied") or 0) == 1
        cls = "bad" if applied else ("good" if int(r.get("final_allow") or 0) == 1 else "yellow")
        trs += f"""<tr>
<td>{_esc(r.get("created_at"))}</td><td>{_esc(r.get("market"))}</td><td>{_esc(r.get("direction"))}</td><td>{_esc(r.get("setup_name"))}</td>
<td>{_esc(r.get("confidence"))}</td><td>{_esc(r.get("bias_gate"))}</td><td>{_esc(r.get("trade_bias"))}</td><td>{_esc(r.get("htf_bias"))}</td>
<td class="{cls}">{_esc(r.get("soft_score"))} · {_esc(r.get("soft_action"))}</td><td>{_esc(r.get("previous_allow"))}->{_esc(r.get("final_allow"))}</td>
<td>{_esc(r.get("reasons_json"))}</td></tr>"""
    if not trs:
        trs = "<tr><td colspan='11'>Noch keine V7239 Logs.</td></tr>"
    return f"""<table><tr><th>Time</th><th>Market</th><th>Dir</th><th>Setup</th><th>Conf</th><th>BiasGate</th><th>TradeBias</th><th>HTF</th><th>Soft</th><th>Allow</th><th>Reasons</th></tr>{trs}</table>"""


def install_v7239_soft_live_gate_enforcement(app):
    if getattr(app.state, "v7239_soft_live_gate_installed", False):
        return

    @app.get("/soft-live-gate", response_class=HTMLResponse)
    def soft_live_gate_page(request: Request):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        s = _stats()
        cfg = s.get("config", {})
        badge = "on" if cfg.get("apply_to_live") else "off"
        body = f"""<div class="card"><span class="badge {badge}">V7239 SOFT LIVE GATE</span>
<p class="muted">Apply to live: <b>{_esc(cfg.get("apply_to_live"))}</b> | Enforced action: <b>{_esc(cfg.get("enforce_action"))}</b></p>
<p><b>Applied blocks:</b> {_esc(s.get("applied_blocks"))} · <b>Final allowed:</b> {_esc(s.get("final_allowed"))} · <b>Soft shadow-only seen:</b> {_esc(s.get("soft_shadow_only_seen"))}</p>
<p><b>Action counts:</b> {_esc(s.get("action_counts"))}</p>
<p><b>Top reasons:</b> {_esc(s.get("top_reasons"))}</p>
</div><div class="card"><h2>Latest</h2>{_table(s.get("latest", [])[-100:])}</div>"""
        return HTMLResponse(_html("TradingBot V7239 - Soft Live Gate", body, request))

    @app.get("/soft-live-gate.json")
    def soft_live_gate_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_stats())

    @app.get("/soft-live-gate-config.json")
    def soft_live_gate_config_json(request: Request):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_cfg())

    @app.get("/soft-live-gate-log", response_class=HTMLResponse)
    def soft_live_gate_log_page(request: Request, limit: Optional[int] = 250):
        if not _token_ok(request):
            return HTMLResponse("unauthorized", status_code=401)
        rows = _logs(int(limit or 250))
        body = f"""<div class="card"><span class="badge">V7239 LOG</span><p>Rows: {_esc(len(rows))}</p>{_table(rows)}</div>"""
        return HTMLResponse(_html("TradingBot V7239 - Soft Live Gate Log", body, request))

    @app.get("/soft-live-gate-log.json")
    def soft_live_gate_log_json(request: Request, limit: Optional[int] = 250):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"version": "V7239", "rows": _logs(int(limit or 250))})

    @app.post("/soft-live-gate/apply")
    def soft_live_gate_apply_post(request: Request, enabled: bool = True):
        if not _token_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"updated": True, "config": _set_apply_to_live(bool(enabled))})

    app.state.v7239_soft_live_gate_installed = True
    print("[V7239] Soft Live Gate Enforcement installed")
PY

echo "== Patch app/main.py live hook =="
python3 - <<'PY'
from pathlib import Path
from datetime import datetime

p = Path("app/main.py")
s = p.read_text(encoding="utf-8")
Path(f"backups/main_before_v7239_soft_gate_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.py").write_text(s, encoding="utf-8")

if "V7239 SOFT LIVE GATE HOOK" not in s:
    marker = '    client_trade_id = getattr(s, "client_trade_id", None) or getattr(s, "trade_id", None) or getattr(s, "id", None)\n'
    hook = """
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

"""
    if marker not in s:
        raise SystemExit("Could not find client_trade_id marker in app/main.py")
    s = s.replace(marker, marker + hook, 1)
    p.write_text(s, encoding="utf-8")
    print("V7239 hook inserted")
else:
    print("V7239 hook already present")
PY

echo "== Header targets =="
python3 - <<'PY'
from pathlib import Path

p = Path("app/v7208_single_header_mode.py")
if p.exists():
    s = p.read_text(encoding="utf-8")
    routes = ["/soft-live-gate", "/soft-live-gate-log"]
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
            "/soft-live-gate", "/soft-live-gate-log",
        ]
        rest = sorted(existing - set(ordered))
        new = "TARGET_PATHS = {\n" + "\n".join(f'    "{r}",' for r in ordered + rest) + "\n}"
        s = s[:start] + new + s[end:]
    except Exception:
        pass

    if '/soft-live-gate?token=' not in s:
        if '<a href="/entry-bias-optimizer?token={_esc(token)}">Bias</a>' in s:
            s = s.replace(
                '<a href="/entry-bias-optimizer?token={_esc(token)}">Bias</a> |',
                '<a href="/entry-bias-optimizer?token={_esc(token)}">Bias</a> |\\n    <a href="/soft-live-gate?token={_esc(token)}">SoftGate</a> |'
            )
        elif '<a href="/setup-optimizer?token={_esc(token)}">Optimizer</a>' in s:
            s = s.replace(
                '<a href="/setup-optimizer?token={_esc(token)}">Optimizer</a> |',
                '<a href="/setup-optimizer?token={_esc(token)}">Optimizer</a> |\\n    <a href="/soft-live-gate?token={_esc(token)}">SoftGate</a> |'
            )
        else:
            s = s.replace(
                '<a href="/single-header?token={_esc(token)}">V7208</a>',
                '<a href="/soft-live-gate?token={_esc(token)}">SoftGate</a> |\\n    <a href="/single-header?token={_esc(token)}">V7208</a>'
            )
    p.write_text(s, encoding="utf-8")
PY

echo "== main module install hook =="
if ! grep -q "V7239 SOFT LIVE GATE ENFORCEMENT INSTALL" app/main.py; then
cat >> app/main.py <<'PY'

# === V7239 SOFT LIVE GATE ENFORCEMENT INSTALL ===
try:
    from app.v7239_soft_live_gate_enforcement import install_v7239_soft_live_gate_enforcement
    install_v7239_soft_live_gate_enforcement(app)
except Exception as exc:
    print("[V7239] Soft Live Gate Enforcement install failed:", exc)
# === END V7239 SOFT LIVE GATE ENFORCEMENT INSTALL ===
PY
fi

echo "== ops toggle/report scripts =="
cat > ops/v7239_soft_gate_toggle.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/tradingbot_v6000 || exit 1
MODE="${1:-status}"
CFG="data/v7239_soft_live_gate_config.json"
python3 - "$MODE" "$CFG" <<'PY'
import sys, json
from pathlib import Path
mode=sys.argv[1].lower()
p=Path(sys.argv[2])
c=json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
if mode in ("on","true","enable","enabled"):
    c["apply_to_live"]=True
    c["observe_only"]=False
elif mode in ("off","false","disable","disabled"):
    c["apply_to_live"]=False
    c["observe_only"]=True
elif mode not in ("status","show"):
    raise SystemExit("Usage: bash ops/v7239_soft_gate_toggle.sh on|off|status")
p.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"apply_to_live": c.get("apply_to_live"), "observe_only": c.get("observe_only"), "enforce_action": c.get("enforce_action")}, indent=2))
PY
docker restart tradingbot >/dev/null
sleep 4
SH
chmod +x ops/v7239_soft_gate_toggle.sh

cat > ops/v7239_soft_live_gate_report.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
TOKEN="${1:-eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg}"
BASE_URL="${BASE_URL:-http://127.0.0.1}"
cd /opt/tradingbot_v6000 || exit 1
mkdir -p data
TMP="$(mktemp /tmp/v7239_soft_gate.XXXXXX.json)"
trap 'rm -f "$TMP"' EXIT
curl -fsS "${BASE_URL}/soft-live-gate.json?token=${TOKEN}" > "$TMP"
python3 -m json.tool "$TMP" >/dev/null
mv "$TMP" data/v7239_soft_live_gate_report_last.json
trap - EXIT
echo "v7239 soft live gate ok $(date -u +%Y-%m-%dT%H:%M:%SZ)"
SH
chmod +x ops/v7239_soft_live_gate_report.sh

echo "== cron =="
cat > /etc/cron.d/tradingbot_v7239_soft_live_gate <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
27,57 * * * * root cd /opt/tradingbot_v6000 && /opt/tradingbot_v6000/ops/v7239_soft_live_gate_report.sh >> /opt/tradingbot_v6000/data/v7239_soft_live_gate_cron.log 2>&1
CRON
chmod 0644 /etc/cron.d/tradingbot_v7239_soft_live_gate
if command -v systemctl >/dev/null 2>&1; then systemctl restart cron >/dev/null 2>&1 || true; else service cron restart || true; fi

echo "== check script =="
CHECK_FILE="ops/v7000_check.sh"
if [ -f "$CHECK_FILE" ] && ! grep -q "V7239 SOFT LIVE GATE ROUTES" "$CHECK_FILE"; then
cat >> "$CHECK_FILE" <<'EOF'

echo ""
echo "===== V7239 SOFT LIVE GATE ROUTES ====="
TOKEN_FOR_V7239="eHwFukO31kypn0KZenWjht2T815BlQeeZNygm9nUwTg"
for U in soft-live-gate soft-live-gate.json soft-live-gate-config.json soft-live-gate-log soft-live-gate-log.json; do
  echo "/${U}?token=*** -> $(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/${U}?token=${TOKEN_FOR_V7239}")"
done
echo "soft_live_gate_cron -> $(test -f /etc/cron.d/tradingbot_v7239_soft_live_gate && echo OK || echo MISSING)"
echo "soft_live_gate_file -> $(test -f data/v7239_soft_live_gate_report_last.json && echo OK || echo MISSING)"
echo "soft_live_gate_config_apply_to_live -> $(python3 - <<'PY'
import json
print(json.load(open("data/v7239_soft_live_gate_config.json")).get("apply_to_live"))
PY
)"
for R in soft-live-gate soft-live-gate-log master markets single-header entry-bias-optimizer setup-optimizer performance-learning risk-management; do
  TMP="/tmp/v7239_${R}.html"
  CODE="$(curl -s -o "$TMP" -w "%{http_code}" "http://127.0.0.1/${R}?token=${TOKEN_FOR_V7239}")"
  HEADER="$(grep -c 'id="v7208_single_header_bar"' "$TMP" || true)"
  SG="$(grep -c '/soft-live-gate' "$TMP" || true)"
  echo "/${R}?token=*** -> ${CODE} | single_header_hits=${HEADER} | soft_gate_link_hits=${SG}"
done
EOF
chmod +x "$CHECK_FILE"
fi

echo "== syntax =="
python3 -m py_compile app/v7239_soft_live_gate_enforcement.py app/v7238_entry_timing_bias_optimizer.py app/v7208_single_header_mode.py app/main.py

echo "== docker rebuild =="
docker compose up -d --build tradingbot || docker restart tradingbot
sleep 7

TOKEN_TEST="${1:-$TOKEN_DEFAULT}"

echo "== Route Test =="
for U in soft-live-gate soft-live-gate.json soft-live-gate-config.json soft-live-gate-log soft-live-gate-log.json; do
  echo "${U}_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/${U}?token=${TOKEN_TEST}")"
done

echo ""
echo "== Header Link Test =="
for R in soft-live-gate soft-live-gate-log master markets single-header entry-bias-optimizer setup-optimizer performance-learning risk-management; do
  TMP="/tmp/v7239_${R}.html"
  CODE="$(curl -s -o "$TMP" -w "%{http_code}" "http://127.0.0.1/${R}?token=${TOKEN_TEST}")"
  HEADER="$(grep -c 'id="v7208_single_header_bar"' "$TMP" || true)"
  SG="$(grep -c '/soft-live-gate' "$TMP" || true)"
  echo "/${R}?token=*** -> ${CODE} | single_header=${HEADER} | soft_gate_link=${SG}"
done

echo ""
echo "== Snapshot =="
bash ops/v7239_soft_live_gate_report.sh "$TOKEN_TEST" || true
echo "soft_live_gate_file=$(test -f data/v7239_soft_live_gate_report_last.json && echo OK || echo MISSING)"
echo "soft_live_gate_cron=$(test -f /etc/cron.d/tradingbot_v7239_soft_live_gate && echo OK || echo MISSING)"

echo ""
echo "== Config Preview =="
cat data/v7239_soft_live_gate_config.json | python3 -m json.tool | grep -E '"enabled"|"apply_to_live"|"observe_only"|"enforce_action"|"allow_actions"|"update_signal_audit"|"notes"' || true

echo ""
echo "== Soft Gate Stats Preview =="
curl -s "http://127.0.0.1/soft-live-gate.json?token=${TOKEN_TEST}" \
  | python3 -m json.tool \
  | grep -E '"apply_to_live"|"observe_only"|"applied_blocks"|"final_allowed"|"soft_shadow_only_seen"|"action_counts"|"top_reasons"' \
  | head -n 120 || true

echo ""
echo "== Hook Check =="
grep -n "V7239 SOFT LIVE GATE HOOK" app/main.py || true
grep -n "v7239_apply_soft_gate" app/main.py || true

echo ""
echo "== Smoke Existing =="
echo "master_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/master?token=${TOKEN_TEST}")"
echo "markets_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/markets?token=${TOKEN_TEST}")"
echo "entry_bias_optimizer_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/entry-bias-optimizer?token=${TOKEN_TEST}")"
echo "setup_optimizer_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/setup-optimizer?token=${TOKEN_TEST}")"
echo "performance_learning_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/performance-learning?token=${TOKEN_TEST}")"
echo "risk_management_http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1/risk-management?token=${TOKEN_TEST}")"

echo ""
echo "== V7239 SOFT LIVE GATE ENFORCEMENT DONE =="
echo "Toggle OFF: bash ops/v7239_soft_gate_toggle.sh off"
echo "Toggle ON : bash ops/v7239_soft_gate_toggle.sh on"
echo "Status    : bash ops/v7239_soft_gate_toggle.sh status"
