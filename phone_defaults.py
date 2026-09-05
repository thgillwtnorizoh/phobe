#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, hashlib, html, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from pathlib import Path

ARCHIVE_ROOT = "https://onj3.andrelouis.com/phonetones/unzipped/"
AUDIO_EXTS = {".ogg",".mp3",".wav",".m4a",".m4r",".caf",".aac",".flac",".mid",".midi",".mmf",".amr",".qcp",".imy",".3gp"}
USER_AGENT = "phone-default-harvester/0.2 (+github-actions)"

PROP_PATTERNS = {
    "ringtone": [r"ro\.config\.ringtone(?:_1)?", r"default_ringtone", r"ringtone_default"],
    "notification": [r"ro\.config\.notification_sound(?:_1)?", r"default_notification(?:_sound)?", r"notification_sound_default"],
    "alarm": [r"ro\.config\.alarm_alert", r"default_alarm(?:_alert|_sound)?", r"alarm_alert_default"],
}

@dataclass
class Audio:
    brand: str
    model: str
    category: str
    filename: str
    url: str
    relpath: str

@dataclass
class Evidence:
    kind: str
    value: str
    repo: str
    path: str
    line: str

@dataclass
class Result:
    brand: str
    model: str
    ringtone: str = ""
    ringtone_url: str = ""
    notification: str = ""
    notification_url: str = ""
    alarm: str = ""
    alarm_url: str = ""
    confidence: str = "UNRESOLVED"
    evidence_repo: str = ""
    evidence_paths: str = ""
    notes: str = ""

class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.hrefs=[]
    def handle_starttag(self, tag, attrs):
        if tag.lower()=="a":
            for k,v in attrs:
                if k.lower()=="href" and v: self.hrefs.append(html.unescape(v))

def req(url, token="", accept="*/*", timeout=30):
    headers={"User-Agent":USER_AGENT,"Accept":accept}
    if token:
        headers["Authorization"]="Bearer "+token
        headers["X-GitHub-Api-Version"]="2022-11-28"
    r=urllib.request.Request(url, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(r, timeout=timeout) as f: return f.read()
        except urllib.error.HTTPError as e:
            if e.code not in (403,429) or attempt==3: raise
            wait=int(e.headers.get("Retry-After") or 2+attempt*3)
            time.sleep(min(wait,30))
        except urllib.error.URLError:
            if attempt==3: raise
            time.sleep(1+attempt)
    raise RuntimeError("request failed")

def text(url, token=""): return req(url,token).decode("utf-8","replace")
def js(url, token=""): return json.loads(req(url,token,"application/vnd.github+json").decode("utf-8"))

def links(page):
    p=Links(); p.feed(page); return p.hrefs

def children(base, page):
    out=[]
    bp=urllib.parse.urlparse(base).path
    for h in links(page):
        if h in ("../","./","/") or h.startswith(("?","#")): continue
        u=urllib.parse.urljoin(base,h)
        up=urllib.parse.urlparse(u)
        if not up.path.startswith(bp): continue
        out.append(urllib.parse.urlunparse(up._replace(query="",fragment="")))
    return list(dict.fromkeys(out))

def category(rel):
    x=urllib.parse.unquote(rel).lower().replace("_"," ").replace("-"," ")
    parts=[re.sub(r"\s+"," ",p).strip() for p in x.split("/")[:-1]]
    for p in reversed(parts):
        if p in ("ringtones","ringtone","ring tones","calls","call tones"): return "ringtone"
        if p in ("notifications","notification","message tones","message tone","sms","alerts","alert tones"): return "notification"
        if p in ("alarms","alarm","alarm tones"): return "alarm"
    return "unknown"

def crawl_brand(brand, limit=3, delay=.2):
    root=ARCHIVE_ROOT.rstrip("/")+"/"
    bu=urllib.parse.urljoin(root,urllib.parse.quote(brand,safe="")+"/")
    print("[crawl]",bu,file=sys.stderr)
    page=text(bu)
    bp=urllib.parse.urlparse(bu).path
    models=[]
    for u in children(bu,page):
        p=urllib.parse.urlparse(u).path
        rel=p[len(bp):].strip("/") if p.startswith(bp) else ""
        if p.endswith("/") and rel and "/" not in rel:
            name=urllib.parse.unquote(rel)
            if not name.startswith("!"): models.append((name,u))
    models=sorted(set(models))
    if limit: models=models[:limit]
    all_audio=[]
    for n,(model,mu) in enumerate(models,1):
        print(f"[model {n}/{len(models)}] {brand}/{model}",file=sys.stderr)
        todo=[(mu,0)]; seen=set(); mp=urllib.parse.urlparse(mu).path
        while todo:
            u,d=todo.pop()
            if u in seen or d>6: continue
            seen.add(u)
            try: pg=text(u)
            except Exception as e:
                print("[warn]",u,e,file=sys.stderr); continue
            for c in children(u,pg):
                cp=urllib.parse.urlparse(c).path
                if not cp.startswith(mp): continue
                rel=urllib.parse.unquote(cp[len(mp):].lstrip("/"))
                ext=Path(urllib.parse.unquote(cp)).suffix.lower()
                if ext in AUDIO_EXTS:
                    all_audio.append(Audio(brand,model,category(rel),urllib.parse.unquote(Path(cp).name),c,rel))
                elif cp.endswith("/"): todo.append((c,d+1))
            if delay: time.sleep(delay)
    return all_audio

def norm(s): return re.sub(r"[^a-z0-9]+","",Path(urllib.parse.unquote(s)).stem.lower())

def clean(v):
    v=v.strip().strip("\"'").split("#",1)[0].strip()
    if v.startswith(("file://","content://")): v=v.rsplit("/",1)[-1]
    return urllib.parse.unquote(v)

def parse_evidence(body, repo, path):
    out=[]
    for num,raw in enumerate(body.splitlines(),1):
        line=raw.strip()
        if not line or line.startswith("#"): continue
        for kind, pats in PROP_PATTERNS.items():
            for pat in pats:
                m=re.search(rf"(?:^|[\"'<\s])(?:{pat})(?:[\"'>\s]*)\s*(?:=|:)\s*[\"']?([^\"'<>,;\s]+)",line,re.I)
                if m:
                    v=clean(m.group(1))
                    if v: out.append(Evidence(kind,v,repo,path,f"{num}: {raw.strip()}"))
    ded=[]; seen=set()
    for e in out:
        k=(e.kind,e.value,e.repo,e.path)
        if k not in seen: seen.add(k); ded.append(e)
    return ded

def repo_search_queries(brand, model):
    q=f'"{model}" {brand}'
    return [f"android dump {q}", f"vendor {q}", f"firmware {q}"]

def github_repos(brand, model, token, max_repos=4):
    found=[]; seen=set()
    for q in repo_search_queries(brand,model):
        url="https://api.github.com/search/repositories?per_page=8&q="+urllib.parse.quote(q)
        try: data=js(url,token)
        except Exception as e:
            print("[github search warn]",e,file=sys.stderr); continue
        for it in data.get("items",[]):
            full=it.get("full_name","")
            if full and full not in seen:
                seen.add(full); found.append((full,it.get("default_branch") or "main"))
                if len(found)>=max_repos: return found
        time.sleep(.4)
    return found

def likely_config(path):
    low=path.lower(); name=Path(low).name
    if name in {"build.prop","default.prop","system.prop","vendor.prop","product.prop","odm.prop","prop.default","local.prop"}: return True
    if low.endswith(".prop"): return True
    if low.endswith(".xml") and any(x in low for x in ("default","ringtone","sound","setting","config")): return True
    return False

def github_evidence(brand, model, token, max_repos=4):
    evidence=[]
    repos=github_repos(brand,model,token,max_repos)
    print(f"  github repos: {len(repos)}",file=sys.stderr)
    for full,branch in repos:
        tree_url=f"https://api.github.com/repos/{full}/git/trees/{urllib.parse.quote(branch,safe='')}?recursive=1"
        try: tree=js(tree_url,token)
        except Exception as e:
            print("  [tree warn]",full,e,file=sys.stderr); continue
        paths=[x.get("path","") for x in tree.get("tree",[]) if x.get("type")=="blob" and likely_config(x.get("path",""))]
        paths.sort(key=lambda p:(0 if Path(p.lower()).name in {"build.prop","default.prop","prop.default"} else 1,len(p)))
        for path in paths[:30]:
            raw=f"https://raw.githubusercontent.com/{full}/{urllib.parse.quote(branch,safe='')}/{urllib.parse.quote(path,safe='/')}"
            try:
                body=req(raw,token).decode("utf-8","replace")
            except Exception: continue
            ev=parse_evidence(body,full,path)
            evidence.extend(ev)
            if {e.kind for e in evidence} >= {"ringtone","notification","alarm"}: break
        if {e.kind for e in evidence} >= {"ringtone","notification","alarm"}: break
    return evidence

def resolve(brand,model,audios,evidence):
    aa=[a for a in audios if a.model==model]
    r=Result(brand,model)
    repos=[]; paths=[]
    for e in evidence:
        if e.repo not in repos: repos.append(e.repo)
        if e.path not in paths: paths.append(e.path)
    r.evidence_repo="; ".join(repos); r.evidence_paths="; ".join(paths)
    matched=0; declared=0; notes=[]
    for kind in ("ringtone","notification","alarm"):
        vals=[]
        for e in evidence:
            if e.kind==kind and e.value not in vals: vals.append(e.value)
        if vals: declared+=1
        hit=None
        for v in vals:
            nv=norm(v)
            candidates=[a for a in aa if a.category==kind and norm(a.filename)==nv]
            if not candidates: candidates=[a for a in aa if norm(a.filename)==nv]
            if candidates: hit=candidates[0]; break
        if hit:
            setattr(r,kind,hit.filename); setattr(r,kind+"_url",hit.url); matched+=1
        elif vals: notes.append(f"{kind} declared as {vals[0]} but no archive filename matched")
    if matched==3: r.confidence="CONFIRMED"
    elif matched>0: r.confidence="PARTIAL"
    elif declared>0: r.confidence="EVIDENCE_ONLY"
    r.notes=" | ".join(notes)
    return r

def safe(s): return re.sub(r"[^A-Za-z0-9._ -]+","_",s).strip(" .")[:120] or "unknown"

def download_result(r,out):
    d=out/"audio"/safe(r.brand)/safe(r.model); d.mkdir(parents=True,exist_ok=True)
    meta=asdict(r)
    for k in ("ringtone","notification","alarm"):
        u=getattr(r,k+"_url")
        if not u: continue
        ext=Path(urllib.parse.urlparse(u).path).suffix.lower() or ".bin"
        dest=d/(k+ext)
        try:
            b=req(u); dest.write_bytes(b); meta[k+"_sha256"]=hashlib.sha256(b).hexdigest()
        except Exception as e: meta[k+"_download_error"]=str(e)
    (d/"metadata.json").write_text(json.dumps(meta,indent=2,ensure_ascii=False),"utf-8")

def write_outputs(out,audios,results,evidence_map):
    out.mkdir(parents=True,exist_ok=True)
    (out/"catalog.json").write_text(json.dumps([asdict(a) for a in audios],indent=2,ensure_ascii=False),"utf-8")
    (out/"results.json").write_text(json.dumps([asdict(r) for r in results],indent=2,ensure_ascii=False),"utf-8")
    evd=out/"evidence"; evd.mkdir(exist_ok=True)
    for model,ev in evidence_map.items(): (evd/(safe(model)+".json")).write_text(json.dumps([asdict(e) for e in ev],indent=2,ensure_ascii=False),"utf-8")
    fields=list(Result.__dataclass_fields__)
    for name,rows in (("results.csv",results),("confirmed.csv",[r for r in results if r.confidence=="CONFIRMED"]),("unresolved.csv",[r for r in results if r.confidence!="CONFIRMED"])):
        with (out/name).open("w",newline="",encoding="utf-8-sig") as f:
            w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); [w.writerow(asdict(r)) for r in rows]

def self_test():
    b="""ro.config.ringtone=Over_the_Horizon.ogg\nro.config.notification_sound=Skyline.ogg\nro.config.alarm_alert=Morning_Flower.ogg\n"""
    ev=parse_evidence(b,"repo","build.prop")
    assert {(e.kind,e.value) for e in ev}=={("ringtone","Over_the_Horizon.ogg"),("notification","Skyline.ogg"),("alarm","Morning_Flower.ogg")}
    aa=[Audio("Samsung","Galaxy-S9","ringtone","Over_the_Horizon.ogg","r","ringtones/x"),Audio("Samsung","Galaxy-S9","notification","Skyline.ogg","n","notifications/x"),Audio("Samsung","Galaxy-S9","alarm","Morning_Flower.ogg","a","alarms/x")]
    assert resolve("Samsung","Galaxy-S9",aa,ev).confidence=="CONFIRMED"
    print("SELF-TEST PASSED")

def main():
    p=argparse.ArgumentParser(description="Harvest factory-default ringtone/notification/alarm evidence and matching audio.")
    sp=p.add_subparsers(dest="cmd",required=True)
    sp.add_parser("self-test")
    h=sp.add_parser("harvest")
    h.add_argument("--brand",required=True); h.add_argument("--limit-models",type=int,default=3); h.add_argument("--max-repos",type=int,default=4); h.add_argument("--delay",type=float,default=.2); h.add_argument("--download",action="store_true"); h.add_argument("--out-dir",default="out")
    args=p.parse_args()
    if args.cmd=="self-test": self_test(); return 0
    token=os.getenv("GITHUB_TOKEN","")
    audios=crawl_brand(args.brand,args.limit_models,args.delay)
    models=sorted({a.model for a in audios}); results=[]; evidence_map={}; out=Path(args.out_dir)
    for i,model in enumerate(models,1):
        print(f"\n=== [{i}/{len(models)}] {args.brand}/{model} ===")
        ev=github_evidence(args.brand,model,token,args.max_repos); evidence_map[model]=ev
        r=resolve(args.brand,model,audios,ev); results.append(r)
        print(f"{r.confidence}: ringtone={r.ringtone or '?'} notification={r.notification or '?'} alarm={r.alarm or '?'}")
        write_outputs(out,audios,results,evidence_map)
        if args.download and r.confidence in ("CONFIRMED","PARTIAL"): download_result(r,out)
    write_outputs(out,audios,results,evidence_map)
    print(f"\nDone: {len(results)} models; confirmed={sum(r.confidence=='CONFIRMED' for r in results)}; output={out}")
    return 0

if __name__=="__main__": raise SystemExit(main())
