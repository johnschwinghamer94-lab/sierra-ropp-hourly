#!/usr/bin/env python3
"""Build CA_SALES_DATA (CA Sales command deck) LIVE from ServiceTitan, with YTD + MTD.

Per-CA core  <- CA Conversion (318700602) + Tech Leads (216) + Mkt Leads (219)
Trends/detail <- ESTIMATE AC/TGLS (per-row monthly/same-day/opp by sold-by CA)
MTD          <- the same three reports run for July 1-9 (cache/*_mtd.json)
"""
import json, os, datetime as dt
from zoneinfo import ZoneInfo
HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("America/Los_Angeles")
TODAY = dt.datetime.now(TZ).date()
YEAR = TODAY.year
MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"]
MON_ABBR = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
CAS = ["Jeremy Brackett","Jordan Dalrymple","Shawn Brown","Rob Dais","Daniel Muntifering",
       "Fernando Castillo","Jose Valencia","Derik King","Ryan Hernlund-TECH","AJ-Alejandro Ruiz Padilla"]
GOAL = 40000000
def _load(n): return json.load(open(os.path.join(HERE, "cache", n + ".json")))
def num(v):
    try: return float(v or 0)
    except (TypeError, ValueError): return 0.0
def _rowmap(rep):
    F = rep["fields"]; i = F.index("Name")
    return {str(r[i]).strip(): (F, r) for r in rep["rows"]}
def _get(pair, field):
    if not pair: return None
    F, r = pair; return r[F.index(field)] if field in F else None
def _iso(v):
    try: return dt.datetime.fromisoformat(v).date() if isinstance(v, str) else (v.date() if hasattr(v,"date") else None)
    except (ValueError, TypeError): return None

def _estdetail(est, mtd=False):
    EF = est["fields"]; iCA=EF.index("AssignedTechnicians"); iSch=EF.index("ScheduledDate")
    iCrt=EF.index("CreatedDate"); iSub=EF.index("EstimateSalesSubtotal"); iBU=EF.index("JobBusinessUnit")
    nmonths = TODAY.month
    det = {c: {"mo":[0.0]*nmonths,"opp":0,"sold":0,"same":0,"costco":0.0,"reg":0.0,"total":0.0} for c in CAS}
    for r in est["rows"]:
        ca = str(r[iCA]).strip()
        if ca not in det: continue
        sch=_iso(r[iSch])
        if mtd and not (sch and sch.year==YEAR and sch.month==TODAY.month): continue
        crt=_iso(r[iCrt]); sub=num(r[iSub]); d=det[ca]
        d["opp"]+=1; d["total"]+=sub
        if sub>0: d["sold"]+=1
        if sch and crt and sch==crt: d["same"]+=1
        if "costco" in str(r[iBU]).lower(): d["costco"]+=sub
        else: d["reg"]+=sub
        if sch and sch.year==YEAR and sch.month<=nmonths: d["mo"][sch.month-1]+=sub
    return det

def _cas(conv, tl, ml, det):
    cas=[]
    for c in CAS:
        cv=conv.get(c); t=tl.get(c); m=ml.get(c); d=det[c]
        total=round(num(_get(cv,"TotalSales"))) if cv else round(d["total"])
        cas.append({"name":c,"total":total,"mkt":round(num(_get(cv,"TotalSalesFromMarketingLeads"))) if cv else 0,
            "tgl":round(num(_get(cv,"TotalSalesFromTgl"))) if cv else 0,
            "mktClose":round(num(_get(m,"CloseRateFromMarketingLeads"))*1000)/10 if m else 0,
            "tglClose":round(num(_get(t,"CloseRateFromTgl"))*1000)/10 if t else 0,
            "close":round(num(_get(cv,"CloseRate"))*1000)/10 if cv else 0,
            "mktAvg":round(num(_get(m,"ClosedAverageSaleFromMarketingLeads"))) if m else 0,
            "tglAvg":round(num(_get(t,"ClosedAverageSaleFromTgl"))) if t else 0,
            "avg":round(num(_get(cv,"ClosedAverageSale"))) if cv else 0,
            "oppAvg":round(num(_get(cv,"OpportunityAverageSale"))) if cv else 0,
            "options":round(num(_get(cv,"OptionsPerOpportunity"))*100)/100 if cv else 0,
            "completedRev":round(num(_get(cv,"CompletedRevenue"))) if cv else 0,
            "completedJobs":int(num(_get(cv,"CompletedJobs"))) if cv else 0,
            "mo":[round(x) for x in d["mo"]],"sameDay":round(d["same"]/d["opp"]*100) if d["opp"] else 0,
            "sold":d["sold"],"opp":d["opp"],"perOpp":round(d["total"]/d["opp"]) if d["opp"] else 0,
            "buCostco":round(d["costco"]),"buReg":round(d["reg"])})
    cas.sort(key=lambda x:-x["total"]); return cas

def _dept(cas, rng):
    def wavg(key, wkey):
        n=sum(c[key]*c[wkey] for c in cas); de=sum(c[wkey] for c in cas); return round(n/de*10)/10 if de else 0
    total=sum(c["total"] for c in cas); mktT=sum(c["mkt"] for c in cas); tglT=sum(c["tgl"] for c in cas)
    soldAll=sum(c["sold"] for c in cas); oppAll=sum(c["opp"] for c in cas)
    return {"dateRange":rng,"total":total,"mktTotal":mktT,"tglTotal":tglT,
        "mktCloseAll":wavg("mktClose","mkt"),"tglCloseAll":wavg("tglClose","tgl"),
        "avgAll":round(total/soldAll) if soldAll else 0,"sameDayAll":round(sum(c["sameDay"]*c["opp"] for c in cas)/oppAll) if oppAll else 0,
        "oppAll":oppAll,"perOppAll":round(total/oppAll) if oppAll else 0,"optionsAll":wavg("options","opp"),
        "completedRevAll":sum(c["completedRev"] for c in cas),"buCostco":sum(c["buCostco"] for c in cas),
        "buReg":sum(c["buReg"] for c in cas),"cas":cas}

def build():
    est=_load("estimate")
    nmonths = TODAY.month
    cas_ytd=_cas(_rowmap(_load("ca_conversion")),_rowmap(_load("ca_techleads")),_rowmap(_load("ca_mktleads")),_estdetail(est,False))
    cas_mtd=_cas(_rowmap(_load("ca_conversion_mtd")),_rowmap(_load("ca_techleads_mtd")),_rowmap(_load("ca_mktleads_mtd")),_estdetail(est,True))
    ytd_range = f"Jan 1 – {MON_ABBR[TODAY.month-1]} {TODAY.day}, {YEAR}"
    mtd_range = f"{MONTHS[TODAY.month-1]} 1 – {TODAY.day}, {YEAR}"
    ytd=_dept(cas_ytd,ytd_range); mtd=_dept(cas_mtd,mtd_range)
    # monthly dept series from YTD ESTIMATE detail
    det=_estdetail(est,False); monthly=[]
    for mi in range(nmonths):
        tot=round(sum(det[c]["mo"][mi] for c in CAS)); monthly.append({"month":MONTHS[mi],"total":tot,
            "mkt":round(tot*ytd["mktTotal"]/ytd["total"]) if ytd["total"] else 0,
            "tgl":round(tot*ytd["tglTotal"]/ytd["total"]) if ytd["total"] else 0,"cr":0})
    # dist / dow / weekly (YTD)
    EF=est["fields"]; iCA=EF.index("AssignedTechnicians"); iSch=EF.index("ScheduledDate"); iSub=EF.index("EstimateSalesSubtotal")
    from collections import defaultdict
    dist={"< $10k":0,"$10–20k":0,"$20–30k":0,"$30–40k":0,"$40k+":0}; dow=[0.0]*7; weekly=defaultdict(float)
    for r in est["rows"]:
        if str(r[iCA]).strip() not in CAS: continue
        sub=num(r[iSub]); sch=_iso(r[iSch])
        if sub>0: dist["< $10k" if sub<10000 else "$10–20k" if sub<20000 else "$20–30k" if sub<30000 else "$30–40k" if sub<40000 else "$40k+"]+=1
        if sch and sch.year==YEAR:
            dow[sch.weekday()]+=sub; wk=sch-dt.timedelta(days=sch.weekday()); weekly[wk]+=sub
    start=dt.date(YEAR,1,1); end=TODAY; elapsed=round(((end-start).days+1)/365*1000)/1000
    D={"source":"ServiceTitan API — CA Conversion + Tech/Mkt Leads + ESTIMATE (live)","caCount":len(cas_ytd),
       "mktAvgAll":round(ytd["mktTotal"]/max(sum(1 for c in cas_ytd if c['mkt']>0),1)),
       "tglAvgAll":round(ytd["tglTotal"]/max(sum(c['sold'] for c in cas_ytd),1)),
       "goal":GOAL,"elapsedFrac":elapsed,"months":MONTHS[:nmonths],"monthly":monthly,
       "dist":[{"label":k,"count":v} for k,v in dist.items()],
       "dow":[{"day":d,"total":round(dow[i])} for i,d in enumerate(["Mon","Tue","Wed","Thu","Fri","Sat","Sun"])],
       "weekly":[{"label":f"{k.month}/{k.day}","total":round(weekly[k])} for k in sorted(weekly)],
       "leadGen":[],"mtd":{**mtd,"month":MONTHS[TODAY.month-1]}}
    D.update(ytd)   # YTD fields at top level (default view)
    return D

if __name__ == "__main__":
    D=build(); open(os.path.join(HERE,"ca_sales_data.json"),"w").write(json.dumps(D,separators=(",",":")))
    print("YTD: total $%s mkt $%s tgl $%s | MTD(Jul): total $%s mkt $%s tgl $%s"%(
        format(D["total"],","),format(D["mktTotal"],","),format(D["tglTotal"],","),
        format(D["mtd"]["total"],","),format(D["mtd"]["mktTotal"],","),format(D["mtd"]["tglTotal"],",")))
    print("MTD top CA:",D["mtd"]["cas"][0]["name"],"$%s"%format(D["mtd"]["cas"][0]["total"],","))
