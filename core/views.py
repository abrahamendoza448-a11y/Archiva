import csv
import io

import qrcode
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from . import forms as F
from .models import (ApiToken, Auditoria, Caja, Carpeta, Correspondencia, Dependencia, Derivacion, Documento,
                     Estante, Expediente, Fase, Prestamo, Rol, Tramite, Transferencia, VersionDocumento)
from .services import (analizar_documento, buscar, dependencia_de, documentos_visibles, es_archivista,
                       extraer_texto, puede_escribir, responder_consulta, rol_de, rol_requerido, sha256_archivo,
                       sha256_subido)


# --- Autenticación con bloqueo por intentos fallidos (RF-001 / seguridad) -------
class LoginView(auth_views.LoginView):
    template_name = "registration/login.html"

    def _clave(self, request):
        return f"login:{request.META.get('REMOTE_ADDR')}:{request.POST.get('username', '').lower()}"

    def post(self, request, *a, **kw):
        if cache.get(self._clave(request), 0) >= settings.INTENTOS_LOGIN_MAX:
            Auditoria.registrar(request.POST.get("username", ""), "LOGIN_BLOQUEADO")
            messages.error(request, f"Cuenta bloqueada {settings.BLOQUEO_LOGIN_MIN} minutos por intentos fallidos.")
            return self.get(request, *a, **kw)
        return super().post(request, *a, **kw)

    def form_valid(self, form):
        cache.delete(self._clave(self.request))
        resp = super().form_valid(form)
        Auditoria.registrar(self.request, "LOGIN", form.get_user().username)
        return resp

    def form_invalid(self, form):
        k = self._clave(self.request)
        cache.set(k, cache.get(k, 0) + 1, settings.BLOQUEO_LOGIN_MIN * 60)
        Auditoria.registrar(self.request.POST.get("username", ""), "LOGIN_FALLIDO")
        return super().form_invalid(form)


# --- Filtros de visibilidad -----------------------------------------------------------
def expedientes_visibles(user):
    qs = Expediente.objects.select_related("dependencia", "carpeta__caja__estante")
    if rol_de(user) in (Rol.ADMIN, Rol.ARCHIVISTA, Rol.AUDITOR):
        return qs
    dep = dependencia_de(user)
    return qs.filter(dependencia=dep) if dep else qs.none()


def tramites_visibles(user):
    qs = Tramite.objects.select_related("dependencia_actual")
    if rol_de(user) in (Rol.ADMIN, Rol.ARCHIVISTA, Rol.AUDITOR):
        return qs
    dep = dependencia_de(user)
    return qs.filter(Q(dependencia_actual=dep) | Q(derivaciones__desde=dep) | Q(derivaciones__hacia=dep)).distinct() if dep else qs.none()


def exigir_escritura(request):
    if not puede_escribir(request.user):
        raise PermissionDenied


# --- Dashboard ----------------------------------------------------------------------------
@login_required
def dashboard(request):
    docs = documentos_visibles(request.user)
    exps = expedientes_visibles(request.user)
    ctx = {
        "n_docs": docs.count(), "n_exps": exps.count(),
        "n_tramites": tramites_visibles(request.user).filter(estado="EN_CURSO").count(),
        "por_tipo": docs.values("tipo").annotate(n=Count("id")).order_by("-n"),
        "pendientes_transferencia": [e for e in exps.filter(estado="CERRADO") if e.transferencia_pendiente],
        "prestamos_vencidos": [p for p in Prestamo.objects.filter(fecha_devolucion__isnull=True) if p.vencido],
        "ia_pendientes": docs.filter(ia_revisado=False).exclude(tipo_sugerido="").count(),
        "recepciones": Derivacion.objects.filter(recibido__isnull=True, hacia=dependencia_de(request.user)).count()
        if dependencia_de(request.user) else 0,
        "n_digitalizados": docs.exclude(archivo="").count(),
        "n_prestamos": Prestamo.objects.filter(fecha_devolucion__isnull=True).count(),
    }
    ctx["n_solo_fisico"] = ctx["n_docs"] - ctx["n_digitalizados"]
    ctx["pct_digital"] = round(100 * ctx["n_digitalizados"] / ctx["n_docs"]) if ctx["n_docs"] else 0
    # Documentos por mes (últimos 6 meses) y por dependencia — gráficos del panel de escritorio
    hoy = timezone.localdate().replace(day=1)
    meses = []
    for i in range(5, -1, -1):
        y, m = hoy.year, hoy.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        meses.append((y, m))
    conteos = {k: 0 for k in meses}
    for d in docs.values_list("creado", flat=True):
        d = timezone.localtime(d)
        if (d.year, d.month) in conteos:
            conteos[(d.year, d.month)] += 1
    mx = max(conteos.values()) or 1
    nombres = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    ctx["por_mes"] = [{"label": nombres[m], "n": n, "h": max(4, round(100 * n / mx)) if n else 2} for (y, m), n in conteos.items()]
    pd = list(docs.values("dependencia__sigla").annotate(n=Count("id")).order_by("-n")[:6])
    mxd = pd[0]["n"] if pd else 1
    ctx["por_dep"] = [{"sigla": r["dependencia__sigla"], "n": r["n"], "w": round(100 * r["n"] / mxd)} for r in pd]
    ctx["tramites_estado"] = tramites_visibles(request.user).values("estado").annotate(n=Count("id"))
    ctx["actividad"] = Auditoria.objects.exclude(accion__in=["DOC_VER", "BUSCAR", "LOGIN_FALLIDO"]).order_by("-id")[:6] \
        if rol_de(request.user) in (Rol.ADMIN, Rol.AUDITOR) else []
    return render(request, "core/dashboard.html", ctx)


# --- Documentos (RF-003, 008, 009, 010) -------------------------------------------------------
@login_required
def documentos(request):
    qs = documentos_visibles(request.user)
    if t := request.GET.get("tipo"):
        qs = qs.filter(tipo=t)
    return render(request, "core/documentos.html", {"docs": qs[:200], "tipos": Documento.TIPOS})


@login_required
def documento_nuevo(request):
    exigir_escritura(request)
    form = F.DocumentoForm(request.POST or None, request.FILES or None)
    if rol_de(request.user) != Rol.ADMIN and dependencia_de(request.user) and request.method == "GET":
        form.fields["dependencia"].initial = dependencia_de(request.user)
    if request.method == "POST" and form.is_valid():
        # Un funcionario solo puede registrar documentos en su propia dependencia
        if rol_de(request.user) == Rol.FUNCIONARIO and form.cleaned_data["dependencia"] != dependencia_de(request.user):
            raise PermissionDenied
        with transaction.atomic():
            doc = form.save(commit=False)
            doc.creado_por = request.user
            if doc.archivo:
                doc.sha256 = sha256_subido(doc.archivo)
            doc.save()
            if doc.archivo:
                doc.texto = extraer_texto(doc.archivo.path)
                analizar_documento(doc)
                doc.save()
        Auditoria.registrar(request, "DOC_CREAR", doc.codigo, f"sha256={doc.sha256[:16]}")
        messages.success(request, f"Documento {doc.codigo} registrado.")
        return redirect(doc)
    return render(request, "core/form.html", {"form": form, "titulo": "Registrar documento", "multipart": True})


@login_required
def documento_detalle(request, pk):
    doc = get_object_or_404(documentos_visibles(request.user), pk=pk)
    Auditoria.registrar(request, "DOC_VER", doc.codigo)
    return render(request, "core/documento_detalle.html", {
        "doc": doc, "integro": doc.verificar_integridad(), "puede_editar": puede_escribir(request.user),
        "form_version": F.NuevaVersionForm(), "tipo_vista": tipo_de_vista(doc.archivo.name if doc.archivo else "")})


@login_required
def documento_descargar(request, pk, version=None):
    doc = get_object_or_404(documentos_visibles(request.user), pk=pk)
    if version:
        v = get_object_or_404(doc.versiones, numero=version)
        archivo, nombre = v.archivo, f"{doc.codigo}-v{v.numero}"
    else:
        archivo, nombre = doc.archivo, doc.codigo
    if not archivo:
        raise Http404
    Auditoria.registrar(request, "DOC_DESCARGAR", doc.codigo, f"v{version or doc.version}")
    ext = archivo.name.rsplit(".", 1)[-1]
    return FileResponse(archivo.open("rb"), as_attachment=True, filename=f"{nombre}.{ext}")


# Vista previa en el navegador: solo tipos que el navegador muestra sin ejecutar código.
TIPOS_VISTA = {"pdf": ("application/pdf", "pdf"), "png": ("image/png", "imagen"), "jpg": ("image/jpeg", "imagen"),
               "jpeg": ("image/jpeg", "imagen"), "txt": ("text/plain; charset=utf-8", "texto")}


def tipo_de_vista(nombre):
    """'pdf', 'imagen', 'texto' o None si el navegador no puede mostrarlo (p. ej. TIFF)."""
    ext = nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""
    return TIPOS_VISTA.get(ext, (None, None))[1]


@login_required
@xframe_options_sameorigin
def documento_ver(request, pk, version=None):
    """Muestra el archivo en el navegador (sin descargarlo). Mismos permisos que la descarga."""
    doc = get_object_or_404(documentos_visibles(request.user), pk=pk)
    if version:
        v = get_object_or_404(doc.versiones, numero=version)
        archivo, nombre = v.archivo, f"{doc.codigo}-v{v.numero}"
    else:
        archivo, nombre = doc.archivo, doc.codigo
    if not archivo:
        raise Http404
    ext = archivo.name.rsplit(".", 1)[-1].lower()
    if ext not in TIPOS_VISTA:
        raise Http404  # formatos sin vista previa (TIFF): usar Descargar
    Auditoria.registrar(request, "DOC_VISUALIZAR", doc.codigo, f"v{version or doc.version}")
    resp = FileResponse(archivo.open("rb"), as_attachment=False, filename=f"{nombre}.{ext}",
                        content_type=TIPOS_VISTA[ext][0])
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@require_POST
@login_required
def documento_version(request, pk):
    exigir_escritura(request)
    doc = get_object_or_404(documentos_visibles(request.user), pk=pk)
    form = F.NuevaVersionForm(request.POST, request.FILES)
    if form.is_valid():
        with transaction.atomic():
            # Se archiva la versión vigente y se reemplaza; nada se pierde.
            if doc.archivo:
                VersionDocumento.objects.create(documento=doc, numero=doc.version, archivo=doc.archivo.name,
                                                sha256=doc.sha256, autor=doc.creado_por)
            nuevo = form.cleaned_data["archivo"]
            doc.version += 1
            doc.sha256 = sha256_subido(nuevo)
            doc.archivo = nuevo
            doc.save()
            doc.texto = extraer_texto(doc.archivo.path)
            analizar_documento(doc)
            doc.save()
        Auditoria.registrar(request, "DOC_VERSION", doc.codigo, f"v{doc.version}")
        messages.success(request, f"Versión {doc.version} guardada.")
    else:
        messages.error(request, "; ".join(sum(form.errors.values(), [])))
    return redirect(doc)


@require_POST
@login_required
def documento_revisar_ia(request, pk):
    exigir_escritura(request)
    doc = get_object_or_404(documentos_visibles(request.user), pk=pk)
    aceptar = request.POST.get("accion") == "aceptar"
    if aceptar and doc.tipo_sugerido:
        doc.tipo = doc.tipo_sugerido
    doc.ia_revisado = True
    doc.save()
    Auditoria.registrar(request, "IA_REVISION", doc.codigo, "aceptada" if aceptar else "rechazada")
    return redirect(doc)


# --- Expedientes (RF-006) y conservación (RF-012) -------------------------------------------------
@login_required
def expedientes(request):
    return render(request, "core/expedientes.html", {"exps": expedientes_visibles(request.user)[:200]})


@login_required
def expediente_nuevo(request):
    exigir_escritura(request)
    form = F.ExpedienteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        e = form.save(commit=False)
        e.creado_por = request.user
        e.save()
        Auditoria.registrar(request, "EXP_CREAR", e.codigo)
        return redirect(e)
    return render(request, "core/form.html", {"form": form, "titulo": "Abrir expediente"})


@login_required
def expediente_detalle(request, pk):
    e = get_object_or_404(expedientes_visibles(request.user), pk=pk)
    docs = documentos_visibles(request.user).filter(expediente=e)
    return render(request, "core/expediente_detalle.html", {
        "e": e, "docs": docs, "ocultos": e.documentos.count() - docs.count(),
        "archivista": es_archivista(request.user)})


@require_POST
@login_required
@rol_requerido(Rol.ADMIN, Rol.ARCHIVISTA)
def expediente_cerrar(request, pk):
    e = get_object_or_404(Expediente, pk=pk)
    e.estado, e.fecha_cierre = "CERRADO", timezone.localdate()
    e.save()
    Auditoria.registrar(request, "EXP_CERRAR", e.codigo)
    return redirect(e)


@require_POST
@login_required
@rol_requerido(Rol.ADMIN, Rol.ARCHIVISTA)
def expediente_transferir(request, pk):
    e = get_object_or_404(Expediente, pk=pk)
    destino = e.transferencia_pendiente
    if destino:
        Transferencia.objects.create(expediente=e, desde=e.fase, hacia=destino, responsable=request.user)
        e.fase = destino
        e.save()
        Auditoria.registrar(request, "EXP_TRANSFERIR", e.codigo, f"-> {destino}")
        messages.success(request, f"Expediente transferido a {e.get_fase_display()}.")
    else:
        messages.error(request, "El expediente aún no cumple el plazo de transferencia.")
    return redirect(e)


# --- Correspondencia (RF-004) ----------------------------------------------------------------------------
@login_required
def correspondencia(request):
    qs = Correspondencia.objects.select_related("dependencia")
    if rol_de(request.user) == Rol.FUNCIONARIO:
        qs = qs.filter(dependencia=dependencia_de(request.user))
    return render(request, "core/correspondencia.html", {"items": qs[:200]})


@login_required
def correspondencia_nueva(request):
    exigir_escritura(request)
    form = F.CorrespondenciaForm(request.POST or None)
    form.fields["documento"].queryset = documentos_visibles(request.user)
    if request.method == "POST" and form.is_valid():
        c = form.save(commit=False)
        c.registrado_por = request.user
        c.save()
        Auditoria.registrar(request, "COR_REGISTRAR", c.codigo, c.get_tipo_display())
        messages.success(request, f"Registrada con código {c.codigo}.")
        return redirect("correspondencia")
    return render(request, "core/form.html", {"form": form, "titulo": "Registrar correspondencia"})


# --- Trámites (RF-005) ---------------------------------------------------------------------------------------
@login_required
def tramites(request):
    return render(request, "core/tramites.html", {"items": tramites_visibles(request.user)[:200]})


@login_required
def tramite_nuevo(request):
    exigir_escritura(request)
    form = F.TramiteForm(request.POST or None)
    form.fields["expediente"].queryset = expedientes_visibles(request.user)
    if request.method == "POST" and form.is_valid():
        t = form.save(commit=False)
        t.creado_por = request.user
        t.save()
        Auditoria.registrar(request, "TRA_CREAR", t.codigo)
        return redirect("tramite_detalle", pk=t.pk)
    return render(request, "core/form.html", {"form": form, "titulo": "Iniciar trámite"})


@login_required
def tramite_detalle(request, pk):
    t = get_object_or_404(tramites_visibles(request.user), pk=pk)
    form = F.DerivacionForm()
    return render(request, "core/tramite_detalle.html", {"t": t, "form": form, "puede": puede_escribir(request.user)})


@require_POST
@login_required
def tramite_derivar(request, pk):
    exigir_escritura(request)
    t = get_object_or_404(tramites_visibles(request.user), pk=pk, estado="EN_CURSO")
    form = F.DerivacionForm(request.POST)
    if form.is_valid():
        d = form.save(commit=False)
        d.tramite, d.desde, d.derivado_por = t, t.dependencia_actual, request.user
        d.save()
        Auditoria.registrar(request, "TRA_DERIVAR", t.codigo, f"{d.desde.sigla}->{d.hacia.sigla}")
    return redirect("tramite_detalle", pk=t.pk)


@require_POST
@login_required
def tramite_recibir(request, pk):
    exigir_escritura(request)
    t = get_object_or_404(tramites_visibles(request.user), pk=pk)
    d = t.derivaciones.filter(recibido__isnull=True).last()
    if d and (rol_de(request.user) in (Rol.ADMIN, Rol.ARCHIVISTA) or d.hacia == dependencia_de(request.user)):
        d.recibido = timezone.now()
        d.save()
        t.dependencia_actual = d.hacia
        t.save()
        Auditoria.registrar(request, "TRA_RECIBIR", t.codigo, d.hacia.sigla)
    else:
        raise PermissionDenied
    return redirect("tramite_detalle", pk=t.pk)


@require_POST
@login_required
def tramite_concluir(request, pk):
    exigir_escritura(request)
    t = get_object_or_404(tramites_visibles(request.user), pk=pk)
    t.estado = "CONCLUIDO"
    t.save()
    Auditoria.registrar(request, "TRA_CONCLUIR", t.codigo)
    return redirect("tramite_detalle", pk=t.pk)


# --- Archivo físico (RF-007), QR y préstamos ----------------------------------------------------------------------
@login_required
def archivo_fisico(request):
    estantes = Estante.objects.prefetch_related("cajas__carpetas__expedientes")
    return render(request, "core/archivo_fisico.html", {"estantes": estantes, "archivista": es_archivista(request.user),
                                                         "prestamos": Prestamo.objects.filter(fecha_devolucion__isnull=True)})


@login_required
@rol_requerido(Rol.ADMIN, Rol.ARCHIVISTA)
def ubicacion_nueva(request, tipo):
    clases = {"estante": F.EstanteForm, "caja": F.CajaForm, "carpeta": F.CarpetaForm}
    if tipo not in clases:
        raise Http404
    form = clases[tipo](request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        Auditoria.registrar(request, "UBICACION_CREAR", f"{tipo}:{obj}")
        return redirect("archivo_fisico")
    return render(request, "core/form.html", {"form": form, "titulo": f"Nuevo/a {tipo}"})


@login_required
def qr_expediente(request, pk):
    """Etiqueta QR que apunta al expediente (se imprime y se pega en la carpeta física)."""
    e = get_object_or_404(expedientes_visibles(request.user), pk=pk)
    img = qrcode.make(request.build_absolute_uri(e.get_absolute_url()))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return HttpResponse(buf.getvalue(), content_type="image/png")


@login_required
@rol_requerido(Rol.ADMIN, Rol.ARCHIVISTA)
def prestamo_nuevo(request):
    form = F.PrestamoForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        p = form.save(commit=False)
        if Prestamo.objects.filter(expediente=p.expediente, fecha_devolucion__isnull=True).exists():
            messages.error(request, "El expediente ya está prestado.")
        else:
            p.autorizado_por = request.user
            p.save()
            Auditoria.registrar(request, "PRESTAMO", p.expediente.codigo, f"a {p.solicitante.username}")
            return redirect("archivo_fisico")
    return render(request, "core/form.html", {"form": form, "titulo": "Registrar préstamo de expediente"})


@require_POST
@login_required
@rol_requerido(Rol.ADMIN, Rol.ARCHIVISTA)
def prestamo_devolver(request, pk):
    p = get_object_or_404(Prestamo, pk=pk, fecha_devolucion__isnull=True)
    p.fecha_devolucion = timezone.now()
    p.save()
    Auditoria.registrar(request, "DEVOLUCION", p.expediente.codigo)
    return redirect("archivo_fisico")


# --- Búsqueda (RF-011) + asistente IA ------------------------------------------------------------------------------------
@login_required
def busqueda(request):
    q = request.GET.get("q", "").strip()
    ctx = {"q": q, "tipos": Documento.TIPOS, "dependencias": Dependencia.objects.all()}
    if q or request.GET.get("tipo") or request.GET.get("dependencia"):
        ctx["resultados"] = buscar(request.user, q, request.GET.get("tipo", ""), request.GET.get("dependencia", ""))
        Auditoria.registrar(request, "BUSCAR", q[:80])
    if request.GET.get("asistente") and q:
        ctx["respuestas"] = responder_consulta(request.user, q)
    return render(request, "core/busqueda.html", ctx)


# --- Reportes (RF-014) -------------------------------------------------------------------------------------------------------------
@login_required
def reportes(request):
    exps = expedientes_visibles(request.user)
    ctx = {
        "por_dependencia": documentos_visibles(request.user).values("dependencia__sigla").annotate(n=Count("id")).order_by("-n"),
        "por_fase": exps.values("fase").annotate(n=Count("id")),
        "por_estado_tramite": tramites_visibles(request.user).values("estado").annotate(n=Count("id")),
    }
    return render(request, "core/reportes.html", ctx)


@login_required
def reporte_inventario_csv(request):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="inventario_documental.csv"'
    resp.write("﻿")
    w = csv.writer(resp)
    w.writerow(["Código", "Título", "Tipo", "Dependencia", "Expediente", "Nivel", "Versión", "SHA-256", "Creado"])
    for d in documentos_visibles(request.user):
        w.writerow([d.codigo, d.titulo, d.get_tipo_display(), d.dependencia.sigla,
                    d.expediente.codigo if d.expediente else "", d.get_nivel_acceso_display(),
                    d.version, d.sha256, d.creado.strftime("%Y-%m-%d %H:%M")])
    Auditoria.registrar(request, "EXPORTAR_INVENTARIO")
    return resp


# --- Auditoría (RF-013) -----------------------------------------------------------------------------------------------------------------
@login_required
@rol_requerido(Rol.ADMIN, Rol.AUDITOR)
def auditoria(request):
    ok, roto = Auditoria.verificar_cadena()
    return render(request, "core/auditoria.html", {"registros": Auditoria.objects.order_by("-id")[:300],
                                                    "cadena_ok": ok, "registro_roto": roto})


@login_required
@rol_requerido(Rol.ADMIN, Rol.AUDITOR, Rol.ARCHIVISTA)
def verificar_integridad(request):
    """Recalcula SHA-256 de todos los archivos y reporta alteraciones o faltantes."""
    problemas = []
    total = 0
    for d in Documento.objects.exclude(archivo=""):
        total += 1
        if not d.verificar_integridad():
            problemas.append(d)
    Auditoria.registrar(request, "VERIFICAR_INTEGRIDAD", detalle=f"{total} revisados, {len(problemas)} con problemas")
    return render(request, "core/integridad.html", {"total": total, "problemas": problemas})


# --- API (RF-015) — autenticación por token Bearer o sesión -------------------------------------------------------------------------
def _usuario_api(request):
    if request.user.is_authenticated:
        return request.user
    cab = request.headers.get("Authorization", "")
    if cab.startswith("Bearer "):
        tok = ApiToken.objects.filter(hash=ApiToken.hash_de(cab[7:]), activo=True).select_related("user").first()
        if tok and tok.user.is_active:
            return tok.user
    return None


def _json_doc(d):
    return {"codigo": d.codigo, "titulo": d.titulo, "tipo": d.tipo, "dependencia": d.dependencia.sigla,
            "expediente": d.expediente.codigo if d.expediente else None, "sha256": d.sha256,
            "version": d.version, "resumen": d.resumen, "creado": d.creado.isoformat()}


def api_documentos(request):
    user = _usuario_api(request)
    if not user:
        return JsonResponse({"error": "No autenticado"}, status=401)
    Auditoria.registrar(user, "API_DOCUMENTOS")
    return JsonResponse({"resultados": [_json_doc(d) for d in documentos_visibles(user)[:100]]})


def api_buscar(request):
    user = _usuario_api(request)
    if not user:
        return JsonResponse({"error": "No autenticado"}, status=401)
    q = request.GET.get("q", "")
    Auditoria.registrar(user, "API_BUSCAR", q[:80])
    return JsonResponse({"resultados": [{**_json_doc(d), "puntaje": s} for d, s in buscar(user, q, limite=25)]})
