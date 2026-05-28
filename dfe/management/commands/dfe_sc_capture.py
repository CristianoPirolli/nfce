from django.core.management.base import BaseCommand
from django.utils import timezone

from dfe.models import Empresa, DfeSyncState
from dfe.services.sefaz_sc_capture import capturar_sc_para_cnpj


class Command(BaseCommand):
    help = 'Captura incremental de NFC-e SC por CNPJ.'

    def handle(self, *args, **options):
        empresas = Empresa.objects.filter(ativa=True)

        for empresa in empresas:
            state, _ = DfeSyncState.objects.get_or_create(empresa=empresa)

            if state.bloqueado_ate and state.bloqueado_ate > timezone.now():
                self.stdout.write(f'{empresa.cnpj}: bloqueado até {state.bloqueado_ate}')
                continue

            if state.proxima_captura_em and state.proxima_captura_em > timezone.now():
                self.stdout.write(f'{empresa.cnpj}: próxima captura em {state.proxima_captura_em}')
                continue

            resultado = capturar_sc_para_cnpj(empresa.cnpj)
            self.stdout.write(str(resultado))