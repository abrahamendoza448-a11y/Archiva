"""Prueba de restauración ligera: verifica que el ZIP no esté alterado (SHA-256 de cada archivo)."""
import hashlib
import json
import zipfile

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Verifica la integridad de un respaldo generado por 'respaldar'."

    def add_arguments(self, p):
        p.add_argument("zip")

    def handle(self, *a, zip, **kw):
        with zipfile.ZipFile(zip) as z:
            man = json.loads(z.read("MANIFIESTO.json"))
            malos = [n for n, h in man.items() if hashlib.sha256(z.read(n)).hexdigest() != h]
        if malos:
            raise CommandError(f"Respaldo alterado o corrupto: {malos}")
        self.stdout.write(self.style.SUCCESS(f"Respaldo íntegro ({len(man)} archivos verificados)."))
