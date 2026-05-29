"""Worker assíncrono de captura SEF-SC.

Processo de longa duração (vira serviço do Windows). A cada ciclo seleciona as
empresas devidas, reivindica cada uma com claim atômico e submete a um pool de
threads. A captura é I/O-bound (espera HTTP do SEFAZ), então threads dão
paralelismo real. A lógica de captura em si não muda — o worker só orquestra.
"""
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from dfe.services.sefaz_sc_capture import (
    capturar_sc_para_cnpj,
    resync_sc_para_cnpj,
    verificar_cancelamentos_sc_pendentes,
)
from dfe.services.worker_fila import (
    garantir_states,
    liberar_claim,
    states_devidos_captura,
    states_devidos_resync,
    tentar_claim,
    worker_id,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Worker de longa duração: captura/resync/cancelamentos em background.'

    def handle(self, *args, **options):
        self._parar = threading.Event()
        self._ultimo_cancel = None
        self._futuros = set()  # tasks em andamento/pendentes no pool
        self._registrar_sinais()

        quem = worker_id()
        pool = ThreadPoolExecutor(
            max_workers=settings.DFE_WORKER_CONCURRENCY,
            thread_name_prefix='dfe-worker',
        )
        self.stdout.write(self.style.SUCCESS(
            f'Worker {quem} iniciado — concorrência {settings.DFE_WORKER_CONCURRENCY}, '
            f'ciclo {settings.DFE_WORKER_CICLO_SEGUNDOS}s.'
        ))

        try:
            while not self._parar.is_set():
                inicio = time.monotonic()
                try:
                    self._ciclo(pool, quem)
                except Exception:
                    logger.exception('Erro inesperado no ciclo do worker')
                finally:
                    # A thread do loop não deve vazar conexão entre ciclos.
                    connection.close()

                decorrido = time.monotonic() - inicio
                espera = max(0.0, settings.DFE_WORKER_CICLO_SEGUNDOS - decorrido)
                self._parar.wait(timeout=espera)
        finally:
            self.stdout.write('Parando — aguardando tasks em andamento drenarem...')
            pool.shutdown(wait=True)
            self.stdout.write(self.style.SUCCESS('Worker finalizado.'))

    # --- Ciclo ---

    def _ciclo(self, pool, quem):
        garantir_states()

        # Só reivindica até preencher as vagas livres do pool. Assim os claims
        # acompanham as tasks realmente em execução — evita marcar dezenas como
        # em_execucao enquanto esperam na fila (e o risco de claim "órfão" falso).
        self._futuros = {f for f in self._futuros if not f.done()}
        vagas = settings.DFE_WORKER_CONCURRENCY - len(self._futuros)
        if vagas <= 0:
            return

        # 1) Captura incremental das empresas devidas.
        for state in states_devidos_captura():
            if self._parar.is_set() or vagas <= 0:
                break
            if tentar_claim(state, quem):
                self._submeter(pool, self._run, capturar_sc_para_cnpj, state.empresa.cnpj, state.pk)
                vagas -= 1

        # 2) Resync por empresa, quando a cadência venceu.
        for state in states_devidos_resync():
            if self._parar.is_set() or vagas <= 0:
                break
            if tentar_claim(state, quem):
                self._submeter(pool, self._run, resync_sc_para_cnpj, state.empresa.cnpj, state.pk)
                vagas -= 1

        # 3) Verificação global de cancelamentos, em cadência fixa.
        if vagas > 0 and self._cancelamentos_devido():
            self._ultimo_cancel = timezone.now()
            self._submeter(pool, self._run_cancelamentos)

    def _submeter(self, pool, fn, *args):
        self._futuros.add(pool.submit(fn, *args))

    def _cancelamentos_devido(self) -> bool:
        if self._ultimo_cancel is None:
            return True
        decorrido = (timezone.now() - self._ultimo_cancel).total_seconds()
        return decorrido >= settings.DFE_CANCELAMENTOS_INTERVALO_MIN * 60

    # --- Tasks (rodam nas threads do pool) ---

    def _run(self, fn, cnpj, state_pk):
        """Executa uma task por-empresa e SEMPRE libera o claim e a conexão."""
        try:
            fn(cnpj)
        except Exception:
            logger.exception('Erro inesperado em %s(%s)', fn.__name__, cnpj)
        finally:
            try:
                liberar_claim(state_pk)
            finally:
                connection.close()

    def _run_cancelamentos(self):
        try:
            verificar_cancelamentos_sc_pendentes()
        except Exception:
            logger.exception('Erro na verificação de cancelamentos')
        finally:
            connection.close()

    # --- Parada graciosa ---

    def _registrar_sinais(self):
        def handler(signum, frame):
            self.stdout.write(f'\nSinal {signum} recebido — encerrando graciosamente.')
            self._parar.set()

        for nome in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
            sig = getattr(signal, nome, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Fora da main thread ou sinal indisponível na plataforma.
                pass
