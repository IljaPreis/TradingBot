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

app=FastAPI(title="TradingBot V30 Master")

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
async def health(): return {"status":"online","name":"TradingBot V30 Master","markets":len(WATCHLIST)}
@app.get("/test-telegram")
async def test_telegram(): await tg("✅ TradingBot V30 Master ist online."); return {"telegram":"sent"}
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
    msg=f"""🚨 <b>TradingBot V30 Master</b>
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
    return f"""<html><head><title>TradingBot V30</title><style>body{{font-family:Arial;background:#0f172a;color:#e5e7eb;padding:20px}}.card{{background:#111827;padding:18px;border-radius:12px;margin-bottom:20px}}table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #334155;padding:8px;text-align:left}}th{{color:#93c5fd}}b{{color:#fff}}</style></head><body><h1>TradingBot V30 Master</h1><div class='card'><b>Status:</b> Online<br><b>Märkte:</b> {len(WATCHLIST)}</div><div class='card'><h2>Top-Setups / Priorität</h2><ul>{top_html}</ul></div><div class='card'><h2>Aktive Trades</h2><ul>{trades_html}</ul></div><div class='card'><h2>Macro Intelligence</h2><b>Regime:</b> {ma['regime']}<br><b>Risk:</b> {ma['risk_score']}%<br><b>USD:</b> {ma['usd_score']}%<br><b>Bonds:</b> {ma['bonds_score']}%<br><b>Öl:</b> {ma['oil_score']}%<br><b>Gründe:</b> {', '.join(ma['reasons'])}</div><div class='card'><h2>Wirtschaftskalender</h2><ul>{cal_html}</ul></div><div class='card'><h2>Headlines</h2><ul>{news_html}</ul></div><div class='card'><h2>Watchlist Live-State</h2><table><tr><th>Markt</th><th>Bias</th><th>Score</th><th>Conf</th><th>Trigger</th><th>Preis</th><th>Status</th><th>Update</th></tr>{rows}</table></div></body></html>"""
