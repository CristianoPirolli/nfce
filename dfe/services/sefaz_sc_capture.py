import base64
import gzip
import logging
from datetime import datetime, timedelta
from io import BytesIO
from lxml import etree
from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

_SAFE_XML_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
    load_dtd=False,
)


def _safe_fromstring(data):
    return etree.fromstring(data, parser=_SAFE_XML_PARSER)


import time
import zipfile


def _nome_entrada_zip(chave: str, tipo: str) -> str:
    if tipo == 'NFE_PROC':
        return f'{chave}.xml'
    return f'{chave}-evento_cancelada.xml'


def _ano_mes_da_chave(chave: str):
    """Extrai (ano, mês) dos dígitos AAMM da chave de acesso da NFC-e.

    Layout chNFe (44 dígitos): cUF(2) + AAMM(4) + CNPJ(14) + mod(2) + ...
    Retorna (None, None) se a chave não tem o formato esperado.
    """
    if not chave or len(chave) < 6 or not chave.isdigit():
        return None, None
    aa = int(chave[2:4])
    mm = int(chave[4:6])
    if not (1 <= mm <= 12):
        return None, None
    return 2000 + aa, mm


def _caminho_zip(empresa, emitido_em, chave: str = ''):
    """Resolve (pasta, arquivo_zip) para a NFC-e ou evento.

    Ordem de precedência para o mês/ano:
      1) emitido_em (dhEmi do XML) — válido para NFE_PROC
      2) AAMM extraído da chave    — usado para eventos (dhRegEvento ≠ dhEmi)
      3) now() — fallback final
    """
    root = getattr(settings, 'NFCE_XML_ROOT', None)
    if not root:
        return None, None

    if emitido_em:
        ano, mes = emitido_em.year, emitido_em.month
    else:
        ano, mes = _ano_mes_da_chave(chave)
        if not ano:
            agora = timezone.now()
            ano, mes = agora.year, agora.month

    pasta = root / empresa.cnpj / f'{ano:04d}'
    return pasta, pasta / f'{mes:02d}.zip'


def _entradas_existentes(arquivo_zip, tentativas=4) -> set:
    """Retorna o conjunto de nomes de entrada já presentes no ZIP, ou vazio se não existe."""
    import zipfile as _zf
    espera = 0.5
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            with _zf.ZipFile(arquivo_zip, 'r') as zf:
                return set(zf.namelist())
        except (FileNotFoundError, _zf.BadZipFile):
            return set()
        except OSError as exc:
            ultimo_erro = exc
            logger.warning(
                'Falha ao ler entradas de %s (tentativa %d/%d): %s',
                arquivo_zip, tentativa, tentativas, exc,
            )
            time.sleep(espera)
            espera *= 2
    raise ultimo_erro


def _gravar_zip_com_retry(arquivo_zip, pasta, entradas, tentativas=4):
    """Abre o .zip uma única vez e escreve todas as entradas.

    `entradas` é uma lista de tuplas (nome_entrada, xml_str).
    Faz retry com backoff em OSError (lock SMB, antivírus, share intermitente).
    """
    espera = 0.5
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            pasta.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(arquivo_zip, mode='a', compression=zipfile.ZIP_DEFLATED) as zf:
                for nome, xml in entradas:
                    zf.writestr(nome, xml)
            return
        except OSError as exc:
            ultimo_erro = exc
            logger.warning(
                'Falha ao escrever %s (tentativa %d/%d): %s',
                arquivo_zip, tentativa, tentativas, exc,
            )
            time.sleep(espera)
            espera *= 2
    raise ultimo_erro


def _gravar_zip_sem_duplicata(arquivo_zip, pasta, entradas, tentativas=4):
    """Igual a _gravar_zip_com_retry, mas ignora entradas já presentes no ZIP.

    Garante idempotência: re-processar um lote após falha parcial do ZIP não
    cria entradas duplicadas no arquivo.
    """
    existentes = _entradas_existentes(arquivo_zip, tentativas=tentativas)
    novas = [(nome, xml) for nome, xml in entradas if nome not in existentes]
    if novas:
        _gravar_zip_com_retry(arquivo_zip, pasta, novas, tentativas=tentativas)


def _extrair_resumo(xml_str: str) -> dict:
    """Extrai número, série e valor total do XML para denormalizar no DB."""
    out = {'numero_nfce': '', 'serie': '', 'valor_total': None}
    try:
        root = _safe_fromstring(xml_str.encode('utf-8'))
    except Exception:
        return out

    def _t(tag):
        el = root.find(f'.//{{*}}{tag}')
        return el.text if el is not None and el.text else ''

    out['numero_nfce'] = _t('nNF')
    out['serie'] = _t('serie')
    valor = _t('vNF')
    if valor:
        from decimal import Decimal, InvalidOperation
        try:
            out['valor_total'] = Decimal(valor)
        except InvalidOperation:
            pass
    return out


def _extrair_dh_emi(xml_str: str):
    """Lê <dhEmi> do XML e devolve datetime aware (ou None)."""
    try:
        root = _safe_fromstring(xml_str.encode('utf-8'))
    except Exception:
        return None
    el = root.find('.//{*}dhEmi')
    if el is None or not el.text:
        return None
    try:
        return datetime.fromisoformat(el.text)
    except ValueError:
        return None

from dfe.models import Empresa, DfeSetting, DfeSyncState, NfceDocumento, CapturaHistorico
from dfe.services.sefaz_sc_distribuicao_client import (
    build_xml_dist_nsu,
    build_xml_sol_dfe,
    enviar_requisicao,
    SefazError,
    SefazPermanenteError,
    SefazTransitorioError,
)


# Re-tentativa após falha de comunicação.
RETRY_TRANSITORIO_MIN = 10   # timeout/conexão/5xx — provável instabilidade momentânea
RETRY_PERMANENTE_MIN = 60    # SSL/4xx/config — espera mais antes de tentar de novo

# Fallback de agendamento (em horas) por cStat após uma resposta válida.
# QUALQUER cStat não listado usa o default — nunca deixa o CNPJ "devido"
# indefinidamente (proteção contra consumo indevido do SEFAZ).
_INTERVALO_PROXIMA_HORAS = {
    '110': 1,    # nenhum NSU novo — pode reconsultar em 1h
    '117': 12,   # sincronizado (nada novo) — folga maior
    '118': 12,   # houve documentos mas a rodada parou (parcial/teto) — conservador
    '108': 1,    # serviço paralisado momentaneamente — espera o serviço voltar
    '109': 1,    # serviço paralisado sem previsão — idem
}
_INTERVALO_PROXIMA_DEFAULT_HORAS = 1


def _intervalo_proxima(ultimo_cstat):
    """timedelta até a próxima captura, sempre >= 1h, com fallback seguro."""
    horas = _INTERVALO_PROXIMA_HORAS.get(ultimo_cstat, _INTERVALO_PROXIMA_DEFAULT_HORAS)
    return timedelta(hours=horas)


def extrair_texto(xml_root, tag_name):
    item = xml_root.find(f'.//{{*}}{tag_name}')
    return item.text if item is not None else None


def descompactar_lote(lote_dist_comp: str) -> bytes:
    raw = base64.b64decode(lote_dist_comp)
    return gzip.GzipFile(fileobj=BytesIO(raw)).read()


def _resolver_cert(empresa: Empresa):
    """Retorna (cert_path, cert_password, ver_aplic).

    Prioridade:
      1) Certificado próprio da empresa (override).
      2) Certificado do contador via upload criptografado (DfeSetting.cert_pfx).
      3) Configuração legada por caminho no disco (DfeSetting.cert_path).
    """
    if empresa.cert_pfx and empresa.cert_password_encrypted:
        return (
            empresa.cert_pfx.path,
            empresa.get_cert_password(),
            empresa.ver_aplic or 'NFCE-DJANGO-1.0',
        )

    setting = DfeSetting.objects.first()
    if not setting:
        raise Exception(
            f'Empresa {empresa.cnpj} não possui certificado próprio e o '
            f'certificado do contador não está configurado.'
        )

    if setting.cert_pfx and setting.cert_password_encrypted:
        return (
            setting.cert_pfx.path,
            setting.get_cert_password(),
            setting.ver_aplic or 'NFCE-DJANGO-1.0',
        )

    if setting.cert_path and setting.cert_password:
        return (
            setting.cert_path,
            setting.cert_password,
            setting.ver_aplic or 'NFCE-DJANGO-1.0',
        )

    raise Exception(
        f'Empresa {empresa.cnpj} não possui certificado próprio e o '
        f'certificado do contador não está configurado.'
    )


MAX_LOTES_POR_CAPTURA = 200  # teto de segurança (200 * 50 = 10.000 docs)


def capturar_sc_para_cnpj(cnpj: str, max_lotes: int = MAX_LOTES_POR_CAPTURA):
    """Captura iterativamente todos os lotes disponíveis na SEF-SC."""
    empresa = Empresa.objects.get(cnpj=cnpj)
    state, _ = DfeSyncState.objects.get_or_create(empresa=empresa)

    # Inicia registro de histórico
    historico = CapturaHistorico.objects.create(
        empresa=empresa,
        nsu_inicial=state.ultimo_nsu_sc
    )

    # Resolução de certificado: falha aqui é de configuração (sem cert / senha
    # não decriptável) — trata como permanente, registra e sai sem chamar o WS.
    try:
        cert_path, cert_password, ver_aplic = _resolver_cert(empresa)
    except Exception as exc:
        res = _finalizar_com_erro(
            state, cnpj, str(exc), transitorio=False,
            parou_por='erro_certificado',
            nsu_inicial=state.ultimo_nsu_sc,
        )
        historico.fim = timezone.now()
        historico.erro = str(exc)
        historico.save()
        return res

    lotes = []
    total_docs = 0
    total_vigentes = 0
    total_canceladas = 0
    nsu_inicial = state.ultimo_nsu_sc
    ultimo_cstat = None
    ultimo_motivo = None
    parou_por = 'sem_mais_documentos'
    erro = None
    erro_transitorio = False

    for i in range(max_lotes):
        nsu_antes = state.ultimo_nsu_sc

        xml_dist = build_xml_dist_nsu(
            cnpj=empresa.cnpj,
            ult_nsu=nsu_antes,
            ver_aplic=ver_aplic,
        )

        # Comunicação + parsing da resposta. Falhas aqui param a captura
        # graciosamente, preservando o NSU já avançado nas iterações anteriores.
        try:
            response_xml = enviar_requisicao(
                xml_dist=xml_dist,
                cert_pfx_path=cert_path,
                cert_password=cert_password,
            )
            root = _safe_fromstring(response_xml)
        except SefazPermanenteError as exc:
            erro, erro_transitorio = str(exc), False
            parou_por = 'erro_permanente'
            break
        except SefazTransitorioError as exc:
            erro, erro_transitorio = str(exc), True
            parou_por = 'erro_transitorio'
            break
        except etree.XMLSyntaxError as exc:
            # Resposta ilegível (página de erro de gateway, HTML, etc.) — transitório.
            erro = f'Resposta da SEFAZ-SC ilegível (XML inválido): {exc}'
            erro_transitorio = True
            parou_por = 'resposta_invalida'
            break

        cstat = extrair_texto(root, 'cStat')

        # Sem cStat = corpo inesperado (possível SOAP Fault). Não avança às cegas.
        if cstat is None:
            erro = 'Resposta da SEFAZ-SC sem cStat (possível SOAP Fault).'
            erro_transitorio = True
            parou_por = 'resposta_sem_cstat'
            break

        xmotivo = extrair_texto(root, 'xMotivo')
        ult_nsu_ret = extrair_texto(root, 'ultNuNSURet')
        qt_dfe_ret = extrair_texto(root, 'qtDfeRet')
        lote_dist_comp = extrair_texto(root, 'loteDistComp')

        qt_int = int(qt_dfe_ret) if qt_dfe_ret else 0
        ultimo_cstat = cstat
        ultimo_motivo = xmotivo
        state.ultimo_cstat = cstat
        state.ultimo_motivo = xmotivo
        state.ultima_captura = timezone.now()

        if ult_nsu_ret:
            state.ultimo_nsu_sc = int(ult_nsu_ret)

        # Persistência do lote: erro de gravação aqui é tratado como transitório
        # (lock SMB, disco) e não deve avançar o NSU sem ter salvo os documentos.
        if cstat == '118' and lote_dist_comp:
            try:
                lote_xml = descompactar_lote(lote_dist_comp)
                res_lote = persistir_lote(empresa, lote_xml)
                total_vigentes += res_lote.get('novas_vigentes', 0)
                total_canceladas += res_lote.get('novas_canceladas', 0)
            except Exception as exc:
                # Reverte o avanço de NSU desta iteração para reprocessar depois.
                state.ultimo_nsu_sc = nsu_antes
                erro = f'Falha ao salvar o lote (NSU {nsu_antes}): {exc}'
                erro_transitorio = True
                parou_por = 'erro_persistencia'
                break

        lotes.append({
            'iteracao': i + 1,
            'cstat': cstat,
            'xmotivo': xmotivo,
            'nsu_antes': nsu_antes,
            'nsu_depois': state.ultimo_nsu_sc,
            'qt_dfe_ret': qt_int,
        })
        total_docs += qt_int

        # Bloqueio / throttle do WS — para imediatamente e agenda re-tentativa.
        if cstat == '657':
            state.bloqueado_ate = timezone.now() + timezone.timedelta(hours=1)
            state.proxima_captura_em = state.bloqueado_ate
            parou_por = 'bloqueado_657'
            break

        # Critérios de parada normais.
        if cstat != '118':
            parou_por = f'cstat_{cstat}'
            break
        if qt_int < 50:
            parou_por = 'lote_parcial'
            break
        if state.ultimo_nsu_sc == nsu_antes:
            parou_por = 'nsu_sem_avanco'
            break
    else:
        parou_por = 'limite_max_lotes'

    # Finaliza registro de histórico
    historico.fim = timezone.now()
    historico.nsu_final = state.ultimo_nsu_sc
    historico.qt_vigentes = total_vigentes
    historico.qt_canceladas = total_canceladas
    historico.cstat = ultimo_cstat
    historico.xmotivo = ultimo_motivo
    historico.erro = erro
    historico.save()

    if erro:
        # Mantém o que já foi capturado nesta rodada, mas registra a falha e
        # agenda re-tentativa proporcional ao tipo de erro.
        minutos = RETRY_TRANSITORIO_MIN if erro_transitorio else RETRY_PERMANENTE_MIN
        state.ultimo_erro = erro
        state.ultimo_erro_em = timezone.now()
        state.proxima_captura_em = timezone.now() + timezone.timedelta(minutes=minutos)
        state.save()
        return {
            'cnpj': cnpj,
            'cstat': ultimo_cstat,
            'xmotivo': ultimo_motivo,
            'ult_nsu': state.ultimo_nsu_sc,
            'nsu_inicial': nsu_inicial,
            'qt_dfe_ret': total_docs,
            'lotes': lotes,
            'parou_por': parou_por,
            'erro': erro,
            'erro_transitorio': erro_transitorio,
        }

    # Sucesso — limpa erro anterior e SEMPRE agenda a próxima captura.
    # Crítico contra consumo indevido: qualquer cStat não previsto cai no
    # fallback seguro (>=1h), para que nenhum CNPJ fique "devido" para sempre
    # e seja re-consultado a cada ciclo do worker.
    state.ultimo_erro = ''
    state.ultimo_erro_em = None
    state.proxima_captura_em = timezone.now() + _intervalo_proxima(ultimo_cstat)

    state.save()

    return {
        'cnpj': cnpj,
        'cstat': ultimo_cstat,
        'xmotivo': ultimo_motivo,
        'ult_nsu': state.ultimo_nsu_sc,
        'nsu_inicial': nsu_inicial,
        'qt_dfe_ret': total_docs,
        'lotes': lotes,
        'parou_por': parou_por,
        'erro': None,
    }


def _finalizar_com_erro(state, cnpj, mensagem, transitorio, parou_por, nsu_inicial):
    """Registra uma falha pré-loop no estado e devolve o dict de resultado padrão."""
    minutos = RETRY_TRANSITORIO_MIN if transitorio else RETRY_PERMANENTE_MIN
    state.ultimo_erro = mensagem
    state.ultimo_erro_em = timezone.now()
    state.proxima_captura_em = timezone.now() + timezone.timedelta(minutes=minutos)
    state.save()
    return {
        'cnpj': cnpj,
        'cstat': None,
        'xmotivo': None,
        'ult_nsu': state.ultimo_nsu_sc,
        'nsu_inicial': nsu_inicial,
        'qt_dfe_ret': 0,
        'lotes': [],
        'parou_por': parou_por,
        'erro': mensagem,
        'erro_transitorio': transitorio,
    }


def persistir_lote(empresa: Empresa, lote_xml: bytes) -> dict:
    """Persiste todos os documentos de um lote agrupando escritas por .zip mensal.

    Estratégia: extrai todos os documentos do lote → agrupa por (pasta, arquivo_zip)
    → abre cada zip uma única vez (com retry) → comita as linhas no DB.
    Reduz drasticamente locks/permission-denied no compartilhamento SMB.
    """
    lote_root = _safe_fromstring(lote_xml)

    # Etapa 1 — extrai e classifica todos os documentos do lote.
    docs = []
    for dist in lote_root.findall('.//{*}distNFCeSC'):
        nsu = dist.attrib.get('NSU')
        chave = dist.attrib.get('chAcesso')

        nfe_proc = dist.find('.//{*}nfeProc')
        proc_evento = dist.find('.//{*}procEventoNFe')

        if nfe_proc is not None:
            tipo = 'NFE_PROC'
            xml = etree.tostring(nfe_proc, encoding='unicode')
        elif proc_evento is not None:
            tipo = 'EVENTO_CANCELAMENTO'
            xml = etree.tostring(proc_evento, encoding='unicode')
        else:
            tipo = 'OUTRO_EVENTO'
            xml = etree.tostring(dist, encoding='unicode')

        if not chave:
            chave = extrair_chave_do_xml(xml)
        if not chave:
            continue

        emitido_em = _extrair_dh_emi(xml)
        pasta, arquivo_zip = _caminho_zip(empresa, emitido_em, chave)
        entrada = _nome_entrada_zip(chave, tipo)

        resumo = _extrair_resumo(xml) if tipo == 'NFE_PROC' else {
            'numero_nfce': '', 'serie': '', 'valor_total': None,
        }

        docs.append({
            'nsu': int(nsu),
            'chave': chave,
            'tipo': tipo,
            'xml': xml,
            'emitido_em': emitido_em,
            'pasta': pasta,
            'arquivo_zip': arquivo_zip,
            'entrada': entrada,
            'numero_nfce': resumo['numero_nfce'],
            'serie': resumo['serie'],
            'valor_total': resumo['valor_total'],
        })

    if not docs:
        return {'novas_vigentes': 0, 'novas_canceladas': 0}

    # Etapa 2 — DB primeiro, dentro de uma transação atômica.
    cancelamentos = []
    novas_vigentes = 0
    with transaction.atomic():
        for d in docs:
            obj, created = NfceDocumento.objects.update_or_create(
                empresa=empresa,
                chave_acesso=d['chave'],
                nsu=d['nsu'],
                defaults={
                    'tipo_documento': d['tipo'],
                    'arquivo_zip': str(d['arquivo_zip']) if d['arquivo_zip'] else '',
                    'arquivo_entrada': d['entrada'],
                    'emitido_em': d['emitido_em'],
                    'numero_nfce': d['numero_nfce'],
                    'serie': d['serie'],
                    'valor_total': d['valor_total'],
                    'cancelada': False,
                }
            )
            if created and d['tipo'] == 'NFE_PROC':
                novas_vigentes += 1
            
            if d['tipo'] == 'EVENTO_CANCELAMENTO':
                cancelamentos.append(d['chave'])

        novas_canceladas = 0
        if cancelamentos:
            # Marca como cancelada e conta quantas foram de fato alteradas para canceladas
            # que antes eram vigentes.
            novas_canceladas = NfceDocumento.objects.filter(
                empresa=empresa,
                chave_acesso__in=cancelamentos,
                tipo_documento='NFE_PROC',
                cancelada=False
            ).update(
                cancelada=True,
                cancelamento_verificado_em=timezone.now(),
            )
            # Ajusta o contador de vigentes se uma nota que entrou agora como vigente
            # também teve seu cancelamento no mesmo lote.
            # (É raro mas possível se o lote trouxer NFe e logo depois o Evento)
            # No entanto, o update_or_create acima já contou como vigente.
            # Para simplificar, as canceladas são contadas separadamente.

    # Etapa 3 — ZIP...
    if getattr(settings, 'NFCE_XML_ROOT', None):
        grupos = {}
        for d in docs:
            chave_grupo = str(d['arquivo_zip'])
            grupos.setdefault(chave_grupo, {'pasta': d['pasta'], 'zip': d['arquivo_zip'], 'entradas': []})
            grupos[chave_grupo]['entradas'].append((d['entrada'], d['xml']))

        for g in grupos.values():
            _gravar_zip_sem_duplicata(g['zip'], g['pasta'], g['entradas'])
            
    return {'novas_vigentes': novas_vigentes, 'novas_canceladas': novas_canceladas}


def extrair_chave_do_xml(xml: str):
    try:
        root = _safe_fromstring(xml.encode('utf-8'))

        inf_nfe = root.find('.//{*}infNFe')
        if inf_nfe is not None:
            id_attr = inf_nfe.attrib.get('Id')
            if id_attr and id_attr.startswith('NFe'):
                return id_attr.replace('NFe', '')

        inf_evento = root.find('.//{*}infEvento')
        if inf_evento is not None:
            ch_nfe = inf_evento.find('.//{*}chNFe')
            if ch_nfe is not None:
                return ch_nfe.text

    except Exception:
        return None

    return None


def resync_sc_para_cnpj(cnpj: str):
    empresa = Empresa.objects.get(cnpj=cnpj)
    cert_path, cert_password, ver_aplic = _resolver_cert(empresa)

    state, _ = DfeSyncState.objects.get_or_create(empresa=empresa)

    if not state.resync_ativo:
        return {
            'cnpj': cnpj,
            'status': 'RESYNC_INATIVO',
            'mensagem': 'Resync desativado para esta empresa.',
        }

    nsu_original = state.ultimo_nsu_sc
    nsu_resync = state.resync_nsu_inicial or 0

    xml_dist = build_xml_dist_nsu(
        cnpj=empresa.cnpj,
        ult_nsu=nsu_resync,
        ver_aplic=ver_aplic,
    )

    try:
        response_xml = enviar_requisicao(
            xml_dist=xml_dist,
            cert_pfx_path=cert_path,
            cert_password=cert_password,
        )
        root = _safe_fromstring(response_xml)
    except (SefazError, etree.XMLSyntaxError) as exc:
        transitorio = not isinstance(exc, SefazPermanenteError)
        minutos = RETRY_TRANSITORIO_MIN if transitorio else RETRY_PERMANENTE_MIN
        state.ultimo_erro = f'Resync: {exc}'
        state.ultimo_erro_em = timezone.now()
        state.proxima_captura_em = timezone.now() + timezone.timedelta(minutes=minutos)
        state.save()
        return {
            'cnpj': cnpj,
            'tipo': 'resync',
            'status': 'ERRO',
            'erro': str(exc),
            'erro_transitorio': transitorio,
            'nsu_original': nsu_original,
            'resync_nsu_inicial': nsu_resync,
        }

    cstat = extrair_texto(root, 'cStat')
    xmotivo = extrair_texto(root, 'xMotivo')
    ult_nsu_ret = extrair_texto(root, 'ultNuNSURet')
    qt_dfe_ret = extrair_texto(root, 'qtDfeRet')
    lote_dist_comp = extrair_texto(root, 'loteDistComp')

    if cstat == '118' and lote_dist_comp:
        try:
            lote_xml = descompactar_lote(lote_dist_comp)
            persistir_lote(empresa, lote_xml)
        except Exception as exc:
            minutos = (
                RETRY_PERMANENTE_MIN
                if isinstance(exc, PermissionError)
                else RETRY_TRANSITORIO_MIN
            )
            mensagem = f'Resync: falha ao salvar o lote: {exc}'
            state.ultimo_erro = mensagem
            state.ultimo_erro_em = timezone.now()
            state.proxima_captura_em = timezone.now() + timezone.timedelta(
                minutes=minutos
            )
            state.save()
            return {
                'cnpj': cnpj,
                'tipo': 'resync',
                'status': 'ERRO',
                'erro': mensagem,
                'erro_transitorio': minutos == RETRY_TRANSITORIO_MIN,
                'nsu_original': nsu_original,
                'resync_nsu_inicial': nsu_resync,
            }

    if ult_nsu_ret:
        state.resync_nsu_inicial = int(ult_nsu_ret)

    state.ultimo_resync_em = timezone.now()

    # Importante:
    # Não deixa o resync reduzir o cursor principal da captura incremental.
    if nsu_original and state.ultimo_nsu_sc < nsu_original:
        state.ultimo_nsu_sc = nsu_original

    # Se o resync encontrou NSU maior que o incremental, podemos avançar o principal.
    if ult_nsu_ret and int(ult_nsu_ret) > state.ultimo_nsu_sc:
        state.ultimo_nsu_sc = int(ult_nsu_ret)

    state.ultimo_cstat = cstat
    state.ultimo_motivo = xmotivo

    if cstat == '110':
        state.proxima_captura_em = timezone.now() + timezone.timedelta(hours=1)

    elif cstat == '657':
        state.bloqueado_ate = timezone.now() + timezone.timedelta(hours=1)
        state.proxima_captura_em = state.bloqueado_ate

    state.save()

    return {
        'cnpj': cnpj,
        'tipo': 'resync',
        'cstat': cstat,
        'xmotivo': xmotivo,
        'nsu_original': nsu_original,
        'resync_nsu_inicial': nsu_resync,
        'resync_nsu_novo': state.resync_nsu_inicial,
        'ultimo_nsu_sc': state.ultimo_nsu_sc,
        'qt_dfe_ret': qt_dfe_ret,
    }


def verificar_cancelamentos_sc_pendentes():
    documentos = NfceDocumento.objects.filter(
        tipo_documento='NFE_PROC',
        cancelada=False,
        cancelamento_verificado_em__isnull=True,
    ).order_by('capturado_em')[:100]

    resultados = []

    for doc in documentos:
        # Falha de comunicação interrompe a rodada: não marca como verificado
        # (para reprocessar depois) e evita martelar um serviço instável/fora do ar.
        try:
            resultado = consultar_documento_por_chave(
                doc.empresa.cnpj,
                doc.chave_acesso
            )
        except (SefazError, etree.XMLSyntaxError) as exc:
            logger.warning(
                'Verificação de cancelamento interrompida na chave %s: %s',
                doc.chave_acesso, exc,
            )
            resultados.append({
                'chave_acesso': doc.chave_acesso,
                'erro': str(exc),
            })
            break

        if resultado.get('cancelada'):
            doc.cancelada = True

        doc.cancelamento_verificado_em = timezone.now()
        doc.save(update_fields=['cancelada', 'cancelamento_verificado_em'])

        resultados.append(resultado)

    return resultados


def consultar_documento_por_chave(cnpj: str, chave_acesso: str):
    empresa = Empresa.objects.get(cnpj=cnpj)
    cert_path, cert_password, ver_aplic = _resolver_cert(empresa)

    xml_dist = build_xml_sol_dfe(
        cnpj=cnpj,
        chave_acesso=chave_acesso,
        ver_aplic=ver_aplic,
    )

    response_xml = enviar_requisicao(
        xml_dist=xml_dist,
        cert_pfx_path=cert_path,
        cert_password=cert_password,
    )

    root = _safe_fromstring(response_xml)

    cstat = extrair_texto(root, 'cStat')
    xmotivo = extrair_texto(root, 'xMotivo')
    lote_dist_comp = extrair_texto(root, 'loteDistComp')

    cancelada = False

    if lote_dist_comp:
        lote_xml = descompactar_lote(lote_dist_comp)
        if b'procEventoNFe' in lote_xml and (b'110111' in lote_xml or b'110112' in lote_xml):
            cancelada = True

    return {
        'chave_acesso': chave_acesso,
        'cstat': cstat,
        'xmotivo': xmotivo,
        'cancelada': cancelada,
    }
