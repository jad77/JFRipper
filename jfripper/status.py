"""Status page: what's ripping, how long it'll take, recent discs, and a box to name the ones
waiting for a name. Serves / (page), /api/status (JSON) and POST /api/name (JSON)."""
import glob
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION_LABEL, jobs, naming
from .config import conf
from .util import log

DEFAULT_ENCODE_RATIO = 0.15  # encode seconds per second of movie, until we've measured one


def tail(path, size=8192):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - size))
            return f.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return ""


def rip_fraction(job):
    m = re.findall(r"^PRGV:(\d+),(\d+),(\d+)", tail(job.get("progress_file")), re.M)
    if m:
        _, total, mx = (int(x) for x in m[-1])
        return total / mx if mx else None
    if job.get("expected_bytes") and job.get("work"):
        done = sum(os.path.getsize(p) for p in
                   glob.glob(os.path.join(job["work"], "**", "*.mkv"), recursive=True))
        return min(done / job["expected_bytes"], 0.99)
    return None


def encode_progress(job):
    text = tail(job.get("progress_file"))
    times = re.findall(r"^out_time_us=(\d+)", text, re.M)
    speeds = re.findall(r"^speed=\s*([\d.]+)x", text, re.M)
    if not times or not job.get("duration"):
        return None, None
    frac = min(int(times[-1]) / 1e6 / job["duration"], 0.999)
    speed = float(speeds[-1]) if speeds else None
    return frac, speed


def encode_ratio(all_jobs):
    """Measured encode time per second of movie, from the most recent finished job."""
    for j in all_jobs:
        if j.get("encode_secs") and j.get("duration") and j.get("codec") not in ("h264", "hevc"):
            return j["encode_secs"] / j["duration"]
    return DEFAULT_ENCODE_RATIO


def describe(job, all_jobs, now):
    stage = job.get("stage")
    out = {k: job.get(k) for k in ("stamp", "label", "name", "title", "stage", "message",
                                   "started", "finished", "path", "duration", "media", "src_res",
                                   "src_codec", "out_codec", "out_note", "kind", "episodes",
                                   "episode")}
    out["name"] = job.get("title") or job.get("name") or job.get("label") or "Disc"
    stage_elapsed = now - job.get("stage_started", job.get("started", now))
    out["stage_elapsed"] = stage_elapsed
    frac = eta = None
    if stage == "ripping":
        frac = rip_fraction(job)
        if frac and frac > 0.01:
            eta = stage_elapsed / frac * (1 - frac)
        # whole job: the rip plus the encode still to come
        if job.get("duration") and job.get("media") != "Audio CD":
            out["encode_estimate"] = job["duration"] * encode_ratio(all_jobs)
    elif stage == "encoding":
        frac, speed = encode_progress(job)
        out["speed"] = speed
        if frac and speed:
            eta = job["duration"] * (1 - frac) / speed
        elif frac and frac > 0.01:
            eta = stage_elapsed / frac * (1 - frac)
    elif stage == "filing":
        frac = 1.0
    out["fraction"] = frac
    out["eta_secs"] = eta
    if eta is not None:
        out["done_at"] = now + eta + (out.get("encode_estimate") or 0)
    return out


def status():
    now = time.time()
    all_jobs = jobs.all_jobs()
    described = [describe(j, all_jobs, now) for j in all_jobs]
    return {"now": now, "version": VERSION_LABEL,
            "allow_naming": bool(conf().status["allow_naming"]),
            "active": [d for d in described if d["stage"] in jobs.ACTIVE],
            "needs_name": [d for d in described if d["stage"] == "needs_name"],
            "recent": [d for d in described
                       if d["stage"] not in jobs.ACTIVE and d["stage"] != "needs_name"][:8]}


def name_job(req):
    stamp = str(req.get("stamp") or "")
    job = jobs.load(stamp) if re.fullmatch(r"\d{8}-\d{6}", stamp) else None
    if not job:
        return naming.result(False, "that rip's job record is gone")
    if job.get("kind") == "tv":
        skip = re.findall(r"\d+", str(req.get("skip") or ""))
        return naming.tv(stamp, req.get("show"), req.get("season"), req.get("first_episode"),
                         req.get("year"), skip)
    if job.get("media") == "Audio CD":
        tracks = [t for t in str(req.get("tracks") or "").splitlines() if t.strip()]
        return naming.cd(stamp, req.get("artist"), req.get("album"), req.get("year"), tracks)
    return naming.movie(stamp, req.get("title") or "", req.get("year") or "")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JFRipper</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1f;--muted:#6e6e73;--line:#e3e1dc;--bar:#e8e6e1;
--accent:#2f6fdb;--ok:#2e8b57;--warn:#c27c0e;--bad:#c0392b;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e21;--ink:#f2f2f4;--muted:#9a9aa1;
--line:#2c2c30;--bar:#2c2c30;--accent:#5b93f0;--ok:#4cb67a;--warn:#e0a03a;--bad:#e2604f;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:15px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:14px}
.title{font-size:22px;font-weight:650;margin:0 0 2px}
.stage{color:var(--muted);font-size:14px;margin-bottom:16px}
.bar{height:10px;background:var(--bar);border-radius:99px;overflow:hidden}
.fill{height:100%;background:var(--accent);border-radius:99px;transition:width 1s ease}
.row{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:10px;font-size:14px;color:var(--muted)}
.big{font-size:34px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--ink);margin-top:14px}
.big small{font-size:15px;font-weight:500;color:var(--muted)}
.idle{color:var(--muted);text-align:center;padding:32px 20px}
.steps{display:flex;gap:6px;margin-bottom:14px;font-size:12px}
.steps span{flex:1;text-align:center;padding:4px 0;border-radius:6px;background:var(--bar);color:var(--muted)}
.steps span.on{background:var(--accent);color:#fff}
.steps span.past{color:var(--ink)}
ul{list-style:none;margin:0;padding:0}
li{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-top:1px solid var(--line);font-size:15px}
li:first-child{border-top:0}
.tag{font-size:13px;white-space:nowrap}
.specs{display:flex;flex-wrap:wrap;gap:6px;margin:-6px 0 16px}
.chip{font-size:12.5px;padding:3px 9px;border-radius:99px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.chip b{color:var(--ink);font-weight:600}
.chip.media{background:var(--ink);color:var(--card);border-color:var(--ink);font-weight:600}
.sub{display:block;font-size:12.5px;color:var(--muted)}
.done{color:var(--ok)}.needs_name{color:var(--warn)}.failed{color:var(--bad)}
.label{font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
form{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-top:12px}
form .wide{grid-column:1/-1}
input,textarea{font:inherit;font-size:15px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);width:100%}
textarea{min-height:70px}
button{font:inherit;font-size:15px;font-weight:600;padding:9px 14px;border:0;border-radius:8px;background:var(--accent);color:#fff;cursor:pointer}
button:disabled{opacity:.5}
.msg{font-size:14px;margin-top:8px;grid-column:1/-1}
.path{font-size:12.5px;color:var(--muted);word-break:break-all}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:20px}
</style></head><body><main>
<h1>JFRipper</h1>
<div id="active"></div>
<div id="needs"></div>
<div class="card"><div class="label">Recent discs</div><ul id="recent"></ul></div>
<footer id="foot">Loading…</footer>
</main>
<script>
const STAGES=["ripping","encoding","filing"];
const NAMES={ripping:"Ripping disc",queued:"Waiting to convert",encoding:"Converting",filing:"Adding to Jellyfin"};
const fmtDur=s=>{s=Math.max(0,Math.round(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60);
  return h?`${h} h ${m} min`:m?`${m} min`:`${s} s`};
const fmtClock=t=>new Date(t*1000).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"});
const res=r=>r?String(r).replace("x","×"):"";
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function specs(j){
  const c=[];
  if(j.media) c.push(`<span class="chip media">${esc(j.media)}</span>`);
  if(j.src_res||j.src_codec) c.push(`<span class="chip">Rip <b>${esc(res(j.src_res))}</b> ${esc(j.src_codec||"")}</span>`);
  if(j.out_codec) c.push(`<span class="chip">Encode <b>${esc(res(j.src_res))}</b> ${esc(j.out_codec)}${j.out_note?" · "+esc(j.out_note):""}</span>`);
  else if(j.stage==="ripping"&&j.media!=="Audio CD") c.push(`<span class="chip">Encode next</span>`);
  return c.length?`<div class="specs">${c.join("")}</div>`:"";
}
function specLine(j){
  const p=[j.media, j.src_res&&`${res(j.src_res)} ${j.src_codec||""}`.trim(), j.out_codec&&`→ ${j.out_codec}`].filter(Boolean);
  return p.length?`<span class="sub">${esc(p.join(" · "))}</span>`:"";
}
function card(j){
  const st=j.stage==="queued"?"encoding":j.stage, i=STAGES.indexOf(st), pct=j.fraction!=null?Math.round(j.fraction*100):null;
  const steps=STAGES.map((s,k)=>`<span class="${k==i&&j.stage!=="queued"?"on":k<i?"past":""}">${k<i?"✓ ":""}${NAMES[s].split(" ")[0]}</span>`).join("");
  let big=j.done_at?`${fmtClock(j.done_at)} <small>est. finish</small>`:`<small>${j.stage==="queued"?"next in line":"estimating…"}</small>`;
  let extra=[];
  if(j.eta_secs!=null) extra.push(`${fmtDur(j.eta_secs)} left in this step`);
  if(j.encode_estimate) extra.push(`then ~${fmtDur(j.encode_estimate)} converting`);
  if(j.speed) extra.push(`${j.speed.toFixed(1)}× real-time`);
  const ep=j.kind==="tv"?(j.episode?` · episode ${j.episode} of ${j.episodes}`:` · ${j.episodes} episodes`):"";
  const len=j.duration?` · ${fmtDur(j.duration)} ${j.kind==="tv"?(j.episode?"episode":"total"):j.media==="Audio CD"?"":"movie"}`:"";
  return `<div class="card"><div class="title">${esc(j.name)}</div>
  <div class="stage">${NAMES[j.stage]||j.stage} · ${fmtDur(j.stage_elapsed)} so far${ep}${len}</div>
  ${specs(j)}
  <div class="steps">${steps}</div>
  <div class="bar"><div class="fill" style="width:${pct??2}%"></div></div>
  <div class="row"><span>${pct!=null?pct+"%":""}</span><span>${extra.join(" · ")}</span></div>
  <div class="big">${big}</div></div>`;
}
function nameForm(j){
  const f=(n,ph,cls="")=>`<input name="${n}" placeholder="${ph}" class="${cls}" autocomplete="off">`;
  let fields;
  if(j.kind==="tv") fields=f("show","Show","wide")+f("year","Year (optional)")+f("season","Season")+f("first_episode","First episode")+f("skip","Skip titles, e.g. 7","wide");
  else if(j.media==="Audio CD") fields=f("artist","Artist","wide")+f("album","Album","wide")+f("year","Year (optional)")+`<textarea name="tracks" class="wide" placeholder="Track names, one per line (optional)"></textarea>`;
  else fields=f("title","Movie title","wide")+f("year","Year");
  const what=j.kind==="tv"?`TV disc, ${j.episodes} episodes`:j.media==="Audio CD"?"Audio CD":"Movie";
  return `<div class="card"><div class="title">${esc(j.label||j.name)}</div>
  <div class="stage" style="margin-bottom:4px">${what} · needs a name</div>
  <div class="path">${esc(j.path)}</div>
  <form data-stamp="${esc(j.stamp)}">${fields}<button class="wide">File it</button><div class="msg"></div></form></div>`;
}
function recent(j){
  const label={done:"In Jellyfin",failed:esc(j.message||"Failed")}[j.stage]||j.stage;
  const when=j.finished?new Date(j.finished*1000).toLocaleString([],{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}):"";
  return `<li><span>${esc(j.name)}${specLine(j)}</span><span class="tag ${j.stage}">${label} · ${when}</span></li>`;
}
let needsKey=null;
async function tick(){
  try{
    const d=await (await fetch("api/status",{cache:"no-store"})).json();
    document.getElementById("active").innerHTML=d.active.length?d.active.map(card).join("")
      :`<div class="card idle">No disc ripping. Put one in the drive and it starts on its own.</div>`;
    // Only redraw the naming forms when the list changes, so typing isn't wiped out.
    const key=d.allow_naming+d.needs_name.map(j=>j.stamp+j.path).join();
    if(key!==needsKey){
      needsKey=key;
      document.getElementById("needs").innerHTML=!d.needs_name.length?"":
        `<div class="label" style="margin:22px 0 8px">Needs a name</div>`+(d.allow_naming?d.needs_name.map(nameForm).join(""):
        d.needs_name.map(j=>`<div class="card"><div class="title">${esc(j.label||j.name)}</div><div class="path">${esc(j.path)}</div></div>`).join(""));
    }
    document.getElementById("recent").innerHTML=d.recent.length?d.recent.map(recent).join("")
      :`<li><span style="color:var(--muted)">Nothing yet</span></li>`;
    document.getElementById("foot").textContent=`JFRipper ${d.version} · updated ${fmtClock(d.now)} · refreshes every 5 s`;
  }catch(e){document.getElementById("foot").textContent="Can't reach JFRipper - retrying…"}
}
document.addEventListener("submit",async e=>{
  e.preventDefault();
  const form=e.target, btn=form.querySelector("button"), msg=form.querySelector(".msg");
  const body=Object.fromEntries(new FormData(form)); body.stamp=form.dataset.stamp;
  btn.disabled=true; msg.className="msg"; msg.textContent="Filing…";
  try{
    const r=await (await fetch("api/name",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})).json();
    msg.className="msg "+(r.ok?"done":"failed"); msg.textContent=(r.ok?"Done: ":"")+r.message;
    if(r.ok) setTimeout(()=>{needsKey=null;tick()},2500);
  }catch(err){msg.className="msg failed"; msg.textContent="Couldn't reach JFRipper"}
  btn.disabled=false;
});
tick(); setInterval(tick,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = self.path.split("?")[0]
        if route == "/api/status":
            self.send(200, json.dumps(status()).encode(), "application/json")
        elif route in ("/", "/index.html"):
            self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.split("?")[0] != "/api/name":
            self.send_error(404)
            return
        # JSON only: a browser won't send that cross-site without a CORS preflight, which
        # this server never approves, so another web page can't file things behind your back.
        if not conf().status["allow_naming"] or \
                self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.send_error(403)
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 65536)
            req = json.loads(self.rfile.read(length) or b"{}")
            out = name_job(req if isinstance(req, dict) else {})
        except Exception as exc:
            log(f"status page: naming failed: {exc!r}")
            out = naming.result(False, f"something went wrong: {exc}")
        self.send(200, json.dumps(out).encode(), "application/json")

    def log_message(self, *args):
        pass


def serve():
    host, port = conf().status["host"], int(conf().status["port"])
    server = ThreadingHTTPServer((host, port), Handler)
    log(f"status page on http://{host}:{port}/")
    server.serve_forever()
