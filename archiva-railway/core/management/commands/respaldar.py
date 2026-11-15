"""Respaldo completo: base de datos + documentos + manifiesto SHA-256. Programar con cron (ver README)."""
import hashlib
import json
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Genera un respaldo (ZIP) con base de datos, documentos y manifiesto de integridad."

    def add_arguments(self, p):
        p.add_argument("--destino", default=str(settings.BACKUP_DIR))

    def handle(self, *a, destino, **kw):
        destino = Path(destino)
        destino.mkdir(parents=True, exist_ok=True)
        marca = timezone.now().strftime("%Y%m%d-%H%M%S")
        volcado = destino / f"db-{marca}.json"
        with open(volcado, "w", encoding="utf-8") as f:
            call_command("dumpdata", "--natural-foreign", "--exclude", "contenttypes", "--exclude", "sessions", stdout=f)
        zip_path = destino / f"archiva-respaldo-{marca}.zip"
        manifiesto = {}
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(volcado, "db.json")
            manifiesto["db.json"] = hashlib.sha256(volcado.read_bytes()).hexdigest()
            raiz = Path(settings.DOCUMENTOS_ROOT)
            for p in raiz.rglob("*") if raiz.exists() else []:
                if p.is_file():
                    rel = f"documentos/{p.relative_to(raiz)}"
                    z.write(p, rel)
                    manifiesto[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            z.writestr("MANIFIESTO.json", json.dumps(manifiesto, indent=2))
        volcado.unlink()
        self.stdout.write(self.style.SUCCESS(f"Respaldo creado: {zip_path} ({len(manifiesto)} archivos)"))
