"""Fila baseada no banco para o worker assíncrono de captura.

A "fila" não é uma tabela — é uma consulta sobre DfeSyncState. Estas funções
selecionam empresas devidas e fazem o claim atômico que evita que duas execuções
(threads do pool ou um command manual) processem a mesma empresa ao mesmo tempo.
"""
import os
import socket
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from dfe.models import Empresa, DfeSyncState


def worker_id() -> str:
    """Identificador do processo worker (hostname:pid) para diagnóstico."""
    return f'{socket.gethostname()}:{os.getpid()}'


def _cutoff_orfao():
    """Instante antes do qual um claim em execução é considerado órfão."""
    return timezone.now() - timedelta(minutes=settings.DFE_CLAIM_ORFAO_MIN)


def _livre_ou_orfao() -> Q:
    """Filtro: state não está em execução, ou o claim atual é órfão (processo morto)."""
    return Q(em_execucao=False) | Q(execucao_iniciada_em__lt=_cutoff_orfao())


def _liberado_do_bloqueio() -> Q:
    agora = timezone.now()
    return Q(bloqueado_ate__isnull=True) | Q(bloqueado_ate__lte=agora)


def garantir_states():
    """Garante que toda empresa ativa tenha um DfeSyncState."""
    for empresa in Empresa.objects.filter(ativa=True):
        DfeSyncState.objects.get_or_create(empresa=empresa)


def states_devidos_captura():
    """Empresas ativas com captura devida, não bloqueadas e livres (ou órfãs).

    Aplica também o piso mínimo por CNPJ (DFE_MIN_INTERVALO_CAPTURA_MIN): nunca
    seleciona um CNPJ cuja última consulta válida foi há menos que o piso —
    rede de segurança contra consumo indevido, mesmo se o agendamento falhar.
    """
    agora = timezone.now()
    piso = agora - timedelta(minutes=settings.DFE_MIN_INTERVALO_CAPTURA_MIN)
    return (
        DfeSyncState.objects
        .filter(empresa__ativa=True)
        .filter(Q(proxima_captura_em__isnull=True) | Q(proxima_captura_em__lte=agora))
        .filter(Q(ultima_captura__isnull=True) | Q(ultima_captura__lte=piso))
        .filter(_liberado_do_bloqueio())
        .filter(_livre_ou_orfao())
        .select_related('empresa')
    )


def states_devidos_resync():
    """Empresas ativas com resync devido (por ultimo_resync_em + intervalo)."""
    agora = timezone.now()
    cutoff = agora - timedelta(hours=settings.DFE_RESYNC_INTERVALO_HORAS)
    return (
        DfeSyncState.objects
        .filter(empresa__ativa=True, resync_ativo=True)
        .filter(Q(ultimo_resync_em__isnull=True) | Q(ultimo_resync_em__lte=cutoff))
        .filter(Q(proxima_captura_em__isnull=True) | Q(proxima_captura_em__lte=agora))
        .filter(_liberado_do_bloqueio())
        .filter(_livre_ou_orfao())
        .select_related('empresa')
    )


def tentar_claim(state: DfeSyncState, quem: str) -> bool:
    """Reivindica o state de forma atômica. Retorna True se conseguiu.

    O UPDATE condicional garante exclusão mútua: só um chamador transforma
    em_execucao=False→True (ou rouba um claim órfão). Concorrentes recebem 0.
    """
    linhas = (
        DfeSyncState.objects
        .filter(pk=state.pk)
        .filter(_livre_ou_orfao())
        .update(
            em_execucao=True,
            execucao_iniciada_em=timezone.now(),
            worker_id=quem,
        )
    )
    return linhas == 1


def liberar_claim(state_pk: int):
    """Libera o claim ao fim da execução (sucesso ou erro)."""
    DfeSyncState.objects.filter(pk=state_pk).update(
        em_execucao=False,
        execucao_iniciada_em=None,
        worker_id='',
    )
