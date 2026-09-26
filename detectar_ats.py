"""Ayudante para completar fuentes.yaml.

    python detectar_ats.py               # prueba Greenhouse/Lever/Ashby/SmartRecruiters para
                                         # las empresas que todavía no tienen fuente
    python detectar_ats.py --verificar   # prueba cada fuente ya cargada en fuentes.yaml
    python detectar_ats.py --url "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/..."
                                         # te dice qué poner en fuentes.yaml a partir de un link de un aviso

Las sugerencias son eso: sugerencias. Mirá que las ubicaciones de ejemplo sean de
la empresa correcta antes de copiarlas (hay slugs que coinciden con otra empresa).
"""
from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import yaml

from fuentes_lib import ADAPTERS, HEADERS
from monitor import abrir_sheet, empresas_del_sheet, norm

ROOT = Path(__file__).parent


def slugs(nombre: str):
    base = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode().strip()
    cands = {
        re.sub(r"[^a-z0-9]", "", base.lower()),
        re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-"),
        re.sub(r"[^A-Za-z0-9]", "", base),  # SmartRecruiters suele usar CamelCase
    }
    return [c for c in cands if len(c) >= 3]


def probar(url, **kw):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, **kw)
        return r.json() if r.ok else None
    except Exception:  # noqa: BLE001
        return None


def detectar(nombre):
    hits = []
    for s in slugs(nombre):
        d = probar(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs")
        if d and d.get("jobs") is not None:
            locs = {(j.get("location") or {}).get("name", "") for j in d["jobs"][:30]}
            hits.append(("greenhouse", s, len(d["jobs"]), locs))
        d = probar(f"https://api.lever.co/v0/postings/{s}", params={"mode": "json"})
        if isinstance(d, list) and d:
            locs = {(j.get("categories") or {}).get("location", "") for j in d[:30]}
            hits.append(("lever", s, len(d), locs))
        d = probar(f"https://api.ashbyhq.com/posting-api/job-board/{s}")
        if d and d.get("jobs"):
            locs = {j.get("location", "") for j in d["jobs"][:30]}
            hits.append(("ashby", s, len(d["jobs"]), locs))
        d = probar(f"https://api.smartrecruiters.com/v1/companies/{s}/postings", params={"limit": 30})
        if d and d.get("totalFound"):
            locs = {(j.get("location") or {}).get("country", "") for j in d.get("content", [])}
            hits.append(("smartrecruiters", s, d["totalFound"], locs))
    # dedup por (tipo, cantidad)
    vistos, out = set(), []
    for h in hits:
        if (h[0], h[2]) not in vistos:
            vistos.add((h[0], h[2]))
            out.append(h)
    return out


def desde_url(url):
    p = urlparse(url)
    host, parts = p.netloc, [x for x in p.path.split("/") if x]
    if "myworkdayjobs.com" in host:
        parts = [x for x in parts if not re.match(r"^[a-z]{2}(-[A-Z]{2})?$", x)]
        return {"tipo": "workday", "url": f"https://{host}/{parts[0]}"}
    if "greenhouse.io" in host:
        slug = parts[0] if parts else parse_qs(p.query).get("for", [""])[0]
        return {"tipo": "greenhouse", "slug": slug}
    if "lever.co" in host:
        return {"tipo": "lever", "slug": parts[0]}
    if "ashbyhq.com" in host:
        return {"tipo": "ashby", "slug": parts[0]}
    if "smartrecruiters.com" in host:
        return {"tipo": "smartrecruiters", "slug": parts[0]}
    if "eightfold.ai" in host or "careers.microsoft.com" in host:
        dom = parse_qs(p.query).get("domain", ["<dominio>.com"])[0]
        return {"tipo": "eightfold", "host": host, "domain": dom, "location": "Argentina"}
    if "oraclecloud.com" in host:
        m = re.search(r"/sites/([^/]+)", p.path)
        return {"tipo": "oracle_hcm", "host": host, "site": m.group(1) if m else "<CX_...>"}
    if "amazon.jobs" in host:
        return {"tipo": "amazon", "location": "Argentina"}
    return {"tipo": "html", "url": url,
            "_nota": "sistema no reconocido; probá con --verificar si la página no es JS"}


def verificar(cfg):
    kws = cfg["filtros"]["palabras_busqueda"]
    for nombre, src in (cfg.get("empresas") or {}).items():
        if not src:
            continue
        for s in (src if isinstance(src, list) else [src]):
            try:
                jobs = ADAPTERS[s["tipo"]](s, kws)
                ej = "; ".join(f"{j.title} [{j.location}]" for j in jobs[:2])
                print(f"OK    {nombre:22} {s['tipo']:15} {len(jobs):4} avisos  {ej[:90]}")
            except Exception as e:  # noqa: BLE001
                print(f"FALLA {nombre:22} {s['tipo']:15} {str(e)[:100]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verificar", action="store_true")
    ap.add_argument("--url")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "fuentes.yaml").read_text(encoding="utf-8"))

    if args.url:
        print(yaml.safe_dump(desde_url(args.url), allow_unicode=True, sort_keys=False))
        return
    if args.verificar:
        verificar(cfg)
        return

    ws = abrir_sheet()
    if not ws:
        raise SystemExit("Necesito GOOGLE_SERVICE_ACCOUNT_JSON y SHEET_ID para leer el Sheet.")
    empresas, _ = empresas_del_sheet(ws, set())
    ya = {norm(k) for k, v in (cfg.get("empresas") or {}).items() if v}
    print("# Pegá en fuentes.yaml (sección empresas:) las que correspondan\n")
    for key, (nombre, _, _) in sorted(empresas.items()):
        if key in ya:
            continue
        hits = detectar(nombre)
        if not hits:
            print(f"# {nombre}: no encontrado en Greenhouse/Lever/Ashby/SmartRecruiters")
            continue
        for tipo, slug, n, locs in hits:
            ejemplo = ", ".join(sorted(l for l in locs if l))[:100]
            print(f"  {nombre}: {{tipo: {tipo}, slug: {slug}}}   # {n} avisos; ej: {ejemplo}")


if __name__ == "__main__":
    main()
