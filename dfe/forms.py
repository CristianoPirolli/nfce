from django import forms

from .models import Empresa
from .services.cert_inspector import CertificadoInvalido, inspecionar_pfx


CNPJ_RE = r'^\d{14}$'


class EmpresaForm(forms.ModelForm):
    cnpj = forms.RegexField(
        regex=CNPJ_RE,
        error_messages={'invalid': 'Informe o CNPJ com 14 dígitos numéricos.'},
        label='CNPJ',
    )

    class Meta:
        model = Empresa
        fields = ('razao_social', 'cnpj', 'ativa')


class CertificadoUploadForm(forms.Form):
    cert_pfx = forms.FileField(label='Arquivo .pfx do certificado A1')
    cert_password = forms.CharField(
        label='Senha do certificado',
        widget=forms.PasswordInput(render_value=False),
        max_length=255,
    )

    def __init__(self, *args, empresa: Empresa = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.empresa = empresa
        self._info = None

    def clean(self):
        cleaned = super().clean()
        arquivo = cleaned.get('cert_pfx')
        senha = cleaned.get('cert_password')

        if not arquivo or not senha:
            return cleaned

        pfx_bytes = arquivo.read()
        arquivo.seek(0)

        try:
            info = inspecionar_pfx(pfx_bytes, senha)
        except CertificadoInvalido as exc:
            raise forms.ValidationError(str(exc))

        if self.empresa and info['cnpj_titular']:
            if info['cnpj_titular'] != self.empresa.cnpj:
                raise forms.ValidationError(
                    f"CNPJ do titular do certificado ({info['cnpj_titular']}) "
                    f"não corresponde ao CNPJ da empresa ({self.empresa.cnpj})."
                )

        self._info = info
        return cleaned

    @property
    def info_cert(self):
        return self._info
