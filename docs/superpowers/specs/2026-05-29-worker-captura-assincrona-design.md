# Design — Captura assíncrona e agendada (worker + banco como fila)

**Data:** 2026-05-29
**Status:** Aprovado (design)

## Objetivo

Permitir que as capturas de NFC-e da SEFAZ-SC rodem de forma **automática e em
paralelo** para muitas empresas, sem travar a interface. Hoje a captura é
síncrona: o botão "Capturar agora" dispara `capturar_sc_para_cnpj` dentro do
request HTTP e bloqueia a tela até terminar todos os lotes daquela empresa, uma
empresa por vez.

Estado final desejado:
- Captura roda sozinha num intervalo, para todas as empresas "devidas".
- Botão "Capturar agora" vira **não-bloqueante**: só "fura a fila" (marca para
  captura imediata) e responde na hora.
- Vários CNPJs capturados em paralelo.
- A automação periódica cobre os três jobs que chamam o SEFAZ:
  **captura incremental**, **verificação de cancelamentos** e **resync**.

## Contexto e restrições

- **Máquina única.** O "motor" (worker) roda num servidor só. A aplicação será
  exposta a outras máquinas da rede via abertura de porta no firewall — isso é
  uma questão de **servir HTTP**, não de distribuir o worker.
- **Windows Server 2022.** Pretende-se rodar como **serviço do Windows**.
- **Postgres** em produção (quando `DB_HOST` definido); SQLite em dev.
- Já existe `capturar_sc_para_cnpj`, `resync_sc_para_cnpj` e
  `verificar_cancelamentos_sc_pendentes` com tratamento de erro, retry/backoff e
  agendamento (`proxima_captura_em`, `bloqueado_ate`). Essa lógica **não muda**.
- Já existe o command `dfe_sc_capture` que percorre empresas ativas respeitando
  `bloqueado_ate`/`proxima_captura_em` (serial).

## Decisão de arquitetura

**Worker com pool de threads + banco como fila.** Descartado Celery: no Windows
o pool `prefork` não funciona (exigiria pool de threads/gevent de qualquer
forma), e para um único servidor I/O-bound o broker (Redis/RabbitMQ) não agrega
— a durabilidade da fila já vem de graça dos campos de agendamento no banco.
Celery só compensaria com múltiplas máquinas de processamento, o que não é o
caso (o motor fica numa máquina só).

Captura é **I/O-bound** (espera resposta HTTP do SEFAZ), então um
`ThreadPoolExecutor` dá paralelismo real mesmo com o GIL, sem o custo
operacional do multiprocessing.

### Topologia (dois serviços Windows na mesma máquina)

```
┌─────────────────────────────────────────────────────────────┐
│                    Máquina única (servidor)                    │
│  ┌──────────────────┐         ┌───────────────────────────┐  │
│  │  Serviço Web      │         │  Serviço Worker           │  │
│  │  (waitress WSGI)  │         │  (manage.py dfe_sc_worker)│  │
│  │  - botão = marca  │         │  loop a cada N s:         │  │
│  │    proxima_=agora │         │   1. busca "devidas"      │  │
│  │  - mostra status  │         │   2. ThreadPoolExecutor   │  │
│  └────────┬──────────┘         └───────────┬───────────────┘  │
│           └──────────► Postgres ◄──────────┘                  │
│                    (estado + "fila")                          │
│                          ▼                                    │
│                  pasta de rede (.zip)                         │
└─────────────────────────────────────────────────────────────┘
        ▲ porta liberada no firewall → outras máquinas (navegador → UI)
```

**Separação de responsabilidades:**
- **Web** nunca chama o SEFAZ. Só lê estado e enfileira (marca timestamps).
- **Worker** é o único que chama o SEFAZ. Decide o que está devido, processa em
  paralelo, grava resultado/erro no estado.
- A lógica de captura não muda; o worker apenas a **orquestra**.

## Modelo de estado e "fila"

A fila não é uma tabela nova — é uma **consulta** sobre `DfeSyncState`. Uma
empresa está "devida" para captura quando:

```
ativa = True
E (proxima_captura_em IS NULL OU proxima_captura_em <= agora)
E (bloqueado_ate      IS NULL OU bloqueado_ate      <= agora)
E não está em execução (claim)
```

### Campos novos em `DfeSyncState`

| Campo | Tipo | Propósito |
|---|---|---|
| `em_execucao` | BooleanField (default False) | Um worker está processando esta empresa agora |
| `execucao_iniciada_em` | DateTimeField null | Quando o claim começou — detecta claim órfão |
| `worker_id` | CharField blank | Quem reivindicou (hostname+pid) — diagnóstico |

### Claim atômico (evita processamento duplo)

Reivindicação via `UPDATE` condicional (atômico no Postgres):

```python
linhas = DfeSyncState.objects.filter(
    pk=state.pk, em_execucao=False
).update(
    em_execucao=True,
    execucao_iniciada_em=agora,
    worker_id=meu_id,
)
# linhas == 1 → reivindiquei; linhas == 0 → outro já pegou, pulo
```

Garante que duas execuções (threads do pool, ou um `dfe_sc_capture` manual
rodando junto) nunca peguem a mesma empresa. Ao terminar (sucesso ou erro), o
worker libera: `em_execucao=False`.

**Claim órfão:** se `em_execucao=True` mas `execucao_iniciada_em` é mais antigo
que `DFE_CLAIM_ORFAO_MIN`, considera-se que o processo anterior morreu e a
empresa é re-reivindicável. Evita empresa presa para sempre após crash.

### Cadência dos três jobs

- **Captura** → usa `proxima_captura_em` (já existe). Quem define a próxima é a
  própria `capturar_sc_para_cnpj` (+1h/+12h/+10min conforme resultado).
- **Cancelamentos** → cadência fixa global (`DFE_CANCELAMENTOS_INTERVALO_MIN`),
  pois `verificar_cancelamentos_sc_pendentes()` já varre todas as empresas. O
  worker dispara 1 task quando o intervalo global vence.
- **Resync** → por empresa, usando `ultimo_resync_em` + `DFE_RESYNC_INTERVALO_HORAS`.

## O worker

**Comando:** `manage.py dfe_sc_worker` — processo de longa duração (vira serviço).

**Loop:**
```
enquanto não houver sinal de parada:
    1. CAPTURA:
       devidas = empresas com proxima_captura_em vencida, não bloqueadas, livres
       para cada → claim atômico → se conseguiu, submete ao pool
    2. CANCELAMENTOS (se intervalo global venceu):
       submete 1 task de verificar_cancelamentos_sc_pendentes()
    3. RESYNC (se intervalo venceu, por empresa):
       devidas_resync → claim → submete ao pool
    4. dorme até completar DFE_WORKER_CICLO_SEGUNDOS
```

**Pool:** `ThreadPoolExecutor(max_workers=DFE_WORKER_CONCURRENCY)`, instância de
longa duração reusada entre ciclos.

**Ciclo de vida da conexão (crítico em threads):**
```python
def _run_task(fn, *args):
    try:
        fn(*args)               # ex.: capturar_sc_para_cnpj(cnpj)
    finally:
        liberar_claim(...)      # em_execucao = False
        connection.close()      # fecha a conexão thread-local
```
O `connection.close()` no `finally` evita conexões Postgres vazando por thread.

**Parada graciosa:** captura sinal de parada do serviço (SIGINT/SIGTERM; no
Windows o sinal do serviço/CTRL_BREAK) → para de submeter novas tasks, espera as
em andamento drenarem com timeout, libera claims, sai. Restart do serviço não
deixa empresas presas em `em_execucao`.

**Resiliência:** exceção dentro de uma task é capturada e logada (a própria
`capturar_sc_para_cnpj` já trata e grava `ultimo_erro`); exceção inesperada
nunca derruba o loop principal — loga e segue para o próximo ciclo.

## Mudança no botão (web) e status na UI

A view `consulta_capturar` passa a só **enfileirar**:

```python
state, _ = DfeSyncState.objects.get_or_create(empresa=empresa)
if state.em_execucao:
    messages.info(request, 'Captura já em andamento para esta empresa.')
elif _captura_liberada_em(state):           # ainda na janela de bloqueio/espera
    messages.warning(request, 'Captura bloqueada até ... (consumo indevido).')
else:
    state.proxima_captura_em = timezone.now()  # fura a fila
    state.ultimo_erro = ''
    state.save(update_fields=['proxima_captura_em', 'ultimo_erro'])
    messages.success(request, 'Captura agendada — será processada em instantes.')
```

A web **nunca** chama o SEFAZ. O bloqueio de consumo-indevido e a contagem
regressiva já existentes continuam valendo.

**Status na página da empresa:**

| Condição | Badge |
|---|---|
| `em_execucao = True` | 🔄 Capturando agora… |
| `proxima_captura_em <= agora` e livre | ⏳ Na fila |
| `bloqueado_ate > agora` | 🔒 Bloqueado até HH:MM |
| `ultimo_erro` preenchido | ⚠ Última falha: … (já existe) |
| senão | ✓ Em dia (próxima em HH:MM) |

**Auto-refresh sem framework JS:** enquanto `em_execucao`/`na fila`, a página
recarrega a cada ~10s (meta refresh condicional ou `setInterval`); quando fica
"Em dia", para de recarregar.

**Visão geral (opcional, barato):** coluna de status agregado na lista de
empresas, para o operador ver de relance quem está capturando / na fila / com erro.

## Configuração

Lida em `settings.py` no padrão `_env_*` já existente.

| Variável | Default | Significado |
|---|---|---|
| `DFE_WORKER_CONCURRENCY` | `5` | Threads simultâneas (CNPJs em paralelo) |
| `DFE_WORKER_CICLO_SEGUNDOS` | `30` | Intervalo entre ciclos do loop |
| `DFE_CANCELAMENTOS_INTERVALO_MIN` | `60` | Cadência da verificação de cancelamentos |
| `DFE_RESYNC_INTERVALO_HORAS` | `24` | Cadência do resync por empresa |
| `DFE_CLAIM_ORFAO_MIN` | `30` | Idade que torna um claim "órfão" |

## Empacotamento como serviço Windows (documentado, não automatizado)

Criar `docs/deploy-windows.md` com o passo a passo de dois serviços via NSSM:
1. **Web:** `waitress-serve --listen=0.0.0.0:8000 config.wsgi:application`
   (adicionar `waitress` ao `requirements.txt`).
2. **Worker:** `python manage.py dfe_sc_worker`.

Mais a regra de firewall para a porta. Os comandos `nssm install ...` ficam
prontos no doc. Não criar/instalar serviços por código.

## Testes

Cobrir a **orquestração** (a captura em si já é testada). Todos com mock no
cliente SEFAZ — nenhum teste bate na rede real.

- **Claim atômico:** duas reivindicações concorrentes da mesma empresa → só uma
  obtém (`update()` retorna 1 vs 0).
- **Seleção de devidas:** a query respeita `proxima_captura_em`, `bloqueado_ate`,
  `em_execucao`, `ativa`.
- **Claim órfão:** `em_execucao=True` antigo é re-reivindicado; recente não.
- **Liberação no `finally`:** task que estoura exceção ainda libera o claim.
- **Botão enfileira:** a view marca `proxima_captura_em` e **não** chama o SEFAZ
  (mock garante zero chamadas).

**Limpeza pós-testes:** ao final, excluir quaisquer arquivos temporários /
artefatos gerados durante os testes (ex.: bancos de teste residuais, `.zip`/XML
de fixtures, `debug_*_soap.xml` criados em execuções). Não deixar lixo no repo.

## Fora de escopo (YAGNI)

- Celery / broker.
- Distribuição entre múltiplas máquinas.
- Paralelismo por multiprocessing.
- Dashboard de monitoramento dedicado (o status na UI já atende).
