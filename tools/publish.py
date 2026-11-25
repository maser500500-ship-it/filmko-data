#!/usr/bin/env python3
# النشر التلقائي بدون الهاتف: يقرأ المهام الجاهزة (فيديو اتشفّر على GitHub) وينشر المفتاح والروابط في db/pub.enc
import json, os, re, subprocess, sys, time, base64, tempfile, urllib.request, urllib.parse, urllib.error

HLKEY = os.environ.get("HLKEY", "")
PUBKEY = os.environ.get("PUBKEY", "")
if not HLKEY or not PUBKEY:
    print("no keys"); sys.exit(0)

def ossl(args, data, pw):
    env = dict(os.environ, PW=pw)
    r = subprocess.run(["openssl", "enc"] + args + ["-aes-256-cbc", "-pbkdf2", "-md", "sha256", "-pass", "env:PW"],
                       input=data, capture_output=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[:200])
    return r.stdout

def dec(path, pw, it):
    return json.loads(ossl(["-d", "-iter", str(it)], open(path, "rb").read(), pw))

def enc(obj, path, pw, it):
    open(path, "wb").write(ossl(["-iter", str(it), "-salt"], json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(), pw))

def http(url, token=None, raw=False):
    h = {"User-Agent": "filmko"}
    if token: h["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
        b = r.read()
    return b if raw else json.loads(b)

try:
    hl_obj = dec("db/hl.enc", HLKEY, 10000)
    hl = hl_obj["docs"]
except Exception as e:
    print("hl.enc unreadable:", e); sys.exit(0)

jobs = json.loads((hl.get("hub/pjobs") or {}).get("json") or "[]")
if not jobs:
    print("no jobs"); sys.exit(0)

repo, token = "", ""
for it in (hl.get("secrets/src_video") or {}).get("items", []):
    if it.get("enabled", True) and it.get("key") and (it.get("extra") or {}).get("repo"):
        repo, token = it["extra"]["repo"], it["key"]; break
if not repo:
    v = hl.get("secrets/vidlink") or {}
    repo, token = v.get("repo", ""), v.get("key", "")
enc_key = (hl.get("secrets/enc") or {}).get("key", "")
if not repo or not enc_key:
    print("no video repo / enc key"); sys.exit(0)
pilot = hl.get("secrets/pilot") or {}
allow_incomplete = str(pilot.get("incomplete", "1")) not in ("0", "False", "false")

pub = dec("db/pub.enc", PUBKEY, 10000)
docs = pub["docs"]
now = int(time.time() * 1000)
changed = False
jobs_changed = False
state_doc = dict(docs.get("pilot/state") or {})   # حالة المهام للتطبيق (عام: بدون أسرار)

def gh_api(path, method="GET", body=None, tok=None):
    h = {"User-Agent": "filmko", "Accept": "application/vnd.github+json"}
    if tok: h["Authorization"] = "Bearer " + tok
    data = None
    if body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    req = urllib.request.Request("https://api.github.com" + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return -1, {}

def mark(job, **kw):
    global jobs_changed
    s = state_doc.setdefault(job["id"], {})
    for k, v in kw.items():
        if k == "item":
            s.setdefault("i", {})[v[0]] = v[1]
        else:
            s[k] = v
    jobs_changed = True

# ---- تجهيز البيانات (TMDb) على GitHub: ما عاد التطبيق لازم يكون شغال
tm_cfg = hl.get("secrets/tmdb") or {}
tmdb_key = tm_cfg.get("key", "") if tm_cfg.get("enabled", True) else ""
LANGS = {"ar": "العربية", "en": "الإنجليزية", "hi": "الهندية", "tr": "التركية", "ko": "الكورية", "ja": "اليابانية",
         "fr": "الفرنسية", "es": "الإسبانية", "de": "الألمانية", "it": "الإيطالية", "zh": "الصينية", "ru": "الروسية",
         "pt": "البرتغالية", "fa": "الفارسية", "ur": "الأردية"}
CTRY = {"EG": "مصر", "SA": "السعودية", "AE": "الإمارات", "KW": "الكويت", "LB": "لبنان", "SY": "سوريا", "JO": "الأردن",
        "MA": "المغرب", "TN": "تونس", "DZ": "الجزائر", "IQ": "العراق", "US": "الولايات المتحدة", "GB": "بريطانيا",
        "FR": "فرنسا", "DE": "ألمانيا", "IT": "إيطاليا", "ES": "إسبانيا", "TR": "تركيا", "IN": "الهند", "KR": "كوريا الجنوبية",
        "JP": "اليابان", "CN": "الصين", "RU": "روسيا", "CA": "كندا", "AU": "أستراليا", "MX": "المكسيك", "BR": "البرازيل"}

def tmdb(path, q="", lang="ar"):
    k = tmdb_key.strip()
    v4 = len(k) > 40 or k.startswith("eyJ")
    u = "https://api.themoviedb.org/3/%s?language=%s%s" % (path, lang, q) + ("" if v4 else "&api_key=" + k)
    h = {"User-Agent": "filmko"}
    if v4: h["Authorization"] = "Bearer " + k
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=30) as r:
            return json.loads(r.read())
    except Exception:
        return None

def fetch_meta(job):
    meta = {"name": job.get("query", ""), "poster": "", "story": "", "genre": "", "year": "", "country": "",
            "language": "", "rating": 0, "cast": [], "seasons": 0}
    if not tmdb_key:
        return meta
    tv = job.get("type") == "series"
    kind = "tv" if tv else "movie"
    tid = int(job.get("tmdbId") or 0)
    if tid <= 0:
        r = tmdb("search/" + kind, "&query=" + urllib.parse.quote(job.get("query", "")))
        res = (r or {}).get("results") or []
        tid = res[0]["id"] if res else 0
    j = tmdb("%s/%d" % (kind, tid), "&append_to_response=credits") if tid > 0 else None
    if not j:
        return meta
    story = j.get("overview") or ""
    if not story:
        story = ((tmdb("%s/%d" % (kind, tid), lang="en") or {}).get("overview")) or ""
    codes = (j.get("origin_country") or []) if tv else [c.get("iso_3166_1") for c in j.get("production_countries") or []]
    pp = j.get("poster_path") or ""
    date = j.get("first_air_date" if tv else "release_date") or ""
    lc = j.get("original_language") or ""
    meta.update({
        "name": j.get("name" if tv else "title") or meta["name"],
        "poster": ("https://image.tmdb.org/t/p/w500" + pp) if pp else "",
        "story": story,
        "genre": "، ".join(g["name"] for g in j.get("genres") or [] if g.get("name")),
        "year": date[:4],
        "country": "، ".join([CTRY.get(c, c) for c in codes if c][:2]),
        "language": LANGS.get(lc, lc),
        "rating": round(float(j.get("vote_average") or 0), 1),
        "cast": [c["name"] for c in ((j.get("credits") or {}).get("cast") or [])[:10] if c.get("name")],
        "seasons": int(j.get("number_of_seasons") or 0) if tv else 0,
    })
    return meta

def guess_section(meta, typ, fallback):
    lang = meta.get("language", "")
    arabic = lang == "العربية"
    if fallback in ("ar_movie", "fo_movie", "ar_series", "fo_series"):
        return fallback
    return ("ar_" if arabic else "fo_") + ("series" if typ == "series" else "movie")

pilot_cfg = hl.get("secrets/pilot") or {}

def meta_step(job):
    tid = job.get("titleId") or ""
    if not tid:
        return False
    if job.get("existing"):
        job["metaState"] = "done"; job["complete"] = True
        mark(job, m="done", c=True, t=job.get("title", ""), s=job.get("section", ""))
        return True
    if ("titles/" + tid) in docs and (docs["titles/" + tid] or {}).get("name"):
        job["metaState"] = "done"
        mark(job, m="done", c=bool(docs["titles/" + tid].get("poster") and docs["titles/" + tid].get("story")),
             t=docs["titles/" + tid].get("name", ""), s=docs["titles/" + tid].get("section", ""))
        return True
    meta = fetch_meta(job)
    typ = job.get("type", "movie")
    section = job.get("section") or guess_section(meta, typ, str(pilot_cfg.get("section", "")))
    name = meta["name"] or job.get("query", "")
    docs["titles/" + tid] = {
        "name": name, "poster": meta["poster"], "story": meta["story"], "genre": meta["genre"], "year": meta["year"],
        "country": meta["country"], "language": meta["language"], "rating": meta["rating"], "cast": meta["cast"],
        "type": typ, "section": section, "seasons": meta["seasons"] if typ == "series" else 0,
        "quality": "", "published": False, "createdAt": now}
    complete = bool(meta["poster"] and meta["story"])
    job["metaState"] = "done"; job["complete"] = complete; job["title"] = name; job["section"] = section
    mark(job, m="done", c=complete, t=name, s=section)
    return True

# ---- إرسال الفيديو لمستودع الفيديو (fetch.yml) من GitHub مباشرة
def send_item(job, item):
    if not repo or not token:
        return False
    st, runs = gh_api("/repos/%s/actions/workflows/fetch.yml/runs?per_page=40" % repo, tok=token)
    if st == 200:
        for r in runs.get("workflow_runs", []):
            if r.get("display_title") == "fetch " + item["id"]:
                return True
    elif st != -1 and st != 404:
        pass
    label = ("%s م%s ح%s" % (job.get("query", ""), item.get("season", 1), item.get("number", 1))) if job.get("type") == "series" else job.get("query", "")
    st, _ = gh_api("/repos/%s/actions/workflows/fetch.yml/dispatches" % repo, "POST",
                   {"ref": "main", "inputs": {"url": item.get("src", ""), "id": item["id"], "name": label}}, tok=token)
    return st == 204

QK = {360: "360", 720: "720", 1080: "1080"}
QL = {"360": "360p", "720": "720p HD", "1080": "1080p Full HD", "4k": "4K Ultra HD"}
qkey = lambda q: QK.get(q, "4k")

def unwrap(wrapped_b64):
    with tempfile.TemporaryDirectory() as d:
        open(d + "/k.der", "wb").write(base64.b64decode(enc_key))
        subprocess.run(["openssl", "pkey", "-inform", "DER", "-in", d + "/k.der", "-out", d + "/k.pem"], check=True)
        open(d + "/w.bin", "wb").write(base64.b64decode(wrapped_b64))
        r = subprocess.run(["openssl", "pkeyutl", "-decrypt", "-inkey", d + "/k.pem", "-in", d + "/w.bin",
                            "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256", "-pkeyopt", "rsa_mgf1_md:sha256"],
                           capture_output=True, check=True)
        return r.stdout

for job in jobs:
    if job.get("metaState", "new") == "new":
        try:
            if meta_step(job):
                changed = True
        except Exception as e:
            print("meta failed", job.get("id"), e)
    for item in job.get("items", []):
        if item.get("state", "new") == "new":
            try:
                if send_item(job, item):
                    item["state"] = "sent"; mark(job, item=(item["id"], "sent"))
                    print("sent", item["id"])
            except Exception as e:
                print("send failed", item.get("id"), e)
    tid = job.get("titleId") or ""
    if job.get("metaState") != "done" or not tid or ("titles/" + tid) not in docs:
        continue
    auto_pub = job.get("auto", True) and (job.get("complete") or allow_incomplete)
    for item in job.get("items", []):
        if item.get("state") == "new":
            continue          # لسه ما اتبعتش (الإرسال فوق بيحاول كل دورة)
        # أي حالة تانية (حتى المتعلّقة القديمة: error/linked) نجرّب: الإصدار موجود؟ انشره
        vid = item["id"]
        series = job.get("type") == "series"
        ep_id = "s%02de%03d" % (int(item.get("season", 1)), int(item.get("number", 1)))
        target = ("titles/%s/episodes/%s" % (tid, ep_id)) if series else ("titles/" + tid)
        if (docs.get(target) or {}).get("main"):
            continue
        try:
            rel = http("https://api.github.com/repos/%s/releases/tags/v-%s" % (repo, vid), token or None)
        except Exception as e:
            print(vid, "release not ready"); continue
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        if vid + ".key" not in assets:
            continue
        qs = sorted(int(m.group(1)) for n in assets for m in [re.match(r"^%s-(\d+)\.flk$" % re.escape(vid), n)] if m)
        legacy = (vid + ".flk") in assets and not qs
        if not qs and not legacy:
            continue
        try:
            k = unwrap(http(assets[vid + ".key"], raw=True).decode().strip())
            assert len(k) == 32
        except Exception as e:
            print(vid, "key unwrap failed", e); continue
        docs["vkeys/" + vid] = {"k": base64.b64encode(k).decode(), "at": now}
        if vid + ".mirrors.json" in assets:
            try:
                mj = json.loads(http(assets[vid + ".mirrors.json"], raw=True))
                m = {re.sub(r"[^A-Za-z0-9]", "_", n): [u for u in v if str(u).startswith("http")] for n, v in mj.items() if isinstance(v, list)}
                if m: docs["vmirrors/" + vid] = m
            except Exception as e:
                print(vid, "mirrors skipped", e)
        base = "https://github.com/%s/releases/download/v-%s" % (repo, vid)
        flat = {"main": "", **{p + q: "" for q in QL for p in ("play", "dl")}}
        label = ""
        if legacy:
            flat["main"] = "%s/%s.flk" % (base, vid)
        else:
            play = {}
            for q in qs:
                u = "%s/%s-%d.flk" % (base, vid, q)
                flat["play" + qkey(q)] = u; flat["dl" + qkey(q)] = u; play[qkey(q)] = u
            best = max([q for q in qs if q <= 1080] or [min(qs)])
            flat["main"] = play.get(qkey(best)) or next(iter(play.values()))
            label = QL[qkey(max(qs))]
        tm = dict(docs.get("titles/" + tid) or {})
        tm["quality"] = label
        if auto_pub:
            tm["published"] = True; tm["createdAt"] = now
        if series:
            ep = dict(docs.get(target) or {})
            ep.update({"season": int(item.get("season", 1)), "number": int(item.get("number", 1)), "createdAt": now})
            if item.get("name"): ep["name"] = item["name"]
            ep.update(flat); ep.pop("links", None)
            docs[target] = ep
        else:
            tm.update(flat); tm.pop("links", None)
        docs["titles/" + tid] = tm
        item["state"] = "published" if auto_pub else "linked"
        mark(job, item=(vid, item["state"]))
        changed = True
        print("published", vid, target)

if jobs_changed:
    docs["pilot/state"] = state_doc
    changed = True
    # حالة المهام في hl.enc كمان، عشان الدورة الجاية تكمل من نفس المكان
    try:
        hl.setdefault("hub/pjobs", {})["json"] = json.dumps(jobs, ensure_ascii=False, separators=(",", ":"))
        hl_obj["ts"] = now
        enc(hl_obj, "db/hl.enc", HLKEY, 10000)
    except Exception as e:
        print("hl write failed", e)

if changed:
    pub["ts"] = now
    enc(pub, "db/pub.enc", PUBKEY, 10000)
