import json, html
from pathlib import Path
from datetime import datetime, timezone
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


def _esc(x): return html.escape(str(x))

def _root():
    for p in [Path('/app'), Path('/opt/tradingbot_v6000'), Path.cwd()]:
        if (p / 'data').exists(): return p
    return Path.cwd()

def _data(name): return _root() / 'data' / name

def _read_json(name, default=None):
    if default is None: default = {}
    try:
        p = _data(name)
        if p.exists():
            x = json.loads(p.read_text(encoding='utf-8', errors='ignore'))
            if isinstance(x, dict): return x
    except Exception as exc:
        return {'load_error': str(exc)}
    return default

def _cfg():
    c = {'version':'V7236','enabled':True,'inject_master':True,'show_performance_summary':True,
         'show_risk_summary':True,'show_quick_links':True,'observe_only':True,
         'note':'Master integration is UI-only. It does not execute, block, close, or modify trades.'}
    c.update(_read_json('v7236_master_integration_config.json', {}))
    return c

def _token_ok(request: Request):
    try:
        from app.v7200_event_risk import _token_ok as real_token_ok
        return real_token_ok(request)
    except Exception:
        return bool(request.query_params.get('token',''))

def _safe_perf():
    try:
        from app.v7219_v7226_performance_learning_pack import _summary
        return _summary()
    except Exception as exc:
        return {'load_error':str(exc),'rows_total':0,'best_setups':[],'weak_setups':[],'best_shadow_edges':[]}

def _safe_risk():
    try:
        from app.v7227_v7235_risk_trade_management_pack import _risk_suite
        return _risk_suite()
    except Exception as exc:
        return {'load_error':str(exc),'position_overview':{},'recommendations':{},'open_trade_risk':{},'sl_tp_review':{},'cluster_exposure':{}}

def _safe_event():
    try:
        from app.v7200_event_risk import _event_data
        return _event_data()
    except Exception as exc:
        return {'risk_level':'ERROR','cooldown_active':False,'upcoming_count':0,'load_error':str(exc)}

def _safe_control():
    try:
        from app.v7207_master_compact_control_center import _status
        return _status()
    except Exception as exc:
        return {'safe_state':False,'risky_on':True,'load_error':str(exc)}

def _first_setup(rows):
    if not rows: return '-'
    r = rows[0]
    parts = []
    if r.get('market'): parts.append(str(r.get('market')))
    parts.append(str(r.get('setup_name') or r.get('setup') or '-'))
    if r.get('total_r') is not None: parts.append(str(r.get('total_r'))+'R')
    elif r.get('avg_r') is not None: parts.append('avg '+str(r.get('avg_r'))+'R')
    return ' | '.join(parts)

def _master_data():
    perf, risk, event, control = _safe_perf(), _safe_risk(), _safe_event(), _safe_control()
    po = risk.get('position_overview', {}) or {}
    rec = risk.get('recommendations', {}) or {}
    otr = risk.get('open_trade_risk', {}) or {}
    sltp = risk.get('sl_tp_review', {}) or {}
    cluster = risk.get('cluster_exposure', {}) or {}
    return {
        'version':'V7236','mode':'MASTER_INTEGRATION_SUMMARY','now_utc':datetime.now(timezone.utc).isoformat(),
        'safe_state':control.get('safe_state'),'risky_on':control.get('risky_on'),
        'event_risk':{'risk_level':event.get('risk_level'),'cooldown_active':event.get('cooldown_active'),'upcoming_count':event.get('upcoming_count')},
        'performance':{'rows_total':perf.get('rows_total',0),'best_setup':_first_setup(perf.get('best_setups',[])),
                       'weak_setup':_first_setup(perf.get('weak_setups',[])),'best_shadow_edge':_first_setup(perf.get('best_shadow_edges',[])),
                       'news_event_view':perf.get('news_event_view',{})},
        'risk':{'open_trade_count':po.get('open_trade_count',0),'total_open_r':po.get('total_open_r',0),'flag_counts':po.get('flag_counts',{}),
                'risk_counts':otr.get('risk_counts',{}),'priority_counts':rec.get('priority_counts',{}),
                'invalid_sl_tp':sltp.get('invalid_count',0),'cluster_count':cluster.get('cluster_count',0)},
        'observe_only':True
    }

def _badge(label, value, tone='neutral'):
    return f'<div class="v7236-kpi {tone}"><span>{_esc(label)}</span><b>{_esc(value)}</b></div>'

def _btn(token, href, label):
    join = '&' if '?' in href else '?'
    return f'<a class="v7236-btn" href="{_esc(href)}{join}token={_esc(token)}">{_esc(label)}</a>'

def _tile(token, href, icon, title, sub, tone='blue'):
    join = '&' if '?' in href else '?'
    return f'<a class="v7236-tile {tone}" href="{_esc(href)}{join}token={_esc(token)}"><div class="v7236-icon">{_esc(icon)}</div><div class="v7236-txt"><b>{_esc(title)}</b><span>{_esc(sub)}</span></div><div class="v7236-arr">&rsaquo;</div></a>'

def _master_panel(request: Request):
    token, d = request.query_params.get('token',''), _master_data()
    perf, risk, event = d.get('performance',{}), d.get('risk',{}), d.get('event_risk',{})
    safe_tone = 'good' if d.get('safe_state') and not d.get('risky_on') else 'warn'
    open_tone = 'good' if int(risk.get('open_trade_count') or 0) == 0 else 'warn'
    event_tone = 'good' if not event.get('cooldown_active') and str(event.get('risk_level','')).upper() in {'LOW','NONE'} else 'warn'
    invalid_tone = 'good' if int(risk.get('invalid_sl_tp') or 0) == 0 else 'danger'

    perf_card = f'''<div class="v7236-card"><div class="v7236-card-head"><span>🧠</span><h3>V7219-V7226 Performance & Learning</h3></div>
      <div class="v7236-grid-mini">{_badge('Datenbasis', perf.get('rows_total',0))}{_badge('Event Risk', event.get('risk_level','-'), event_tone)}</div>
      <div class="v7236-line"><b>Best:</b> {_esc(perf.get('best_setup','-'))}</div><div class="v7236-line"><b>Weak:</b> {_esc(perf.get('weak_setup','-'))}</div><div class="v7236-line"><b>Shadow Edge:</b> {_esc(perf.get('best_shadow_edge','-'))}</div>
      <div class="v7236-actions">{_btn(token,'/performance-learning','Performance')}{_btn(token,'/setup-performance','Setups')}{_btn(token,'/market-session-performance','Market/Session')}{_btn(token,'/shadow-edge','Shadow Edge')}{_btn(token,'/best-times','Best Times')}{_btn(token,'/weak-setups','Weak')}</div></div>'''

    risk_card = f'''<div class="v7236-card"><div class="v7236-card-head"><span>🛡️</span><h3>V7227-V7235 Risk & Trade Management</h3></div>
      <div class="v7236-grid-mini">{_badge('Open Trades', risk.get('open_trade_count',0), open_tone)}{_badge('Open R', risk.get('total_open_r',0))}{_badge('SL/TP Fehler', risk.get('invalid_sl_tp',0), invalid_tone)}{_badge('Cluster', risk.get('cluster_count',0))}</div>
      <div class="v7236-line"><b>Risk Counts:</b> {_esc(risk.get('risk_counts',{}))}</div><div class="v7236-line"><b>Prioritaet:</b> {_esc(risk.get('priority_counts',{}))}</div><div class="v7236-line"><b>Status:</b> observe-only | keine Auto-Actions</div>
      <div class="v7236-actions">{_btn(token,'/risk-management','Risk Suite')}{_btn(token,'/position-overview','Positions')}{_btn(token,'/open-trade-risk','Trade Risk')}{_btn(token,'/exit-readiness','Exit')}{_btn(token,'/sl-tp-review','SL/TP')}{_btn(token,'/cluster-exposure','Cluster')}{_btn(token,'/daily-risk-report','Daily Risk')}</div></div>'''

    quick = f'''<div class="v7236-card v7236-quick"><h3>⚡ Neue Master-Shortcuts</h3>{_tile(token,'/performance-learning','🧠','Performance Learning','Setup-, Markt-, Session-, Shadow- und Weak-Setup-Auswertung.','green')}{_tile(token,'/risk-management','🛡️','Risk Management','Offene Trades, Exit-Readiness, SL/TP, Cluster und News-Risk.','blue')}{_tile(token,'/daily-performance-report','📈','Daily Performance Report','Taeglicher Lernbericht aus V7226.','purple')}{_tile(token,'/daily-risk-report','📋','Daily Risk Report','Taeglicher Risk-Management-Bericht aus V7235.','orange')}</div>'''

    return f'''<section id="v7236_master_integration" class="v7236-shell"><style>
.v7236-shell{{margin:18px 22px 26px 22px;color:#e8eef5;font-family:Arial,sans-serif}}.v7236-wrap{{background:#0f1824;border:1px solid #263447;border-radius:18px;padding:18px}}.v7236-head{{display:flex;gap:12px;margin-bottom:14px}}.v7236-rocket{{font-size:36px}}.v7236-title h2{{margin:0;font-size:26px;line-height:1.15}}.v7236-title p{{margin:6px 0 0 0;color:#a9b8c9;font-size:15px}}
.v7236-state{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:14px 0}}.v7236-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}.v7236-card{{background:#0b1220;border:1px solid #263447;border-radius:15px;padding:14px;margin-bottom:14px;overflow:hidden}}.v7236-card-head{{display:flex;align-items:center;gap:10px;margin-bottom:10px}}.v7236-card-head span{{font-size:24px}}.v7236-card h3,.v7236-quick h3{{margin:0 0 10px 0;font-size:20px}}.v7236-grid-mini{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:10px 0}}
.v7236-kpi{{background:#111a28;border:1px solid #263447;border-radius:12px;padding:10px}}.v7236-kpi span{{display:block;color:#a9b8c9;font-size:12px;margin-bottom:5px}}.v7236-kpi b{{font-size:22px}}.v7236-kpi.good b{{color:#39d06f}}.v7236-kpi.warn b{{color:#f3c747}}.v7236-kpi.danger b{{color:#ff5c67}}.v7236-line{{padding:6px 0;color:#dce7f3;font-size:14px;border-bottom:1px solid rgba(38,52,71,.55)}}.v7236-actions{{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}}.v7236-btn{{display:inline-block;padding:8px 10px;border-radius:999px;background:#14243a;border:1px solid #2d4d73;color:#9fd0ff!important;text-decoration:none;font-size:13px}}
.v7236-tile{{display:flex;align-items:center;gap:12px;border:1px solid #263447;background:#101a28;border-radius:15px;padding:13px;margin:10px 0;text-decoration:none!important;color:#e8eef5!important}}.v7236-tile.green{{border-left:4px solid #27d46f}}.v7236-tile.blue{{border-left:4px solid #4da3ff}}.v7236-tile.purple{{border-left:4px solid #b36cff}}.v7236-tile.orange{{border-left:4px solid #ff8b3d}}.v7236-icon{{font-size:24px;width:36px;text-align:center}}.v7236-txt{{flex:1;min-width:0}}.v7236-txt b{{display:block;font-size:17px;margin-bottom:4px}}.v7236-txt span{{display:block;color:#a9b8c9;font-size:13px;line-height:1.35}}.v7236-arr{{font-size:28px;color:#718195}}
@media(max-width:760px){{.v7236-shell{{margin:14px 20px 22px 20px}}.v7236-wrap{{padding:14px;border-radius:16px}}.v7236-title h2{{font-size:23px}}.v7236-grid{{grid-template-columns:1fr}}.v7236-state{{grid-template-columns:1fr}}.v7236-grid-mini{{grid-template-columns:repeat(2,minmax(0,1fr))}}.v7236-btn{{font-size:12px;padding:8px 9px}}}}
</style><div class="v7236-wrap"><div class="v7236-head"><div class="v7236-rocket">🚀</div><div class="v7236-title"><h2>V7236 Master Integration</h2><p>Performance Learning + Risk Management direkt im Master. Handy-optimiert | observe-only.</p></div></div><div class="v7236-state">{_badge('Safe State', d.get('safe_state'), safe_tone)}{_badge('Risky Toggles', d.get('risky_on'), 'good' if not d.get('risky_on') else 'danger')}{_badge('Event Risk', event.get('risk_level','-'), event_tone)}</div><div class="v7236-grid">{perf_card}{risk_card}</div>{quick}</div></section>'''

def _inject_master_html(text, request):
    cfg = _cfg()
    if not cfg.get('enabled', True) or not cfg.get('inject_master', True) or 'id="v7236_master_integration"' in text: return text
    panel = _master_panel(request)
    idx = text.find('id="v7208_single_header_bar"')
    if idx >= 0:
        end = text.find('</div>', idx)
        if end >= 0: return text[:end+6] + panel + text[end+6:]
    low = text.lower(); bidx = low.find('<body')
    if bidx >= 0:
        bend = text.find('>', bidx)
        if bend >= 0: return text[:bend+1] + panel + text[bend+1:]
    if '</body>' in text: return text.replace('</body>', panel+'</body>')
    return panel + text

class V7236MasterIntegrationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        try:
            if request.url.path != '/master' or response.status_code != 200 or 'text/html' not in response.headers.get('content-type',''): return response
            body = b''
            async for chunk in response.body_iterator: body += chunk
            new_text = _inject_master_html(body.decode('utf-8', errors='ignore'), request)
            headers = dict(response.headers); headers.pop('content-length', None); headers.pop('content-encoding', None)
            return HTMLResponse(new_text, status_code=response.status_code, headers=headers)
        except Exception as exc:
            return HTMLResponse(f'<pre>V7236 master integration error: {_esc(exc)}</pre>', status_code=500)

def _standalone_page(request): return f'<!doctype html><html><head><meta charset="utf-8"><title>V7236 Master Integration</title></head><body style="background:#08111c;margin:0;color:#e8eef5">{_master_panel(request)}</body></html>'

def install_v7236_master_integration_pack(app):
    if getattr(app.state, 'v7236_master_integration_installed', False): return
    app.add_middleware(V7236MasterIntegrationMiddleware)
    @app.get('/master-integration', response_class=HTMLResponse)
    def master_integration_page(request: Request):
        if not _token_ok(request): return HTMLResponse('unauthorized', status_code=401)
        return HTMLResponse(_standalone_page(request))
    @app.get('/master-integration.json')
    def master_integration_json(request: Request):
        if not _token_ok(request): return JSONResponse({'error':'unauthorized'}, status_code=401)
        return JSONResponse(_master_data())
    @app.get('/master-integration-config.json')
    def master_integration_config_json(request: Request):
        if not _token_ok(request): return JSONResponse({'error':'unauthorized'}, status_code=401)
        return JSONResponse(_cfg())
    app.state.v7236_master_integration_installed = True
    print('[V7236] Master Integration Pack installed')
