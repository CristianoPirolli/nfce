from django.db import migrations
from django.utils import timezone


def backfill(apps, schema_editor):
    NfceDocumento = apps.get_model('dfe', 'NfceDocumento')

    eventos = NfceDocumento.objects.filter(tipo_documento='EVENTO_CANCELAMENTO')
    pares = eventos.values_list('empresa_id', 'chave_acesso').distinct()

    agora = timezone.now()
    for empresa_id, chave in pares:
        NfceDocumento.objects.filter(
            empresa_id=empresa_id,
            chave_acesso=chave,
            tipo_documento='NFE_PROC',
        ).update(cancelada=True, cancelamento_verificado_em=agora)

    # Eventos não devem aparecer como "cancelados".
    NfceDocumento.objects.filter(
        tipo_documento='EVENTO_CANCELAMENTO',
        cancelada=True,
    ).update(cancelada=False)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('dfe', '0006_nfcedocumento_emitido_em'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
