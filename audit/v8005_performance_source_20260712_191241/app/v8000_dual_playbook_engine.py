from pathlib import Path
from datetime import datetime, timezone
import sqlite3, html, json

VERSION="V8000-FAST-STRICT-V2"
MODE="observe_only_fast_no_live_changes"
TOKEN_NOTE="observe only"

DBS=[Path("data/v7000_learning.sqlite3"),Path("/opt/tradingbot_v6000/data/v7000_learning.sqlite3"),Path("/app/data/v7000_learning.sqlite3")]

A_STRONG=["INTERNAL_MSS_BEAR_RECENT","BOS_BEAR_RECENT","MSS_BEAR_RECENT","MSS_BEAR","INTERNAL_MSS_BEAR"]
B_GOOD=["BOS_BEAR","BEAR_OB_RETEST","BEAR_OB_RECENT","BULL_OB_RETEST","BULL_OB_RECENT","MSS_BULL_RECENT"]
WEAK=["BPR","BULL_FVG","FVG_RETEST","FVG_RECENT","BOS_BULL","INTERNAL_MSS_BULL_RECENT","REVERSAL_LONG_MSS_RECLAIM"]

def now(): return datetime.now(timezone.utc).isoformat()
def esc(x): return html.escape(str(x if x is not None else ""))
def norm(x): return str(x or "").upper().strip()
def fnum(x):
    try:
        if x is None or str(x).strip()=="": return None
        return float(x)
    except Exception:
        return None

def pick_db():
    for p in DBS:
        if p.exists(): return p
    return None

def q(x): return '"' + str(x).replace('"','""') + '"'

def tables(con):
    return [r[0] for r in con.execute("select name from sqlite_master where type='table'").fetchall()]

def cols(con,t):
    return [r[1] for r in con.execute(f"pragma table_info({q(t)})").fetchall()]

def col(cs,*names):
    low={c.lower():c for c in cs}
    for n in names:
        if n.lower() in low: return low[n.lower()]
    return None

def family(setup):
    t=norm(setup)
    if "BPR" in t: return "BPR"
    if "FVG" in t: return "FVG"
    if "_OB" in t or "BEAR_OB" in t or "BULL_OB" in t or "ORDER_BLOCK" in t: return "OB"
    if "MSS" in t: return "MSS"
    if "BOS" in t: return "BOS"
    return "OTHER"

def side(setup,direction):
    t=norm(setup)+" "+norm(direction)
    if "SHORT" in t or "SELL" in t or "BEAR" in t: return "SHORT"
    if "LONG" in t or "BUY" in t or "BULL" in t: return "LONG"
    return "UNKNOWN"

def load_signals(limit=80):
    db=pick_db()
    if not db: return [],"DB_NOT_FOUND"
    con=sqlite3.connect(str(db))
    ts=tables(con)
    table=None
    for cand in ["v72437_live_permission_log","signal_audit","v7242_pine_runtime_log","v7241_master_pine_eval_log"]:
        if cand in ts:
            table=cand
            break
    if not table: return [],"NO_SIGNAL_TABLE"

    cs=cols(con,table)
    cid=col(cs,"id")
    ct=col(cs,"created_at","timestamp","ts","received_at","generated_utc","updated_at")
    cm=col(cs,"market","symbol","ticker")
    csu=col(cs,"setup_name","setup","trigger","pattern","strategy")
    cd=col(cs,"direction","side","signal_side","trade_direction")
    cc=col(cs,"confidence","final_confidence","conf","score","technical_score","setup_quality")
    ca=col(cs,"new_action","proposed_state","action","decision","state","status")
    cr=col(cs,"reason","block_reason","message","note")

    use=[x for x in [cid,ct,cm,csu,cd,cc,ca,cr] if x]
    order=cid or ct or use[0]
    sql=f"select {','.join(q(x) for x in use)} from {q(table)} order by {q(order)} desc limit ?"

    out=[]
    for row in con.execute(sql,(limit,)).fetchall():
        d=dict(zip(use,row))
        setup=d.get(csu) if csu else ""
        dire=d.get(cd) if cd else ""
        out.append({
            "time":d.get(ct) if ct else "",
            "source_table":table,
            "market":norm(d.get(cm) if cm else ""),
            "direction":side(setup,dire),
            "setup_name":setup or "UNKNOWN",
            "confidence":d.get(cc) if cc else "",
            "action":d.get(ca) if ca else "",
            "reason":d.get(cr) if cr else "",
        })
    return out,table

def evaluate(s):
    setup=norm(s.get("setup_name"))
    fam=family(setup)
    conf=fnum(s.get("confidence"))
    a_strong=any(x in setup for x in A_STRONG)
    b_good=any(x in setup for x in B_GOOD)
    strong=a_strong or b_good
    weak=any(x in setup for x in WEAK)

    playbook="NO_PLAYBOOK"
    score=0
    reasons=[]
    blocks=[]

    if fam=="OB":
        playbook="PLAYBOOK_A_HTF_OB_REACTION"
        score+=5
        reasons.append("Orderblock / Supply-Demand Reaction erkannt.")
    elif fam in ["MSS","BOS"] and not weak:
        playbook="PLAYBOOK_B_SESSION_MOMENTUM"
        score+=4
        reasons.append("MSS/BOS Momentum erkannt.")
    elif fam in ["FVG","BPR"]:
        blocks.append("FVG/BPR nur Confluence, kein Hauptsetup.")

    if a_strong:
        score+=4
        reasons.append("A-Setup aus V7246: echtes A+ Review möglich.")
    elif b_good:
        score+=1
        reasons.append("B-Setup aus V7246: Review möglich, aber nicht A+.")
    if weak:
        score-=8
        blocks.append("Setup gehört zu schwachen/riskanten V7246 Gruppen.")
    if conf is not None and conf>=55:
        score+=1
        reasons.append("Confidence im Review-Bereich.")
    if conf is not None and conf>=62:
        score+=1
        reasons.append("Confidence über Live-Schwelle, aber Live bleibt aus.")

    # Strikte V8000-Regel:
    # A+ nur für echte A_STRONG Setups.
    # B_GOOD Setups maximal Review.
    # Live bleibt immer aus.
    if weak:
        state="NO_TRADE_WEAK_MODEL"
    elif a_strong and conf is not None and conf>=55 and score>=8:
        state="A_PLUS_REVIEW_CANDIDATE"
    elif a_strong and conf is not None and conf>=50 and score>=7:
        state="REVIEW_CANDIDATE"
    elif b_good and conf is not None and conf>=55 and score>=6:
        state="REVIEW_CANDIDATE"
    elif fam=="OB" and conf is not None and conf>=58 and score>=6:
        state="REVIEW_CANDIDATE"
    elif score>=3:
        state="WATCH_ONLY"
    else:
        state="NO_TRADE"

    return {**s,"entry_family":fam,"playbook":playbook,"score":score,"final_state":state,"live_allowed":False,"review_allowed":state in ["A_PLUS_REVIEW_CANDIDATE","REVIEW_CANDIDATE"],"reasons":reasons,"blocks":blocks+["V8000 observe only: keine Live-Regeländerung."]}

def dedupe_evaluated(sig):
    groups={}
    for x in sig:
        key=(norm(x.get("market")), norm(x.get("direction")), norm(x.get("setup_name")))
        old=groups.get(key)
        if old is None:
            y=dict(x)
            y["duplicate_count"]=1
            groups[key]=y
            continue

        old["duplicate_count"]=int(old.get("duplicate_count") or 1)+1

        # Besseren Kandidaten behalten: höherer State/Score, dann neuere Zeit.
        rank={"A_PLUS_REVIEW_CANDIDATE":5,"REVIEW_CANDIDATE":4,"WATCH_ONLY":3,"NO_TRADE_WEAK_MODEL":1,"NO_TRADE":0}
        new_rank=rank.get(x.get("final_state"),0)
        old_rank=rank.get(old.get("final_state"),0)
        if (new_rank, x.get("score") or 0, str(x.get("time") or "")) > (old_rank, old.get("score") or 0, str(old.get("time") or "")):
            y=dict(x)
            y["duplicate_count"]=old["duplicate_count"]
            groups[key]=y

    return list(groups.values())

def all_data(limit=80):
    raw,table=load_signals(limit)
    sig=[evaluate(x) for x in raw]
    sig=dedupe_evaluated(sig)
    sig.sort(key=lambda x:(x.get("review_allowed"),x.get("score",0),str(x.get("time") or "")),reverse=True)
    return sig,table

def payload(kind="master",limit=80):
    sig,table=all_data(limit)
    a=[x for x in sig if x["playbook"]=="PLAYBOOK_A_HTF_OB_REACTION"]
    b=[x for x in sig if x["playbook"]=="PLAYBOOK_B_SESSION_MOMENTUM"]
    rev=[x for x in sig if x["review_allowed"]]
    watch=[x for x in sig if x["final_state"]=="WATCH_ONLY"]
    blocked=[x for x in sig if x["final_state"].startswith("NO_TRADE")]
    return {
        "ok":True,"version":VERSION,"mode":MODE,"generated_utc":now(),"signal_table":table,
        "summary":{
            "signals":len(sig),"playbook_a_ob_reaction":len(a),"playbook_b_session_momentum":len(b),
            "review_candidates":len(rev),"a_plus_review_candidates":sum(1 for x in sig if x["final_state"]=="A_PLUS_REVIEW_CANDIDATE"),
            "watch_only":len(watch),"blocked_or_no_trade":len(blocked),
            "live_candidates":0,"live_rule_changes":False
        },
        "signals":sig,"playbook_a":a,"playbook_b":b,"review":rev,"watch":watch,"blocked":blocked,
        "note":"V8000 Emergency Fast: Portfolio/Playbook observe only."
    }

def v8000_master_payload(limit=80): return payload("master",limit)
def v8000_portfolio_payload(limit=80): return payload("portfolio",limit)
def v8000_playbooks_payload(limit=80): return payload("playbooks",limit)
def v8000_a_plus_payload(limit=80): return payload("aplus",limit)

def cls(x):
    t=norm(x)
    if "A_PLUS" in t or "REVIEW" in t or "PLAYBOOK" in t: return "good"
    if "WATCH" in t: return "warn"
    if "NO_TRADE" in t or "WEAK" in t: return "bad"
    return "muted"

def row(x):
    rs="<br>".join(esc(r) for r in x.get("reasons",[])[:4])
    bs="<br>".join(esc(r) for r in x.get("blocks",[])[:4])
    return f"<tr><td>{esc(x.get('time'))}</td><td><b>{esc(x.get('market'))}</b></td><td>{esc(x.get('direction'))}</td><td><b>{esc(x.get('setup_name'))}</b></td><td class='{cls(x.get('playbook'))}'>{esc(x.get('playbook'))}</td><td class='{cls(x.get('final_state'))}'>{esc(x.get('final_state'))}</td><td>{esc(x.get('score'))}</td><td>{esc(x.get('entry_family'))}</td><td>{esc(x.get('confidence'))}</td><td class='small'>{rs}</td><td class='small'>{bs}</td></tr>"

def page(title,data,rows):
    s=data["summary"]
    body="".join(row(x) for x in rows) or "<tr><td colspan='11'>Keine Daten.</td></tr>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(title)}</title><style>
body{{margin:0;background:#07111e;color:#eef6ff;font-family:Arial}}.wrap{{max-width:1450px;margin:auto;padding:14px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}.card,.section{{background:#101d2e;border:1px solid #273a52;border-radius:16px;padding:14px;margin:10px 0;overflow:auto}}
.value{{font-size:28px;font-weight:900}}.good{{color:#22c55e;font-weight:900}}.warn{{color:#facc15;font-weight:900}}.bad{{color:#f87171;font-weight:900}}.muted{{color:#9ca3af;font-weight:900}}
table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{border-bottom:1px solid #273a52;padding:7px;text-align:left;white-space:nowrap;vertical-align:top}}th{{color:#a5d8ff}}a{{color:#93c5fd}}.small{{white-space:normal;min-width:220px;color:#cbd5e1}}
@media(max-width:900px){{.grid{{grid-template-columns:1fr 1fr}}}}
</style></head><body><div class='wrap'>
<h1>{esc(title)}</h1><p class='muted'>V8000 Fast Emergency · observe only · keine Live-Regeländerung · {esc(data.get('generated_utc'))}</p>
<div class='grid'><div class='card'>Signals<div class='value good'>{s['signals']}</div></div><div class='card'>Review<div class='value good'>{s['review_candidates']}</div></div><div class='card'>Watch<div class='value warn'>{s['watch_only']}</div></div><div class='card'>Blocked<div class='value bad'>{s['blocked_or_no_trade']}</div></div></div>
<div class='section'><a href='/master'>Master</a> · <a href='/v8000/master'>V8000</a> · <a href='/v8000/portfolio'>Portfolio</a> · <a href='/v8000/playbooks'>Playbooks</a> · <a href='/v8000/a-plus'>A+</a></div>
<div class='section'><table><thead><tr><th>Zeit</th><th>Market</th><th>Dir</th><th>Setup</th><th>Playbook</th><th>State</th><th>Score</th><th>Family</th><th>Conf</th><th>Reasons</th><th>Blocks</th></tr></thead><tbody>{body}</tbody></table></div>
</div></body></html>"""

def v8000_master_html():
    d=v8000_master_payload()
    return page("🚀 V8000 Master FAST",d,d["review"][:40])
def v8000_portfolio_html():
    d=v8000_portfolio_payload()
    return page("🧭 V8000 Portfolio FAST",d,d["signals"][:60])
def v8000_playbooks_html():
    d=v8000_playbooks_payload()
    return page("📘 V8000 Playbooks FAST",d,(d["playbook_a"]+d["playbook_b"])[:60])
def v8000_a_plus_html():
    d=v8000_a_plus_payload()
    return page("⭐ V8000 A+ Selector FAST",d,d["review"][:60])

if __name__=="__main__":
    print(json.dumps(v8000_master_payload(),indent=2))
