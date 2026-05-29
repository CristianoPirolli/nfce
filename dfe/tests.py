from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dfe.models import Empresa, DfeSetting, DfeSyncState
from dfe.services.sefaz_sc_capture import _intervalo_proxima
from dfe.services.worker_fila import (
    liberar_claim,
    states_devidos_captura,
    tentar_claim,
)


def _empresa(cnpj='47707501000176', razao='Empresa Teste', ativa=True):
    return Empresa.objects.create(razao_social=razao, cnpj=cnpj, ativa=ativa)


class ClaimAtomicoTests(TestCase):
    def test_claim_exclui_concorrente(self):
        """Dois claims da mesma empresa: só um obtém (UPDATE atômico)."""
        state = DfeSyncState.objects.create(empresa=_empresa())

        primeiro = tentar_claim(state, 'worker-A')
        segundo = tentar_claim(state, 'worker-B')

        self.assertTrue(primeiro)
        self.assertFalse(segundo)
        state.refresh_from_db()
        self.assertTrue(state.em_execucao)
        self.assertEqual(state.worker_id, 'worker-A')

    def test_liberar_claim_reabre(self):
        state = DfeSyncState.objects.create(empresa=_empresa())
        self.assertTrue(tentar_claim(state, 'worker-A'))

        liberar_claim(state.pk)

        state.refresh_from_db()
        self.assertFalse(state.em_execucao)
        self.assertEqual(state.worker_id, '')
        self.assertIsNone(state.execucao_iniciada_em)
        # Após liberar, pode ser reivindicada de novo.
        self.assertTrue(tentar_claim(state, 'worker-B'))

    def test_claim_orfao_e_reivindicado(self):
        """Claim antigo (processo morto) é roubado; claim recente não."""
        agora = timezone.now()

        orfao = DfeSyncState.objects.create(
            empresa=_empresa(cnpj='11111111111111'),
            em_execucao=True,
            execucao_iniciada_em=agora - timedelta(minutes=40),
            worker_id='morto',
        )
        recente = DfeSyncState.objects.create(
            empresa=_empresa(cnpj='22222222222222'),
            em_execucao=True,
            execucao_iniciada_em=agora - timedelta(minutes=5),
            worker_id='vivo',
        )

        self.assertTrue(tentar_claim(orfao, 'novo'))    # órfão (>30min) roubado
        self.assertFalse(tentar_claim(recente, 'novo'))  # recente preservado


class SelecaoDevidasTests(TestCase):
    def test_selecao_respeita_agendamento_e_bloqueio(self):
        agora = timezone.now()

        # Devida: sem agendamento.
        e_sem = DfeSyncState.objects.create(empresa=_empresa(cnpj='10000000000001'))
        # Devida: agendada no passado.
        e_passado = DfeSyncState.objects.create(
            empresa=_empresa(cnpj='10000000000002'),
            proxima_captura_em=agora - timedelta(minutes=1),
        )
        # NÃO devida: agendada no futuro.
        DfeSyncState.objects.create(
            empresa=_empresa(cnpj='10000000000003'),
            proxima_captura_em=agora + timedelta(hours=1),
        )
        # NÃO devida: bloqueada.
        DfeSyncState.objects.create(
            empresa=_empresa(cnpj='10000000000004'),
            bloqueado_ate=agora + timedelta(hours=1),
        )
        # NÃO devida: em execução recente.
        DfeSyncState.objects.create(
            empresa=_empresa(cnpj='10000000000005'),
            em_execucao=True,
            execucao_iniciada_em=agora,
        )
        # NÃO devida: empresa inativa.
        DfeSyncState.objects.create(
            empresa=_empresa(cnpj='10000000000006', ativa=False),
        )

        pks = set(states_devidos_captura().values_list('pk', flat=True))
        self.assertEqual(pks, {e_sem.pk, e_passado.pk})

    def test_piso_minimo_por_cnpj(self):
        """Não seleciona CNPJ com consulta válida recente, mesmo se 'devido'."""
        agora = timezone.now()
        # Devida pelo agendamento, mas consultada agora há pouco → piso barra.
        DfeSyncState.objects.create(
            empresa=_empresa(cnpj='20000000000001'),
            proxima_captura_em=agora - timedelta(minutes=1),
            ultima_captura=agora - timedelta(minutes=2),
        )
        # Devida e última consulta antiga → passa o piso.
        ok = DfeSyncState.objects.create(
            empresa=_empresa(cnpj='20000000000002'),
            proxima_captura_em=agora - timedelta(minutes=1),
            ultima_captura=agora - timedelta(hours=2),
        )
        pks = set(states_devidos_captura().values_list('pk', flat=True))
        self.assertEqual(pks, {ok.pk})


class AgendamentoFallbackTests(TestCase):
    def test_fallback_para_cstat_desconhecido(self):
        """Qualquer cStat não previsto agenda >= 1h (nunca deixa 'devido')."""
        self.assertEqual(_intervalo_proxima('110'), timedelta(hours=1))
        self.assertEqual(_intervalo_proxima('117'), timedelta(hours=12))
        self.assertEqual(_intervalo_proxima('118'), timedelta(hours=12))
        self.assertEqual(_intervalo_proxima('108'), timedelta(hours=1))
        # cStat inesperado / rejeição → fallback seguro de 1h.
        self.assertEqual(_intervalo_proxima('999'), timedelta(hours=1))
        self.assertEqual(_intervalo_proxima(None), timedelta(hours=1))
        self.assertGreaterEqual(_intervalo_proxima('qualquer'), timedelta(hours=1))


class BotaoEnfileiraTests(TestCase):
    def setUp(self):
        # Certificado do contador (legado) para o botão não barrar por falta de cert.
        DfeSetting.objects.create(
            nfce_sc_cert_cnpj_contador='99999999999999',
            cert_path='C:/fake/cert.pfx',
            cert_password='senha',
        )
        self.empresa = _empresa()

    def test_botao_enfileira_sem_chamar_sefaz(self):
        antes = timezone.now()
        resp = self.client.post(
            reverse('dfe:consulta_capturar', args=[self.empresa.id])
        )
        self.assertEqual(resp.status_code, 302)  # redireciona

        state = DfeSyncState.objects.get(empresa=self.empresa)
        # A view apenas marcou proxima_captura_em ~ agora (não chamou o SEFAZ).
        self.assertIsNotNone(state.proxima_captura_em)
        self.assertGreaterEqual(state.proxima_captura_em, antes)
        self.assertFalse(state.em_execucao)

    def test_botao_nao_reenfileira_se_em_execucao(self):
        marcado = timezone.now() + timedelta(minutes=5)
        DfeSyncState.objects.create(
            empresa=self.empresa,
            em_execucao=True,
            execucao_iniciada_em=timezone.now(),
            proxima_captura_em=marcado,
        )
        self.client.post(reverse('dfe:consulta_capturar', args=[self.empresa.id]))

        state = DfeSyncState.objects.get(empresa=self.empresa)
        # Não mexeu no agendamento porque já está em execução.
        self.assertEqual(state.proxima_captura_em, marcado)
