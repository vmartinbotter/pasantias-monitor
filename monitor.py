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

from fuentes_lib import ADAPTERS, NO_LOCATION

ROOT = Path(__file__).parent
ESTADO = ROOT / "estado" / "vistos.json"
DIAS_FALLA_PARA_AVISAR = 3


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


def marcar_en_sheet(ws, col, fila_nro, fila, link):
    """Completa solo celdas vacías: Estado, Programa abierto, Link."""
    cambios = []
    def set_si_vacio(header, valor):
        if header in col and not fila.get(header, "").strip():
            cambios.append({"range": gspread_a1(fila_nro, col[header] + 1), "values": [[valor]]})
    set_si_vacio("Estado", "Programa abierto")
    set_si_vacio("Programa de pasantías abierto", "Sí")
    set_si_vacio("Vía / Link de postulación", link)
    if cambios:
        ws.batch_update(cambios, value_input_option="USER_ENTERED")


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

    nuevos, sin_fuente, errores = [], [], []
    for key, (nombre, fila_nro, fila) in sorted(empresas.items()):
        if key not in fuentes:
            sin_fuente.append(nombre)
            continue
        _, src = fuentes[key]
        srcs = src if isinstance(src, list) else [src]
        encontrados = []
        try:
            for s in srcs:
                jobs = ADAPTERS[s["tipo"]](s, keywords)
                for j in jobs:
                    if not inc.search(j.title) or (exc and exc.search(j.title)):
                        continue
                    if s["tipo"] not in NO_LOCATION and not s.get("sin_filtro_ubicacion") \
                            and j.location and not re.match(r"^\d+\s", j.location) \
                            and not loc_re.search(j.location):  # "3 Locations" = no sabemos
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
        print(f"{nombre}: {len(encontrados)} pasantías, {len(frescos)} nuevas")
        for j in frescos:
            ya[j.id] = date.today().isoformat()
            nuevos.append((nombre, j))
        if frescos and ws and fila_nro and cfg.get("actualizar_sheet") and not args.dry_run:
            try:
                marcar_en_sheet(ws, col, fila_nro, fila, frescos[0].url)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] no pude actualizar el Sheet para {nombre}: {e}", file=sys.stderr)

    # ---- armar mensaje
    if nuevos or errores:
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
        if errores:
            html_lines.append("\n⚠️ <b>Fuentes que vienen fallando</b> (revisá fuentes.yaml):")
            html_lines += [f"• {escape(e)}" for e in errores]
            txt_lines.append("\nFuentes que vienen fallando:")
            txt_lines += [f"  - {e}" for e in errores]
        titulo = (f"🎓 {len(nuevos)} pasantía(s) nueva(s)" if nuevos else "Monitor de pasantías: errores")
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
