from django.db import models


class Empresa(models.Model):
    razao_social = models.CharField(max_length=255)
    cnpj = models.CharField(max_length=14, unique=True)
    ativa = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.razao_social} - {self.cnpj}'


class DfeSetting(models.Model):
    nfce_sc_cert_cnpj_contador = models.CharField(
        max_length=14,
        help_text='CNPJ do escritório contábil responsável pelo certificado.'
    )
    cert_path = models.CharField(max_length=500)
    cert_password = models.CharField(max_length=255)
    ver_aplic = models.CharField(max_length=20, default='NFCE-DJANGO-1.0')

    def __str__(self):
        return f'Configuração DFe SC - {self.nfce_sc_cert_cnpj_contador}'


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

    xml = models.TextField()
    cancelada = models.BooleanField(default=False)
    cancelamento_verificado_em = models.DateTimeField(null=True, blank=True)
    capturado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('empresa', 'chave_acesso', 'nsu')

    def __str__(self):
        return self.chave_acesso