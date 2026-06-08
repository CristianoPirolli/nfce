from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from dfe.models import DfeExecucaoLock


class DfeLockError(Exception):
    """Ja existe uma execucao ativa para o mesmo CNPJ/certificado/filtro."""


def _lock_ttl_min() -> int:
    return getattr(settings, 'DFE_LOCK_TTL_MIN', 30)


@contextmanager
def sefaz_execucao_lock(cnpj: str, tipo_execucao: str, certificado: str, filtro: str):
    chave = {
        'cnpj': cnpj,
        'tipo_execucao': tipo_execucao,
        'certificado': certificado or '',
        'filtro': filtro or '',
    }
    agora = timezone.now()
    DfeExecucaoLock.objects.filter(expira_em__lte=agora).delete()

    try:
        with transaction.atomic():
            lock = DfeExecucaoLock.objects.create(
                **chave,
                expira_em=agora + timedelta(minutes=_lock_ttl_min()),
            )
    except IntegrityError as exc:
        raise DfeLockError(
            f'Execucao concorrente bloqueada para {cnpj} '
            f'({tipo_execucao}/{filtro}/{certificado}).'
        ) from exc

    try:
        yield
    finally:
        DfeExecucaoLock.objects.filter(pk=lock.pk).delete()
