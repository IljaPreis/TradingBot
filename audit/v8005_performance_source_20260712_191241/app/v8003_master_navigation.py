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

VERSION = "V8003-MASTER-NAVIGATION-CLEANUP"
MODE = "navigation_only_observe_only"

SAFETY = {
    "observe_only": True,
    "enforce": False,
    "live_rule_changes": False,
    "auto_blocking": False,
    "auto_filtering": False,
    "db_writes": False,
    "navigation_only": True,
}


def _root() -> Path:
    for p in (Path("/app"), Path("/opt/tradingbot_v6000"), Path.cwd()):
        if (p / "app").exists() and (p / "data").exists():
            return p
    return Path.cwd()


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _token_suffix(token: str) -> str:
    return f"?token={quote(str(token))}" if token else ""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _v8000_summary() -> dict[str, Any]:
    try:
        from app.v8000_dual_playbook_engine import v8000_master_payload

        data = v8000_master_payload(limit=220)
        summary = data.get("summary") if isinstance(data, dict) else None
        if isinstance(summary, dict):
            return summary
    except Exception:
        pass
    return {
        "signals": "-",
        "playbook_a_ob_reaction": "-",
        "playbook_b_session_momentum": "-",
        "review_candidates": "-",
        "a_plus_review_candidates": "-",
        "watch_only": "-",
        "blocked_or_no_trade": "-",
        "live_candidates": 0,
        "live_rule_changes": False,
    }


def _v8001_rows() -> int | str:
    root = _root()
    candidates = (
        root / "data" / "v7000_learning.sqlite3",
        Path("/opt/tradingbot_v6000/data/v7000_learning.sqlite3"),
        Path("/app/data/v7000_learning.sqlite3"),
    )
    for db in candidates:
        if not db.exists():
            continue
        con = None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            row = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v8001_pine_enriched_signals'"
            ).fetchone()
            if not row:
                return 0
            return int(con.execute("SELECT COUNT(*) FROM v8001_pine_enriched_signals").fetchone()[0])
        except Exception:
            continue
        finally:
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass
    return "-"


def _v8002_safety() -> dict[str, Any]:
    cfg = _read_json(_root() / "data" / "v8002_news_policy_config.json")
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "observe_only": bool(cfg.get("observe_only", True)),
        "enforce": bool(cfg.get("enforce", False)),
        "live_rule_changes": False,
        "high_pre_minutes": cfg.get("high_pre_minutes", 15),
        "high_post_minutes_confirmed": cfg.get("high_post_minutes_confirmed", 15),
        "high_post_minutes_unconfirmed": cfg.get("high_post_minutes_unconfirmed", 30),
        "tier1_post_minutes": cfg.get("tier1_post_minutes", 30),
        "calendar_authority": cfg.get("calendar_authority", "economic_calendar_events"),
        "actual_and_headline_authority": cfg.get("actual_and_headline_authority", "financialjuice"),
    }


def _route_paths(app) -> set[str]:
    result: set[str] = set()
    try:
        for route in app.routes:
            path = getattr(route, "path", None)
            if path:
                result.add(str(path))
    except Exception:
        pass
    return result


def _catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": "current",
            "title": "Aktuelles V8000-System",
            "subtitle": "Primäre Arbeitsseiten für V8000, V8001 und V8002.",
            "items": [
                ("V8000 Master", "/v8000/master", "Review-Kandidaten, Watch und No-Trade im Dual-Playbook-System.", "primary"),
                ("V8000 Portfolio", "/v8000/portfolio", "Gesamte deduplizierte V8000-Signalliste.", "primary"),
                ("V8000 Playbooks", "/v8000/playbooks", "Playbook A HTF-OB-Reaktion und Playbook B Session Momentum.", "primary"),
                ("V8000 A+", "/v8000/a-plus", "A+- und Review-Auswahl; Live bleibt deaktiviert.", "primary"),
                ("V8001 Pine Audit", "/v8001/pine-audit", "Playbook-, Zonen-, Bias- und Signal-ID-Audit.", "new"),
                ("V8002 News Policy", "/v8002/news-policy", "Kanonischer Kalender plus FinancialJuice-Bestätigung.", "new"),
            ],
        },
        {
            "id": "operations",
            "title": "Betrieb und Sicherheit",
            "subtitle": "Serverstatus, Positionen, Risiken und Trade-Management.",
            "items": [
                ("Health", "/health", "Grundlegender Server-Healthcheck.", "ops"),
                ("Stability", "/stability-v7243", "Gesamtcheck für DB, Pine, News, Heartbeats und Gates.", "ops"),
                ("Control Center", "/control-center", "Kompakte Betriebsübersicht ohne Regeländerung.", "ops"),
                ("Decision Suite", "/decision-suite", "Entscheidungs-, Explain- und Review-Ansicht.", "ops"),
                ("Position Overview", "/position-overview", "Offene Positionen und Risikozusammenfassung.", "ops"),
                ("Open Trade Risk", "/open-trade-risk", "Risiko offener Trades.", "ops"),
                ("Exit Readiness", "/exit-readiness", "Exit- und Absicherungsbereitschaft.", "ops"),
                ("Trade Recommendations", "/trade-management-recommendations", "Observe-only Trade-Management-Empfehlungen.", "ops"),
            ],
        },
        {
            "id": "news",
            "title": "News und Kalender",
            "subtitle": "Geplante Events, Actuals, Headline-Wirkung und News-Fenster.",
            "items": [
                ("Canonical Calendar", "/calendar", "Primäre Kalenderansicht mit geplanten Events und Actuals.", "news"),
                ("V8002 News Policy", "/v8002/news-policy", "15/15-Minuten-Fenster; 30 Minuten bei fehlendem Actual/Tier-1.", "new"),
                ("Event Risk", "/event-risk", "Aktuelles Event-Risiko nach Markt.", "news"),
                ("Pre-News Manager", "/pre-news-manager", "Vor- und Nach-Event-Beobachtung.", "news"),
                ("FinancialJuice Bridge", "/financialjuice-v7243", "FinancialJuice-Verbindung und Datenstatus.", "news"),
                ("FinancialJuice Impact", "/financialjuice-impact-v7243", "Headline-Relevanz nach Markt.", "news"),
                ("Macro Actuals", "/macro-actual-v7243", "Actual/Forecast/Previous-Zuordnung.", "news"),
                ("News Data Quality", "/v7302-news-quality", "Qualitäts- und Reparaturstatus der Newsdaten.", "news"),
            ],
        },
        {
            "id": "pine",
            "title": "Pine und Signal-Pipeline",
            "subtitle": "Pine-Auswertung, Runtime, Heartbeats, Scoring und Ranking.",
            "items": [
                ("Pine Master", "/pine-master", "Master-Pine-Evaluator und Review.", "pine"),
                ("Pine Runtime", "/pine-runtime", "Eingehende Pine-Signale und Runtime-Weiterleitung.", "pine"),
                ("Price Heartbeats", "/price-heartbeat-monitor", "Aktualität der Marktpreise.", "pine"),
                ("Signal Quality", "/signal-quality", "Signalqualität und Gate-Vorschau.", "pine"),
                ("Entry Scoring", "/entry-scoring", "News-aware Entry-Scoring.", "pine"),
                ("Ranking Snapshot", "/ranking-snapshot", "Kompaktes Kandidatenranking.", "pine"),
                ("V8001 Pine Audit", "/v8001/pine-audit", "Neue Dual-Playbook- und Zone-ID-Payloads.", "new"),
            ],
        },
        {
            "id": "learning",
            "title": "Performance und Learning",
            "subtitle": "Ergebnisse, Sessions, News, Shadow-Edge und Schwächen.",
            "items": [
                ("Performance Learning", "/performance-learning", "Zentrale Performance- und Lernübersicht.", "learn"),
                ("Setup Performance", "/setup-performance", "Performance nach Setup.", "learn"),
                ("Market/Session Performance", "/market-session-performance", "Performance nach Markt und Session.", "learn"),
                ("News Performance", "/news-performance", "Performance im News-Kontext.", "learn"),
                ("Shadow Edge", "/shadow-edge", "Auswertung der Shadow-Signale.", "learn"),
                ("Best Times", "/best-times", "Beste Handelszeiten aus den Daten.", "learn"),
                ("Weak Setups", "/weak-setups", "Schwache Setups zur Review.", "learn"),
                ("Daily Performance", "/daily-performance-report", "Tagesbericht zur Performance.", "learn"),
            ],
        },
        {
            "id": "advanced",
            "title": "Erweiterte Review-Boards",
            "subtitle": "Spezialisierte V7244-V7246-Auswertungen; nicht als primäre Startseite nötig.",
            "items": [
                ("Live Quality", "/v7244-live-quality", "Observe-only Live/Review/Shadow-Vergleich.", "advanced"),
                ("Explain Compare", "/v7244-explain", "Erklärung der Gate-Unterschiede.", "advanced"),
                ("Next Live", "/v7244d-next-live", "Zeigt fehlende Bedingungen; keine Live-Freigabe.", "advanced"),
                ("Threshold Simulator", "/v7244e-threshold-sim", "Schwellen-Simulation ohne Wirkung.", "advanced"),
                ("Recommended Preset", "/v7244f-recommended-preset", "Preset-Vorschlag, nicht automatisch aktiv.", "advanced"),
                ("MFE/MAE Excursion", "/v7245-excursion", "MFE-/MAE-Datenbasis.", "advanced"),
                ("TP Optimizer", "/v7245b-tp-optimizer", "TP/CRV-Auswertung.", "advanced"),
                ("TP Recommendations", "/v7245c-recommendations", "TP-Empfehlungen nach Datenbasis.", "advanced"),
                ("Entry Schema", "/v7246b-entry-schema", "MSS/BOS/OB/FVG/BPR-Schemaanalyse.", "advanced"),
                ("Entry Overlay", "/v7246c-entry-overlay", "Entry-Grade und TP-Bias.", "advanced"),
                ("Signal Overlay", "/v7246d-signal-overlay", "Verbindung echter Signale mit Entry-Score.", "advanced"),
                ("Entry Guard", "/v7246g-entry-guard", "Observe-only Entry-Guard-Empfehlung.", "advanced"),
            ],
        },
        {
            "id": "legacy",
            "title": "Legacy und technische Ansichten",
            "subtitle": "Bleiben erreichbar, sind aber keine primären Master-Kacheln mehr.",
            "items": [
                ("Legacy Dashboard", "/dashboard", "Alte Hauptübersicht.", "legacy"),
                ("Learning Legacy", "/learning", "Alte Learning-Ansicht.", "legacy"),
                ("Decisions Legacy", "/decisions", "Alte ALLOW/BLOCK-Karten.", "legacy"),
                ("Shadow Detail", "/shadow-detail", "Detaillierte Shadow-Trades.", "legacy"),
                ("Manual Close", "/manual", "Manuelles Schließen vorhandener Trades.", "legacy"),
                ("Master Fast", "/master-fast", "Ältere schnelle Master-Variante.", "legacy"),
                ("Master Clean", "/master-clean", "Ältere Clean-Variante.", "legacy"),
                ("Master Integration", "/master-integration", "Ältere Integrationsübersicht.", "legacy"),
            ],
        },
    ]


def _ai_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": "current-inputs",
            "title": "Aktuelle Eingänge",
            "subtitle": "Neueste V8000/V8001/V8002-Quellen für die Review-Pipeline.",
            "items": [
                ("V8000 Master", "/v8000/master", "Aktuelle Dual-Playbook-Kandidaten.", "primary"),
                ("V8001 Pine Audit", "/v8001/pine-audit", "Neue Signal-, Struktur- und Zonen-IDs.", "new"),
                ("V8002 News Policy", "/v8002/news-policy", "Observe-only Newsfenster und Headline-Kontext.", "new"),
                ("V7302 News Quality", "/v7302-news-quality", "Newsdaten-Qualität vor AI-Auswertung.", "news"),
            ],
        },
        {
            "id": "ai-overviews",
            "title": "AI-Übersichten",
            "subtitle": "V7601 ist die primäre Deep-Intelligence-Seite; V7600 bleibt Legacy-Übersicht.",
            "items": [
                ("V7400 AI Evolution", "/v7400-ai-evolution", "Observe-only Learning-Evolution.", "advanced"),
                ("V7500 AI Decision", "/v7500-ai-decision", "Observe-only Decision-Vorschläge.", "advanced"),
                ("V7600 Intelligence Legacy", "/v7600-intelligence", "Ältere Übersicht; V7601 ist primär.", "legacy"),
                ("V7601 Deep Intelligence", "/v7601-deep-intelligence", "Primäre Deep-Intelligence-Auswertung.", "primary"),
            ],
        },
        {
            "id": "weakness",
            "title": "Weakness Discovery",
            "subtitle": "Schwächen erkennen und über mehrere Quellen bestätigen.",
            "items": [
                ("V7602 Weakness Audit", "/v7602-weakness-audit", "Schwache Markt/Side/Setup-Cluster.", "ai"),
                ("V7603 Blocklist Review", "/v7603-candidate-blocklist-review", "Nur Kandidatenreview; kein Blocking.", "ai"),
                ("V7604 Session Audit", "/v7604-derived-session-audit", "Abgeleitete Session-Schwächen.", "ai"),
                ("V7605 Cross Check", "/v7605-cross-check-matrix", "Quellen-, Session- und Setup-Abgleich.", "ai"),
            ],
        },
        {
            "id": "proposal",
            "title": "Vorschläge und Ursachen",
            "subtitle": "Alle Regeln bleiben deaktiviert und benötigen manuelle Prüfung.",
            "items": [
                ("V7606 Proposed Filters", "/v7606-proposed-filter-report", "Deaktivierte Filtervorschläge.", "ai"),
                ("V7607 Trade Detail", "/v7607-top-cluster-trade-detail", "Einzeltrades hinter schwachen Clustern.", "ai"),
                ("V7608 Root Cause", "/v7608-root-cause-summary", "Zusammengefasste Ursachenanalyse.", "ai"),
                ("V7609 Confirmation Bridge", "/v7609-v7601-confirmation-bridge", "Abgleich mit V7601.", "ai"),
            ],
        },
        {
            "id": "warnings",
            "title": "Disabled Drafts und Warning Simulation",
            "subtitle": "Warnungen statt automatischer Blocks oder Live-Regeländerungen.",
            "items": [
                ("V7610 Disabled Rules", "/v7610-disabled-rule-draft-board", "Deaktivierte Regelentwürfe.", "warn"),
                ("V7611 Warning Simulation", "/v7611-warning-only-simulation-board", "Historische Warning-only-Simulation.", "warn"),
                ("V7612 Warning Dashboard", "/v7612-warning-dashboard", "Warnstufen und Trennqualität.", "warn"),
            ],
        },
    ]


def _catalog_payload(app, ai: bool = False) -> dict[str, Any]:
    paths = _route_paths(app)
    sections = _ai_catalog() if ai else _catalog()
    out_sections = []
    for section in sections:
        items = []
        for title, path, description, kind in section["items"]:
            items.append(
                {
                    "title": title,
                    "path": path,
                    "description": description,
                    "kind": kind,
                    "available": path in paths,
                }
            )
        out_sections.append({**section, "items": items})
    return {
        "ok": True,
        "version": VERSION,
        "mode": MODE,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "safety": dict(SAFETY),
        "active_routes": len(paths),
        "page": "ai_review" if ai else "master",
        "sections": out_sections,
        "v8000_summary": _v8000_summary(),
        "v8001_rows": _v8001_rows(),
        "v8002": _v8002_safety(),
    }


def _status_badge(available: bool) -> str:
    if available:
        return "<span class='status ok'>AKTIV</span>"
    return "<span class='status missing'>FEHLT</span>"


def _kind_icon(kind: str) -> str:
    return {
        "primary": "🚀",
        "new": "🆕",
        "ops": "🛡️",
        "news": "📰",
        "pine": "🌲",
        "learn": "📊",
        "advanced": "🧪",
        "legacy": "🗂️",
        "ai": "🧠",
        "warn": "⚠️",
    }.get(kind, "•")


def _page(app, token: str = "", ai: bool = False) -> str:
    data = _catalog_payload(app, ai=ai)
    suffix = _token_suffix(token)
    summary = data["v8000_summary"]
    news = data["v8002"]

    nav = (
        f"<a class='nav active' href='/master-ai-review{suffix}'>AI Review</a>"
        if ai
        else f"<a class='nav active' href='/master{suffix}'>Master</a>"
    )
    nav += (
        f"<a class='nav' href='/master{suffix}'>Master</a>"
        if ai
        else f"<a class='nav' href='/master-ai-review{suffix}'>AI Review</a>"
    )
    nav += f"<a class='nav' href='/v8000/master{suffix}'>V8000</a>"
    nav += f"<a class='nav' href='/v8001/pine-audit{suffix}'>V8001</a>"
    nav += f"<a class='nav' href='/v8002/news-policy{suffix}'>V8002</a>"

    blocks = []
    for section in data["sections"]:
        cards = []
        for item in section["items"]:
            cls = "card" if item["available"] else "card unavailable"
            href = f"{item['path']}{suffix}" if item["available"] else "#"
            cards.append(
                f"""
                <a class='{cls}' href='{_esc(href)}'>
                  <div class='icon'>{_kind_icon(item['kind'])}</div>
                  <div class='cardbody'>
                    <div class='cardtop'><h3>{_esc(item['title'])}</h3>{_status_badge(item['available'])}</div>
                    <p>{_esc(item['description'])}</p>
                    <code>{_esc(item['path'])}</code>
                  </div>
                  <div class='arrow'>›</div>
                </a>
                """
            )
        blocks.append(
            f"""
            <section id='{_esc(section['id'])}' class='section'>
              <div class='sectionhead'><div><h2>{_esc(section['title'])}</h2><p>{_esc(section['subtitle'])}</p></div></div>
              <div class='cards'>{''.join(cards)}</div>
            </section>
            """
        )

    title = "AI Review Center" if ai else "TradingBot Master"
    subtitle = (
        "Observe-only AI-Review-Pipeline · keine automatischen Blocks · keine Live-Regeländerungen"
        if ai
        else "V8000/V8001/V8002 · klare Navigation statt mehrfacher Master-Overlays"
    )
    return f"""<!doctype html>
<html lang='de'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{_esc(title)} · V8003</title>
<style>
:root{{--bg:#07111e;--panel:#101d2e;--panel2:#0b1728;--line:#273a52;--text:#eef6ff;--muted:#9caec4;--blue:#93c5fd;--green:#22c55e;--yellow:#facc15;--red:#f87171}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:Arial,sans-serif}}a{{color:inherit}}.wrap{{max-width:1500px;margin:auto;padding:14px}}
.hero{{background:linear-gradient(135deg,#101d2e,#0b1728);border:1px solid var(--line);border-radius:18px;padding:18px;margin:10px 0 14px}}.hero h1{{font-size:34px;margin:0 0 6px}}.hero p{{margin:0;color:var(--muted);line-height:1.45}}.pillrow{{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}}.pill{{padding:6px 10px;border-radius:999px;background:#0b1728;border:1px solid var(--line);font-size:12px;font-weight:800}}.safe{{color:#86efac}}.navrow{{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}}.nav{{text-decoration:none;padding:9px 13px;background:var(--panel);border:1px solid var(--line);border-radius:12px;color:var(--blue);font-weight:800}}.nav.active{{border-color:#4c8fd8;background:#10243a}}
.stats{{display:grid;grid-template-columns:repeat(8,minmax(115px,1fr));gap:9px;margin:12px 0}}.stat{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px}}.stat .label{{color:var(--muted);font-size:11px;text-transform:uppercase}}.stat .value{{font-size:25px;font-weight:900;margin-top:4px}}.good{{color:var(--green)}}.warntext{{color:var(--yellow)}}.bad{{color:var(--red)}}
.safetybox{{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:13px;margin:12px 0}}.safetyitem{{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:10px}}.safetyitem b{{display:block;margin-bottom:4px}}.safetyitem span{{color:var(--muted);font-size:12px;line-height:1.4}}
.section{{margin:18px 0}}.sectionhead h2{{margin:0;font-size:23px}}.sectionhead p{{margin:4px 0 10px;color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.card{{display:flex;align-items:center;gap:12px;text-decoration:none;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:13px;min-height:105px}}.card:hover{{border-color:#4c8fd8;background:#122238}}.card.unavailable{{opacity:.45;pointer-events:none}}.icon{{font-size:28px;width:50px;height:50px;display:flex;align-items:center;justify-content:center;background:var(--panel2);border:1px solid var(--line);border-radius:13px;flex:0 0 auto}}.cardbody{{min-width:0;flex:1}}.cardtop{{display:flex;gap:8px;align-items:center;justify-content:space-between}}.card h3{{font-size:18px;margin:0}}.card p{{color:var(--muted);margin:5px 0 7px;line-height:1.35}}code{{color:#a5d8ff;font-size:11px}}.arrow{{font-size:28px;color:#718096}}.status{{font-size:10px;padding:4px 7px;border-radius:999px;font-weight:900}}.status.ok{{background:#12301f;color:#86efac}}.status.missing{{background:#3a1717;color:#fca5a5}}
.footer{{margin:22px 0;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px}}
@media(max-width:1100px){{.stats{{grid-template-columns:repeat(4,1fr)}}.safetybox{{grid-template-columns:repeat(2,1fr)}}}}
@media(max-width:760px){{.wrap{{padding:10px}}.hero h1{{font-size:29px}}.cards{{grid-template-columns:1fr}}.stats{{grid-template-columns:repeat(2,1fr)}}.safetybox{{grid-template-columns:1fr}}.card{{min-height:96px}}}}
</style></head><body><div class='wrap'>
<div class='hero'><h1>{_esc(title)}</h1><p>{_esc(subtitle)}</p><div class='pillrow'><span class='pill safe'>OBSERVE ONLY</span><span class='pill safe'>ENFORCE FALSE</span><span class='pill safe'>LIVE RULE CHANGES FALSE</span><span class='pill'>V8003 NAVIGATION ONLY</span><span class='pill'>{_esc(data['active_routes'])} aktive Pfade</span></div></div>
<div class='navrow'>{nav}</div>
<div class='stats'>
<div class='stat'><div class='label'>Signals</div><div class='value'>{_esc(summary.get('signals','-'))}</div></div>
<div class='stat'><div class='label'>Playbook A</div><div class='value good'>{_esc(summary.get('playbook_a_ob_reaction','-'))}</div></div>
<div class='stat'><div class='label'>Playbook B</div><div class='value good'>{_esc(summary.get('playbook_b_session_momentum','-'))}</div></div>
<div class='stat'><div class='label'>Review</div><div class='value good'>{_esc(summary.get('review_candidates','-'))}</div></div>
<div class='stat'><div class='label'>A+</div><div class='value good'>{_esc(summary.get('a_plus_review_candidates','-'))}</div></div>
<div class='stat'><div class='label'>Watch</div><div class='value warntext'>{_esc(summary.get('watch_only','-'))}</div></div>
<div class='stat'><div class='label'>Blocked</div><div class='value bad'>{_esc(summary.get('blocked_or_no_trade','-'))}</div></div>
<div class='stat'><div class='label'>V8001 Rows</div><div class='value'>{_esc(data.get('v8001_rows','-'))}</div></div>
</div>
<div class='safetybox'>
<div class='safetyitem'><b>V8000</b><span>Live candidates: {_esc(summary.get('live_candidates',0))}<br>Live rule changes: {_esc(summary.get('live_rule_changes',False))}</span></div>
<div class='safetyitem'><b>V8001</b><span>Server enrichment observe-only.<br>Hauptsignale 15m / Bar Close.</span></div>
<div class='safetyitem'><b>V8002</b><span>Observe-only: {_esc(news.get('observe_only'))}<br>Enforce: {_esc(news.get('enforce'))}</span></div>
<div class='safetyitem'><b>Newsfenster</b><span>High: {_esc(news.get('high_pre_minutes'))}m vorher / {_esc(news.get('high_post_minutes_confirmed'))}m nachher<br>Ohne Actual/Tier-1: {_esc(news.get('high_post_minutes_unconfirmed'))}–{_esc(news.get('tier1_post_minutes'))}m</span></div>
</div>
{''.join(blocks)}
<div class='footer'>Version {_esc(VERSION)} · Mode {_esc(MODE)} · Generated {_esc(data['generated_utc'])} · Keine DB-Schreibvorgänge durch V8003.</div>
</div></body></html>"""


class V8003MasterNavigationMiddleware:
    """Final navigation-only renderer for /master and /master-ai-review."""

    def __init__(self, app, fastapi_app):
        self.app = app
        self.fastapi_app = fastapi_app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and str(scope.get("method") or "GET").upper() == "GET":
            path = str(scope.get("path") or "")
            if path in {"/master", "/master-ai-review"}:
                query = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="ignore"))
                token = (query.get("token") or [""])[0]
                response = HTMLResponse(_page(self.fastapi_app, token=token, ai=(path == "/master-ai-review")))
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def install_v8003_master_navigation(app) -> None:
    if getattr(app.state, "v8003_master_navigation_installed", False):
        return

    @app.get("/v8003/master-map.json")
    def v8003_master_map(request: Request, page: str = "master"):
        return JSONResponse(_catalog_payload(app, ai=(str(page).lower() in {"ai", "review", "ai-review"})))

    @app.get("/v8003/master-audit", response_class=HTMLResponse)
    def v8003_master_audit(request: Request):
        token = request.query_params.get("token", "")
        return HTMLResponse(_page(app, token=token, ai=False))

    @app.get("/v8003/ai-review", response_class=HTMLResponse)
    def v8003_ai_review(request: Request):
        token = request.query_params.get("token", "")
        return HTMLResponse(_page(app, token=token, ai=True))

    # Added last: this becomes the final canonical presentation layer while
    # existing routes and old master pages remain available for rollback.
    app.add_middleware(V8003MasterNavigationMiddleware, fastapi_app=app)
    app.state.v8003_master_navigation_installed = True
    print("[V8003] Master navigation cleanup installed (observe-only, navigation-only)")
