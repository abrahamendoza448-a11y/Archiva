from django.contrib import admin

from .models import (ApiToken, Auditoria, Caja, Carpeta, Correspondencia, Dependencia, Derivacion, Documento,
                     Estante, Expediente, Perfil, Prestamo, Tramite, Transferencia, VersionDocumento)

for m in (Dependencia, Perfil, Estante, Caja, Carpeta, Expediente, Documento, VersionDocumento, Correspondencia,
          Tramite, Derivacion, Prestamo, Transferencia):
    admin.site.register(m)


@admin.register(Auditoria)
class AuditoriaAdmin(admin.ModelAdmin):
    list_display = ("fecha", "usuario", "accion", "objeto", "ip")
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ("nombre", "user", "activo", "creado")
    exclude = ("hash",)
    def has_add_permission(self, request): return False
