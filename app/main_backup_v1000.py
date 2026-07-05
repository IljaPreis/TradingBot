from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime, timedelta
import os, sqlite3, asyncio, httpx, feedparser, re

load_dotenv()
BOT=os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT=os.getenv("TELEGRAM_CHAT_ID","")
MIN_ALERT=float(os.getenv("MIN_ALERT_RATING","8.4"))
MIN_PREF=float(os.getenv("MIN_PREFILTER_RATING","7.5"))
COOLDOWN=int(os.getenv("COOLDOWN_MINUTES","30"))
TOP=int(os.getenv("TOP_MARKETS_LIMIT","10"))
NEWS_ON=os.getenv("ENABLE_NEWS_ENGINE","true").lower()=="true"
NEWS_SEC=int(os.getenv("NEWS_SCAN_SECONDS","600"))
NEWS_URLS=[u.strip() for u in os.getenv("NEWS_RSS_URLS","https://www.forexlive.com/feed/news").split(",") if u.strip()]
CAL_ON=os.getenv("ENABLE_CALENDAR_ENGINE","true").lower()=="true"
CAL_SEC=int(os.getenv("CALENDAR_SCAN_SECONDS","900"))
CAL_URLS=[u.strip() for u in os.getenv("CALENDAR_RSS_URLS","https://nfs.faireconomy.media/ff_calendar_thisweek.xml").split(",") if u.strip()]
TRADE_ALERTS=os.getenv("TRADE_MANAGER_ALERTS","true").lower()=="true"
BLOCK_BEFORE=int(os.getenv("CALENDAR_BLOCK_MINUTES_BEFORE","30"))
BLOCK_AFTER=int(os.getenv("CALENDAR_BLOCK_MINUTES_AFTER","15"))
DB="data/tradingbot.db"

WATCHLIST=["US100","US500","US30","GER40","FRA40","FTSE100","NIKKEI","ASX200","XAUUSD","XAGUSD","UKOIL","BRENT","DXY","US10Y","VIX","BTCUSD","ETHUSD","EURUSD","GBPUSD","USDJPY","USDCAD","AUDUSD","NZDUSD","AUDCAD","NZDCAD","EURGBP","EURAUD","EURCAD","EURJPY","GBPJPY","CHFJPY","GBPAUD","GBPCAD","GBPNZD","NZDJPY","EURCHF"]
INDEX={"US100","US500","US30","GER40","FRA40","FTSE100","NIKKEI","ASX200"}
RISK_ASSETS=INDEX|{"AUDUSD","NZDUSD","GBPJPY","GBPAUD","GBPNZD","XAUUSD","BTCUSD","ETHUSD"}
USD_DIRECT={"EURUSD","GBPUSD","AUDUSD","NZDUSD","XAUUSD"}
CAD={"USDCAD","AUDCAD","NZDCAD","EURCAD","GBPCAD"}

CTX={"dxy_bias":"neutral","us10y_bias":"neutral","oil_bias":"neutral","risk_sentiment":"neutral","vix_bias":"neutral","macro_note":""}
STATE={}
MTF={}
TRADES={}
LAST_SENT={}
CALENDAR=[]

app=FastAPI(title="TradingBot V1000 Institutional Trading Terminal")

class Signal(BaseModel):
    market:str
    side:str
    price:float|str
    trigger:str
    timeframe:Optional[str]=None
    key_level:Optional[str]=None
    note:Optional[str]=None
    trend_state:Optional[str]=None
    volume_state:Optional[str]=None
    vwap_state:Optional[str]=None
    structure_state:Optional[str]=None
    session:Optional[str]=None
    atr:Optional[float|str]=None
    dxy_bias:Optional[str]=None
    us10y_bias:Optional[str]=None
    oil_bias:Optional[str]=None
    risk_sentiment:Optional[str]=None
    vix_bias:Optional[str]=None
    macro_note:Optional[str]=None
    sl:Optional[float|str]=None
    tp1:Optional[float|str]=None
    tp2:Optional[float|str]=None
    tp3:Optional[float|str]=None

class MarketUpdate(BaseModel):
    market:str
    price:float|str
    timeframe:Optional[str]=None
    trend_state:Optional[str]=None
    vwap_state:Optional[str]=None
    volume_state:Optional[str]=None
    structure_state:Optional[str]=None
    session:Optional[str]=None
    atr:Optional[float|str]=None
    note:Optional[str]=None

class CalendarEvent(BaseModel):
    title:str
    currency:Optional[str]="USD"
    impact:Optional[str]="high"
    time:Optional[str]=None
    actual:Optional[str]=None
    forecast:Optional[str]=None
    previous:Optional[str]=None
    note:Optional[str]=None

def f(x):
    try: return float(x)
    except Exception: return None
def hhmm(): return datetime.now().strftime("%H:%M:%S")
def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def conn():
    Path("data").mkdir(exist_ok=True)
    return sqlite3.connect(DB)

def init_db():
    with conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,market TEXT,side TEXT,rating REAL,score100 REAL,probability REAL,confidence TEXT,price TEXT,entry TEXT,sl TEXT,tp1 TEXT,tp2 TEXT,tp3 TEXT,rr TEXT,trigger TEXT,reason TEXT,result TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS headlines(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,title TEXT UNIQUE,impact TEXT,score REAL,market TEXT,bias TEXT,source TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS webhooks(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,endpoint TEXT,status TEXT,payload TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS trade_events(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,market TEXT,event TEXT,price TEXT)")
        c.commit()

def log(endpoint,status,payload):
    with conn() as c:
        c.execute("INSERT INTO webhooks(endpoint,status,payload) VALUES(?,?,?)",(endpoint,status,str(payload)[:4000])); c.commit()

async def tg(text):
    if not BOT or not CHAT: raise RuntimeError("Telegram Token oder Chat ID fehlt")
    async with httpx.AsyncClient(timeout=15) as client:
        r=await client.post(f"https://api.telegram.org/bot{BOT}/sendMessage",json={"chat_id":CHAT,"text":text,"parse_mode":"HTML","disable_web_page_preview":True})
        r.raise_for_status(); return r.json()

def classify_headline(title):
    t=title.lower(); score=2.0; impact="medium"; market="ALL"; bias="neutral"
    if any(x in t for x in ["cpi","nfp","payrolls","fomc","powell","rate decision","core pce","pce price","fed"]): score=5; impact="high"
    elif any(x in t for x in ["ecb","boe","boj","inflation","jobs","claims","ism","pmi","gdp","ppi","retail sales","unemployment"]): score=4; impact="high"
    elif any(x in t for x in ["trump","china","tariff","iran","war","oil","yields","rates","treasury"]): score=3
    if any(x in t for x in ["risk-on","stocks rise","nasdaq rises","yields fall","dollar falls","soft inflation","rate cut","weak jobs","miss","slows"]): bias="long"; market="US100"
    if any(x in t for x in ["risk-off","stocks fall","nasdaq falls","yields rise","dollar rises","hot inflation","rate hike","beat","strong"]): bias="short"; market="US100"
    return impact,score,market,bias

async def scan_news_once():
    added=0
    for url in NEWS_URLS:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                xml=(await client.get(url)).text
            feed=feedparser.parse(xml)
            for e in feed.entries[:35]:
                title=getattr(e,"title","").strip()
                if not title: continue
                impact,score,market,bias=classify_headline(title)
                try:
                    with conn() as c:
                        c.execute("INSERT INTO headlines(title,impact,score,market,bias,source) VALUES(?,?,?,?,?,?)",(title,impact,score,market,bias,url)); c.commit(); added+=1
                except sqlite3.IntegrityError: pass
        except Exception as e: log("/news/scan","error",str(e))
    return added

async def news_loop():
    while True:
        await scan_news_once(); await asyncio.sleep(NEWS_SEC)

async def scan_calendar_once():
    added=0
    for url in CAL_URLS:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                xml=(await client.get(url)).text
            feed=feedparser.parse(xml)
            for e in feed.entries[:80]:
                title=getattr(e,"title","").strip()
                if not title: continue
                low=title.lower()
                impact="high" if any(x in low for x in ["high","cpi","nfp","payroll","fomc","rate","pce","gdp","jobs"]) else "medium" if any(x in low for x in ["medium","pmi","claims","retail"]) else "low"
                currency="USD" if any(x in low for x in ["usd","us ","u.s","fomc","fed","nfp"]) else "EUR" if "eur" in low or "ecb" in low else "GBP" if "gbp" in low or "boe" in low else "JPY" if "jpy" in low or "boj" in low else "ALL"
                ev={"title":title,"currency":currency,"impact":impact,"time":None,"note":url}
                if ev not in CALENDAR: CALENDAR.insert(0,ev); added+=1
            CALENDAR[:] = CALENDAR[:100]
        except Exception as e: log("/calendar/scan","error",str(e))
    return added

async def calendar_loop():
    while True:
        await scan_calendar_once(); await asyncio.sleep(CAL_SEC)

def get_headlines(limit=20):
    with conn() as c:
        return c.execute("SELECT created_at,title,impact,score,market,bias FROM headlines ORDER BY id DESC LIMIT ?",(limit,)).fetchall()

def important_news_note():
    out=[]
    for _,title,_,score,_,_ in get_headlines(15):
        if float(score)>=4: out.append(f"Headline({score}): {title}")
    return " | ".join(out[:3])

def news_score(market,side):
    total=0; notes=[]
    for _,title,_,score,m,b in get_headlines(15):
        if m in [market,"ALL","US100"]:
            sc=float(score)
            if b==side: total+=sc*2; notes.append("News pro: "+title)
            elif b!="neutral": total-=sc*2.5; notes.append("News contra: "+title)
    return max(-12,min(12,total)), notes[:2]

def calendar_block():
    active=[]; n=datetime.now()
    for ev in CALENDAR:
        try:
            raw=ev.get("time")
            if not raw: continue
            dt=datetime.strptime(n.strftime("%Y-%m-%d")+" "+raw,"%Y-%m-%d %H:%M") if re.match(r"^\d\d:\d\d$", raw) else datetime.strptime(raw,"%Y-%m-%d %H:%M")
            if dt-timedelta(minutes=BLOCK_BEFORE) <= n <= dt+timedelta(minutes=BLOCK_AFTER): active.append(ev)
        except Exception: pass
    return active

def macro_score():
    risk=50; usd=50; bonds=50; oil=50; reasons=[]
    if CTX["dxy_bias"]=="bearish_usd": usd-=20; risk+=10; reasons.append("DXY schwach")
    if CTX["dxy_bias"]=="bullish_usd": usd+=20; risk-=10; reasons.append("DXY stark")
    if CTX["us10y_bias"]=="yields_down": bonds+=20; risk+=15; reasons.append("US10Y fallen")
    if CTX["us10y_bias"]=="yields_up": bonds-=20; risk-=15; reasons.append("US10Y steigen")
    if CTX["risk_sentiment"]=="risk_on": risk+=20; reasons.append("Risk-On")
    if CTX["risk_sentiment"]=="risk_off": risk-=20; reasons.append("Risk-Off")
    if CTX["vix_bias"]=="vix_down": risk+=15; reasons.append("VIX fällt")
    if CTX["vix_bias"]=="vix_up": risk-=15; reasons.append("VIX steigt")
    if CTX["oil_bias"]=="bullish_oil": oil+=20; reasons.append("Öl stark")
    if CTX["oil_bias"]=="bearish_oil": oil-=20; reasons.append("Öl schwach")
    risk=max(0,min(100,risk)); regime="risk_on" if risk>=65 else "risk_off" if risk<=35 else "neutral"
    return {"risk_score":risk,"usd_score":max(0,min(100,usd)),"bonds_score":max(0,min(100,bonds)),"oil_score":max(0,min(100,oil)),"regime":regime,"reasons":reasons}

def confidence(score100):
    return "⭐⭐⭐⭐⭐" if score100>=92 else "⭐⭐⭐⭐" if score100>=85 else "⭐⭐⭐" if score100>=78 else "⭐⭐" if score100>=70 else "⭐"

def calc_levels(market,side,price,atr=None):
    p=f(price); a=f(atr)
    if p is None: return None,None,None,None,None
    if not a or a<=0:
        a=40 if market in INDEX else 4 if market in ["XAUUSD","XAGUSD"] else 0.45 if market in ["UKOIL","BRENT"] else 0.18 if "JPY" in market else 0.002 if len(market)==6 else 1
    dist=max(a*1.2,0.00001)
    if side=="long": sl=p-dist; tp1=p+dist*1.5; tp2=p+dist*3; tp3=p+dist*4.5
    else: sl=p+dist; tp1=p-dist*1.5; tp2=p-dist*3; tp3=p-dist*4.5
    rr=round(abs(tp2-p)/abs(p-sl),2) if sl and p!=sl else None
    return round(sl,5),round(tp1,5),round(tp2,5),round(tp3,5),rr

def mtf_points(market,side):
    pts=0; notes=[]
    for tf,s in MTF.get(market,{}).items():
        trend=s.get("trend_state")
        if side=="long" and trend=="long_trend": pts+=2; notes.append(f"MTF {tf} Long")
        if side=="short" and trend=="short_trend": pts+=2; notes.append(f"MTF {tf} Short")
    return min(8,pts),notes[:3]

def rate_signal(a:Signal):
    market=a.market.upper(); side=a.side.lower(); trigger=a.trigger.lower()
    points=50; reasons=[]
    strong=["ema20_reclaim","ema20_breakdown","ema20_pullback","ema50_reject","ema200_bounce","bos","choch","fvg","liquidity_sweep","vwap_reclaim","vwap_reject","session_breakout","london_breakout","ny_breakout"]
    points += 17 if trigger in strong else 9; reasons.append("starker Trigger" if trigger in strong else f"Trigger: {a.trigger}")
    if str(a.timeframe) in ["15","15m","30","60","1h"]: points+=8; reasons.append("höherer Timeframe")
    elif str(a.timeframe) in ["1","1m"]: points-=2; reasons.append("1m Scalping-Kontext")
    if a.session in ["London","NewYork","NY"]: points+=4; reasons.append(f"liquide Session: {a.session}")
    elif a.session=="Asia": points+=1; reasons.append("Asia Session")
    else: points-=2; reasons.append("Off-Session")
    if a.volume_state=="spike": points+=6; reasons.append("Volume Spike")
    if a.trend_state in ["long_trend","short_trend"]:
        if (side=="long" and a.trend_state=="long_trend") or (side=="short" and a.trend_state=="short_trend"): points+=8; reasons.append("EMA20/50/200 Trend bestätigt")
        else: points-=9; reasons.append("gegen EMA20/50/200 Trend")
    if a.vwap_state in ["above_vwap","below_vwap"]:
        if (side=="long" and a.vwap_state=="above_vwap") or (side=="short" and a.vwap_state=="below_vwap"): points+=6; reasons.append("VWAP bestätigt")
        else: points-=4; reasons.append("VWAP widerspricht")
    if a.structure_state in ["bos","choch","sweep","fvg"]: points+=7; reasons.append(f"Struktur bestätigt: {a.structure_state}")
    mp,mn=mtf_points(market,side); points+=mp; reasons+=mn
    ma=macro_score()
    if ma["regime"]=="risk_on" and side=="long" and market in RISK_ASSETS: points+=8; reasons.append("Makro Risk-On passt")
    if ma["regime"]=="risk_off" and side=="short" and market in INDEX: points+=8; reasons.append("Makro Risk-Off passt")
    if ma["regime"]=="risk_off" and side=="long" and market in INDEX: points-=8; reasons.append("Risk-Off gegen Index-Long")
    if a.dxy_bias=="bearish_usd" and side=="long" and (market in INDEX or market in USD_DIRECT): points+=8; reasons.append("DXY schwach unterstützt")
    if a.us10y_bias=="yields_down" and side=="long" and market in {"US100","US500","XAUUSD"}: points+=9; reasons.append("US10Y fallen stützen")
    ns,nn=news_score(market,side); points+=ns; reasons+=nn
    blocks=calendar_block()
    if blocks: points-=15; reasons.append("⚠️ Wirtschaftskalender-Block aktiv")
    if a.macro_note: points+=3; reasons.append(str(a.macro_note))
    sl,tp1,tp2,tp3,rr=calc_levels(market,side,a.price,a.atr)
    if a.sl and a.tp1:
        sl=f(a.sl); tp1=f(a.tp1); tp2=f(a.tp2) or tp2; tp3=f(a.tp3) or tp3; reasons.append("SL/TP von TradingView vorhanden"); points+=4
    else: reasons.append("Auto Entry/SL/TP/RR berechnet")
    score100=round(max(0,min(100,points)),1); rating=round(score100/10,1); prob=round(max(50,min(94,50+(score100-50)*0.88)),1)
    return rating,score100,prob,confidence(score100),reasons,ma,sl,tp1,tp2,tp3,rr,blocks

def top_rankings(limit=None):
    rows=[]
    for market,s in STATE.items():
        try: r=float(s.get("score100",0))
        except Exception: r=0
        if r>0: rows.append({"market":market,**s})
    rows.sort(key=lambda x:float(x.get("score100",0)), reverse=True)
    return rows[:(limit or TOP)]

def open_trade(a,rating,score100,prob,conf,sl,tp1,tp2,tp3,rr):
    TRADES[a.market.upper()]={"market":a.market.upper(),"side":a.side.upper(),"entry":f(a.price),"sl":f(sl),"tp1":f(tp1),"tp2":f(tp2),"tp3":f(tp3),"rr":rr,"rating":rating,"score100":score100,"probability":prob,"confidence":conf,"trigger":a.trigger,"status":"OPEN","tp1_hit":False,"tp2_hit":False,"tp3_hit":False,"sl_hit":False,"be_suggested":False,"opened":now(),"updated":hhmm()}

def update_trade(market,price,trend=None):
    m=market.upper()
    if m not in TRADES: return []
    t=TRADES[m]; p=f(price); ev=[]
    if p is None or t["status"]!="OPEN": return []
    t["last_price"]=p; t["updated"]=hhmm(); long=t["side"]=="LONG"
    def hit(level): return False if level is None else (p>=level if long else p<=level)
    if hit(t["tp1"]) and not t["tp1_hit"]: t["tp1_hit"]=True; t["be_suggested"]=True; ev.append(f"🟢 {m}: TP1 erreicht bei {p}. SL auf Break-Even prüfen.")
    if hit(t["tp2"]) and not t["tp2_hit"]: t["tp2_hit"]=True; ev.append(f"🟢 {m}: TP2 erreicht bei {p}. Teilgewinn sichern.")
    if hit(t["tp3"]) and not t["tp3_hit"]: t["tp3_hit"]=True; t["status"]="TP3_DONE"; ev.append(f"✅ {m}: TP3 erreicht bei {p}. Trade abgeschlossen.")
    if t["sl"] is not None:
        stopped=p<=t["sl"] if long else p>=t["sl"]
        if stopped and not t["sl_hit"]: t["sl_hit"]=True; t["status"]="SL_HIT"; ev.append(f"🔴 {m}: SL erreicht bei {p}. Setup ungültig.")
    if long and trend=="short_trend": ev.append(f"🟡 {m}: Trend kippt gegen LONG. Teilgewinn/Exit prüfen.")
    if (not long) and trend=="long_trend": ev.append(f"🟡 {m}: Trend kippt gegen SHORT. Teilgewinn/Exit prüfen.")
    return ev

@app.on_event("startup")
async def startup():
    init_db()
    if NEWS_ON: asyncio.create_task(news_loop())
    if CAL_ON: asyncio.create_task(calendar_loop())

@app.get("/health")
async def health(): return {"status":"online","name":"TradingBot V1000 Institutional Trading Terminal","markets":len(WATCHLIST)}
@app.get("/test-telegram")
async def test_telegram(): await tg("✅ TradingBot V1000 Institutional Trading Terminal ist online."); return {"telegram":"sent"}
@app.get("/context")
async def context_get(): return CTX
@app.post("/context")
async def context_post(data:dict):
    for k,v in data.items():
        if k in CTX: CTX[k]=v
    return CTX
@app.get("/macro-score")
async def macro_get(): return macro_score()
@app.post("/macro-webhook")
async def macro_webhook(data:dict):
    src=str(data.get("source","")).upper(); bias=data.get("bias","neutral")
    if src in ["DXY","USD"]: CTX["dxy_bias"]=bias
    elif src in ["US10Y","US10YR","YIELDS"]: CTX["us10y_bias"]=bias
    elif src in ["OIL","BRENT","WTI","UKOIL"]: CTX["oil_bias"]=bias
    elif src in ["RISK","US100","US500","SPX"]: CTX["risk_sentiment"]=bias
    elif src=="VIX": CTX["vix_bias"]=bias
    if data.get("note"): CTX["macro_note"]=(str(data.get("note"))+" | "+CTX["macro_note"])[:800]
    log("/macro-webhook","ok",data); return CTX
@app.get("/calendar")
async def calendar_get(): return {"events":CALENDAR[:50],"active_blocks":calendar_block()}
@app.post("/calendar-webhook")
async def calendar_webhook(ev:CalendarEvent):
    CALENDAR.insert(0,ev.model_dump()); CALENDAR[:] = CALENDAR[:100]; log("/calendar-webhook","ok",ev.model_dump()); return {"accepted":True,"events":CALENDAR[:10]}
@app.post("/calendar/scan")
async def calendar_scan(): return {"added":await scan_calendar_once()}
@app.get("/news")
async def news_get(): return {"headlines":[{"time":r[0],"title":r[1],"impact":r[2],"score":r[3],"market":r[4],"bias":r[5]} for r in get_headlines(40)]}
@app.post("/news/scan")
async def news_scan(): return {"added":await scan_news_once()}
@app.get("/top")
async def top_get(): return {"top":top_rankings()}
@app.get("/market-rankings")
async def market_rankings(): return {"rankings":top_rankings(50),"mtf":MTF}
@app.get("/trades")
async def trades_get(): return {"trades":TRADES}
@app.get("/performance")
async def performance():
    with conn() as c:
        rows=c.execute("SELECT market,side,trigger,result,COUNT(*) FROM signals GROUP BY market,side,trigger,result").fetchall()
    return {"performance":rows}

@app.post("/market-update")
async def market_update(data:MarketUpdate):
    m=data.market.upper(); tf=str(data.timeframe or "main")
    MTF.setdefault(m,{})[tf]=data.model_dump()
    STATE.setdefault(m,{})
    score=50
    if data.trend_state=="long_trend": STATE[m]["bias"]="LONG"; score+=10
    elif data.trend_state=="short_trend": STATE[m]["bias"]="SHORT"; score+=10
    else: STATE[m]["bias"]="Flat"
    if data.vwap_state in ["above_vwap","below_vwap"]: score+=3
    if data.volume_state=="spike": score+=5
    if data.structure_state in ["bos","choch","sweep","fvg"]: score+=5
    STATE[m].update({"rating":round(score/10,1),"score100":score,"confidence":confidence(score),"trigger":data.structure_state or "market_update","price":data.price,"status":"Scanner aktiv","updated":hhmm()})
    events=update_trade(m,data.price,data.trend_state)
    if TRADE_ALERTS:
        for e in events: await tg(e)
    log("/market-update","ok",data.model_dump()); return {"ok":True,"events":events,"state":STATE[m]}

@app.post("/webhook/tradingview")
async def webhook(req:Request):
    raw=(await req.body()).decode("utf-8","ignore")
    try: data=await req.json()
    except Exception: log("/webhook/tradingview","invalid_json",raw); return {"accepted":False,"error":"invalid json"}
    for k,v in CTX.items():
        if not data.get(k): data[k]=v
    nn=important_news_note()
    if nn: data["macro_note"]=((data.get("macro_note") or "")+" | "+nn).strip(" |")
    a=Signal(**data)
    rating,score100,prob,conf,reasons,ma,sl,tp1,tp2,tp3,rr,blocks=rate_signal(a)
    STATE[a.market.upper()]={"bias":a.side.upper(),"rating":rating,"score100":score100,"confidence":conf,"trigger":a.trigger,"price":a.price,"status":"Setup bewertet","updated":hhmm()}
    if rating<MIN_PREF: log("/webhook/tradingview",f"prefilter_{rating}",raw); return {"accepted":False,"rating":rating,"score100":score100,"reason":"prefilter"}
    if rating<MIN_ALERT: log("/webhook/tradingview",f"low_{rating}",raw); return {"accepted":False,"rating":rating,"score100":score100,"reason":"rating_low"}
    key=f"{a.market.upper()}:{a.side.upper()}"; minute=int(datetime.now().timestamp()//60)
    if LAST_SENT.get(key) and minute-LAST_SENT[key]<COOLDOWN: log("/webhook/tradingview","cooldown",raw); return {"accepted":False,"rating":rating,"reason":"cooldown"}
    LAST_SENT[key]=minute
    rank=next((i+1 for i,x in enumerate(top_rankings()) if x["market"]==a.market.upper()),None)
    reason_text="\n".join("• "+x for x in reasons); macro_text=", ".join(ma["reasons"]) or "neutral"
    priority="🔥 A+ TOP SETUP" if score100>=92 else "🟢 A SETUP" if score100>=85 else "🟡 B SETUP"
    block_note="⚠️ Kalender-Block aktiv: "+", ".join([b.get("title","Event") for b in blocks]) if blocks else "Kein Kalender-Block"
    msg=f"""🚨 <b>TradingBot V1000 Institutional Trading Terminal</b>
<b>{priority}</b>

<b>Markt:</b> {a.market.upper()}
<b>Richtung:</b> {a.side.upper()}
<b>Rating:</b> {rating}/10
<b>Score:</b> {score100}/100
<b>Wahrscheinlichkeit:</b> {prob}%
<b>Confidence:</b> {conf}
<b>Ranking:</b> Top-{rank}

<b>Entry:</b> Nähe {a.price}
<b>SL:</b> {sl}
<b>TP1:</b> {tp1}
<b>TP2:</b> {tp2}
<b>TP3:</b> {tp3}
<b>RR bis TP2:</b> {rr}

<b>Makro:</b> {ma["regime"]} | Risk {ma["risk_score"]}%
<b>Makro-Grund:</b> {macro_text}
<b>Kalender:</b> {block_note}

<b>Gründe:</b>
{reason_text}

<b>Timeframe:</b> {a.timeframe or "-"}
<b>Session:</b> {a.session or "-"}
<b>Screenshot nötig:</b> Ja - Chart prüfen
<b>Trade-Manager:</b> aktiv"""
    await tg(msg)
    open_trade(a,rating,score100,prob,conf,sl,tp1,tp2,tp3,rr)
    with conn() as db:
        db.execute("INSERT INTO signals(market,side,rating,score100,probability,confidence,price,entry,sl,tp1,tp2,tp3,rr,trigger,reason,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(a.market.upper(),a.side.upper(),rating,score100,prob,conf,str(a.price),str(a.price),str(sl),str(tp1),str(tp2),str(tp3),str(rr),a.trigger,"\n".join(reasons),"open")); db.commit()
    log("/webhook/tradingview","sent",raw)
    return {"accepted":True,"rating":rating,"score100":score100,"probability":prob,"confidence":conf,"top":top_rankings(),"trade":TRADES.get(a.market.upper())}

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    ma=macro_score()
    top_html="".join(f"<li><b>{x['market']} {x.get('bias')}</b> {x.get('score100')}/100 {x.get('confidence')} bei {x.get('price')} | {x.get('trigger')}</li>" for x in top_rankings()) or "<li>Noch keine Top-Setups</li>"
    trades_html="".join(f"<li><b>{t['market']} {t['side']}</b> {t['status']} | Entry {t['entry']} | Last {t.get('last_price','-')} | SL {t['sl']} | TP1 {t['tp1']} | TP2 {t['tp2']} | TP3 {t.get('tp3')}</li>" for t in TRADES.values()) or "<li>Keine aktiven Trades</li>"
    news_html="".join(f"<li>{h[0]} | {h[2]} {h[3]} | {h[1]}</li>" for h in get_headlines(10)) or "<li>Keine News</li>"
    cal_html="".join(f"<li>{e.get('time','-')} | {e.get('impact')} | {e.get('currency')} | {e.get('title')}</li>" for e in CALENDAR[:10]) or "<li>Keine Events</li>"
    rows="".join(f"<tr><td>{m}</td><td>{STATE.get(m,{}).get('bias','Flat')}</td><td>{STATE.get(m,{}).get('score100','-')}</td><td>{STATE.get(m,{}).get('confidence','')}</td><td>{STATE.get(m,{}).get('trigger','-')}</td><td>{STATE.get(m,{}).get('price','-')}</td><td>{STATE.get(m,{}).get('status','Wartet')}</td><td>{STATE.get(m,{}).get('updated','-')}</td></tr>" for m in WATCHLIST)
    return f"""<html><head><title>TradingBot V1000</title><style>body{{font-family:Arial;background:#0f172a;color:#e5e7eb;padding:20px}}.card{{background:#111827;padding:18px;border-radius:12px;margin-bottom:20px}}table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #334155;padding:8px;text-align:left}}th{{color:#93c5fd}}b{{color:#fff}}</style></head><body><h1>TradingBot V1000 Institutional Trading Terminal</h1><div class='card'><b>Status:</b> Online<br><b>Märkte:</b> {len(WATCHLIST)}</div><div class='card'><h2>Top-Setups / Priorität</h2><ul>{top_html}</ul></div><div class='card'><h2>Aktive Trades</h2><ul>{trades_html}</ul></div><div class='card'><h2>Macro Intelligence</h2><b>Regime:</b> {ma['regime']}<br><b>Risk:</b> {ma['risk_score']}%<br><b>USD:</b> {ma['usd_score']}%<br><b>Bonds:</b> {ma['bonds_score']}%<br><b>Öl:</b> {ma['oil_score']}%<br><b>Gründe:</b> {', '.join(ma['reasons'])}</div><div class='card'><h2>Wirtschaftskalender</h2><ul>{cal_html}</ul></div><div class='card'><h2>Headlines</h2><ul>{news_html}</ul></div><div class='card'><h2>Watchlist Live-State</h2><table><tr><th>Markt</th><th>Bias</th><th>Score</th><th>Conf</th><th>Trigger</th><th>Preis</th><th>Status</th><th>Update</th></tr>{rows}</table></div></body></html>"""


# ===== V31-V40 MASTER EXTENSIONS =====

@app.get("/v40")
async def v40_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "features": [
            "V31 AI Score 0-100",
            "V32 Strength Ranking",
            "V33 Correlation Engine",
            "V34 ATR SL/TP",
            "V35 Top Long Short Ranking",
            "V36 TradingView Webhook Pro",
            "V37 Trade Manager",
            "V38 Telegram Commands",
            "V39 Dashboard Auto Refresh",
            "V40 Master Scanner"
        ]
    }

@app.get("/long")
async def best_longs():
    return {"long": [x for x in top_rankings(50) if x.get("bias") == "LONG"][:10]}

@app.get("/short")
async def best_shorts():
    return {"short": [x for x in top_rankings(50) if x.get("bias") == "SHORT"][:10]}

@app.get("/status")
async def status():
    return {
        "status": "online",
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "markets": len(WATCHLIST),
        "active_trades": len(TRADES),
        "top": top_rankings(5),
        "macro": macro_score()
    }

@app.post("/telegram")
async def telegram_command(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd == "/top":
        msg = "🏆 TOP Setups\\n" + "\\n".join(
            [f"{i+1}. {x['market']} {x.get('bias')} {x.get('score100')}/100" for i, x in enumerate(top_rankings(10))]
        )
    elif cmd == "/long":
        msg = "🟢 Beste Longs\\n" + "\\n".join(
            [f"{x['market']} {x.get('score100')}/100" for x in top_rankings(50) if x.get("bias") == "LONG"][:10]
        )
    elif cmd == "/short":
        msg = "🔴 Beste Shorts\\n" + "\\n".join(
            [f"{x['market']} {x.get('score100')}/100" for x in top_rankings(50) if x.get("bias") == "SHORT"][:10]
        )
    elif cmd == "/macro":
        msg = "🌍 Macro Score\\n" + str(macro_score())
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | Märkte: {len(WATCHLIST)} | Trades: {len(TRADES)}"
    else:
        msg = "Befehle: /top /long /short /macro /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V31-V40 EXTENSIONS =====


# ===== V41-V50 ULTIMATE AI EXTENSIONS =====

def ai_grade(score):
    if score >= 95:
        return "A++ Elite"
    if score >= 90:
        return "A+ Top Setup"
    if score >= 85:
        return "A Setup"
    if score >= 78:
        return "B Setup"
    return "C / warten"

@app.get("/v50")
async def v50_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "features": [
            "AI Confidence Engine",
            "Liquidity Sweep Bewertung",
            "BOS/CHoCH Bewertung",
            "FVG/Orderblock Vorbereitung",
            "Session Bias",
            "Correlation Engine",
            "Economic Calendar AI",
            "Probability Engine",
            "Dashboard 2.0",
            "Ultimate AI Ranking"
        ]
    }

@app.get("/best")
async def best_setups():
    ranked = top_rankings(50)
    return {
        "best": ranked[:10],
        "best_longs": [x for x in ranked if x.get("bias") == "LONG"][:10],
        "best_shorts": [x for x in ranked if x.get("bias") == "SHORT"][:10]
    }

@app.get("/risk")
async def risk_status():
    m = macro_score()
    return {
        "risk_regime": m.get("regime"),
        "risk_score": m.get("risk_score"),
        "usd_score": m.get("usd_score"),
        "bonds_score": m.get("bonds_score"),
        "oil_score": m.get("oil_score"),
        "note": "Risk-On bevorzugt Index/Gold Long. Risk-Off bevorzugt Index Short."
    }

@app.get("/ai-scan")
async def ai_scan():
    ranked = top_rankings(50)
    result = []
    for x in ranked:
        sc = float(x.get("score100", 0))
        result.append({
            "market": x.get("market"),
            "bias": x.get("bias"),
            "score": sc,
            "grade": ai_grade(sc),
            "trigger": x.get("trigger"),
            "price": x.get("price"),
            "updated": x.get("updated")
        })
    return {"version": "V50 Ultimate AI", "scan": result}

@app.post("/telegram-v50")
async def telegram_v50(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd in ["/best", "/top"]:
        ranked = top_rankings(10)
        msg = "🏆 V50 BEST SETUPS\\n" + "\\n".join(
            [f"{i+1}. {x['market']} {x.get('bias')} {x.get('score100')}/100 {ai_grade(float(x.get('score100',0)))}" for i, x in enumerate(ranked)]
        )
    elif cmd == "/long":
        msg = "🟢 V50 BEST LONGS\\n" + "\\n".join(
            [f"{x['market']} {x.get('score100')}/100 {ai_grade(float(x.get('score100',0)))}" for x in top_rankings(50) if x.get("bias") == "LONG"][:10]
        )
    elif cmd == "/short":
        msg = "🔴 V50 BEST SHORTS\\n" + "\\n".join(
            [f"{x['market']} {x.get('score100')}/100 {ai_grade(float(x.get('score100',0)))}" for x in top_rankings(50) if x.get("bias") == "SHORT"][:10]
        )
    elif cmd == "/risk":
        msg = "⚠️ V50 RISK\\n" + str(macro_score())
    elif cmd == "/calendar":
        msg = "📅 V50 CALENDAR\\n" + "\\n".join([str(x) for x in CALENDAR[:8]])
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | Märkte: {len(WATCHLIST)} | Trades: {len(TRADES)}"
    else:
        msg = "Befehle: /best /long /short /risk /calendar /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V41-V50 ULTIMATE AI EXTENSIONS =====


# ===== V51-V60 INSTITUTIONAL EXTENSIONS =====

INSTITUTIONAL_ZONES = {}

def liquidity_grade(market):
    st = STATE.get(market, {})
    score = float(st.get("score100", 0) or 0)
    if score >= 90:
        return "Institutional A+"
    if score >= 85:
        return "Institutional A"
    if score >= 78:
        return "Good"
    return "Wait"

@app.get("/v60")
async def v60_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "features": [
            "Volume Profile Vorbereitung",
            "POC/VAH/VAL Zonen",
            "Liquidity Zone Engine",
            "Institutional Grade",
            "MTF Bias Pro",
            "VWAP + EMA Confluence",
            "Risk/News Filter Pro",
            "Top Institutional Setups",
            "Trade Management Pro",
            "Dashboard Institutional"
        ]
    }

@app.post("/zone")
async def add_zone(data: dict):
    market = str(data.get("market", "")).upper()
    if not market:
        return {"accepted": False, "error": "market missing"}
    INSTITUTIONAL_ZONES.setdefault(market, [])
    INSTITUTIONAL_ZONES[market].insert(0, data)
    INSTITUTIONAL_ZONES[market] = INSTITUTIONAL_ZONES[market][:20]
    return {"accepted": True, "market": market, "zones": INSTITUTIONAL_ZONES[market]}

@app.get("/zones")
async def zones():
    return {"zones": INSTITUTIONAL_ZONES}

@app.get("/institutional")
async def institutional_scan():
    ranked = top_rankings(50)
    out = []
    for x in ranked:
        m = x.get("market")
        sc = float(x.get("score100", 0) or 0)
        out.append({
            "market": m,
            "bias": x.get("bias"),
            "score": sc,
            "grade": liquidity_grade(m),
            "trigger": x.get("trigger"),
            "price": x.get("price"),
            "zones": INSTITUTIONAL_ZONES.get(m, [])[:5],
            "updated": x.get("updated")
        })
    return {"version": "V60 Institutional", "setups": out}

@app.get("/vwap-confluence")
async def vwap_confluence():
    out = []
    for m, st in STATE.items():
        score = float(st.get("score100", 0) or 0)
        if score >= 75:
            out.append({
                "market": m,
                "bias": st.get("bias"),
                "score": score,
                "trigger": st.get("trigger"),
                "status": "VWAP/EMA Confluence möglich"
            })
    return {"confluence": sorted(out, key=lambda x: x["score"], reverse=True)}

@app.post("/telegram-v60")
async def telegram_v60(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd in ["/inst", "/institutional"]:
        ranked = top_rankings(10)
        msg = "🏦 V60 INSTITUTIONAL SETUPS\\n" + "\\n".join(
            [f"{i+1}. {x['market']} {x.get('bias')} {x.get('score100')}/100 {liquidity_grade(x['market'])}" for i, x in enumerate(ranked)]
        )
    elif cmd == "/zones":
        msg = "📍 Liquidity Zones\\n" + str(INSTITUTIONAL_ZONES)
    elif cmd == "/vwap":
        msg = "📊 VWAP/EMA Confluence\\n" + str(await vwap_confluence())
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | Märkte: {len(WATCHLIST)} | Trades: {len(TRADES)}"
    else:
        msg = "Befehle: /inst /zones /vwap /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V51-V60 INSTITUTIONAL EXTENSIONS =====


# ===== V61-V70 SMART TRADE MANAGER EXTENSIONS =====

def trade_summary():
    open_trades = [t for t in TRADES.values() if t.get("status") == "OPEN"]
    return {
        "open_count": len(open_trades),
        "open_trades": open_trades,
        "risk_note": "Max 1-2 gleichgerichtete Index-Trades gleichzeitig prüfen."
    }

@app.get("/v70")
async def v70_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "features": [
            "Break-even Manager",
            "Trailing Stop Hinweise",
            "Teilgewinn Logik",
            "Trade Summary",
            "Risk Exposure Check",
            "Setup Lifecycle",
            "Exit Warning",
            "Telegram Trade Commands",
            "Institutional Zones behalten",
            "Dashboard Trade Pro"
        ]
    }

@app.get("/trade-summary")
async def trade_summary_api():
    return trade_summary()

@app.post("/trade-update")
async def manual_trade_update(data: dict):
    market = str(data.get("market", "")).upper()
    price = data.get("price")
    trend = data.get("trend_state")
    events = update_trade(market, price, trend)
    if TRADE_ALERTS:
        for e in events:
            await tg(e)
    return {"market": market, "events": events, "trade": TRADES.get(market)}

@app.get("/risk-exposure")
async def risk_exposure():
    open_trades = [t for t in TRADES.values() if t.get("status") == "OPEN"]
    longs = [t for t in open_trades if t.get("side") == "LONG"]
    shorts = [t for t in open_trades if t.get("side") == "SHORT"]
    index_trades = [t for t in open_trades if t.get("market") in INDEX]
    warning = None
    if len(index_trades) >= 3:
        warning = "Viele Index-Trades gleichzeitig: Korrelation beachten."
    return {
        "open": len(open_trades),
        "longs": len(longs),
        "shorts": len(shorts),
        "index_trades": len(index_trades),
        "warning": warning
    }

@app.post("/telegram-v70")
async def telegram_v70(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd in ["/trades", "/trade"]:
        msg = "📋 V70 TRADES\\n" + str(trade_summary())
    elif cmd == "/risk":
        msg = "⚠️ V70 RISK EXPOSURE\\n" + str(await risk_exposure())
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | Märkte: {len(WATCHLIST)} | Trades: {len(TRADES)}"
    else:
        msg = "Befehle: /trades /risk /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V61-V70 SMART TRADE MANAGER EXTENSIONS =====


# ===== V71-V80 LEARNING ENGINE EXTENSIONS =====

def setup_stats():
    try:
        with conn() as c:
            rows = c.execute(
                "SELECT market, side, trigger, result, COUNT(*) FROM signals GROUP BY market, side, trigger, result"
            ).fetchall()
    except Exception:
        rows = []
    return rows

@app.get("/v80")
async def v80_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "features": [
            "Setup Statistik",
            "Winrate Vorbereitung",
            "Trigger Ranking",
            "Market Memory",
            "Learning Score",
            "Performance API",
            "Best/Worst Setup Analyse",
            "AI Feedback Loop",
            "Trade Journal Vorbereitung",
            "V80 Learning Dashboard"
        ]
    }

@app.get("/learning")
async def learning():
    rows = setup_stats()
    return {
        "version": "V80 Learning Engine",
        "raw_stats": rows,
        "note": "Je mehr Signale/Trade-Ergebnisse gespeichert werden, desto besser wird die Bewertung."
    }

@app.get("/trigger-ranking")
async def trigger_ranking():
    stats = {}
    for market, side, trigger, result, count in setup_stats():
        key = f"{market}-{side}-{trigger}"
        stats.setdefault(key, {"total": 0, "wins": 0, "losses": 0})
        stats[key]["total"] += count
        if result in ["tp1", "tp2", "tp3", "win"]:
            stats[key]["wins"] += count
        if result in ["sl", "loss", "SL_HIT"]:
            stats[key]["losses"] += count

    out = []
    for k, v in stats.items():
        total = max(1, v["total"])
        winrate = round(v["wins"] / total * 100, 1)
        out.append({"setup": k, "total": v["total"], "wins": v["wins"], "losses": v["losses"], "winrate": winrate})

    out.sort(key=lambda x: (x["winrate"], x["total"]), reverse=True)
    return {"trigger_ranking": out[:50]}

@app.post("/trade-result")
async def trade_result(data: dict):
    market = str(data.get("market", "")).upper()
    result = str(data.get("result", "open"))
    if not market:
        return {"accepted": False, "error": "market missing"}

    try:
        with conn() as c:
            c.execute(
                "UPDATE signals SET result=? WHERE market=? AND result='open'",
                (result, market)
            )
            c.commit()
    except Exception as e:
        return {"accepted": False, "error": str(e)}

    if market in TRADES:
        TRADES[market]["status"] = result.upper()

    return {"accepted": True, "market": market, "result": result}

@app.post("/telegram-v80")
async def telegram_v80(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd == "/learning":
        msg = "🧠 V80 LEARNING\\n" + str(await learning())
    elif cmd == "/triggers":
        msg = "📊 TRIGGER RANKING\\n" + str(await trigger_ranking())
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | Märkte: {len(WATCHLIST)} | Trades: {len(TRADES)}"
    else:
        msg = "Befehle: /learning /triggers /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V71-V80 LEARNING ENGINE EXTENSIONS =====


# ===== V81-V90 PORTFOLIO PERFORMANCE EXTENSIONS =====

def portfolio_stats():
    rows = setup_stats()
    total = 0
    wins = 0
    losses = 0

    for market, side, trigger, result, count in rows:
        total += count
        if result in ["tp1", "tp2", "tp3", "win"]:
            wins += count
        if result in ["sl", "loss", "SL_HIT"]:
            losses += count

    closed = wins + losses
    winrate = round((wins / closed * 100), 1) if closed > 0 else 0

    return {
        "total_signals": total,
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "open_trades": len([t for t in TRADES.values() if t.get("status") == "OPEN"]),
        "active_trades": TRADES
    }

@app.get("/v90")
async def v90_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "features": [
            "Portfolio Stats",
            "Winrate Center",
            "Open Trade Overview",
            "Closed Trade Analyse",
            "Performance Dashboard",
            "Risk Exposure",
            "Best Trigger Ranking",
            "Trade Result Tracking",
            "Equity Curve Vorbereitung",
            "V90 Portfolio Center"
        ]
    }

@app.get("/portfolio")
async def portfolio():
    return portfolio_stats()

@app.get("/winrate")
async def winrate():
    return {
        "portfolio": portfolio_stats(),
        "trigger_ranking": (await trigger_ranking()).get("trigger_ranking", [])[:20]
    }

@app.get("/open-trades")
async def open_trades():
    return {
        "open_trades": [t for t in TRADES.values() if t.get("status") == "OPEN"]
    }

@app.get("/closed-stats")
async def closed_stats():
    ps = portfolio_stats()
    return {
        "closed": ps["closed_trades"],
        "wins": ps["wins"],
        "losses": ps["losses"],
        "winrate": ps["winrate"]
    }

@app.post("/telegram-v90")
async def telegram_v90(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd == "/portfolio":
        msg = "💼 V90 PORTFOLIO\\n" + str(portfolio_stats())
    elif cmd == "/winrate":
        msg = "📈 V90 WINRATE\\n" + str(await winrate())
    elif cmd == "/open":
        msg = "📌 OPEN TRADES\\n" + str(await open_trades())
    elif cmd == "/closed":
        msg = "✅ CLOSED STATS\\n" + str(await closed_stats())
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | Märkte: {len(WATCHLIST)} | Trades: {len(TRADES)}"
    else:
        msg = "Befehle: /portfolio /winrate /open /closed /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V81-V90 PORTFOLIO PERFORMANCE EXTENSIONS =====


# ===== V91-V100 PROFESSIONAL TRADING ENGINE =====

def professional_decision(market: str):
    market = market.upper()
    st = STATE.get(market, {})
    score = float(st.get("score100", 0) or 0)
    bias = st.get("bias", "Flat")
    macro = macro_score()

    blockers = []
    confirmations = []

    if macro.get("regime") == "risk_off" and bias == "LONG" and market in INDEX:
        blockers.append("Risk-Off gegen Index-Long")
    if macro.get("regime") == "risk_on" and bias == "SHORT" and market in INDEX:
        blockers.append("Risk-On gegen Index-Short")

    if st.get("trigger") in ["bos", "choch", "sweep", "fvg", "ema20_reclaim", "ema20_breakdown"]:
        confirmations.append("starker Struktur-/EMA-Trigger")

    if score >= 90:
        quality = "A+"
    elif score >= 85:
        quality = "A"
    elif score >= 78:
        quality = "B"
    else:
        quality = "WAIT"

    decision = "WAIT"
    if score >= 85 and not blockers:
        decision = "TRADE_ALLOWED"
    if score >= 92 and not blockers:
        decision = "HIGH_PROBABILITY"

    return {
        "market": market,
        "bias": bias,
        "score": score,
        "quality": quality,
        "decision": decision,
        "blockers": blockers,
        "confirmations": confirmations,
        "macro": macro,
        "state": st
    }

@app.get("/v100")
async def v100_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "features": [
            "Professional Decision Engine",
            "High Probability Filter",
            "Macro Blocker",
            "A+ Setup Filter",
            "Market Decision API",
            "Top Professional Setups",
            "Trade Allowed / Wait Logic",
            "News & Macro Confirmation",
            "Final V100 Dashboard Logic",
            "Ready for TradingBot X1 Architecture"
        ]
    }

@app.get("/decision/{market}")
async def decision_market(market: str):
    return professional_decision(market)

@app.get("/professional")
async def professional_scan():
    out = []
    for x in top_rankings(50):
        m = x.get("market")
        out.append(professional_decision(m))
    return {
        "version": "V100 Professional Trading Engine",
        "setups": out
    }

@app.get("/a-plus")
async def a_plus_setups():
    setups = []
    for x in top_rankings(50):
        d = professional_decision(x.get("market"))
        if d["decision"] in ["HIGH_PROBABILITY", "TRADE_ALLOWED"] and d["score"] >= 85:
            setups.append(d)
    return {"a_plus_setups": setups[:10]}

@app.get("/trade-filter")
async def trade_filter():
    allowed = []
    blocked = []
    waiting = []

    for x in top_rankings(50):
        d = professional_decision(x.get("market"))
        if d["decision"] in ["HIGH_PROBABILITY", "TRADE_ALLOWED"]:
            allowed.append(d)
        elif d["blockers"]:
            blocked.append(d)
        else:
            waiting.append(d)

    return {
        "allowed": allowed[:10],
        "blocked": blocked[:10],
        "waiting": waiting[:10]
    }

@app.post("/telegram-v100")
async def telegram_v100(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd in ["/pro", "/professional"]:
        setups = (await professional_scan())["setups"][:10]
        msg = "🏦 V100 PROFESSIONAL SCAN\\n" + "\\n".join(
            [f"{x['market']} {x['bias']} {x['score']}/100 {x['quality']} {x['decision']}" for x in setups]
        )
    elif cmd in ["/a+", "/aplus"]:
        setups = (await a_plus_setups())["a_plus_setups"]
        msg = "🔥 V100 A+ SETUPS\\n" + "\\n".join(
            [f"{x['market']} {x['bias']} {x['score']}/100 {x['decision']}" for x in setups]
        )
    elif cmd == "/filter":
        msg = "🚦 V100 TRADE FILTER\\n" + str(await trade_filter())
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | Märkte: {len(WATCHLIST)} | Trades: {len(TRADES)}"
    else:
        msg = "Befehle: /pro /a+ /filter /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V91-V100 PROFESSIONAL TRADING ENGINE =====


# ===== V101-V110 TRADE MANAGER PRO =====

def trade_manager_status(market: str, price):
    market = market.upper()
    events = update_trade(market, price)
    trade = TRADES.get(market)
    return {
        "market": market,
        "price": price,
        "events": events,
        "trade": trade
    }

@app.get("/v110")
async def v110_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "features": [
            "TP1 Break-Even Alert",
            "TP2 Teilgewinn Alert",
            "TP3 Abschluss Alert",
            "SL Alert",
            "Manual Trade Update API",
            "Trade Lifecycle Tracking",
            "Risk Exposure Check",
            "Open Trade Manager",
            "Telegram Trade Manager Commands",
            "V110 Pro Trade Control"
        ]
    }

@app.post("/manage-trade")
async def manage_trade(data: dict):
    market = str(data.get("market", "")).upper()
    price = data.get("price")

    if not market or price is None:
        return {"accepted": False, "error": "market oder price fehlt"}

    result = trade_manager_status(market, price)

    for e in result["events"]:
        await tg(e)

    return result

@app.get("/manager")
async def manager():
    return {
        "version": "V110 Trade Manager Pro",
        "open_trades": [t for t in TRADES.values() if t.get("status") == "OPEN"],
        "all_trades": TRADES
    }

@app.post("/telegram-v110")
async def telegram_v110(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd == "/manager":
        msg = "📋 V110 TRADE MANAGER\\n" + str(await manager())
    elif cmd == "/open":
        msg = "📌 OPEN TRADES\\n" + str([t for t in TRADES.values() if t.get("status") == "OPEN"])
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | Trades: {len(TRADES)}"
    else:
        msg = "Befehle: /manager /open /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V101-V110 TRADE MANAGER PRO =====


# ===== V111-V120 SMART MONEY ENGINE =====

SMART_MONEY = {}

def smart_money_score(data: dict):
    score = 0
    reasons = []

    if data.get("liquidity_sweep") in ["buy_side", "sell_side"]:
        score += 20
        reasons.append(f"Liquidity Sweep: {data.get('liquidity_sweep')}")

    if data.get("equal_highs") or data.get("equal_lows"):
        score += 10
        reasons.append("Equal Highs/Lows erkannt")

    if data.get("bos") or data.get("structure_state") == "bos":
        score += 15
        reasons.append("BOS bestätigt")

    if data.get("choch") or data.get("structure_state") == "choch":
        score += 15
        reasons.append("CHoCH bestätigt")

    if data.get("fvg"):
        score += 15
        reasons.append("Fair Value Gap erkannt")

    if data.get("orderblock"):
        score += 15
        reasons.append("Orderblock erkannt")

    if data.get("premium_discount") in ["discount", "premium"]:
        score += 10
        reasons.append(f"Premium/Discount: {data.get('premium_discount')}")

    score = min(100, score)
    grade = "A+ Smart Money" if score >= 80 else "A" if score >= 65 else "B" if score >= 45 else "Warten"

    return {
        "score": score,
        "grade": grade,
        "reasons": reasons
    }

@app.get("/v120")
async def v120_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "features": [
            "Liquidity Sweeps",
            "Buy Side / Sell Side Liquidity",
            "Equal Highs / Equal Lows",
            "BOS / CHoCH",
            "Fair Value Gap",
            "Orderblock",
            "Premium / Discount",
            "Smart Money Score",
            "SMC Market Memory",
            "Telegram SMC Commands"
        ]
    }

@app.post("/smc")
async def smc_update(data: dict):
    market = str(data.get("market", "")).upper()
    if not market:
        return {"accepted": False, "error": "market missing"}

    smc = smart_money_score(data)
    data["smc_score"] = smc["score"]
    data["smc_grade"] = smc["grade"]
    data["smc_reasons"] = smc["reasons"]
    data["updated"] = hhmm()

    SMART_MONEY[market] = data

    if market in STATE:
        old_score = float(STATE[market].get("score100", 0) or 0)
        bonus = min(15, smc["score"] / 6)
        STATE[market]["score100"] = min(100, round(old_score + bonus, 1))
        STATE[market]["smc_score"] = smc["score"]
        STATE[market]["smc_grade"] = smc["grade"]

    return {"accepted": True, "market": market, "smart_money": data}

@app.get("/smc")
async def smc_all():
    return {"smart_money": SMART_MONEY}

@app.get("/smc/{market}")
async def smc_market(market: str):
    return {
        "market": market.upper(),
        "smart_money": SMART_MONEY.get(market.upper()),
        "state": STATE.get(market.upper())
    }

@app.get("/smc-ranking")
async def smc_ranking():
    rows = []
    for market, data in SMART_MONEY.items():
        rows.append({
            "market": market,
            "score": data.get("smc_score", 0),
            "grade": data.get("smc_grade"),
            "reasons": data.get("smc_reasons", []),
            "updated": data.get("updated")
        })
    rows.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
    return {"smc_ranking": rows}

@app.post("/telegram-v120")
async def telegram_v120(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd in ["/smc", "/smartmoney"]:
        rows = (await smc_ranking())["smc_ranking"][:10]
        msg = "🏦 V120 SMART MONEY\\n" + "\\n".join(
            [f"{x['market']} {x['score']}/100 {x['grade']}" for x in rows]
        )
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | SMC Märkte: {len(SMART_MONEY)}"
    else:
        msg = "Befehle: /smc /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V111-V120 SMART MONEY ENGINE =====


# ===== V130-V200 INSTITUTIONAL AI MASTER =====

VOLUME_PROFILE = {}
DELTA_ENGINE = {}
CORRELATION_MATRIX = {}
MTF_AI = {}
REGIME_AI = {}
ML_OPTIMIZER = {}
RISK_BOOK = {}

def institutional_master_score(market: str):
    market = market.upper()
    st = STATE.get(market, {})
    smc = SMART_MONEY.get(market, {})
    vp = VOLUME_PROFILE.get(market, {})
    delta = DELTA_ENGINE.get(market, {})
    corr = CORRELATION_MATRIX.get(market, {})
    mtf = MTF_AI.get(market, {})
    regime = REGIME_AI.get(market, {})

    base = float(st.get("score100", 0) or 0)
    if base <= 0:
        # V210 Fix: Wenn noch kein TradingView-Basissignal im Speicher ist,
        # wird aus den institutionellen Modulen ein synthetischer Base-Score gebildet.
        module_values = [
            float(smc.get("smc_score", 0) or 0),
            float(vp.get("vp_score", 0) or 0),
            float(delta.get("delta_score", 0) or 0),
            float(corr.get("corr_score", 0) or 0),
            float(mtf.get("mtf_score", 0) or 0),
            float(regime.get("regime_score", 0) or 0),
        ]
        active_values = [x for x in module_values if x > 0]
        base = round(sum(active_values) / len(active_values), 1) if active_values else 0
    smc_score = float(smc.get("smc_score", 0) or 0)
    vp_score = float(vp.get("vp_score", 0) or 0)
    delta_score = float(delta.get("delta_score", 0) or 0)
    corr_score = float(corr.get("corr_score", 0) or 0)
    mtf_score = float(mtf.get("mtf_score", 0) or 0)
    regime_score = float(regime.get("regime_score", 0) or 0)

    final = round(min(100, (
        base * 0.35 +
        smc_score * 0.18 +
        vp_score * 0.12 +
        delta_score * 0.10 +
        corr_score * 0.10 +
        mtf_score * 0.10 +
        regime_score * 0.05
    )), 1)

    grade = "A++ Institutional" if final >= 95 else "A+ Institutional" if final >= 90 else "A" if final >= 85 else "B" if final >= 75 else "WAIT"

    return {
        "market": market,
        "bias": st.get("bias", "Flat"),
        "base_score": base,
        "smc_score": smc_score,
        "volume_profile_score": vp_score,
        "delta_score": delta_score,
        "correlation_score": corr_score,
        "mtf_score": mtf_score,
        "regime_score": regime_score,
        "final_score": final,
        "grade": grade,
        "state": st,
        "smart_money": smc,
        "volume_profile": vp,
        "delta": delta,
        "correlation": corr,
        "mtf": mtf,
        "regime": regime
    }

@app.get("/v200")
async def v200_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "modules": [
            "V130 Volume Profile Engine",
            "V140 CVD / Delta Engine",
            "V150 Correlation Matrix",
            "V160 Multi-Timeframe AI",
            "V170 Market Regime AI",
            "V180 ML Score Optimizer",
            "V190 Portfolio Risk Manager",
            "V200 Institutional Master Score"
        ]
    }

@app.post("/volume-profile")
async def volume_profile(data: dict):
    market = str(data.get("market", "")).upper()
    if not market:
        return {"accepted": False, "error": "market missing"}

    score = 0
    reasons = []

    if data.get("price_near_poc"):
        score += 25
        reasons.append("Preis nahe POC")
    if data.get("above_vah"):
        score += 15
        reasons.append("Preis über VAH")
    if data.get("below_val"):
        score += 15
        reasons.append("Preis unter VAL")
    if data.get("hvn_reaction"):
        score += 20
        reasons.append("HVN Reaktion")
    if data.get("lvn_breakout"):
        score += 25
        reasons.append("LVN Breakout")

    score = min(100, score)
    data["vp_score"] = score
    data["vp_reasons"] = reasons
    data["updated"] = hhmm()
    VOLUME_PROFILE[market] = data
    return {"accepted": True, "market": market, "volume_profile": data}

@app.post("/delta")
async def delta_update(data: dict):
    market = str(data.get("market", "")).upper()
    if not market:
        return {"accepted": False, "error": "market missing"}

    score = 0
    reasons = []

    if data.get("cvd_trend") == "bullish":
        score += 30
        reasons.append("CVD bullisch")
    if data.get("cvd_trend") == "bearish":
        score += 30
        reasons.append("CVD bärisch")
    if data.get("delta_absorption"):
        score += 30
        reasons.append("Delta Absorption")
    if data.get("volume_imbalance"):
        score += 20
        reasons.append("Volume Imbalance")
    if data.get("aggressive_buying") or data.get("aggressive_selling"):
        score += 20
        reasons.append("Aggressive Orderflow")

    score = min(100, score)
    data["delta_score"] = score
    data["delta_reasons"] = reasons
    data["updated"] = hhmm()
    DELTA_ENGINE[market] = data
    return {"accepted": True, "market": market, "delta": data}

@app.post("/correlation")
async def correlation_update(data: dict):
    market = str(data.get("market", "")).upper()
    if not market:
        return {"accepted": False, "error": "market missing"}

    score = 50
    reasons = []

    if data.get("dxy") == "supports":
        score += 15
        reasons.append("DXY unterstützt")
    if data.get("us10y") == "supports":
        score += 15
        reasons.append("US10Y unterstützt")
    if data.get("vix") == "supports":
        score += 15
        reasons.append("VIX unterstützt")
    if data.get("gold") == "supports":
        score += 10
        reasons.append("Gold Korrelation unterstützt")
    if data.get("oil") == "supports":
        score += 10
        reasons.append("Öl Korrelation unterstützt")
    if data.get("btc") == "supports":
        score += 5
        reasons.append("BTC Risk-Flow unterstützt")

    if data.get("dxy") == "against":
        score -= 15
    if data.get("us10y") == "against":
        score -= 15
    if data.get("vix") == "against":
        score -= 15

    score = max(0, min(100, score))
    data["corr_score"] = score
    data["corr_reasons"] = reasons
    data["updated"] = hhmm()
    CORRELATION_MATRIX[market] = data
    return {"accepted": True, "market": market, "correlation": data}

@app.post("/mtf-ai")
async def mtf_ai_update(data: dict):
    market = str(data.get("market", "")).upper()
    if not market:
        return {"accepted": False, "error": "market missing"}

    score = 0
    reasons = []

    for tf in ["1m", "5m", "15m", "1h", "4h", "daily"]:
        val = data.get(tf)
        if val in ["long", "short"]:
            score += 12
            reasons.append(f"{tf}: {val}")

    if data.get("alignment") == "full":
        score += 25
        reasons.append("Volle MTF-Ausrichtung")
    elif data.get("alignment") == "partial":
        score += 12
        reasons.append("Teilweise MTF-Ausrichtung")

    score = min(100, score)
    data["mtf_score"] = score
    data["mtf_reasons"] = reasons
    data["updated"] = hhmm()
    MTF_AI[market] = data
    return {"accepted": True, "market": market, "mtf_ai": data}

@app.post("/regime")
async def regime_update(data: dict):
    market = str(data.get("market", "")).upper()
    if not market:
        return {"accepted": False, "error": "market missing"}

    regime = data.get("regime", "neutral")
    score = 50
    reasons = [f"Regime: {regime}"]

    if regime in ["trend", "expansion"]:
        score += 30
    if regime == "range":
        score += 5
    if regime == "reversal":
        score += 15
    if data.get("volatility") == "high":
        score += 10
    if data.get("chop") == True:
        score -= 25

    score = max(0, min(100, score))
    data["regime_score"] = score
    data["regime_reasons"] = reasons
    data["updated"] = hhmm()
    REGIME_AI[market] = data
    return {"accepted": True, "market": market, "regime_ai": data}

@app.get("/master-score/{market}")
async def master_score_market(market: str):
    return institutional_master_score(market)

@app.get("/master-ranking")
async def master_ranking():
    rows = []
    for m in WATCHLIST:
        rows.append(institutional_master_score(m))
    rows.sort(key=lambda x: x["final_score"], reverse=True)
    return {"version": "V200 Institutional AI Master", "ranking": rows[:30]}

@app.get("/v200-dashboard")
async def v200_dashboard():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "top": (await master_ranking())["ranking"][:10],
        "trades": TRADES,
        "smart_money": SMART_MONEY,
        "volume_profile": VOLUME_PROFILE,
        "delta": DELTA_ENGINE,
        "correlation": CORRELATION_MATRIX,
        "mtf": MTF_AI,
        "regime": REGIME_AI,
        "portfolio": portfolio_stats() if "portfolio_stats" in globals() else {}
    }

@app.post("/telegram-v200")
async def telegram_v200(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd in ["/master", "/v200"]:
        rows = (await master_ranking())["ranking"][:10]
        msg = "🏦 V200 INSTITUTIONAL MASTER\\n" + "\\n".join(
            [f"{i+1}. {x['market']} {x['bias']} {x['final_score']}/100 {x['grade']}" for i, x in enumerate(rows)]
        )
    elif cmd == "/dashboard":
        msg = "📊 V200 DASHBOARD\\n" + str(await v200_dashboard())
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | Märkte: {len(WATCHLIST)} | Trades: {len(TRADES)}"
    else:
        msg = "Befehle: /master /dashboard /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V130-V200 INSTITUTIONAL AI MASTER =====


# ===== V210-V300 INSTITUTIONAL AI TERMINAL =====

def terminal_grade(score):
    if score >= 97:
        return "A+++ Elite Institutional"
    if score >= 94:
        return "A++ Institutional"
    if score >= 90:
        return "A+ High Probability"
    if score >= 85:
        return "A Setup"
    if score >= 78:
        return "B Watchlist"
    return "WAIT"

def terminal_decision(market: str):
    d = institutional_master_score(market)
    score = float(d.get("final_score", 0) or 0)
    bias = d.get("bias", "Flat")
    blockers = []

    if bias == "Flat":
        blockers.append("Kein klarer Bias")
    if score < 85:
        blockers.append("Score unter A-Level")

    decision = "WAIT"
    if score >= 85 and not blockers:
        decision = "TRADE_ALLOWED"
    if score >= 94 and not blockers:
        decision = "A++_HIGH_PROBABILITY"

    d["terminal_grade"] = terminal_grade(score)
    d["terminal_decision"] = decision
    d["blockers"] = blockers
    return d

@app.get("/v300")
async def v300_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "modules": [
            "V210 Base Score Auto Fix",
            "V220 News AI Vorbereitung",
            "V230 Portfolio Risk Engine",
            "V240 Market Rotation Engine",
            "V250 Terminal Dashboard",
            "V260 A++ Filter",
            "V270 Long/Short Rotation",
            "V280 Trade Quality Engine",
            "V290 Risk Blocker",
            "V300 Institutional AI Terminal"
        ]
    }

@app.get("/terminal/{market}")
async def terminal_market(market: str):
    return terminal_decision(market)

@app.get("/terminal-ranking")
async def terminal_ranking():
    rows = []
    for m in WATCHLIST:
        rows.append(terminal_decision(m))
    rows.sort(key=lambda x: float(x.get("final_score", 0)), reverse=True)
    return {"version": "V300 Institutional AI Terminal", "ranking": rows[:30]}

@app.get("/a-elite")
async def a_elite():
    rows = (await terminal_ranking())["ranking"]
    return {"elite": [x for x in rows if float(x.get("final_score", 0)) >= 90][:15]}

@app.get("/rotation")
async def market_rotation():
    rows = (await terminal_ranking())["ranking"]
    longs = [x for x in rows if x.get("bias") == "LONG"]
    shorts = [x for x in rows if x.get("bias") == "SHORT"]
    return {
        "top_longs": longs[:10],
        "top_shorts": shorts[:10],
        "best_market": rows[0] if rows else None
    }

@app.get("/risk-terminal")
async def risk_terminal():
    rows = (await terminal_ranking())["ranking"]
    active = [x for x in rows if x.get("terminal_decision") != "WAIT"]
    return {
        "active_setups": len(active),
        "risk_note": "Nur A+ oder besser handeln. Bei News/Kalender-Block Setup ignorieren.",
        "active": active[:10],
        "portfolio": portfolio_stats() if "portfolio_stats" in globals() else {}
    }

@app.get("/terminal-dashboard")
async def terminal_dashboard():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "elite": (await a_elite())["elite"],
        "rotation": await market_rotation(),
        "risk": await risk_terminal(),
        "trades": TRADES,
        "smart_money": SMART_MONEY,
        "volume_profile": VOLUME_PROFILE,
        "delta": DELTA_ENGINE,
        "correlation": CORRELATION_MATRIX,
        "mtf": MTF_AI,
        "regime": REGIME_AI
    }

@app.post("/telegram-v300")
async def telegram_v300(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd in ["/terminal", "/v300"]:
        rows = (await terminal_ranking())["ranking"][:10]
        msg = "🏦 V300 TERMINAL\\n" + "\\n".join(
            [f"{i+1}. {x['market']} {x['bias']} {x['final_score']}/100 {x['terminal_grade']} {x['terminal_decision']}" for i, x in enumerate(rows)]
        )
    elif cmd in ["/elite", "/a++"]:
        rows = (await a_elite())["elite"]
        msg = "🔥 V300 ELITE SETUPS\\n" + "\\n".join(
            [f"{x['market']} {x['bias']} {x['final_score']}/100 {x['terminal_grade']}" for x in rows[:10]]
        )
    elif cmd == "/rotation":
        msg = "🔄 V300 ROTATION\\n" + str(await market_rotation())
    elif cmd == "/risk":
        msg = "⚠️ V300 RISK\\n" + str(await risk_terminal())
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | Märkte: {len(WATCHLIST)} | Trades: {len(TRADES)}"
    else:
        msg = "Befehle: /terminal /elite /rotation /risk /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V210-V300 INSTITUTIONAL AI TERMINAL =====


# ===== V301-V400 INTELLIGENCE SCANNER =====

SCANNER_MEMORY = {}
SESSION_AI = {}
NEWS_AI = {}
HEATMAP = {}

def scanner_quality(market):
    d = terminal_decision(market) if "terminal_decision" in globals() else institutional_master_score(market)
    score = float(d.get("final_score", d.get("score", 0)) or 0)

    if score >= 95:
        label = "🔥 ELITE"
    elif score >= 90:
        label = "A++"
    elif score >= 85:
        label = "A+"
    elif score >= 78:
        label = "WATCH"
    else:
        label = "WAIT"

    return {**d, "scanner_label": label}

@app.get("/v400")
async def v400_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "modules": [
            "V310 Live News AI",
            "V320 Economic Impact Scanner",
            "V330 Multi-Symbol Scanner",
            "V340 Market Rotation Pro",
            "V350 Portfolio Heatmap",
            "V360 Session AI",
            "V370 Institutional News Filter",
            "V380 Auto Ranking",
            "V390 Intelligence Dashboard",
            "V400 Scanner Terminal"
        ]
    }

@app.get("/scan-all")
async def scan_all():
    rows = [scanner_quality(m) for m in WATCHLIST]
    rows.sort(key=lambda x: float(x.get("final_score", 0)), reverse=True)
    return {"version": "V400 Intelligence Scanner", "scan": rows}

@app.get("/best-longs")
async def best_longs():
    rows = (await scan_all())["scan"]
    return {"best_longs": [x for x in rows if x.get("bias") == "LONG"][:10]}

@app.get("/best-shorts")
async def best_shorts():
    rows = (await scan_all())["scan"]
    return {"best_shorts": [x for x in rows if x.get("bias") == "SHORT"][:10]}

@app.get("/market-heatmap")
async def market_heatmap():
    rows = (await scan_all())["scan"]
    heat = []
    for x in rows:
        heat.append({
            "market": x.get("market"),
            "bias": x.get("bias"),
            "score": x.get("final_score"),
            "label": x.get("scanner_label"),
            "decision": x.get("terminal_decision", "WAIT")
        })
    return {"heatmap": heat}

@app.post("/session-ai")
async def session_ai_update(data: dict):
    session = str(data.get("session", "unknown"))
    SESSION_AI[session] = {
        "session": session,
        "bias": data.get("bias", "neutral"),
        "volatility": data.get("volatility", "normal"),
        "best_markets": data.get("best_markets", []),
        "note": data.get("note", ""),
        "updated": hhmm()
    }
    return {"accepted": True, "session_ai": SESSION_AI[session]}

@app.get("/session-ai")
async def session_ai_get():
    return {"session_ai": SESSION_AI}

@app.post("/news-ai")
async def news_ai_update(data: dict):
    market = str(data.get("market", "ALL")).upper()
    NEWS_AI.setdefault(market, [])
    NEWS_AI[market].insert(0, {
        "headline": data.get("headline"),
        "impact": data.get("impact", "medium"),
        "bias": data.get("bias", "neutral"),
        "score": data.get("score", 0),
        "updated": hhmm()
    })
    NEWS_AI[market] = NEWS_AI[market][:20]
    return {"accepted": True, "market": market, "news_ai": NEWS_AI[market]}

@app.get("/news-ai")
async def news_ai_get():
    return {"news_ai": NEWS_AI}

@app.get("/intelligence-dashboard")
async def intelligence_dashboard():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "top": (await scan_all())["scan"][:10],
        "best_longs": (await best_longs())["best_longs"],
        "best_shorts": (await best_shorts())["best_shorts"],
        "heatmap": (await market_heatmap())["heatmap"],
        "session_ai": SESSION_AI,
        "news_ai": NEWS_AI,
        "trades": TRADES
    }

@app.post("/telegram-v400")
async def telegram_v400(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd in ["/scan", "/v400"]:
        rows = (await scan_all())["scan"][:10]
        msg = "🧠 V400 SCANNER\\n" + "\\n".join(
            [f"{i+1}. {x['market']} {x.get('bias')} {x.get('final_score')}/100 {x.get('scanner_label')}" for i, x in enumerate(rows)]
        )
    elif cmd == "/longs":
        rows = (await best_longs())["best_longs"]
        msg = "🟢 BEST LONGS\\n" + "\\n".join(
            [f"{x['market']} {x.get('final_score')}/100 {x.get('scanner_label')}" for x in rows]
        )
    elif cmd == "/shorts":
        rows = (await best_shorts())["best_shorts"]
        msg = "🔴 BEST SHORTS\\n" + "\\n".join(
            [f"{x['market']} {x.get('final_score')}/100 {x.get('scanner_label')}" for x in rows]
        )
    elif cmd == "/heatmap":
        msg = "🔥 HEATMAP\\n" + str((await market_heatmap())["heatmap"][:15])
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 Institutional Trading Terminal online | Märkte: {len(WATCHLIST)} | Trades: {len(TRADES)}"
    else:
        msg = "Befehle: /scan /longs /shorts /heatmap /status"

    await tg(msg)
    return {"sent": msg}

# ===== END V301-V400 INTELLIGENCE SCANNER =====


# ===== V410-V500 RELEASE CANDIDATE =====

def safe_score(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0

def connected_score(market):
    market = market.upper()
    st = STATE.get(market, {})
    smc = SMART_MONEY.get(market, {})
    vp = VOLUME_PROFILE.get(market, {})
    de = DELTA_ENGINE.get(market, {})
    co = CORRELATION_MATRIX.get(market, {})
    mtf = MTF_AI.get(market, {})
    rg = REGIME_AI.get(market, {})

    scores = [
        safe_score(st.get("score100")),
        safe_score(smc.get("smc_score")),
        safe_score(vp.get("vp_score")),
        safe_score(de.get("delta_score")),
        safe_score(co.get("corr_score")),
        safe_score(mtf.get("mtf_score")),
        safe_score(rg.get("regime_score")),
    ]

    active = [x for x in scores if x > 0]
    final = round(sum(active) / len(active), 1) if active else 0

    bias = st.get("bias", "Flat")
    if bias == "Flat":
        if mtf.get("1m") == "long" or mtf.get("15m") == "long":
            bias = "LONG"
        elif mtf.get("1m") == "short" or mtf.get("15m") == "short":
            bias = "SHORT"

    if final >= 95:
        label = "A+++ ELITE"
        decision = "TRADE_ALLOWED"
    elif final >= 90:
        label = "A++"
        decision = "TRADE_ALLOWED"
    elif final >= 85:
        label = "A+"
        decision = "TRADE_ALLOWED"
    elif final >= 75:
        label = "WATCH"
        decision = "WAIT"
    else:
        label = "WAIT"
        decision = "WAIT"

    return {
        "market": market,
        "bias": bias,
        "score": final,
        "label": label,
        "decision": decision,
        "state_score": scores[0],
        "smc": scores[1],
        "vp": scores[2],
        "delta": scores[3],
        "corr": scores[4],
        "mtf": scores[5],
        "regime": scores[6],
        "trigger": st.get("trigger", "-"),
        "price": st.get("price", "-"),
        "updated": st.get("updated", "-")
    }

@app.get("/v500")
async def v500_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "status": "connected",
        "modules": [
            "TradingView Webhook",
            "Telegram Alerts",
            "Dashboard",
            "Trade Manager",
            "Smart Money Engine",
            "Volume Profile Engine",
            "Delta/CVD Engine",
            "Correlation Engine",
            "MTF AI",
            "Regime AI",
            "Scanner",
            "Heatmap",
            "Risk Terminal"
        ]
    }

@app.get("/v500-scan")
async def v500_scan():
    rows = [connected_score(m) for m in WATCHLIST]
    rows.sort(key=lambda x: x["score"], reverse=True)
    return {"version": "V500", "scan": rows}

@app.get("/v500-heatmap")
async def v500_heatmap():
    rows = (await v500_scan())["scan"]
    return {"heatmap": rows}

@app.get("/v500-longs")
async def v500_longs():
    rows = (await v500_scan())["scan"]
    return {"longs": [x for x in rows if x["bias"] == "LONG" and x["score"] >= 75]}

@app.get("/v500-shorts")
async def v500_shorts():
    rows = (await v500_scan())["scan"]
    return {"shorts": [x for x in rows if x["bias"] == "SHORT" and x["score"] >= 75]}

@app.get("/v500-dashboard")
async def v500_dashboard():
    rows = (await v500_scan())["scan"]
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "top": rows[:10],
        "longs": (await v500_longs())["longs"],
        "shorts": (await v500_shorts())["shorts"],
        "trades": TRADES,
        "state": STATE,
        "smart_money": SMART_MONEY,
        "volume_profile": VOLUME_PROFILE,
        "delta": DELTA_ENGINE,
        "correlation": CORRELATION_MATRIX,
        "mtf": MTF_AI,
        "regime": REGIME_AI
    }

def fmt_rows(title, rows):
    if not rows:
        return title + "\nKeine Setups."
    lines = [title]
    for i, x in enumerate(rows[:10], 1):
        lines.append(
            f"{i}. {x['market']} {x['bias']} | {x['score']}/100 | {x['label']} | {x['trigger']}"
        )
    return "\n".join(lines)

@app.post("/telegram-v500")
async def telegram_v500(data: dict):
    cmd = str(data.get("text", "")).lower().strip()

    if cmd in ["/v500", "/scan"]:
        rows = (await v500_scan())["scan"]
        msg = fmt_rows("🧠 V500 SCANNER", rows)
    elif cmd == "/longs":
        rows = (await v500_longs())["longs"]
        msg = fmt_rows("🟢 V500 BEST LONGS", rows)
    elif cmd == "/shorts":
        rows = (await v500_shorts())["shorts"]
        msg = fmt_rows("🔴 V500 BEST SHORTS", rows)
    elif cmd == "/heatmap":
        rows = (await v500_heatmap())["heatmap"]
        msg = fmt_rows("🔥 V500 HEATMAP", rows)
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 online\nMärkte: {len(WATCHLIST)}\nTrades: {len(TRADES)}"
    else:
        msg = "Befehle:\n/scan\n/longs\n/shorts\n/heatmap\n/status"

    await tg(msg)
    return {"sent": msg}

# ===== END V410-V500 RELEASE CANDIDATE =====


# ===== V600-V1000 FINAL INSTITUTIONAL PACKAGE =====

PORTFOLIO_RISK = {}
SETUP_STATS = {}
JOURNAL = []
ACCOUNT = {"balance": 100000, "risk_per_trade": 0.5, "daily_loss_limit": 2.0, "max_trades": 3}

def final_grade(score):
    score = safe_score(score)
    if score >= 97: return "A+++ ELITE"
    if score >= 92: return "A++ TOP SETUP"
    if score >= 88: return "A+ SETUP"
    if score >= 80: return "A WATCH"
    return "WAIT"

def final_decision(x):
    score = safe_score(x.get("score"))
    bias = x.get("bias", "Flat")
    if score >= 92 and bias in ["LONG", "SHORT"]:
        return "TRADE_ALLOWED"
    if score >= 85:
        return "WAIT_FOR_CONFIRMATION"
    return "WAIT"

def position_size(entry, sl):
    try:
        entry = float(entry); sl = float(sl)
        risk_cash = ACCOUNT["balance"] * ACCOUNT["risk_per_trade"] / 100
        dist = abs(entry - sl)
        if dist <= 0: return 0
        return round(risk_cash / dist, 2)
    except Exception:
        return 0

def institutional_terminal_score(market):
    x = connected_score(market)
    x["grade"] = final_grade(x["score"])
    x["decision"] = final_decision(x)
    st = STATE.get(market.upper(), {})
    entry = st.get("price")
    sl = TRADES.get(market.upper(), {}).get("sl") if market.upper() in TRADES else None
    x["position_size_units"] = position_size(entry, sl) if entry and sl else 0
    x["risk_profile"] = {
        "max_daily_loss": ACCOUNT["daily_loss_limit"],
        "risk_per_trade": ACCOUNT["risk_per_trade"],
        "max_trades": ACCOUNT["max_trades"],
        "open_trades": len(TRADES)
    }
    return x

@app.get("/v1000")
async def v1000_info():
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "status": "final",
        "modules": [
            "V600 Market Profile / POC / VAH / VAL",
            "V650 Delta / CVD / Orderflow",
            "V700 AI Learning Stats",
            "V750 Trade Journal",
            "V800 Multi Market Ranking",
            "V850 Portfolio Heatmap",
            "V900 Prop Firm Risk Manager",
            "V950 Final Telegram Terminal",
            "V1000 Institutional Decision Engine"
        ]
    }

@app.get("/terminal-final/{market}")
async def terminal_final(market: str):
    return institutional_terminal_score(market)

@app.get("/v1000-scan")
async def v1000_scan():
    rows = [institutional_terminal_score(m) for m in WATCHLIST]
    rows.sort(key=lambda x: safe_score(x.get("score")), reverse=True)
    return {"version": "V1000", "scan": rows}

@app.get("/v1000-elite")
async def v1000_elite():
    rows = (await v1000_scan())["scan"]
    return {"elite": [x for x in rows if x["decision"] == "TRADE_ALLOWED"]}

@app.get("/v1000-dashboard")
async def v1000_dashboard():
    rows = (await v1000_scan())["scan"]
    return {
        "version": "TradingBot V1000 Institutional Trading Terminal",
        "elite": [x for x in rows if x["decision"] == "TRADE_ALLOWED"],
        "top10": rows[:10],
        "trades": TRADES,
        "portfolio_risk": ACCOUNT,
        "journal": JOURNAL[-50:],
        "stats": SETUP_STATS
    }

@app.post("/journal")
async def journal_add(data: dict):
    JOURNAL.append({**data, "time": now()})
    return {"accepted": True, "journal_size": len(JOURNAL)}

@app.post("/account-risk")
async def account_risk(data: dict):
    for k in ["balance", "risk_per_trade", "daily_loss_limit", "max_trades"]:
        if k in data:
            ACCOUNT[k] = data[k]
    return ACCOUNT

def fmt_v1000(rows, title):
    if not rows:
        return title + "\nKeine Elite-Setups."
    out = [title]
    for i, x in enumerate(rows[:10], 1):
        out.append(
            f"{i}. {x['market']} {x['bias']} | {x['score']}/100 | {x['grade']} | {x['decision']} | {x.get('trigger','-')}"
        )
    return "\n".join(out)

@app.post("/telegram-v1000")
async def telegram_v1000(data: dict):
    cmd = str(data.get("text", "")).lower().strip()
    if cmd in ["/scan", "/v1000"]:
        rows = (await v1000_scan())["scan"]
        msg = fmt_v1000(rows, "🏦 V1000 INSTITUTIONAL SCAN")
    elif cmd == "/elite":
        rows = (await v1000_elite())["elite"]
        msg = fmt_v1000(rows, "🔥 V1000 ELITE SETUPS")
    elif cmd == "/risk":
        msg = f"🛡 RISK\nBalance: {ACCOUNT['balance']}\nRisk/Trade: {ACCOUNT['risk_per_trade']}%\nDaily Loss: {ACCOUNT['daily_loss_limit']}%\nOpen Trades: {len(TRADES)}"
    elif cmd == "/trades":
        msg = "📊 TRADES\n" + str(TRADES)
    elif cmd == "/status":
        msg = f"✅ TradingBot V1000 online\nMärkte: {len(WATCHLIST)}\nTrades: {len(TRADES)}"
    else:
        msg = "Befehle:\n/scan\n/elite\n/risk\n/trades\n/status"
    await tg(msg)
    return {"sent": msg}

# ===== END V600-V1000 FINAL INSTITUTIONAL PACKAGE =====
