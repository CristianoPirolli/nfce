from django.core.management.base import BaseCommand

from dfe.services.sefaz_sc_capture import verificar_cancelamentos_sc_pendentes


class Command(BaseCommand):
    help = 'Verifica cancelamentos pendentes de NFC-e SC.'

    def handle(self, *args, **options):
        resultados = verificar_cancelamentos_sc_pendentes()
        self.stdout.write(str(resultados))