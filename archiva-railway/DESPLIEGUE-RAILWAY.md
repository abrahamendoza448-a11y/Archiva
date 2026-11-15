# ARCHIVA en Railway (demostración)

1. Suba esta carpeta a un repositorio de GitHub (sin `.env` ni `data/`; ya están excluidos).
2. En https://railway.com → New Project → Deploy from GitHub repo → elija el repositorio.
   Railway detecta `railway.json` y construye con `Dockerfile.railway`.
3. En el servicio → Variables, agregue:
   - `ARCHIVA_DEBUG` = `0`
   - `ARCHIVA_SECRET_KEY` = una clave larga y aleatoria
   - `ARCHIVA_OCR` = `none`
   - `RAILWAY_PUBLIC_DOMAIN` = `${{RAILWAY_PUBLIC_DOMAIN}}`  (Railway la rellena sola)
4. Servicio → Settings → Networking → Generate Domain. Abra la dirección https://….up.railway.app
5. Ingrese con `archivista` / `Archiva-2026!` (también admin, funcionario, auditor).

Notas: los datos son de ejemplo y se regeneran en cada despliegue (SQLite sin volumen).
No use información real. Cambie las claves antes de cualquier uso serio.
