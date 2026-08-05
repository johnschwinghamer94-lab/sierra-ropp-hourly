#!/usr/bin/env python3
"""Build SERVICE_DATA (HVAC Service deck) LIVE + accurate from ServiceTitan, with
YTD + MTD + month-over-month.

  core metrics <- Field Conversion Report v1 (technician/328361546, IncludeInactive=False)
  team labels  <- base Field Conversion 4829 (has Team field)
  memberships  <- Memberships Sold By (sold-by/4814), counted per SoldBy tech (+ by SoldOn month)
  IAQ          <- IAQ Sold (technician/623417405), per PrimaryTechnician (+ by CompletionDate month)
  monthly/MTD  <- Field Conversion v1 run per month (cache/service_monthly.json)
"""
import json, os, datetime as dt
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo
HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("America/Los_Angeles")
TODAY = dt.datetime.now(TZ).date()
YEAR = TODAY.year
MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"]
MON_ABBR = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
TEAM_LABEL = {"2a Service NO Sam Maintenance":"Core Service (No-SAM)",
              "2b Service with SAM Maintenance":"Service + SAM","1Silo":"Silo Install Techs"}
def _load(n): return json.load(open(os.path.join(HERE, "cache", n + ".json")))
def num(v):
    try: return float(v or 0)
    except (TypeError, ValueError): return 0.0
def _iso(v):
    try: return dt.datetime.fromisoformat(v).date() if isinstance(v, str) else (v.date() if hasattr(v,"date") else None)
    except (ValueError, TypeError): return None
def isinv(v):
    s=str(int(v)) if isinstance(v,(int,float)) and float(v).is_integer() else str(v).strip()
    return s.isdigit() and len(s)>=6

def _team_map():
    fc=_load("service_fieldconv"); FC={n:fc["fields"].index(n) for n in fc["fields"]}
    return {str(r[FC["Name"]]).strip():str(r[FC["Team"]]).strip() for r in fc["rows"]}

def build_techs(v1rows, F, team_map, msold, iaq_by, iaqn_by):
    c={n:F.index(n) for n in F}; techs=[]
    for r in v1rows:
        if str(r[c["TechnicianBusinessUnit"]]).strip()!="HVAC - Service" or num(r[c["CompletedJobs"]])<=0: continue
        name=str(r[c["Name"]]).strip(); opp=num(r[c["Opportunity"]]); conv=num(r[c["OpportunityConversionRate"]])
        converted=round(opp*conv); rev=round(num(r[c["CompletedRevenueWithAdjustments"]]))
        mSold=msold.get(name,0); mConv=round(num(r[c["MembershipConversionRate"]])*1000)/10
        techs.append({"name":name,"team":team_map.get(name,""),"jobs":int(num(r[c["CompletedJobs"]])),
            "opp":int(opp),"conv":round(conv*1000)/10,"converted":converted,"rev":rev,
            "totalConv":round(num(r[c["TotalConversionRate"]])*1000)/10,"slmConv":round(num(r[c["SlmConversionRate"]])*1000)/10,
            "replacement":int(num(r[c["ReplacementOpportunity"]])),"upsold":int(num(r[c["Upsold"]])),
            "jobAvg":round(rev/opp) if opp else 0,"opo":round(num(r[c["OptionsPerOpportunity"]])*100)/100,
            "leads":int(num(r[c["LeadsSet"]])),"hrs":round(num(r[c["JobBillableHours"]])),
            "mSold":mSold,"mConv":mConv,"mOpp":round(mSold/(mConv/100)) if mConv else 0,
            "iaq":iaq_by.get(name,0),"iaqN":iaqn_by.get(name,0)})
    techs.sort(key=lambda x:-x["rev"]); return techs

def agg(ts):
    O=sum(t["opp"] for t in ts); C=sum(t["converted"] for t in ts); H=sum(t["hrs"] for t in ts)
    R=sum(t["rev"] for t in ts); MS=sum(t["mSold"] for t in ts); MO=sum(t["mOpp"] for t in ts)
    oppW=sum(t["opo"]*t["opp"] for t in ts)
    return {"techs":len(ts),"jobs":sum(t["jobs"] for t in ts),"opp":O,"converted":C,
        "conv":round(C/O*1000)/10 if O else 0,"rev":R,"membOpp":MO,"memb":MS,
        "membConv":round(MS/MO*1000)/10 if MO else 0,"leads":sum(t["leads"] for t in ts),
        "iaq":sum(t["iaq"] for t in ts),"jobAvg":round(R/C) if C else 0,"revPerHr":round(R/H) if H else 0,
        "options":round(oppW/O*100)/100 if O else 0,"replacement":sum(t["replacement"] for t in ts)}

def build():
    tm=_team_map()
    if not tm:
        # Every tech's team label comes from here; an empty map silently collapses
        # the whole team breakdown into one unlabeled "Other" bucket.
        print("  WARN: service_fieldconv cache produced no team labels — the team split on "
              "the deck will show every tech under 'Other'.")
    # membership rows: per-tech (all + July) and per-month dept
    memb=_load("service_memberships"); MF=memb["fields"]; iSold=MF.index("SoldBy"); iSoldOn=MF.index("SoldOn")
    msold_all=Counter(); msold_jul=Counter(); memb_mo=defaultdict(int)
    for r in memb["rows"]:
        nm=str(r[iSold]).split(",")[0].strip(); d=_iso(r[iSoldOn]); msold_all[nm]+=1
        if d and d.year==YEAR:
            memb_mo[d.month]+=1
            if d.month==TODAY.month: msold_jul[nm]+=1
    # IAQ: per-tech (all + July), per-month dept
    iaq=_load("service_iaq"); IF=iaq["fields"]; iSub=IF.index("Subtotal"); iComp=IF.index("CompletionDate")
    iTech=IF.index("PrimaryTechnician"); inum=IF.index("Number")
    iaq_all=defaultdict(float); iaqn_all=defaultdict(int); iaq_jul=defaultdict(float); iaqn_jul=defaultdict(int)
    iaq_mo=defaultdict(float); iaqTot=0
    for r in iaq["rows"]:
        if not isinv(r[inum]): continue
        nm=str(r[iTech] or "").split(",")[0].strip(); s=num(r[iSub]); d=_iso(r[iComp]); iaqTot+=s
        iaq_all[nm]+=round(s); iaqn_all[nm]+=1
        if d and d.year==YEAR:
            iaq_mo[d.month]+=s
            if d.month==TODAY.month: iaq_jul[nm]+=round(s); iaqn_jul[nm]+=1
    # YTD techs
    v1=_load("service_v1")
    techs_ytd=build_techs(v1["rows"], v1["fields"], tm, msold_all, iaq_all, iaqn_all)
    # MTD (July) techs
    mo=_load("service_monthly")
    techs_mtd=build_techs(mo["cur_rows"], mo["cur_fields"], tm, msold_jul, iaq_jul, iaqn_jul)
    dept_ytd=agg(techs_ytd); dept_mtd=agg(techs_mtd)
    # teams (YTD)
    bt=defaultdict(list)
    for t in techs_ytd: bt[t["team"]].append(t)
    teams=[{**agg(v),"team":k,"label":TEAM_LABEL.get(k,k or "Other")} for k,v in bt.items()]; teams.sort(key=lambda x:-x["rev"])
    # monthly series (rev/conv from FC, memb from bucket, iaq from bucket)
    monthly=[]
    for i,m in enumerate(mo["monthly"]):
        mn=m["month"]; idx=MONTHS.index(mn)+1
        monthly.append({"month":mn,"rev":m["rev"],"jobs":m["jobs"],"opp":m["opp"],"converted":m["converted"],
            "conv":m["conv"],"memb":memb_mo.get(idx,0),"iaq":round(iaq_mo.get(idx,0))})
    start=dt.date(YEAR,1,1); end=TODAY; elapsed=round(((end-start).days+1)/365*1000)/1000
    date_range = f"Jan 1 – {MON_ABBR[TODAY.month-1]} {TODAY.day}, {YEAR}"
    mtd_range = f"{MONTHS[TODAY.month-1]} 1 – {TODAY.day}, {YEAR}"
    return {"dateRange":date_range,"mtdRange":mtd_range,"month":MONTHS[TODAY.month-1],
        "source":"ServiceTitan API — Field Conversion v1 + Memberships Sold + IAQ (live)","elapsedFrac":elapsed,
        "dept":dept_ytd,"mtdDept":dept_mtd,"techs":techs_ytd,"mtdTechs":techs_mtd,"teams":teams,
        "iaqTotal":round(iaqTot),"iaqMonthly":[{"month":MONTHS[m-1],"total":round(iaq_mo[m])} for m in sorted(iaq_mo)],
        "monthly":monthly,"months":[m["month"] for m in monthly]}

if __name__ == "__main__":
    D=build(); open(os.path.join(HERE,"service_data.json"),"w").write(json.dumps(D,separators=(",",":")))
    print("YTD: %d techs $%s %s%% memb %d | MTD(Jul): $%s %s%% memb %d"%(
        D["dept"]["techs"],format(D["dept"]["rev"],","),D["dept"]["conv"],D["dept"]["memb"],
        format(D["mtdDept"]["rev"],","),D["mtdDept"]["conv"],D["mtdDept"]["memb"]))
    print("monthly rev:",[(m["month"][:3],m["rev"]) for m in D["monthly"]])
