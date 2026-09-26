# Monitor de pasantías 🎓

Todos los días a las 8:17 (hora Argentina) revisa las páginas de empleo de las empresas de tu Sheet **Seguimiento_Pasantias** y te avisa por mail, WhatsApp y/o Telegram cuando aparece una pasantía nueva en Argentina/LATAM. Si encuentra una, también completa en el Sheet el Estado ("Programa abierto"), la columna "Programa de pasantías abierto" ("Sí") y el link. Solo escribe en celdas que estén vacías.

Corre gratis en GitHub Actions. No necesitás tener la compu prendida.

```
Google Sheet ──► monitor.py ──► Workday / SmartRecruiters / Greenhouse / Lever / ...
     ▲               │
     └── marca "Sí" ◄┴──► mail / WhatsApp / Telegram      estado/vistos.json (lo que ya te avisó)
```

---

## Configuración (unos 20 minutos, una sola vez)

### 1. Repo en GitHub
1. Creá un repo **privado** (por ejemplo, `pasantias-monitor`).
2. Subí todos estos archivos, incluida la carpeta `.github/`.

### 2. Acceso al Google Sheet (cuenta de servicio)
1. Entrá a https://console.cloud.google.com y creá un proyecto, por ejemplo "pasantias".
2. En **APIs y servicios → Biblioteca**, habilitá **Google Sheets API**.
3. En **IAM → Cuentas de servicio**, creá una cuenta. Después andá a **Claves → Agregar clave → JSON** y se te descarga un archivo.
4. Abrí tu Sheet, tocá **Compartir** y agregá el mail de la cuenta de servicio (`...@...iam.gserviceaccount.com`) como **Editor**.
5. El ID del Sheet es este: `1576S2uV_D6mFguZi3U_Lj-IJwp70RiPCv6ub3gFreaM`

### 3. Elegí por dónde te llegan los avisos (uno o varios)

Se usa cada canal que tenga sus secrets cargados. Si no cargás los de un canal, ese canal no se usa.

**📧 Mail (Gmail)**
1. En tu cuenta de Google, activá la verificación en 2 pasos (Seguridad → Verificación en 2 pasos).
2. Creá una contraseña de aplicación en https://myaccount.google.com/apppasswords. Son 16 letras.
3. Cargá estos secrets: `SMTP_USER` = tu Gmail, `SMTP_PASS` = esas 16 letras (sin espacios). `MAIL_TO` es opcional: sirve para mandarlo a otro mail.

**💬 WhatsApp (CallMeBot, gratis para uso personal)**
1. Agendá el número **+34 694 25 79 72**. Verificá en https://www.callmebot.com/blog/free-api-whatsapp-messages/ que siga siendo ese.
2. Mandale por WhatsApp: `I allow callmebot to send me messages`
3. Te responde con tu **apikey**.
4. Cargá estos secrets: `WHATSAPP_PHONE` = tu número con código de país (por ejemplo `+5491112345678`), `WHATSAPP_APIKEY` = la apikey.

**✈️ Telegram**
1. Hablale a **@BotFather**, mandá `/newbot` y copiá el token.
2. Mandale un mensaje a tu bot.
3. Abrí `https://api.telegram.org/bot<TOKEN>/getUpdates` y copiá el `chat.id`.
4. Cargá estos secrets: `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.

### 4. Secrets del repo
En el repo, andá a **Settings → Secrets and variables → Actions → New repository secret**. Cargá los dos obligatorios y después los del canal que elegiste:

| Secret | Valor |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | El contenido **completo** del JSON del paso 2 |
| `SHEET_ID` | `1576S2uV_D6mFguZi3U_Lj-IJwp70RiPCv6ub3gFreaM` |
| `SMTP_USER`, `SMTP_PASS`, `MAIL_TO` | Mail |
| `WHATSAPP_PHONE`, `WHATSAPP_APIKEY` | WhatsApp |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram |

### 5. Primera corrida
En **Actions → Monitor de pasantías → Run workflow** podés elegir tres modos:

1. **`verificar`**: prueba cada fuente de `fuentes.yaml` y muestra OK o FALLA. Las fuentes que cargué no las pude probar, así que corregí las que fallen.
2. **`detectar`**: para las empresas que no tienen fuente, prueba si usan Greenhouse, Lever, Ashby o SmartRecruiters, y te da las líneas para pegar en `fuentes.yaml`. Antes de pegarlas, mirá que las ubicaciones de ejemplo sean de esa empresa.
3. **`normal`**: la búsqueda real. La primera vez te manda **todo lo que esté abierto hoy**. De ahí en adelante, solo lo nuevo.

---

## Cómo agregar una empresa a mano
1. Entrá a la página de empleos de la empresa y abrí **cualquier aviso**.
2. Copiá el link y corré:
   ```bash
   python detectar_ats.py --url "https://empresa.wd3.myworkdayjobs.com/es/Careers/job/..."
   ```
3. Pegá la línea que te devuelve en `fuentes.yaml`, debajo de `empresas:`. El nombre tiene que coincidir con el del Sheet.

Pistas según el link:

| Si el link contiene… | Tipo |
|---|---|
| `myworkdayjobs.com` | workday |
| `greenhouse.io` | greenhouse |
| `lever.co` | lever |
| `smartrecruiters.com` | smartrecruiters |
| `ashbyhq.com` | ashby |
| `eightfold.ai` | eightfold |
| `oraclecloud.com` | oracle_hcm |
| otra cosa | `html` (solo sirve si la página no se arma con JavaScript) |

Empresas como Google, Meta, Accenture, P&G o los bancos locales usan portales propios armados con JavaScript. Para esas, lo mejor son las **alertas de LinkedIn**.

## Ajustar qué cuenta como "pasantía"
En `fuentes.yaml → filtros` están las regex `incluir`, `excluir` y `ubicacion`. Por ejemplo, si te llegan muchos avisos de "Student Ambassador", sacá `student` de `incluir`.

## Correrlo en tu compu
```bash
pip install -r requirements.txt
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat clave.json)" SHEET_ID=1576S2uV_D6mFguZi3U_Lj-IJwp70RiPCv6ub3gFreaM
python monitor.py --dry-run              # muestra lo que avisaría, sin notificar ni guardar
python monitor.py --dry-run --empresa Sanofi
```

## Notas
- `estado/vistos.json` guarda los avisos que ya te notificó. Si lo borrás, te vuelve a avisar todo.
- Si una fuente falla 3 días seguidos, te llega un aviso para que la revises.
- GitHub pausa los crons de repos sin actividad por 60 días. Como el script commitea el estado todos los días, eso no debería pasar.
