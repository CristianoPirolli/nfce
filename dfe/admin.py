from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .models import Empresa, DfeSetting, DfeSyncState, NfceDocumento


@admin.register(Empresa)
class EmpresaAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'razao_social',
        'cnpj',
        'ativa',
    )
    list_filter = (
        'ativa',
    )
    search_fields = (
        'razao_social',
        'cnpj',
    )
    ordering = (
        'razao_social',
    )


@admin.register(DfeSetting)
class DfeSettingAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'nfce_sc_cert_cnpj_contador',
        'cert_validade',
        'ver_aplic',
    )
    search_fields = (
        'nfce_sc_cert_cnpj_contador',
    )

    def has_add_permission(self, request):
        """
        Evita múltiplas configurações globais.
        """
        if DfeSetting.objects.exists():
            return False
        return super().has_add_permission(request)


@admin.register(DfeSyncState)
class DfeSyncStateAdmin(admin.ModelAdmin):
    list_display = (
        'empresa',
        'ultimo_nsu_sc',
        'ultimo_cstat',
        'status_resumido',
        'ultima_captura',
        'proxima_captura_em',
        'ultimo_resync_em',
        'resync_nsu_inicial',
        'resync_ativo',
        'bloqueado_ate',
    )
    list_filter = (
        'ultimo_cstat',
        'resync_ativo',
        'ultima_captura',
        'ultimo_resync_em',
        'bloqueado_ate',
    )
    search_fields = (
        'empresa__razao_social',
        'empresa__cnpj',
        'ultimo_cstat',
        'ultimo_motivo',
    )
    readonly_fields = (
        'ultima_captura',
        'proxima_captura_em',
        'bloqueado_ate',
        'ultimo_cstat',
        'ultimo_motivo',
        'ultimo_erro',
        'ultimo_erro_em',
        'ultimo_resync_em',
    )
    ordering = (
        'empresa__razao_social',
    )

    @admin.display(description='Status')
    def status_resumido(self, obj):
        if obj.bloqueado_ate:
            return mark_safe('<span style="color: #b91c1c;">Bloqueado</span>')

        if obj.ultimo_cstat == '118':
            return mark_safe('<span style="color: #15803d;">DF-e localizados</span>')

        if obj.ultimo_cstat == '117':
            return mark_safe('<span style="color: #2563eb;">Sincronizado</span>')

        if obj.ultimo_cstat == '8002':
            return mark_safe('<span style="color: #b45309;">Sem vínculo DTEC</span>')

        if obj.ultimo_cstat:
            return format_html(
                '<span style="color: #6b7280;">{}</span>',
                obj.ultimo_cstat
            )

        return '-'


@admin.register(NfceDocumento)
class NfceDocumentoAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'empresa',
        'nsu',
        'chave_acesso',
        'tipo_documento',
        'cancelada',
        'cancelamento_verificado_em',
        'capturado_em',
    )
    list_filter = (
        'tipo_documento',
        'cancelada',
        'cancelamento_verificado_em',
        'capturado_em',
        'empresa',
    )
    search_fields = (
        'chave_acesso',
        'empresa__razao_social',
        'empresa__cnpj',
        'nsu',
    )
    readonly_fields = (
        'empresa',
        'chave_acesso',
        'nsu',
        'tipo_documento',
        'arquivo_zip',
        'arquivo_entrada',
        'emitido_em',
        'numero_nfce',
        'serie',
        'valor_total',
        'cancelada',
        'cancelamento_verificado_em',
        'capturado_em',
    )
    ordering = (
        '-capturado_em',
        '-nsu',
    )
    date_hierarchy = 'capturado_em'
    list_per_page = 50