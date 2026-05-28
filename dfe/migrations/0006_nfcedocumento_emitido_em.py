from django.db import migrations, models


def backfill_emitido_em(apps, schema_editor):
    NfceDocumento = apps.get_model('dfe', 'NfceDocumento')
    from dfe.services.sefaz_sc_capture import _extrair_dh_emi

    qs = NfceDocumento.objects.filter(emitido_em__isnull=True).only('id', 'xml')
    for doc in qs.iterator():
        dh = _extrair_dh_emi(doc.xml)
        if dh:
            NfceDocumento.objects.filter(id=doc.id).update(emitido_em=dh)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('dfe', '0005_remove_empresa_cert_password_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='nfcedocumento',
            name='emitido_em',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(backfill_emitido_em, noop),
    ]
