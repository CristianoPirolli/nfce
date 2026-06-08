from django.core.management.base import BaseCommand
from django.utils import timezone

from dfe.models import DfeSyncState


class Command(BaseCommand):
    help = 'Lista o status de captura NFC-e SC por empresa.'

    def handle(self, *args, **options):
        linhas = (
            DfeSyncState.objects
            .select_related('empresa')
            .order_by('empresa__razao_social')
        )

        headers = [
            'Empresa',
            'CNPJ',
            'Ultimo NSU',
            'Proxima captura',
            'Ultimo cStat',
            'Motivo',
            'Bloqueada ate',
        ]
        rows = [headers]
        for state in linhas:
            rows.append([
                state.empresa.razao_social[:32],
                state.empresa.cnpj,
                str(state.ultimo_nsu_sc),
                self._fmt_dt(state.proxima_captura_em),
                state.ultimo_cstat or '',
                (state.ultimo_motivo or state.ultimo_erro or '')[:50],
                self._fmt_dt(state.bloqueado_ate),
            ])

        widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
        for idx, row in enumerate(rows):
            linha = ' | '.join(row[i].ljust(widths[i]) for i in range(len(headers)))
            self.stdout.write(linha)
            if idx == 0:
                self.stdout.write('-+-'.join('-' * w for w in widths))

    def _fmt_dt(self, valor):
        if not valor:
            return ''
        local = timezone.localtime(valor)
        return local.strftime('%Y-%m-%d %H:%M:%S')
