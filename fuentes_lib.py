"""Adaptadores para cada sistema de empleos (ATS).

Cada adaptador recibe la config de la empresa (dict de fuentes.yaml) y la lista
de palabras clave, y devuelve una lista de Job. El filtrado fino (título /
ubicación) se hace después en monitor.py, así que acá conviene traer "de más".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

TIMEOUT = 25
HEADERS = {
    "User-Agent": "Mozilla/5.0 (pasantias-monitor; uso personal)",
    "Accept": "application/json, text/html;q=0.9",
}


@dataclass
class Job:
    id: str
    title: str
    location: str
    url: str


def _get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _post(url, json):
    r = requests.post(url, headers={**HEADERS, "Content-Type": "application/json"},
                      json=json, timeout=TIMEOUT)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- Greenhouse
def greenhouse(cfg, keywords):
    slug = cfg["slug"]
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json()
    return [Job(str(j["id"]), j["title"], (j.get("location") or {}).get("name", ""),
                j["absolute_url"]) for j in data.get("jobs", [])]


# ---------------------------------------------------------------- Lever
def lever(cfg, keywords):
    slug = cfg["slug"]
    data = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}).json()
    out = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        out.append(Job(j["id"], j["text"], loc, j["hostedUrl"]))
    return out


# ---------------------------------------------------------------- Ashby
def ashby(cfg, keywords):
    slug = cfg["slug"]
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()
    out = []
    for j in data.get("jobs", []):
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        out.append(Job(j["id"], j["title"], ", ".join(l for l in locs if l), j["jobUrl"]))
    return out


# ---------------------------------------------------------------- SmartRecruiters
def smartrecruiters(cfg, keywords):
    slug = cfg["slug"]
    out, seen = [], set()
    for kw in keywords:
        offset = 0
        while offset < 500:
            data = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                        params={"q": kw, "limit": 100, "offset": offset}).json()
            items = data.get("content", [])
            for j in items:
                if j["id"] in seen:
                    continue
                seen.add(j["id"])
                loc = j.get("location") or {}
                loc_txt = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
                out.append(Job(j["id"], j["name"], loc_txt,
                               f"https://jobs.smartrecruiters.com/{slug}/{j['id']}"))
            offset += 100
            if offset >= data.get("totalFound", 0):
                break
    return out


# ---------------------------------------------------------------- Workday
_LANG = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def _parse_workday(url):
    """https://sanofi.wd3.myworkdayjobs.com/es/SanofiCareers -> (host, tenant, site)"""
    p = urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    parts = [x for x in parts if not _LANG.match(x)]
    return p.netloc, p.netloc.split(".")[0], parts[0]


def workday(cfg, keywords):
    host, tenant, site = _parse_workday(cfg["url"])
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, seen = [], set()
    for kw in keywords:
        offset = 0
        while offset < 100:  # Workday pagina de a 20
            data = _post(api, {"appliedFacets": {}, "limit": 20, "offset": offset,
                               "searchText": kw}).json()
            items = data.get("jobPostings", [])
            for j in items:
                path = j.get("externalPath", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append(Job(path, j.get("title", ""), j.get("locationsText", ""),
                               f"https://{host}/{site}{path}"))
            offset += 20
            if offset >= data.get("total", 0) or not items:
                break
    return out


def workday_detalle(cfg, job):
    """Para avisos con "2 Locations": trae todas las ubicaciones del aviso."""
    host, tenant, site = _parse_workday(cfg["url"])
    info = _get(f"https://{host}/wday/cxs/{tenant}/{site}{job.id}").json().get("jobPostingInfo", {})
    locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
    pais = (info.get("country") or {}).get("descriptor", "")
    return " | ".join(x for x in locs + [pais] if x)


# ---------------------------------------------------------------- Eightfold (Microsoft, Mercado Libre, ...)
def eightfold(cfg, keywords):
    host, domain = cfg["host"], cfg["domain"]
    out, seen = [], set()
    for kw in keywords:
        data = _get(f"https://{host}/api/apply/v2/jobs",
                    params={"domain": domain, "query": kw, "location": cfg.get("location", ""),
                            "start": 0, "num": 50}).json()
        for j in data.get("positions", []):
            jid = str(j["id"])
            if jid in seen:
                continue
            seen.add(jid)
            loc = j.get("location") or ", ".join(j.get("locations") or [])
            url = j.get("canonicalPositionUrl") or f"https://{host}/careers?pid={jid}&domain={domain}"
            out.append(Job(jid, j.get("name", ""), loc, url))
    return out


# ---------------------------------------------------------------- Amazon
def amazon(cfg, keywords):
    out, seen = [], set()
    for kw in keywords:
        data = _get("https://www.amazon.jobs/en/search.json",
                    params={"base_query": kw, "loc_query": cfg.get("location", "Argentina"),
                            "result_limit": 100}).json()
        for j in data.get("jobs", []):
            jid = str(j.get("id_icims") or j.get("id"))
            if jid in seen:
                continue
            seen.add(jid)
            out.append(Job(jid, j["title"], j.get("normalized_location") or j.get("location", ""),
                           "https://www.amazon.jobs" + j["job_path"]))
    return out


# ---------------------------------------------------------------- Oracle Recruiting Cloud
def oracle_hcm(cfg, keywords):
    host, site = cfg["host"], cfg["site"]
    out, seen = [], set()
    for kw in keywords:
        finder = f'findReqs;siteNumber={site},keyword="{kw}",limit=100,sortBy=POSTING_DATES_DESC'
        data = _get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                    params={"onlyData": "true", "expand": "requisitionList", "finder": finder}).json()
        for block in data.get("items", []):
            for j in block.get("requisitionList", []):
                jid = str(j["Id"])
                if jid in seen:
                    continue
                seen.add(jid)
                out.append(Job(jid, j.get("Title", ""), j.get("PrimaryLocation", ""),
                               f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}"))
    return out


# ---------------------------------------------------------------- HTML genérico
def html(cfg, keywords):
    """Busca links en una página de empleos. Solo sirve si la página no se arma con JS.
    Opcional: `link_regex` para quedarse solo con links que parezcan avisos."""
    url = cfg["url"]
    soup = BeautifulSoup(_get(url).text, "html.parser")
    link_re = re.compile(cfg["link_regex"]) if cfg.get("link_regex") else None
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if link_re and not link_re.search(href):
            continue
        text = " ".join((a.get_text(" ") or a.get("aria-label") or a.get("title") or "").split())
        if not text or href in seen:
            continue
        seen.add(href)
        out.append(Job(href, text[:200], "", href))
    return out


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workday": workday,
    "eightfold": eightfold,
    "amazon": amazon,
    "oracle_hcm": oracle_hcm,
    "html": html,
}

# Tipos que devuelven TODOS los avisos y no tienen ubicación estructurada confiable
NO_LOCATION = {"html"}


# ---------------------------------------------------------------- Autodetección
def _slugs(nombre: str):
    import unicodedata
    base = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode().strip()
    cands = [
        re.sub(r"[^a-z0-9]", "", base.lower()),
        re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-"),
        re.sub(r"[^A-Za-z0-9]", "", base),  # SmartRecruiters suele usar CamelCase
    ]
    return [c for i, c in enumerate(cands) if len(c) >= 3 and c not in cands[:i]]


def _cuantos(url, clave, **kw):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, **kw)
        if not r.ok:
            return 0
        d = r.json()
        if clave == "list":
            return len(d) if isinstance(d, list) else 0
        if clave == "totalFound":
            return int(d.get("totalFound") or 0)
        return len(d.get(clave) or [])
    except Exception:  # noqa: BLE001
        return 0


def autodetectar(nombre: str):
    """Prueba Greenhouse / Lever / Ashby / SmartRecruiters. Devuelve la fuente con más
    avisos, o None. Solo acepta fuentes con al menos 1 aviso publicado."""
    mejor, n_mejor = None, 0
    for s in _slugs(nombre):
        pruebas = [
            ({"tipo": "greenhouse", "slug": s}, _cuantos(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs", "jobs")),
            ({"tipo": "lever", "slug": s}, _cuantos(f"https://api.lever.co/v0/postings/{s}", "list", params={"mode": "json"})),
            ({"tipo": "ashby", "slug": s}, _cuantos(f"https://api.ashbyhq.com/posting-api/job-board/{s}", "jobs")),
            ({"tipo": "smartrecruiters", "slug": s}, _cuantos(f"https://api.smartrecruiters.com/v1/companies/{s}/postings", "totalFound", params={"limit": 1})),
        ]
        for src, n in pruebas:
            if n > n_mejor:
                mejor, n_mejor = src, n
    return mejor
