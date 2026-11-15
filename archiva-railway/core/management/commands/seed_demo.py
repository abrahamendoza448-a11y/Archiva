"""Datos de demostración: dependencias, usuarios por rol, archivo físico, expedientes y documentos de ejemplo."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import (Caja, Carpeta, Dependencia, Documento, Estante, Expediente, Perfil, Rol)
from core.services import analizar_documento, sha256_subido

TEXTOS = {
    ("Resolución Municipal de aprobación del POA 2026", "RESOLUCION"):
        "RESOLUCIÓN MUNICIPAL N° RM-045/2026. CONSIDERANDO que el Plan Operativo Anual debe aprobarse. "
        "RESUELVE: Artículo primero. Aprobar el POA 2026 con fecha 15/01/2026. Regístrese, comuníquese y archívese.",
    ("Contrato de provisión de material de escritorio", "CONTRATO"):
        "CONTRATO ADMINISTRATIVO. Las partes: el Gobierno Municipal y la empresa proveedora, NIT 1023456028. "
        "Cláusula primera: objeto del contrato. Monto total Bs. 18.500 con fecha 03/03/2026.",
    ("Informe técnico de infraestructura vial", "INFORME"):
        "INFORME TÉCNICO. Antecedentes: se inspeccionó el tramo norte. Conclusiones: requiere mantenimiento. "
        "Recomendaciones: licitar la reparación de la calzada.",
}


class Command(BaseCommand):
    help = "Carga datos de demostración"

    def handle(self, *a, **kw):
        deps = {}
        for sigla, nombre in [("ALC", "Despacho del Alcalde"), ("SEG", "Secretaría General"),
                              ("FIN", "Dirección Financiera"), ("OOPP", "Obras Públicas"), ("ARC", "Archivo Central")]:
            deps[sigla], _ = Dependencia.objects.get_or_create(sigla=sigla, defaults={"nombre": nombre})
        usuarios = [("admin", Rol.ADMIN, "ALC", True), ("archivista", Rol.ARCHIVISTA, "ARC", False),
                    ("funcionario", Rol.FUNCIONARIO, "FIN", False), ("auditor", Rol.AUDITOR, "SEG", False)]
        for nombre, rol, dep, su in usuarios:
            u, creado = User.objects.get_or_create(username=nombre, defaults={"is_staff": su, "is_superuser": su})
            if creado:
                u.set_password("Archiva-2026!")
                u.save()
            Perfil.objects.get_or_create(user=u, defaults={"rol": rol, "dependencia": deps[dep]})
        est, _ = Estante.objects.get_or_create(codigo="E-01", defaults={"ubicacion": "Sala de archivo, piso 1"})
        caja, _ = Caja.objects.get_or_create(estante=est, codigo="C-01")
        carp, _ = Carpeta.objects.get_or_create(caja=caja, codigo="F-01")
        exp = Expediente.objects.filter(asunto="Proceso de contratación 2026").first() or Expediente.objects.create(
            asunto="Proceso de contratación 2026", dependencia=deps["FIN"], serie="Contrataciones", carpeta=carp)
        # expediente cerrado hace años → pendiente de transferencia (demuestra RF-012)
        if not Expediente.objects.filter(asunto="Convenio interinstitucional 2012").exists():
            Expediente.objects.create(asunto="Convenio interinstitucional 2012", dependencia=deps["SEG"], estado="CERRADO",
                                      fecha_apertura=timezone.localdate() - timedelta(days=4000),
                                      fecha_cierre=timezone.localdate() - timedelta(days=3500), anios_gestion=2)
        for (titulo, tipo), texto in TEXTOS.items():
            if Documento.objects.filter(titulo=titulo).exists():
                continue
            d = Documento(titulo=titulo, tipo="OTRO", dependencia=deps["FIN"], expediente=exp, nivel_acceso="PUBLICO")
            d.archivo.save("demo.txt", ContentFile(texto.encode()), save=False)
            d.archivo.open("rb")
            d.sha256 = sha256_subido(d.archivo)
            d.texto = texto
            analizar_documento(d)
            d.save()
        self.stdout.write(self.style.SUCCESS("Datos de demostración listos. Usuarios: admin, archivista, funcionario, auditor — clave: Archiva-2026!"))
