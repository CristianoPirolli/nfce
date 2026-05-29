from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import models


def empresa_cert_upload_path(instance, filename):
    return f'empresa_{instance.cnpj}/{filename}'


def contador_cert_upload_path(instance, filename):
    return f'contador/{filename}'


private_cert_storage = FileSystemStorage(location=str(settings.PRIVATE_CERTS_ROOT))


def _fernet() -> Fernet:
    key = settings.CERT_SECRET_KEY
    if isinstance(key, str):
        key = key.encode('utf-8')
    return Fernet(key)


class CertPasswordMixin:
    """Guarda/recupera a senha do .pfx criptografada com Fernet.

    Requer que o modelo tenha o campo BinaryField `cert_password_encrypted`.
    """

    def set_cert_password(self, plaintext: str) -> None:
        if not plaintext:
            self.cert_password_encrypted = None
            return
        self.cert_password_encrypted = _fernet().encrypt(plaintext.encode('utf-8'))

    def get_cert_password(self) -> str:
        if not self.cert_password_encrypted:
            return ''
        try:
            return _fernet().decrypt(bytes(self.cert_password_encrypted)).decode('utf-8')
        except InvalidToken:
            raise ValueError('Senha do certificado não pôde ser decriptada (chave incorreta).')


class Empresa(CertPasswordMixin, models.Model):
    razao_social = models.CharField(max_length=255)
    cnpj = models.CharField(max_length=14, unique=True)
    ativa = models.BooleanField(default=True)

    cert_pfx = models.FileField(
        upload_to=empresa_cert_upload_path,
        storage=private_cert_storage,
        blank=True,
        null=True,
        help_text='Certificado digital A1 (.pfx) da própria empresa (e-CNPJ). '
                  'Armazenado em diretório privado, não exposto via HTTP.'
    )
    cert_password_encrypted = models.BinaryField(blank=True, null=True)
    cert_cnpj_titular = models.CharField(
        max_length=14,
        blank=True,
        default='',
        help_text='CNPJ extraído do certificado no momento do upload.'
    )
    cert_validade = models.DateTimeField(null=True, blank=True)
    ver_aplic = models.CharField(max_length=20, default='NFCE-DJANGO-1.0')

    def __str__(self):
        return f'{self.razao_social} - {self.cnpj}'


class DfeSetting(CertPasswordMixin, models.Model):
    """Certificado único do escritório contábil, usado por todas as empresas
    que não tenham um certificado próprio cadastrado (override)."""

    nfce_sc_cert_cnpj_contador = models.CharField(
        max_length=14,
        blank=True,
        default='',
        help_text='CNPJ do escritório contábil (lido do certificado no upload).'
    )

    # Certificado do contador (upload A1 .pfx, senha criptografada).
    cert_pfx = models.FileField(
        upload_to=contador_cert_upload_path,
        storage=private_cert_storage,
        blank=True,
        null=True,
        help_text='Certificado A1 (.pfx) do contador. Usado por todas as empresas '
                  'sem certificado próprio. Armazenado em diretório privado.'
    )
    cert_password_encrypted = models.BinaryField(blank=True, null=True)
    cert_validade = models.DateTimeField(null=True, blank=True)
    ver_aplic = models.CharField(max_length=20, default='NFCE-DJANGO-1.0')

    # Legado — configuração antiga por caminho no disco + senha em texto puro.
    # Mantido para compatibilidade; o upload criptografado acima tem prioridade.
    cert_path = models.CharField(max_length=500, blank=True, default='')
    cert_password = models.CharField(max_length=255, blank=True, default='')

    def __str__(self):
        return f'Certificado do contador - {self.nfce_sc_cert_cnpj_contador or "(sem CNPJ)"}'


class DfeSyncState(models.Model):
    empresa = models.OneToOneField(Empresa, on_delete=models.CASCADE)

    # Captura incremental normal
    ultimo_nsu_sc = models.BigIntegerField(default=0)
    ultima_captura = models.DateTimeField(null=True, blank=True)
    proxima_captura_em = models.DateTimeField(null=True, blank=True)
    bloqueado_ate = models.DateTimeField(null=True, blank=True)

    # Controle do último retorno da SEF/SC
    ultimo_cstat = models.CharField(max_length=10, blank=True, null=True)
    ultimo_motivo = models.TextField(blank=True, null=True)

    # Última falha de comunicação (timeout, conexão, 5xx, payload inválido).
    ultimo_erro = models.TextField(blank=True, default='')
    ultimo_erro_em = models.DateTimeField(null=True, blank=True)

    # Claim de execução pelo worker assíncrono (evita processamento duplo).
    em_execucao = models.BooleanField(default=False)
    execucao_iniciada_em = models.DateTimeField(null=True, blank=True)
    worker_id = models.CharField(max_length=120, blank=True, default='')

    # Controle de re-sincronização
    ultimo_resync_em = models.DateTimeField(null=True, blank=True)
    resync_nsu_inicial = models.BigIntegerField(default=0)
    resync_ativo = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.empresa.cnpj} - NSU {self.ultimo_nsu_sc}'


class NfceDocumento(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    chave_acesso = models.CharField(max_length=44, db_index=True)
    nsu = models.BigIntegerField(db_index=True)

    tipo_documento = models.CharField(
        max_length=30,
        choices=[
            ('NFE_PROC', 'NFC-e'),
            ('EVENTO_CANCELAMENTO', 'Evento de Cancelamento'),
            ('OUTRO_EVENTO', 'Outro Evento'),
        ]
    )

    # Caminho do arquivo .zip mensal que contém o XML deste documento.
    arquivo_zip = models.CharField(max_length=500, blank=True, default='')
    # Nome da entrada dentro do .zip (ex.: "<chave>.xml" ou "<chave>-evento_cancelada.xml").
    arquivo_entrada = models.CharField(max_length=200, blank=True, default='')
    emitido_em = models.DateTimeField(null=True, blank=True, db_index=True)

    # Dados denormalizados para listagem rápida (evita reabrir o .zip por linha).
    numero_nfce = models.CharField(max_length=15, blank=True, default='', db_index=True)
    serie = models.CharField(max_length=5, blank=True, default='')
    valor_total = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)

    cancelada = models.BooleanField(default=False)
    cancelamento_verificado_em = models.DateTimeField(null=True, blank=True)
    capturado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('empresa', 'chave_acesso', 'nsu')

    def __str__(self):
        return self.chave_acesso