from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib.auth.models import User

from .models import (Caja, Carpeta, Correspondencia, Dependencia, Derivacion, Documento, Estante,
                     Expediente, Prestamo, Tramite)


class Base(forms.ModelForm):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        for f in self.fields.values():
            if not isinstance(f.widget, forms.CheckboxInput):
                f.widget.attrs.setdefault("class", "campo")


class DocumentoForm(Base):
    class Meta:
        model = Documento
        fields = ["titulo", "tipo", "dependencia", "expediente", "nivel_acceso", "archivo"]
        # En el celular, este campo permite fotografiar el documento con la cámara o elegir un archivo.
        widgets = {"archivo": forms.ClearableFileInput(attrs={"accept": "application/pdf,image/*,.txt"})}

    def clean_archivo(self):
        f = self.cleaned_data.get("archivo")
        if f:
            if Path(f.name).suffix.lower() not in settings.EXTENSIONES_PERMITIDAS:
                raise forms.ValidationError("Formato no permitido. Use PDF, imagen o TXT.")
            if f.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
                raise forms.ValidationError(f"El archivo supera {settings.MAX_UPLOAD_MB} MB.")
        return f


class NuevaVersionForm(forms.Form):
    archivo = forms.FileField()

    def clean_archivo(self):
        f = self.cleaned_data["archivo"]
        if Path(f.name).suffix.lower() not in settings.EXTENSIONES_PERMITIDAS:
            raise forms.ValidationError("Formato no permitido.")
        return f


class ExpedienteForm(Base):
    class Meta:
        model = Expediente
        fields = ["asunto", "dependencia", "serie", "anios_gestion", "anios_central", "carpeta"]


class CorrespondenciaForm(Base):
    class Meta:
        model = Correspondencia
        fields = ["tipo", "remitente", "destinatario", "asunto", "dependencia", "documento"]


class TramiteForm(Base):
    class Meta:
        model = Tramite
        fields = ["asunto", "solicitante", "dependencia_actual", "expediente"]


class DerivacionForm(Base):
    class Meta:
        model = Derivacion
        fields = ["hacia", "instruccion"]


class PrestamoForm(Base):
    class Meta:
        model = Prestamo
        fields = ["expediente", "solicitante", "fecha_devolucion_prevista"]
        widgets = {"fecha_devolucion_prevista": forms.DateInput(attrs={"type": "date"})}


class EstanteForm(Base):
    class Meta:
        model = Estante
        fields = ["codigo", "ubicacion"]


class CajaForm(Base):
    class Meta:
        model = Caja
        fields = ["estante", "codigo"]


class CarpetaForm(Base):
    class Meta:
        model = Carpeta
        fields = ["caja", "codigo"]


class DependenciaForm(Base):
    class Meta:
        model = Dependencia
        fields = ["nombre", "sigla", "padre"]
