from django.core.management.base import BaseCommand

from dfe.models import Empresa
from dfe.services.sefaz_sc_capture import resync_sc_para_cnpj


class Command(BaseCommand):
    help = 'Re-sincronização de NFC-e SC por CNPJ.'

    def handle(self, *args, **options):
        for empresa in Empresa.objects.filter(ativa=True):
            resultado = resync_sc_para_cnpj(empresa.cnpj)
            self.stdout.write(str(resultado))