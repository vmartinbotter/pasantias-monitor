"""Monitor de pasantías.

1. Lee la lista de empresas de tu Google Sheet (hoja "Seguimiento", columna "Empresa").
2. Para cada empresa que tenga fuente configurada en fuentes.yaml, consulta sus avisos.
3. Filtra los que parecen pasantías y están en Argentina / LATAM.
4. Compara contra estado/vistos.json y te avisa solo de lo nuevo (Telegram y/o mail).
5. (Opcional) Marca en el Sheet la empresa como "Programa abierto" + link, solo en celdas vacías.

Uso:
    python monitor.py              # corrida normal
    python monitor.py --dry-run    # no notifica, no guarda estado, no toca el Sheet
    python monitor.py --empresa Sanofi   # probar una sola empresa
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

import requests
import yaml

from concurrent.futures import ThreadPoolExecutor

from fuentes_lib import ADAPTERS, NO_LOCATION, autodetectar, workday_detalle

ROOT = Path(__file__).parent
ESTADO = ROOT / "estado" / "vistos.json"
DIAS_FALLA_PARA_AVISAR = 3
DIAS_REINTENTO_AUTODETECCION = 7


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ------------------------------------------------------------------ Google Sheet
def abrir_sheet():
    creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("SHEET_ID")
    if not (creds and sheet_id):
        return None
    import gspread
    gc = gspread.service_account_from_dict(json.loads(creds))
    return gc.open_by_key(sheet_id).worksheet(os.environ.get("SHEET_TAB", "Seguimiento"))


def empresas_del_sheet(ws, excluir_estados):
    """Devuelve {nombre_normalizado: (nombre, nro_fila, dict_fila)}."""
    filas = ws.get_all_values()
    header = [h.strip() for h in filas[0]]
    col = {h: i for i, h in enumerate(header)}
    out = {}
    for n, fila in enumerate(filas[1:], start=2):
        fila = fila + [""] * (len(header) - len(fila))
        nombre = fila[col["Empresa"]].strip()
        if not nombre:
            continue
        estado = fila[col["Estado"]].strip() if "Estado" in col else ""
        if estado in excluir_estados:
            continue
        out[norm(nombre)] = (nombre, n, dict(zip(header, fila)))
    return out, col


COL_ESTADO, COL_PROG, COL_LINK = "Estado", "Programa de pasantías abierto", "Vía / Link de postulación"
ATS_DOM = re.compile(r"myworkdayjobs\.com|greenhouse\.io|lever\.co|smartrecruiters\.com|ashbyhq\.com|"
                     r"amazon\.jobs|eightfold\.ai|careers\.microsoft\.com|oraclecloud\.com", re.I)


def cambios_sheet(col, fila_nro, fila, encontrados):
    """Mantiene el Sheet al día con lo que está abierto HOY en Buenos Aires.

    - Hay pasantías: completa Estado ("Programa abierto"), "Sí" y el link, solo si
      están vacíos o si los había puesto el bot (Estado = "Programa abierto").
    - No hay más: si el bot la había marcado (Estado = "Programa abierto" y el link
      es de un sitio de empleos o está vacío), la desmarca.
    Nunca toca filas donde vos cambiaste el Estado (CV enviado, Entrevista, etc.).
    """
    est = fila.get(COL_ESTADO, "").strip()
    prog = fila.get(COL_PROG, "").strip()
    link = fila.get(COL_LINK, "").strip()
    link_del_bot = not link or bool(ATS_DOM.search(link))
    nuevo = {}
    if encontrados:
        if est in ("", "Programa abierto"):
            if not est:
                nuevo[COL_ESTADO] = "Programa abierto"
            if not prog:
                nuevo[COL_PROG] = "Sí"
            if link_del_bot and link not in {j.url for j in encontrados}:
                nuevo[COL_LINK] = encontrados[0].url
        elif not prog:
            nuevo[COL_PROG] = "Sí"
    elif est == "Programa abierto" and link_del_bot:
        nuevo = {COL_ESTADO: "", COL_PROG: "", COL_LINK: ""}
    return [{"range": gspread_a1(fila_nro, col[h] + 1), "values": [[v]]}
            for h, v in nuevo.items() if h in col and fila.get(h, "").strip() != v]


def gspread_a1(row, col):
    from gspread.utils import rowcol_to_a1
    return rowcol_to_a1(row, col)


# ------------------------------------------------------------------ Notificaciones
def notificar(titulo: str, cuerpo_html: str, cuerpo_txt: str):
    enviado = False
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        texto = f"<b>{escape(titulo)}</b>\n\n{cuerpo_html}"
        for i in range(0, len(texto), 3900):  # límite de Telegram: 4096
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20, json={
                "chat_id": chat, "text": texto[i:i + 3900], "parse_mode": "HTML",
                "disable_web_page_preview": True})
            if not r.ok:
                print("Telegram error:", r.text, file=sys.stderr)
        enviado = True
    user, pwd = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    if user and pwd:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"] = titulo, f"Monitor de pasantías <{user}>"
        msg["To"] = os.environ.get("MAIL_TO") or user
        html_mail = ("<div style='font-family:Arial,sans-serif;font-size:14px;line-height:1.6'>"
                     f"<h2 style='color:#5b7f63'>{escape(titulo)}</h2>"
                     + cuerpo_html.strip().replace("\n", "<br>") + "</div>")
        msg.attach(MIMEText(cuerpo_txt, "plain", "utf-8"))
        msg.attach(MIMEText(html_mail, "html", "utf-8"))
        with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465) as s:
            s.login(user, pwd)
            s.send_message(msg)
        enviado = True
    phone, apikey = os.environ.get("WHATSAPP_PHONE"), os.environ.get("WHATSAPP_APIKEY")
    if phone and apikey:
        # CallMeBot: servicio gratuito para mandarte WhatsApps a vos misma
        partes, actual = [], f"*{titulo}*\n"
        for linea in cuerpo_txt.splitlines():
            if len(actual) + len(linea) > 1500:
                partes.append(actual)
                actual = ""
            actual += linea + "\n"
        partes.append(actual)
        phone = re.sub(r"[^0-9]", "", phone)  # CallMeBot quiere solo dígitos: 549...
        for p in partes:
            r = requests.get("https://api.callmebot.com/whatsapp.php", timeout=30,
                             params={"phone": phone, "text": p, "apikey": apikey.strip()})
            # CallMeBot a veces responde 200 aunque falle, así que mostramos siempre la respuesta
            resp = re.sub(r"<[^>]+>", " ", r.text)
            print(f"WhatsApp [{r.status_code}]:", " ".join(resp.split())[:300])
            time.sleep(3)  # CallMeBot limita la frecuencia de mensajes
        enviado = True
    if not (phone and apikey):
        print("WhatsApp: no configurado (faltan secrets WHATSAPP_PHONE / WHATSAPP_APIKEY)")
    if not enviado:
        print(f"\n=== {titulo} ===\n{cuerpo_txt}")


# ------------------------------------------------------------------ Ubicación
MULTI = re.compile(r"^\d+\s|multiple|varias|several", re.I)


def ubicacion_ok(src, job, loc_re) -> bool:
    """Estricto: si no se puede saber dónde es, se descarta."""
    if src["tipo"] in NO_LOCATION or src.get("sin_filtro_ubicacion"):
        return True
    loc = (job.location or "").strip()
    if not loc or MULTI.search(loc):
        loc = ""
        if src["tipo"] == "workday":
            try:
                loc = workday_detalle(src, job)
            except Exception as e:  # noqa: BLE001
                print(f"    [warn] no pude ver ubicaciones de {job.title}: {e}", file=sys.stderr)
        job.location = loc
    return bool(loc) and bool(loc_re.search(loc))


# ------------------------------------------------------------------ Main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--empresa", help="probar solo esta empresa")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "fuentes.yaml").read_text(encoding="utf-8"))
    filtros = cfg["filtros"]
    rx = lambda t: re.compile(re.sub(r"\s*\|\s*", "|", t.strip()), re.I)  # noqa: E731
    inc = rx(filtros["incluir"])
    exc = rx(filtros["excluir"]) if filtros.get("excluir") else None
    loc_re = rx(filtros["ubicacion"])
    keywords = filtros["palabras_busqueda"]
    fuentes = {norm(k): (k, v) for k, v in (cfg.get("empresas") or {}).items() if v}

    ws = abrir_sheet()
    if ws:
        empresas, col = empresas_del_sheet(ws, set(filtros.get("excluir_estados", [])))
        print(f"Sheet: {len(empresas)} empresas")
    else:
        print("Sin credenciales de Google: uso las empresas de fuentes.yaml")
        empresas, col = {k: (v[0], None, {}) for k, v in fuentes.items()}, {}

    if args.empresa:
        empresas = {k: v for k, v in empresas.items() if k == norm(args.empresa)}

    estado = json.loads(ESTADO.read_text()) if ESTADO.exists() else {}
    vistos = estado.setdefault("vistos", {})
    fallas = estado.setdefault("fallas", {})
    estado["ultima_corrida"] = date.today().isoformat()  # mantiene vivo el cron de GitHub

    # ---- empresas nuevas del Sheet sin fuente: intento detectarlas solas
    auto = estado.setdefault("auto_fuentes", {})
    hoy = date.today()
    pendientes = [(k, v[0]) for k, v in empresas.items() if k not in fuentes and (
        k not in auto or (not auto[k].get("fuente") and
                          (hoy - date.fromisoformat(auto[k]["fecha"])).days >= DIAS_REINTENTO_AUTODETECCION))]
    detectadas = []
    if pendientes:
        print(f"Autodetectando {len(pendientes)} empresas sin fuente...")
        with ThreadPoolExecutor(8) as pool:
            for (k, nombre), src in zip(pendientes, pool.map(lambda p: autodetectar(p[1]), pendientes)):
                auto[k] = {"fuente": src, "fecha": hoy.isoformat()}
                if src:
                    detectadas.append(f"{nombre}: {src['tipo']} / {src['slug']}")
    for k, a in auto.items():
        if a.get("fuente") and k not in fuentes:
            fuentes[k] = (k, a["fuente"])

    nuevos, sin_fuente, errores, cambios = [], [], [], []
    for key, (nombre, fila_nro, fila) in sorted(empresas.items()):
        if key not in fuentes:
            sin_fuente.append(nombre)
            continue
        _, src = fuentes[key]
        srcs = src if isinstance(src, list) else [src]
        encontrados, descartados = [], []
        try:
            for s in srcs:
                jobs = ADAPTERS[s["tipo"]](s, keywords)
                for j in jobs:
                    if not inc.search(j.title) or (exc and exc.search(j.title)):
                        continue
                    if not ubicacion_ok(s, j, loc_re):
                        descartados.append(f"{j.title} [{j.location or 'sin ubicación'}]")
                        continue
                    encontrados.append(j)
            fallas.pop(key, None)
        except Exception as e:  # noqa: BLE001
            fallas[key] = fallas.get(key, 0) + 1
            print(f"[ERROR] {nombre}: {e}", file=sys.stderr)
            if fallas[key] >= DIAS_FALLA_PARA_AVISAR:
                errores.append(f"{nombre} ({fallas[key]} corridas seguidas): {str(e)[:120]}")
            continue

        ya = vistos.setdefault(key, {})
        frescos = [j for j in encontrados if j.id not in ya]
        print(f"{nombre}: {len(encontrados)} pasantías en BA, {len(frescos)} nuevas, "
              f"{len(descartados)} descartadas por ubicación")
        for j in frescos:
            print(f"    + {j.title} [{j.location}]")
        for j in frescos:
            ya[j.id] = date.today().isoformat()
            nuevos.append((nombre, j))
        if ws and fila_nro and cfg.get("actualizar_sheet"):
            c = cambios_sheet(col, fila_nro, fila, encontrados)
            if c:
                print(f"    Sheet: {', '.join(x['range'] + '=' + repr(x['values'][0][0])[:40] for x in c)}")
            cambios += c

    if cambios and not args.dry_run:
        try:
            ws.batch_update(cambios, value_input_option="USER_ENTERED")
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] no pude actualizar el Sheet: {e}", file=sys.stderr)

    # ---- armar mensaje
    if nuevos or errores or detectadas:
        html_lines, txt_lines = [], []
        actual = None
        for nombre, j in nuevos:
            if nombre != actual:
                html_lines.append(f"\n🏢 <b>{escape(nombre)}</b>")
                txt_lines.append(f"\n{nombre}")
                actual = nombre
            loc = f" — {escape(j.location)}" if j.location else ""
            html_lines.append(f'• <a href="{escape(j.url)}">{escape(j.title)}</a>{loc}')
            txt_lines.append(f"  - {j.title}{' — ' + j.location if j.location else ''}\n    {j.url}")
        if detectadas:
            html_lines.append("\n🔎 <b>Empresas nuevas que ahora monitoreo</b> (revisá que sea la empresa correcta):")
            html_lines += [f"• {escape(d)}" for d in detectadas]
            txt_lines.append("\nEmpresas nuevas que ahora monitoreo:")
            txt_lines += [f"  - {d}" for d in detectadas]
        if errores:
            html_lines.append("\n⚠️ <b>Fuentes que vienen fallando</b> (revisá fuentes.yaml):")
            html_lines += [f"• {escape(e)}" for e in errores]
            txt_lines.append("\nFuentes que vienen fallando:")
            txt_lines += [f"  - {e}" for e in errores]
        titulo = (f"🎓 {len(nuevos)} pasantía(s) nueva(s) en Buenos Aires" if nuevos
                  else "Monitor de pasantías: novedades")
        if args.dry_run:
            print(f"\n[dry-run] {titulo}\n" + "\n".join(txt_lines))
        else:
            notificar(titulo, "\n".join(html_lines), "\n".join(txt_lines))
    else:
        print("Nada nuevo hoy.")

    print(f"\nEmpresas sin fuente configurada ({len(sin_fuente)}): {', '.join(sin_fuente)}")
    if not args.dry_run:
        ESTADO.parent.mkdir(exist_ok=True)
        ESTADO.write_text(json.dumps(estado, indent=1, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
