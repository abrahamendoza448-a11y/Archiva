"""Servicios de ARCHIVA: integridad, OCR, IA local, permisos y búsqueda.

Toda la IA es LOCAL (reglas + estadística). Ningún documento sale del servidor,
conforme a la condición de residencia de datos de la arquitectura híbrida.
"""
import hashlib
import io
import math
import re
import unicodedata
from collections import Counter
from functools import wraps

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from .models import Documento, Nivel, Rol


# --- Integridad (SHA-256) -----------------------------------------------------
def sha256_archivo(ruta):
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def sha256_subido(archivo):
    h = hashlib.sha256()
    for bloque in archivo.chunks():
        h.update(bloque)
    archivo.seek(0)
    return h.hexdigest()


# --- Permisos (RBAC por dependencia) -------------------------------------------
def rol_de(user):
    if not user.is_authenticated:
        return None
    if user.is_superuser:
        return Rol.ADMIN
    p = getattr(user, "perfil", None)
    return p.rol if p else None


def dependencia_de(user):
    p = getattr(user, "perfil", None)
    return p.dependencia if p else None


def puede_escribir(user):
    return rol_de(user) in (Rol.ADMIN, Rol.ARCHIVISTA, Rol.FUNCIONARIO)


def es_archivista(user):
    return rol_de(user) in (Rol.ADMIN, Rol.ARCHIVISTA)


def rol_requerido(*roles):
    def deco(vista):
        @wraps(vista)
        def envoltura(request, *a, **kw):
            if rol_de(request.user) not in roles:
                raise PermissionDenied
            return vista(request, *a, **kw)
        return envoltura
    return deco


def documentos_visibles(user):
    """Regla central de acceso. La búsqueda y la IA SIEMPRE pasan por aquí."""
    qs = Documento.objects.select_related("dependencia", "expediente")
    rol = rol_de(user)
    if rol in (Rol.ADMIN, Rol.AUDITOR):
        return qs
    if rol is None:
        return qs.none()
    dep = dependencia_de(user)
    filtro = Q(nivel_acceso=Nivel.PUBLICO)
    if dep:
        filtro |= Q(nivel_acceso__in=[Nivel.DEPENDENCIA, Nivel.RESERVADO], dependencia=dep)
    if rol == Rol.ARCHIVISTA:
        filtro |= Q(nivel_acceso=Nivel.DEPENDENCIA)
    return qs.filter(filtro)


# --- OCR / extracción de texto ---------------------------------------------------
def extraer_texto(ruta):
    """Devuelve texto del archivo. PDF con texto → pypdf; imagen/PDF escaneado → Tesseract si existe."""
    ext = str(ruta).lower().rsplit(".", 1)[-1]
    texto = ""
    try:
        if ext == "txt":
            texto = open(ruta, encoding="utf-8", errors="ignore").read()
        elif ext == "pdf":
            from pypdf import PdfReader
            texto = "\n".join((p.extract_text() or "") for p in PdfReader(ruta).pages)
            if len(texto.strip()) < 20 and settings.OCR_MOTOR == "tesseract":
                texto = _ocr_pdf(ruta)
        elif ext in ("png", "jpg", "jpeg", "tif", "tiff") and settings.OCR_MOTOR == "tesseract":
            texto = _ocr_imagen(ruta)
    except Exception:  # un fallo de OCR nunca debe impedir el registro del documento
        texto = ""
    return texto.strip()


def _ocr_imagen(ruta):
    try:
        import pytesseract
        from PIL import Image
        return pytesseract.image_to_string(Image.open(ruta), lang=settings.OCR_IDIOMA)
    except Exception:
        return ""


def _ocr_pdf(ruta):
    """PDF escaneado: ocrmypdf (si está instalado) o rasterizado + Tesseract."""
    try:
        import ocrmypdf  # noqa: F401
        import tempfile
        from pypdf import PdfReader
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            ocrmypdf.ocr(ruta, tmp.name, language=settings.OCR_IDIOMA, force_ocr=True, progress_bar=False)
            return "\n".join((p.extract_text() or "") for p in PdfReader(tmp.name).pages)
    except Exception:
        return ""


# --- IA documental local ---------------------------------------------------------
def normalizar(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


REGLAS_TIPO = {
    "RESOLUCION": ["resolucion", "resuelve", "considerando", "artículo primero", "articulo primero", "decreto municipal"],
    "CONTRATO": ["contrato", "contratante", "clausula", "cláusula", "las partes", "objeto del contrato"],
    "INFORME": ["informe", "antecedentes", "conclusiones", "recomendaciones", "ref:"],
    "NOTA": ["nota", "oficio", "cite", "de mi mayor consideracion", "me dirijo", "atentamente"],
    "FINANCIERO": ["factura", "nit", "monto", "bs.", "pago", "comprobante", "presupuesto", "desembolso"],
    "PLANILLA": ["planilla", "haber basico", "total ganado", "sueldos", "descuentos"],
}


def clasificar(texto):
    """Clasificador por reglas ponderadas. Devuelve (tipo, confianza 0-1)."""
    t = normalizar(texto)
    puntos = {k: sum(t.count(normalizar(p)) for p in ps) for k, ps in REGLAS_TIPO.items()}
    total = sum(puntos.values())
    if not total:
        return "OTRO", 0.0
    mejor = max(puntos, key=puntos.get)
    return mejor, round(puntos[mejor] / total, 2)


def extraer_metadatos(texto):
    meta = {}
    if m := re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", texto):
        meta["fecha_detectada"] = f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    if m := re.search(r"(?:CITE|N[°ºo.]*)\s*[:.]?\s*([A-Z]{2,}[-/\w.]*\d+[-/\w.]*)", texto):
        meta["numero_documento"] = m[1]
    if m := re.search(r"\bNIT\s*[:.]?\s*(\d{6,15})", texto, re.I):
        meta["nit"] = m[1]
    montos = re.findall(r"Bs\.?\s*([\d.,]+)", texto, re.I)
    if montos:
        meta["montos"] = montos[:5]
    return meta


def resumir(texto, max_chars=400):
    oraciones = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", texto).strip())
    return " ".join(oraciones[:3])[:max_chars]


def analizar_documento(doc):
    """Procesa el texto: tipo sugerido, resumen y metadatos. Marca la sugerencia como NO revisada."""
    if not doc.texto:
        return
    tipo, conf = clasificar(doc.texto)
    doc.tipo_sugerido = tipo
    doc.resumen = resumir(doc.texto)
    doc.metadatos = {**extraer_metadatos(doc.texto), "confianza_tipo": conf}
    doc.ia_revisado = False


# --- Búsqueda -----------------------------------------------------------------------
STOP = set("de la el en y a los las del que un una por con para se su al es lo como mas o sobre sus".split())


def tokens(texto):
    return [w for w in re.findall(r"\w+", normalizar(texto)) if w not in STOP and len(w) > 1]


def buscar(user, consulta, tipo="", dependencia="", limite=50):
    """Búsqueda por contenido con ranking TF-IDF simple, siempre limitada a lo que el usuario puede ver."""
    base = documentos_visibles(user)
    if tipo:
        base = base.filter(tipo=tipo)
    if dependencia:
        base = base.filter(dependencia_id=dependencia)
    q_tokens = tokens(consulta)
    if not q_tokens:
        return [(d, 0) for d in base[:limite]]
    filtro = Q()
    for t in q_tokens:
        filtro |= Q(titulo__icontains=t) | Q(texto__icontains=t) | Q(resumen__icontains=t) | Q(codigo__icontains=t)
    candidatos = list(base.filter(filtro)[:500])
    n = max(len(candidatos), 1)
    df = Counter()
    docs_tokens = {}
    for d in candidatos:
        tk = tokens(f"{d.titulo} {d.titulo} {d.resumen} {d.texto}")
        docs_tokens[d.pk] = Counter(tk)
        for t in set(q_tokens):
            if t in docs_tokens[d.pk]:
                df[t] += 1
    resultados = []
    for d in candidatos:
        c = docs_tokens[d.pk]
        largo = sum(c.values()) or 1
        score = sum((c[t] / largo) * (1 + math.log(n / (df[t] or 1))) for t in q_tokens if t in c)
        if normalizar(consulta) in normalizar(d.titulo):
            score += 1
        resultados.append((d, round(score, 4)))
    resultados.sort(key=lambda x: -x[1])
    return resultados[:limite]


def responder_consulta(user, pregunta):
    """Asistente documental local: devuelve los fragmentos más relevantes ENTRE LOS DOCUMENTOS AUTORIZADOS."""
    hallazgos = []
    qt = tokens(pregunta)
    for doc, score in buscar(user, pregunta, limite=5):
        if score <= 0:
            continue
        frases = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", doc.texto or doc.resumen))
        mejores = sorted(frases, key=lambda f: -sum(t in normalizar(f) for t in qt))[:2]
        hallazgos.append({"documento": doc, "fragmentos": [f[:300] for f in mejores if f]})
    return hallazgos
