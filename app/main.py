from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
import os, sqlite3, asyncio, httpx, feedparser
from pathlib import Path
from datetime import datetime

load_dotenv()
BOT=os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT=os.getenv("TELEGRAM_CHAT_ID","")
MIN_ALERT=float(os.getenv("MIN_ALERT_RATING","8.2"))
MIN_PREF=float(os.getenv("MIN_PREFILTER_RATING","7.3"))
COOLDOWN=int(os.getenv("COOLDOWN_MINUTES","30"))
TOP=int(os.getenv("TOP_MARKETS_LIMIT","5"))
NEWS_ON=os.getenv("ENABLE_NEWS_ENGINE","true").lower()=="true"
NEWS_SEC=int(os.getenv("NEWS_SCAN_SECONDS","900"))
NEWS_URLS=[u.strip() for u in os.getenv("NEWS_RSS_URLS","https://www.forexlive.com/feed/news").split(",") if u.strip()]
DB="data/tradingbot.db"

WATCHLIST=["US100","US500","US30","GER40","FRA40","FTSE100","NIKKEI","ASX200","XAUUSD","UKOIL","BRENT","DXY","US10Y","VIX","EURUSD","GBPUSD","USDJPY","USDCAD","AUDUSD","NZDUSD","AUDCAD","NZDCAD","EURGBP","EURAUD","EURCAD","EURJPY","GBPJPY","CHFJPY","GBPAUD","GBPCAD","GBPNZD","NZDJPY","EURCHF"]
INDEX={"US100","US500","US30","GER40","FRA40","FTSE100","NIKKEI","ASX200"}
RISK_ASSETS=INDEX|{"AUDUSD","NZDUSD","GBPJPY","GBPAUD","GBPNZD","XAUUSD"}
CAD={"USDCAD","AUDCAD","NZDCAD","EURCAD","GBPCAD"}
USD_DIRECT={"EURUSD","GBPUSD","AUDUSD","NZDUSD"}
USD_INVERSE={"USDJPY","USDCAD"}

CTX={"dxy_bias":"neutral","us10y_bias":"neutral","oil_bias":"neutral","risk_sentiment":"neutral","vix_bias":"neutral","macro_note":""}
STATE={}
TRADES={}
LAST_SENT={}
app=FastAPI(title="TradingBot V15 Master")

class Alert(BaseModel):
    market:str; side:str; price:float|str; trigger:str
    key_level:Optional[str]=None; timeframe:Optional[str]=None; note:Optional[str]=None
    trend_state:Optional[str]=None; volume_state:Optional[str]=None; vwap_state:Optional[str]=None; structure_state:Optional[str]=None; session:Optional[str]=None
    atr:Optional[float|str]=None; dxy_bias:Optional[str]=None; us10y_bias:Optional[str]=None; oil_bias:Optional[str]=None; risk_sentiment:Optional[str]=None; vix_bias:Optional[str]=None; macro_note:Optional[str]=None
    sl:Optional[float|str]=None; tp1:Optional[float|str]=None; tp2:Optional[float|str]=None; tp3:Optional[float|str]=None

class MarketUpdate(BaseModel):
    market:str; price:float|str
    timeframe:Optional[str]=None; trend_state:Optional[str]=None; vwap_state:Optional[str]=None; volume_state:Optional[str]=None; structure_state:Optional[str]=None; session:Optional[str]=None; note:Optional[str]=None

def con():
    Path("data").mkdir(exist_ok=True)
    return sqlite3.connect(DB)

def init_db():
    with con() as c:
        c.execute("CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,market TEXT,side TEXT,rating REAL,probability REAL,confidence TEXT,price TEXT,trigger TEXT,reason TEXT,result TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS headlines(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,title TEXT UNIQUE,impact TEXT,score REAL,market TEXT,bias TEXT,source TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS webhooks(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,endpoint TEXT,status TEXT,payload TEXT)")
        c.commit()

def log(endpoint,status,payload):
    with con() as c:
        c.execute("INSERT INTO webhooks(endpoint,status,payload) VALUES(?,?,?)",(endpoint,status,str(payload)[:4000])); c.commit()

async def tg(text):
    if not BOT or not CHAT: raise RuntimeError("Telegram fehlt")
    async with httpx.AsyncClient(timeout=15) as client:
        r=await client.post(f"https://api.telegram.org/bot{BOT}/sendMessage",json={"chat_id":CHAT,"text":text,"parse_mode":"HTML","disable_web_page_preview":True})
        r.raise_for_status(); return r.json()

def headline_classify(t):
    low=t.lower(); score=2.0; impact="medium"; market="ALL"; bias="neutral"
    if any(x in low for x in ["cpi","nfp","payrolls","fomc","powell","rate decision"]): score=5; impact="high"
    elif any(x in low for x in ["ecb","boe","boj","inflation","jobs","claims","pmi","gdp","ppi","retail sales"]): score=4; impact="high"
    elif any(x in low for x in ["trump","china","tariff","iran","war","oil","yields","rates"]): score=3
    if any(x in low for x in ["risk-on","stocks rise","nasdaq rises","yields fall","dollar falls","soft inflation","rate cut","weak jobs","miss"]): bias="long"; market="US100"
    if any(x in low for x in ["risk-off","stocks fall","nasdaq falls","yields rise","dollar rises","hot inflation","rate hike","beat"]): bias="short"; market="US100"
    return impact,score,market,bias

async def scan_news():
    added=0
    for url in NEWS_URLS:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                xml=(await client.get(url)).text
            feed=feedparser.parse(xml)
            for e in feed.entries[:25]:
                title=getattr(e,"title","").strip()
                if not title: continue
                impact,score,market,bias=headline_classify(title)
                try:
                    with con() as c:
                        c.execute("INSERT INTO headlines(title,impact,score,market,bias,source) VALUES(?,?,?,?,?,?)",(title,impact,score,market,bias,url)); c.commit(); added+=1
                except sqlite3.IntegrityError: pass
        except Exception as e:
            pass
    return added

async def news_loop():
    while True:
        await scan_news()
        await asyncio.sleep(NEWS_SEC)

def headlines(limit=20):
    with con() as c:
        return c.execute("SELECT created_at,title,impact,score,market,bias FROM headlines ORDER BY id DESC LIMIT ?",(limit,)).fetchall()

def news_note():
    out=[]
    for _,title,impact,score,_,_ in headlines(12):
        if float(score)>=4: out.append(f"Headline({score}): {title}")
    return " | ".join(out[:3])

def news_bias(market,side):
    s=0; notes=[]
    for _,title,_,score,m,b in headlines(12):
        if m in [market,"ALL","US100"]:
            if b==side: s+=float(score)*0.12; notes.append("News pro: "+title)
            elif b!="neutral": s-=float(score)*0.15; notes.append("News contra: "+title)
    return max(-1.2,min(1.2,s)), notes[:2]

def macro():
    risk=50; usd=50; bonds=50; oil=50; r=[]
    if CTX["dxy_bias"]=="bearish_usd": usd-=20; risk+=10; r.append("DXY schwach")
    if CTX["dxy_bias"]=="bullish_usd": usd+=20; risk-=10; r.append("DXY stark")
    if CTX["us10y_bias"]=="yields_down": bonds+=20; risk+=15; r.append("US10Y fallen")
    if CTX["us10y_bias"]=="yields_up": bonds-=20; risk-=15; r.append("US10Y steigen")
    if CTX["risk_sentiment"]=="risk_on": risk+=20; r.append("Risk-On")
    if CTX["risk_sentiment"]=="risk_off": risk-=20; r.append("Risk-Off")
    if CTX["vix_bias"]=="vix_down": risk+=15; r.append("VIX fällt")
    if CTX["vix_bias"]=="vix_up": risk-=15; r.append("VIX steigt")
    if CTX["oil_bias"]=="bullish_oil": oil+=20; r.append("Öl stark")
    if CTX["oil_bias"]=="bearish_oil": oil-=20; r.append("Öl schwach")
    risk=max(0,min(100,risk)); regime="risk_on" if risk>=65 else "risk_off" if risk<=35 else "neutral"
    return {"risk_score":risk,"usd_score":max(0,min(100,usd)),"bonds_score":max(0,min(100,bonds)),"oil_score":max(0,min(100,oil)),"regime":regime,"reasons":r}

def perf_adjust(market,trigger,side):
    with con() as c:
        rows=c.execute("SELECT result,COUNT(*) FROM signals WHERE market=? AND trigger=? AND side=? GROUP BY result",(market,trigger,side)).fetchall()
    total=sum(x[1] for x in rows); wins=sum(x[1] for x in rows if x[0] in ["tp1","tp2","win"])
    if total<10: return 0,"zu wenig Historie"
    wr=wins/total
    if wr>=0.65: return 0.4,f"Historie positiv {round(wr*100)}%"
    if wr<=0.40: return -0.4,f"Historie schwach {round(wr*100)}%"
    return 0,f"Historie neutral {round(wr*100)}%"

def conf(r):
    return "⭐⭐⭐⭐⭐" if r>=9.2 else "⭐⭐⭐⭐" if r>=8.5 else "⭐⭐⭐" if r>=7.8 else "⭐⭐" if r>=7 else "⭐"

def rating(a:Alert):
    side=a.side.lower(); sideU=side.upper(); market=a.market.upper(); trig=a.trigger.lower()
    score=5.0; reasons=[f"Trigger: {a.trigger}"]
    if trig in ["ema20_reclaim","ema20_breakdown","ema20_pullback","ema50_reject","ema200_bounce","bos","choch","fvg","liquidity_sweep"]: score+=1.7
    if str(a.timeframe) in ["15","15m","30","60","1h"]: score+=0.8; reasons.append("stärkerer Timeframe")
    if a.session in ["London","NewYork","NY"]: score+=0.35; reasons.append("liquide Session")
    if a.volume_state=="spike": score+=0.55; reasons.append("Volume Spike")
    if a.trend_state in ["long_trend","short_trend"] and ((a.trend_state=="long_trend" and side=="long") or (a.trend_state=="short_trend" and side=="short")): score+=0.75; reasons.append("EMA20/50/200 Trend bestätigt")
    if a.vwap_state in ["above_vwap","below_vwap"] and ((a.vwap_state=="above_vwap" and side=="long") or (a.vwap_state=="below_vwap" and side=="short")): score+=0.55; reasons.append("VWAP bestätigt")
    if a.structure_state in ["bos","choch","sweep","fvg"]: score+=0.65; reasons.append("Struktur bestätigt")
    if a.sl is not None and a.tp1 is not None: score+=0.4; reasons.append("SL/TP vorhanden")
    m=macro()
    if m["regime"]=="risk_on" and side=="long" and market in RISK_ASSETS: score+=0.8; reasons.append("Makro Risk-On passt")
    if m["regime"]=="risk_off" and side=="short" and market in INDEX: score+=0.8; reasons.append("Makro Risk-Off passt")
    if a.dxy_bias=="bearish_usd" and side=="long" and (market in INDEX or market=="XAUUSD" or market in USD_DIRECT): score+=0.8; reasons.append("DXY schwach unterstützt")
    if a.us10y_bias=="yields_down" and side=="long" and market in {"US100","US500","XAUUSD"}: score+=0.9; reasons.append("US10Y fallen stützen")
    ns,nn=news_bias(market,side); score+=ns; reasons+=nn
    pa,pn=perf_adjust(market,a.trigger,sideU); score+=pa; reasons.append(pn)
    if a.macro_note: score+=0.25; reasons.append(str(a.macro_note))
    r=round(max(0,min(10,score)),1)
    return r, round(max(50,min(94,52+(r-5)*9.2)),1), conf(r), reasons, m

def top():
    rows=[]
    for m,s in STATE.items():
        try: rr=float(s.get("rating",0))
        except: rr=0
        if rr>0: rows.append({"market":m,**s})
    rows.sort(key=lambda x:float(x.get("rating",0)), reverse=True)
    return rows[:TOP]

def f(x):
    try: return float(x)
    except: return None

def open_trade(a,r,p,c):
    TRADES[a.market.upper()]={"market":a.market.upper(),"side":a.side.upper(),"entry":f(a.price),"sl":f(a.sl),"tp1":f(a.tp1),"tp2":f(a.tp2),"rating":r,"probability":p,"confidence":c,"trigger":a.trigger,"status":"OPEN","tp1_hit":False,"tp2_hit":False,"sl_hit":False,"opened":datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

def update_trade(market,price,trend=None):
    m=market.upper()
    if m not in TRADES: return []
    t=TRADES[m]; p=f(price); ev=[]
    if p is None or t["status"]!="OPEN": return []
    long=t["side"]=="LONG"
    if t["tp1"] and not t["tp1_hit"] and ((long and p>=t["tp1"]) or ((not long) and p<=t["tp1"])): t["tp1_hit"]=True; ev.append(f"🟢 {m}: TP1 erreicht. SL auf Break-Even prüfen.")
    if t["tp2"] and not t["tp2_hit"] and ((long and p>=t["tp2"]) or ((not long) and p<=t["tp2"])): t["tp2_hit"]=True; t["status"]="TP2_DONE"; ev.append(f"✅ {m}: TP2 erreicht.")
    if t["sl"] and not t["sl_hit"] and ((long and p<=t["sl"]) or ((not long) and p>=t["sl"])): t["sl_hit"]=True; t["status"]="SL_HIT"; ev.append(f"🔴 {m}: SL erreicht.")
    return ev

@app.on_event("startup")
async def startup():
    init_db()
    if NEWS_ON: asyncio.create_task(news_loop())

@app.get("/health")
async def health(): return {"status":"online","name":"TradingBot V15 Master","markets":len(WATCHLIST)}
@app.get("/context")
async def context_get(): return CTX
@app.post("/context")
async def context_post(data:dict): CTX.update({k:v for k,v in data.items() if k in CTX}); return CTX
@app.get("/macro-score")
async def macro_get(): return macro()
@app.get("/top")
async def top_get(): return {"top":top()}
@app.get("/trades")
async def trades_get(): return {"trades":TRADES}
@app.get("/performance")
async def performance():
    with con() as c: rows=c.execute("SELECT market,side,trigger,result,COUNT(*) FROM signals GROUP BY market,side,trigger,result").fetchall()
    return {"performance":rows}
@app.get("/test-telegram")
async def test_tg(): await tg("✅ TradingBot V15 Master ist online."); return {"telegram":"sent"}
@app.post("/macro-webhook")
async def macro_webhook(data:dict):
    src=str(data.get("source","")).upper(); bias=data.get("bias","neutral")
    if src in ["DXY","USD"]: CTX["dxy_bias"]=bias
    elif src in ["US10Y","YIELDS"]: CTX["us10y_bias"]=bias
    elif src in ["OIL","BRENT","WTI","UKOIL"]: CTX["oil_bias"]=bias
    elif src in ["RISK","US100","US500"]: CTX["risk_sentiment"]=bias
    elif src=="VIX": CTX["vix_bias"]=bias
    if data.get("note"): CTX["macro_note"]=(str(data.get("note"))+" | "+CTX["macro_note"])[:800]
    log("/macro-webhook","ok",data); return CTX
@app.post("/market-update")
async def market_update(data:MarketUpdate):
    m=data.market.upper()
    STATE.setdefault(m,{})
    STATE[m].update({"bias":STATE[m].get("bias","Flat"),"price":data.price,"status":"Market Update","updated":datetime.now().strftime("%H:%M:%S")})
    events=update_trade(m,data.price,data.trend_state)
    for e in events: await tg(e)
    log("/market-update","ok",data.model_dump()); return {"ok":True,"events":events}
@app.post("/webhook/tradingview")
async def webhook(req:Request):
    raw=(await req.body()).decode("utf-8","ignore")
    try: data=await req.json()
    except Exception: log("/webhook/tradingview","invalid_json",raw); return {"accepted":False,"error":"invalid json"}
    for k,v in CTX.items():
        if not data.get(k): data[k]=v
    nn=news_note()
    if nn: data["macro_note"]=((data.get("macro_note") or "")+" | "+nn).strip(" |")
    a=Alert(**data); r,p,c,reasons,m=rating(a)
    STATE[a.market.upper()]={"bias":a.side.upper(),"rating":r,"confidence":c,"trigger":a.trigger,"price":a.price,"status":"bewertet","updated":datetime.now().strftime("%H:%M:%S")}
    if r<MIN_PREF: log("/webhook/tradingview",f"prefilter_{r}",raw); return {"accepted":False,"rating":r}
    key=f"{a.market.upper()}:{a.side.upper()}"; now=int(datetime.now().timestamp()//60)
    if r<MIN_ALERT: log("/webhook/tradingview",f"low_{r}",raw); return {"accepted":False,"rating":r}
    if LAST_SENT.get(key) and now-LAST_SENT[key]<COOLDOWN: log("/webhook/tradingview","cooldown",raw); return {"accepted":False,"rating":r,"reason":"cooldown"}
    LAST_SENT[key]=now
    rank=next((i+1 for i,x in enumerate(top()) if x["market"]==a.market.upper()),None)
    reason_text="\\n".join("• "+x for x in reasons)
    await tg(f"🚨 <b>TradingBot V15 Setup</b>\\n\\n<b>Markt:</b> {a.market.upper()}\\n<b>Richtung:</b> {a.side.upper()}\\n<b>Rating:</b> {r}/10\\n<b>Wahrscheinlichkeit:</b> {p}%\\n<b>Confidence:</b> {c}\\n<b>Ranking:</b> Top-{rank}\\n\\n<b>Gründe:</b>\\n{reason_text}\\n\\n<b>Entry:</b> {a.price}\\n<b>SL:</b> {a.sl}\\n<b>TP1:</b> {a.tp1}\\n<b>TP2:</b> {a.tp2}\\n<b>Trade-Manager:</b> aktiv")
    open_trade(a,r,p,c)
    with con() as db: db.execute("INSERT INTO signals(market,side,rating,probability,confidence,price,trigger,reason,result) VALUES(?,?,?,?,?,?,?,?,?)",(a.market.upper(),a.side.upper(),r,p,c,str(a.price),a.trigger,"\\n".join(reasons),"open")); db.commit()
    log("/webhook/tradingview","sent",raw)
    return {"accepted":True,"rating":r,"probability":p,"confidence":c,"top":top()}
@app.get("/dashboard", response_class=HTMLResponse)
async def dash():
    m=macro()
    top_html="".join(f"<li><b>{x['market']} {x.get('bias')}</b> {x.get('rating')}/10 {x.get('confidence')} bei {x.get('price')} | {x.get('trigger')}</li>" for x in top()) or "<li>Noch keine Top-Setups</li>"
    tr_html="".join(f"<li><b>{t['market']} {t['side']}</b> {t['status']} | Entry {t['entry']} | SL {t['sl']} | TP1 {t['tp1']} | TP2 {t['tp2']}</li>" for t in TRADES.values()) or "<li>Keine aktiven Trades</li>"
    rows="".join(f"<tr><td>{w}</td><td>{STATE.get(w,{}).get('bias','Flat')}</td><td>{STATE.get(w,{}).get('rating','-')}</td><td>{STATE.get(w,{}).get('confidence','')}</td><td>{STATE.get(w,{}).get('price','-')}</td><td>{STATE.get(w,{}).get('status','Wartet')}</td></tr>" for w in WATCHLIST)
    news="".join(f"<li>{h[0]} | {h[2]} {h[3]} | {h[1]}</li>" for h in headlines(10))
    return f"<html><head><style>body{{font-family:Arial;background:#0f172a;color:#e5e7eb;padding:20px}}.card{{background:#111827;padding:18px;border-radius:12px;margin-bottom:20px}}table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #334155;padding:8px}}</style></head><body><h1>TradingBot V15 Master</h1><div class='card'>Status: Online<br>Märkte: {len(WATCHLIST)}</div><div class='card'><h2>Top Setups</h2><ul>{top_html}</ul></div><div class='card'><h2>Aktive Trades</h2><ul>{tr_html}</ul></div><div class='card'><h2>Macro</h2>Regime: {m['regime']} | Risk {m['risk_score']}%</div><div class='card'><h2>News</h2><ul>{news}</ul></div><div class='card'><h2>Watchlist</h2><table><tr><th>Markt</th><th>Bias</th><th>Rating</th><th>Conf</th><th>Preis</th><th>Status</th></tr>{rows}</table></div></body></html>"
