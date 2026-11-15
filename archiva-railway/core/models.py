"""Modelos de ARCHIVA. Cada bloque indica el requisito funcional (RF) que cubre."""
import hashlib
import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.urls import reverse
from django.utils import timezone


class Rol(models.TextChoices):
    ADMIN = "ADMIN", "Administrador"
    ARCHIVISTA = "ARCHIVISTA", "Archivista"
    FUNCIONARIO = "FUNCIONARIO", "Funcionario"
    AUDITOR = "AUDITOR", "Auditor (solo lectura)"


class Nivel(models.TextChoices):
    PUBLICO = "PUBLICO", "Público institucional"
    DEPENDENCIA = "DEPENDENCIA", "Solo mi dependencia"
    RESERVADO = "RESERVADO", "Reservado"


class CodigoAutomatico(models.Model):
    """Genera códigos legibles tipo PREFIJO-AAAA-000123 tras el primer guardado."""
    PREFIJO = "REG"
    codigo = models.CharField(max_length=40, unique=True, blank=True, editable=False)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        nuevo = self.pk is None and not self.codigo
        if nuevo:
            self.codigo = f"tmp-{uuid.uuid4().hex}"
        super().save(*args, **kwargs)
        if nuevo:
            self.codigo = f"{self.PREFIJO}-{timezone.now().year}-{self.pk:06d}"
            super().save(update_fields=["codigo"])


# RF-001 / RF-002 — Usuarios y organización -----------------------------------
class Dependencia(models.Model):
    nombre = models.CharField(max_length=150)
    sigla = models.CharField(max_length=15, unique=True)
    padre = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="hijas")
    activa = models.BooleanField(default=True)

    class Meta:
        ordering = ["sigla"]
        verbose_name_plural = "dependencias"

    def __str__(self):
        return f"{self.sigla} · {self.nombre}"


class Perfil(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="perfil")
    dependencia = models.ForeignKey(Dependencia, null=True, blank=True, on_delete=models.PROTECT)
    rol = models.CharField(max_length=12, choices=Rol.choices, default=Rol.FUNCIONARIO)

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.get_rol_display()})"


# RF-007 — Archivo físico ------------------------------------------------------
class Estante(models.Model):
    codigo = models.CharField(max_length=20, unique=True)
    ubicacion = models.CharField("Ubicación (sala/piso)", max_length=150)

    def __str__(self):
        return self.codigo


class Caja(models.Model):
    estante = models.ForeignKey(Estante, on_delete=models.PROTECT, related_name="cajas")
    codigo = models.CharField(max_length=20)

    class Meta:
        unique_together = ("estante", "codigo")

    def __str__(self):
        return f"{self.estante.codigo}/{self.codigo}"


class Carpeta(models.Model):
    caja = models.ForeignKey(Caja, on_delete=models.PROTECT, related_name="carpetas")
    codigo = models.CharField(max_length=20)

    class Meta:
        unique_together = ("caja", "codigo")

    def __str__(self):
        return f"{self.caja}/{self.codigo}"

    @property
    def etiqueta(self):
        return str(self)


# RF-006 / RF-012 — Expedientes y conservación --------------------------------
class Fase(models.TextChoices):
    GESTION = "GESTION", "Archivo de gestión"
    CENTRAL = "CENTRAL", "Archivo central"
    HISTORICO = "HISTORICO", "Archivo histórico"


class Expediente(CodigoAutomatico):
    PREFIJO = "EXP"
    asunto = models.CharField(max_length=255)
    dependencia = models.ForeignKey(Dependencia, on_delete=models.PROTECT)
    serie = models.CharField("Serie documental", max_length=100, blank=True)
    estado = models.CharField(max_length=10, choices=[("ABIERTO", "Abierto"), ("CERRADO", "Cerrado")], default="ABIERTO")
    fase = models.CharField(max_length=10, choices=Fase.choices, default=Fase.GESTION)
    fecha_apertura = models.DateField(default=timezone.localdate)
    fecha_cierre = models.DateField(null=True, blank=True)
    anios_gestion = models.PositiveSmallIntegerField("Años en archivo de gestión", default=2)
    anios_central = models.PositiveSmallIntegerField("Años en archivo central", default=8)
    carpeta = models.ForeignKey(Carpeta, null=True, blank=True, on_delete=models.SET_NULL, related_name="expedientes")
    creado_por = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-fecha_apertura", "-id"]

    def __str__(self):
        return f"{self.codigo} · {self.asunto}"

    def get_absolute_url(self):
        return reverse("expediente_detalle", args=[self.pk])

    @property
    def vence_gestion(self):
        base = self.fecha_cierre
        return base + timedelta(days=365 * self.anios_gestion) if base else None

    @property
    def vence_central(self):
        v = self.vence_gestion
        return v + timedelta(days=365 * self.anios_central) if v else None

    @property
    def transferencia_pendiente(self):
        hoy = timezone.localdate()
        if self.estado != "CERRADO":
            return None
        if self.fase == Fase.GESTION and self.vence_gestion and self.vence_gestion <= hoy:
            return Fase.CENTRAL
        if self.fase == Fase.CENTRAL and self.vence_central and self.vence_central <= hoy:
            return Fase.HISTORICO
        return None


class Transferencia(models.Model):
    expediente = models.ForeignKey(Expediente, on_delete=models.CASCADE, related_name="transferencias")
    desde = models.CharField(max_length=10, choices=Fase.choices)
    hacia = models.CharField(max_length=10, choices=Fase.choices)
    fecha = models.DateTimeField(default=timezone.now)
    responsable = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    observacion = models.CharField(max_length=255, blank=True)


# RF-003 / RF-008 / RF-009 / RF-010 — Documentos ---------------------------------
def ruta_documento(instance, filename):
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    return f"{timezone.now():%Y/%m}/{uuid.uuid4().hex}{ext}"


class Documento(CodigoAutomatico):
    PREFIJO = "DOC"
    TIPOS = [("RESOLUCION", "Resolución"), ("CONTRATO", "Contrato"), ("INFORME", "Informe"),
             ("NOTA", "Nota / Oficio"), ("FINANCIERO", "Financiero"), ("PLANILLA", "Planilla"),
             ("OTRO", "Otro")]
    titulo = models.CharField(max_length=255)
    tipo = models.CharField(max_length=12, choices=TIPOS, default="OTRO")
    dependencia = models.ForeignKey(Dependencia, on_delete=models.PROTECT)
    expediente = models.ForeignKey(Expediente, null=True, blank=True, on_delete=models.SET_NULL, related_name="documentos")
    nivel_acceso = models.CharField(max_length=12, choices=Nivel.choices, default=Nivel.DEPENDENCIA)
    archivo = models.FileField(upload_to=ruta_documento, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    version = models.PositiveIntegerField(default=1)
    texto = models.TextField("Texto extraído (OCR)", blank=True)
    # Resultados de la IA — siempre requieren revisión humana
    tipo_sugerido = models.CharField(max_length=12, blank=True)
    resumen = models.TextField(blank=True)
    metadatos = models.JSONField(default=dict, blank=True)
    ia_revisado = models.BooleanField("Sugerencia IA revisada", default=False)
    creado_por = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-creado"]

    def __str__(self):
        return f"{self.codigo} · {self.titulo}"

    def get_absolute_url(self):
        return reverse("documento_detalle", args=[self.pk])

    def verificar_integridad(self):
        """RF-013: recalcula SHA-256 del archivo y lo compara con el registrado."""
        if not self.archivo:
            return None
        from .services import sha256_archivo
        try:
            return sha256_archivo(self.archivo.path) == self.sha256
        except FileNotFoundError:
            return False


class VersionDocumento(models.Model):
    documento = models.ForeignKey(Documento, on_delete=models.CASCADE, related_name="versiones")
    numero = models.PositiveIntegerField()
    archivo = models.FileField(upload_to=ruta_documento)
    sha256 = models.CharField(max_length=64)
    fecha = models.DateTimeField(default=timezone.now)
    autor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-numero"]


# RF-004 — Correspondencia -------------------------------------------------------
class Correspondencia(CodigoAutomatico):
    PREFIJO = "COR"
    tipo = models.CharField(max_length=7, choices=[("ENTRADA", "Recepción"), ("SALIDA", "Emisión")])
    remitente = models.CharField(max_length=150)
    destinatario = models.CharField(max_length=150)
    asunto = models.CharField(max_length=255)
    dependencia = models.ForeignKey(Dependencia, on_delete=models.PROTECT)
    documento = models.ForeignKey(Documento, null=True, blank=True, on_delete=models.SET_NULL)
    fecha = models.DateTimeField(default=timezone.now)
    registrado_por = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-fecha"]
        verbose_name_plural = "correspondencia"


# RF-005 — Trámites y derivaciones --------------------------------------------------
class Tramite(CodigoAutomatico):
    PREFIJO = "TRA"
    asunto = models.CharField(max_length=255)
    solicitante = models.CharField(max_length=150)
    correspondencia = models.ForeignKey(Correspondencia, null=True, blank=True, on_delete=models.SET_NULL)
    expediente = models.ForeignKey(Expediente, null=True, blank=True, on_delete=models.SET_NULL)
    dependencia_actual = models.ForeignKey(Dependencia, on_delete=models.PROTECT, related_name="tramites_actuales")
    estado = models.CharField(max_length=10, choices=[("EN_CURSO", "En curso"), ("CONCLUIDO", "Concluido")], default="EN_CURSO")
    creado = models.DateTimeField(auto_now_add=True)
    creado_por = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-creado"]


class Derivacion(models.Model):
    tramite = models.ForeignKey(Tramite, on_delete=models.CASCADE, related_name="derivaciones")
    desde = models.ForeignKey(Dependencia, on_delete=models.PROTECT, related_name="+")
    hacia = models.ForeignKey(Dependencia, on_delete=models.PROTECT, related_name="+")
    instruccion = models.CharField(max_length=255, blank=True)
    fecha = models.DateTimeField(default=timezone.now)
    recibido = models.DateTimeField(null=True, blank=True)
    derivado_por = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["fecha"]


# Control de préstamos / movimiento de expedientes físicos ----------------------------
class Prestamo(models.Model):
    expediente = models.ForeignKey(Expediente, on_delete=models.PROTECT, related_name="prestamos")
    solicitante = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    fecha_salida = models.DateTimeField(default=timezone.now)
    fecha_devolucion_prevista = models.DateField()
    fecha_devolucion = models.DateTimeField(null=True, blank=True)
    autorizado_por = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-fecha_salida"]

    @property
    def vencido(self):
        return self.fecha_devolucion is None and self.fecha_devolucion_prevista < timezone.localdate()


# RF-015 — Tokens de API ----------------------------------------------------------------
class ApiToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="tokens")
    nombre = models.CharField(max_length=80)
    hash = models.CharField(max_length=64, unique=True)  # solo se guarda el hash
    creado = models.DateTimeField(auto_now_add=True)
    activo = models.BooleanField(default=True)

    @staticmethod
    def hash_de(valor):
        return hashlib.sha256(valor.encode()).hexdigest()

    @classmethod
    def emitir(cls, user, nombre):
        valor = "arch_" + uuid.uuid4().hex + uuid.uuid4().hex
        cls.objects.create(user=user, nombre=nombre, hash=cls.hash_de(valor))
        return valor  # se muestra una sola vez


# RF-013 — Auditoría con cadena de hashes ---------------------------------------------------
class Auditoria(models.Model):
    fecha = models.DateTimeField(default=timezone.now)
    usuario = models.CharField(max_length=150, blank=True)
    accion = models.CharField(max_length=40)
    objeto = models.CharField(max_length=120, blank=True)
    detalle = models.CharField(max_length=500, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    hash_previo = models.CharField(max_length=64, blank=True)
    hash = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["id"]
        verbose_name_plural = "auditoría"

    def _calcular(self):
        carga = json.dumps([self.hash_previo, self.fecha.isoformat(), self.usuario, self.accion,
                            self.objeto, self.detalle, self.ip], ensure_ascii=False)
        return hashlib.sha256(carga.encode()).hexdigest()

    @classmethod
    def registrar(cls, request_o_user, accion, objeto="", detalle=""):
        user, ip = request_o_user, None
        if hasattr(request_o_user, "user"):
            user = request_o_user.user
            ip = request_o_user.META.get("REMOTE_ADDR")
        nombre = getattr(user, "username", "") if user and getattr(user, "is_authenticated", False) else (user if isinstance(user, str) else "")
        ultimo = cls.objects.order_by("-id").first()
        reg = cls(usuario=nombre, accion=accion, objeto=str(objeto)[:120], detalle=str(detalle)[:500],
                  ip=ip, hash_previo=ultimo.hash if ultimo else "")
        reg.hash = reg._calcular()
        reg.save()
        return reg

    @classmethod
    def verificar_cadena(cls):
        """Devuelve (ok, id_primer_registro_alterado)."""
        previo = ""
        for r in cls.objects.order_by("id"):
            if r.hash_previo != previo or r._calcular() != r.hash:
                return False, r.id
            previo = r.hash
        return True, None

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Los registros de auditoría son inmutables.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Los registros de auditoría no se pueden eliminar.")
