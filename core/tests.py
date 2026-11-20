import json
import tempfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import ApiToken, Auditoria, Dependencia, Documento, Expediente, Perfil, Rol
from .services import buscar, clasificar, documentos_visibles

TMP = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=TMP, DOCUMENTOS_ROOT=TMP)
class ArchivaTests(TestCase):
    def setUp(self):
        self.fin = Dependencia.objects.create(nombre="Finanzas", sigla="FIN")
        self.obr = Dependencia.objects.create(nombre="Obras", sigla="OOPP")
        self.func_fin = self._user("ffin", Rol.FUNCIONARIO, self.fin)
        self.func_obr = self._user("fobr", Rol.FUNCIONARIO, self.obr)
        self.archivista = self._user("arch", Rol.ARCHIVISTA, self.fin)
        self.auditor = self._user("aud", Rol.AUDITOR, self.fin)

    def _user(self, name, rol, dep):
        u = User.objects.create_user(name, password="x-Clave-segura-1")
        Perfil.objects.create(user=u, rol=rol, dependencia=dep)
        return u

    def _doc(self, titulo, nivel, dep, texto="contenido de prueba"):
        d = Documento(titulo=titulo, nivel_acceso=nivel, dependencia=dep, texto=texto)
        d.save()
        return d

    def test_codigo_automatico_y_hash_al_subir(self):
        self.client.login(username="ffin", password="x-Clave-segura-1")
        archivo = SimpleUploadedFile("a.txt", "RESOLUCIÓN. RESUELVE: aprobar. CONSIDERANDO el POA.".encode())
        r = self.client.post(reverse("documento_nuevo"), {"titulo": "Res 1", "tipo": "OTRO", "dependencia": self.fin.pk,
                                                        "nivel_acceso": "DEPENDENCIA", "archivo": archivo})
        self.assertEqual(r.status_code, 302)
        d = Documento.objects.get()
        self.assertTrue(d.codigo.startswith("DOC-"))
        self.assertEqual(len(d.sha256), 64)
        self.assertTrue(d.verificar_integridad())
        self.assertEqual(d.tipo_sugerido, "RESOLUCION")
        self.assertFalse(d.ia_revisado)

    def test_deteccion_de_alteracion(self):
        self.client.login(username="ffin", password="x-Clave-segura-1")
        self.client.post(reverse("documento_nuevo"), {"titulo": "X", "tipo": "OTRO", "dependencia": self.fin.pk,
                         "nivel_acceso": "PUBLICO", "archivo": SimpleUploadedFile("a.txt", b"original")})
        d = Documento.objects.get()
        with open(d.archivo.path, "ab") as f:
            f.write(b"manipulado")
        self.assertFalse(d.verificar_integridad())

    def test_funcionario_no_registra_en_otra_dependencia(self):
        self.client.login(username="ffin", password="x-Clave-segura-1")
        r = self.client.post(reverse("documento_nuevo"), {"titulo": "X", "tipo": "OTRO", "dependencia": self.obr.pk,
                             "nivel_acceso": "PUBLICO", "archivo": SimpleUploadedFile("a.txt", b"x")})
        self.assertEqual(r.status_code, 403)

    def test_extension_no_permitida(self):
        self.client.login(username="ffin", password="x-Clave-segura-1")
        r = self.client.post(reverse("documento_nuevo"), {"titulo": "X", "tipo": "OTRO", "dependencia": self.fin.pk,
                             "nivel_acceso": "PUBLICO", "archivo": SimpleUploadedFile("virus.exe", b"MZ")})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Documento.objects.count(), 0)

    def test_control_de_acceso_en_visibilidad_busqueda_y_ia(self):
        pub = self._doc("Publico obras", "PUBLICO", self.obr, "presupuesto de la carretera")
        reservado = self._doc("Reservado obras", "RESERVADO", self.obr, "presupuesto secreto carretera")
        propio = self._doc("Propio fin", "DEPENDENCIA", self.fin, "presupuesto interno")
        vis = set(documentos_visibles(self.func_fin))
        self.assertEqual(vis, {pub, propio})
        res = {d for d, _ in buscar(self.func_fin, "presupuesto")}
        self.assertNotIn(reservado, res)
        self.client.login(username="ffin", password="x-Clave-segura-1")
        self.assertEqual(self.client.get(reverse("documento_detalle", args=[reservado.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("documento_descargar", args=[reservado.pk])).status_code, 404)
        r = self.client.get(reverse("busqueda"), {"q": "secreto", "asistente": "1"})
        self.assertNotContains(r, "Reservado obras")

    def test_visualizar_documento_en_navegador(self):
        self.client.login(username="ffin", password="x-Clave-segura-1")
        for nombre, tipo in [("a.txt", "text/plain"), ("b.pdf", "application/pdf"), ("c.png", "image/png")]:
            self.client.post(reverse("documento_nuevo"), {"titulo": nombre, "tipo": "OTRO", "dependencia": self.fin.pk,
                             "nivel_acceso": "DEPENDENCIA", "archivo": SimpleUploadedFile(nombre, b"%PDF-1.4 x")})
            d = Documento.objects.get(titulo=nombre)
            r = self.client.get(reverse("documento_ver", args=[d.pk]))
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r["Content-Type"].startswith(tipo))
            self.assertTrue(r["Content-Disposition"].startswith("inline"))
            self.assertEqual(r["X-Frame-Options"], "SAMEORIGIN")
            self.assertContains(self.client.get(reverse("documento_detalle", args=[d.pk])), "Vista del documento")
        # sin sesión no se puede ver; documento reservado de otra dependencia tampoco
        reservado = self._doc("Reservado", "RESERVADO", self.obr, "x")
        self.assertEqual(self.client.get(reverse("documento_ver", args=[reservado.pk])).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("documento_ver", args=[d.pk])).status_code, 302)

    def test_auditor_es_solo_lectura(self):
        self.client.login(username="aud", password="x-Clave-segura-1")
        r = self.client.post(reverse("expediente_nuevo"), {"asunto": "x", "dependencia": self.fin.pk,
                             "anios_gestion": 2, "anios_central": 8})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get(reverse("auditoria")).status_code, 200)

    def test_funcionario_no_ve_auditoria(self):
        self.client.login(username="ffin", password="x-Clave-segura-1")
        self.assertEqual(self.client.get(reverse("auditoria")).status_code, 403)

    def test_cadena_de_auditoria_detecta_manipulacion(self):
        for i in range(3):
            Auditoria.registrar("u", "A", f"obj{i}")
        self.assertEqual(Auditoria.verificar_cadena(), (True, None))
        Auditoria.objects.filter(pk=2).update(detalle="alterado")  # manipulación directa en BD
        ok, roto = Auditoria.verificar_cadena()
        self.assertFalse(ok)
        self.assertEqual(roto, 2)
        with self.assertRaises(PermissionError):
            Auditoria.objects.get(pk=1).delete()

    def test_transferencia_por_plazos(self):
        hoy = timezone.localdate()
        e = Expediente.objects.create(asunto="Viejo", dependencia=self.fin, estado="CERRADO",
                                      fecha_cierre=hoy - timedelta(days=365 * 3))
        self.assertEqual(e.transferencia_pendiente, "CENTRAL")
        self.client.login(username="arch", password="x-Clave-segura-1")
        self.client.post(reverse("expediente_transferir", args=[e.pk]))
        e.refresh_from_db()
        self.assertEqual(e.fase, "CENTRAL")
        self.assertIsNone(e.transferencia_pendiente)
        self.assertEqual(e.transferencias.count(), 1)

    def test_flujo_de_tramite(self):
        self.client.login(username="arch", password="x-Clave-segura-1")
        self.client.post(reverse("tramite_nuevo"), {"asunto": "Solicitud", "solicitante": "Juan",
                                                    "dependencia_actual": self.fin.pk})
        from .models import Tramite
        t = Tramite.objects.get()
        self.client.post(reverse("tramite_derivar", args=[t.pk]), {"hacia": self.obr.pk, "instruccion": "Revisar"})
        self.client.post(reverse("tramite_recibir", args=[t.pk]))
        t.refresh_from_db()
        self.assertEqual(t.dependencia_actual, self.obr)
        self.assertEqual(t.derivaciones.get().recibido is not None, True)

    def test_prestamo_unico(self):
        e = Expediente.objects.create(asunto="E", dependencia=self.fin)
        self.client.login(username="arch", password="x-Clave-segura-1")
        datos = {"expediente": e.pk, "solicitante": self.func_fin.pk,
                 "fecha_devolucion_prevista": str(timezone.localdate() + timedelta(days=7))}
        self.client.post(reverse("prestamo_nuevo"), datos)
        self.client.post(reverse("prestamo_nuevo"), datos)
        from .models import Prestamo
        self.assertEqual(Prestamo.objects.count(), 1)

    def test_api_con_token_y_filtrado(self):
        pub = self._doc("Visible", "PUBLICO", self.obr, "agua potable")
        self._doc("Oculto", "RESERVADO", self.obr, "agua potable")
        self.assertEqual(self.client.get(reverse("api_buscar"), {"q": "agua"}).status_code, 401)
        token = ApiToken.emitir(self.func_fin, "integracion")
        r = self.client.get(reverse("api_buscar"), {"q": "agua"}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["codigo"] for x in r.json()["resultados"]], [pub.codigo])
        self.assertEqual(ApiToken.objects.get().hash == token, False)  # solo se guarda el hash

    def test_bloqueo_por_intentos_fallidos(self):
        from django.core.cache import cache
        cache.clear()
        for _ in range(5):
            self.client.post(reverse("login"), {"username": "ffin", "password": "mala"})
        r = self.client.post(reverse("login"), {"username": "ffin", "password": "x-Clave-segura-1"})
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(r, "bloqueada")
        cache.clear()

    def test_clasificador_y_login_requerido(self):
        self.assertEqual(clasificar("Contrato. Cláusula primera, las partes acuerdan")[0], "CONTRATO")
        self.assertEqual(self.client.get(reverse("documentos")).status_code, 302)

    def test_vistas_principales_cargan(self):
        self.client.login(username="arch", password="x-Clave-segura-1")
        for n in ["dashboard", "documentos", "expedientes", "correspondencia", "tramites", "archivo_fisico",
                  "busqueda", "reportes", "reporte_inventario", "integridad"]:
            self.assertEqual(self.client.get(reverse(n)).status_code, 200, n)
