import base64
import gzip
from datetime import datetime
from io import BytesIO
from lxml import etree
from django.utils import timezone

_SAFE_XML_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
    load_dtd=False,
)


def _safe_fromstring(data):
    return etree.fromstring(data, parser=_SAFE_XML_PARSER)


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

from dfe.models import Empresa, DfeSetting, DfeSyncState, NfceDocumento
from dfe.services.sefaz_sc_distribuicao_client import (
    build_xml_dist_nsu,
    build_xml_sol_dfe,
    enviar_requisicao,
)


def extrair_texto(xml_root, tag_name):
    item = xml_root.find(f'.//{{*}}{tag_name}')
    return item.text if item is not None else None


def descompactar_lote(lote_dist_comp: str) -> bytes:
    raw = base64.b64decode(lote_dist_comp)
    return gzip.GzipFile(fileobj=BytesIO(raw)).read()


def _resolver_cert(empresa: Empresa):
    """Retorna (cert_path, cert_password, ver_aplic) priorizando o cert da empresa."""
    if empresa.cert_pfx and empresa.cert_password_encrypted:
        return (
            empresa.cert_pfx.path,
            empresa.get_cert_password(),
            empresa.ver_aplic or 'NFCE-DJANGO-1.0',
        )

    setting = DfeSetting.objects.first()
    if not setting:
        raise Exception(
            f'Empresa {empresa.cnpj} não possui certificado cadastrado e não há DfeSetting global.'
        )
    return setting.cert_path, setting.cert_password, setting.ver_aplic


MAX_LOTES_POR_CAPTURA = 200  # teto de segurança (200 * 50 = 10.000 docs)


def capturar_sc_para_cnpj(cnpj: str, max_lotes: int = MAX_LOTES_POR_CAPTURA):
    """Captura iterativamente todos os lotes disponíveis na SEF-SC.

    O serviço retorna até 50 documentos por chamada (cStat=118). Continuamos
    chamando enquanto a SEFAZ devolver lote cheio, parando em cStat≠118,
    qtDfeRet<50, NSU sem avanço, ou no teto max_lotes.
    """
    empresa = Empresa.objects.get(cnpj=cnpj)
    cert_path, cert_password, ver_aplic = _resolver_cert(empresa)

    state, _ = DfeSyncState.objects.get_or_create(empresa=empresa)

    lotes = []
    total_docs = 0
    nsu_inicial = state.ultimo_nsu_sc
    ultimo_cstat = None
    ultimo_motivo = None
    parou_por = 'sem_mais_documentos'

    for i in range(max_lotes):
        nsu_antes = state.ultimo_nsu_sc

        xml_dist = build_xml_dist_nsu(
            cnpj=empresa.cnpj,
            ult_nsu=nsu_antes,
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

        if cstat == '118' and lote_dist_comp:
            lote_xml = descompactar_lote(lote_dist_comp)
            persistir_lote(empresa, lote_xml)

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

    # Agendamento da próxima captura automática (mantém comportamento anterior).
    if ultimo_cstat == '110':
        state.proxima_captura_em = timezone.now() + timezone.timedelta(hours=1)
    elif ultimo_cstat == '117':
        state.proxima_captura_em = timezone.now() + timezone.timedelta(hours=12)
    elif ultimo_cstat == '118':
        state.proxima_captura_em = timezone.now() + timezone.timedelta(hours=12)

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
    }


def persistir_lote(empresa: Empresa, lote_xml: bytes):
    lote_root = _safe_fromstring(lote_xml)

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

        NfceDocumento.objects.update_or_create(
            empresa=empresa,
            chave_acesso=chave,
            nsu=int(nsu),
            defaults={
                'tipo_documento': tipo,
                'xml': xml,
                'emitido_em': _extrair_dh_emi(xml),
                # Evento em si não é "cancelado"; quem fica cancelada é a NFC-e ligada.
                'cancelada': False,
            }
        )

        # Ao receber um evento de cancelamento (tpEvento 110111/110112),
        # marca a NFC-e correspondente como cancelada.
        if tipo == 'EVENTO_CANCELAMENTO':
            NfceDocumento.objects.filter(
                empresa=empresa,
                chave_acesso=chave,
                tipo_documento='NFE_PROC',
            ).update(
                cancelada=True,
                cancelamento_verificado_em=timezone.now(),
            )


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

    response_xml = enviar_requisicao(
        xml_dist=xml_dist,
        cert_pfx_path=cert_path,
        cert_password=cert_password,
    )

    root = _safe_fromstring(response_xml)

    cstat = extrair_texto(root, 'cStat')
    xmotivo = extrair_texto(root, 'xMotivo')
    ult_nsu_ret = extrair_texto(root, 'ultNuNSURet')
    qt_dfe_ret = extrair_texto(root, 'qtDfeRet')
    lote_dist_comp = extrair_texto(root, 'loteDistComp')

    if cstat == '118' and lote_dist_comp:
        lote_xml = descompactar_lote(lote_dist_comp)
        persistir_lote(empresa, lote_xml)

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
        resultado = consultar_documento_por_chave(
            doc.empresa.cnpj,
            doc.chave_acesso
        )

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