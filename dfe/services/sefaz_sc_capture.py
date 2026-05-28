import base64
import gzip
from io import BytesIO
from lxml import etree
from django.utils import timezone

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


def capturar_sc_para_cnpj(cnpj: str):
    empresa = Empresa.objects.get(cnpj=cnpj)
    setting = DfeSetting.objects.first()

    state, _ = DfeSyncState.objects.get_or_create(empresa=empresa)

    xml_dist = build_xml_dist_nsu(
        cnpj=empresa.cnpj,
        ult_nsu=state.ultimo_nsu_sc,
        ver_aplic=setting.ver_aplic,
    )

    response_xml = enviar_requisicao(
        xml_dist=xml_dist,
        cert_pfx_path=setting.cert_path,
        cert_password=setting.cert_password,
    )

    root = etree.fromstring(response_xml)

    cstat = extrair_texto(root, 'cStat')
    xmotivo = extrair_texto(root, 'xMotivo')
    ult_nsu_ret = extrair_texto(root, 'ultNuNSURet')
    qt_dfe_ret = extrair_texto(root, 'qtDfeRet')
    lote_dist_comp = extrair_texto(root, 'loteDistComp')

    state.ultimo_cstat = cstat
    state.ultimo_motivo = xmotivo
    state.ultima_captura = timezone.now()

    if ult_nsu_ret:
        state.ultimo_nsu_sc = int(ult_nsu_ret)

    if cstat == '110':
        state.proxima_captura_em = timezone.now() + timezone.timedelta(hours=1)

    elif cstat == '117':
        state.proxima_captura_em = timezone.now() + timezone.timedelta(hours=12)

    elif cstat == '118':
        if lote_dist_comp:
            lote_xml = descompactar_lote(lote_dist_comp)
            persistir_lote(empresa, lote_xml)

        if qt_dfe_ret and int(qt_dfe_ret) < 50:
            state.proxima_captura_em = timezone.now() + timezone.timedelta(hours=12)
        else:
            state.proxima_captura_em = timezone.now()

    elif cstat == '657':
        state.bloqueado_ate = timezone.now() + timezone.timedelta(hours=1)
        state.proxima_captura_em = state.bloqueado_ate

    state.save()

    return {
        'cnpj': cnpj,
        'cstat': cstat,
        'xmotivo': xmotivo,
        'ult_nsu': state.ultimo_nsu_sc,
        'qt_dfe_ret': qt_dfe_ret,
    }


def persistir_lote(empresa: Empresa, lote_xml: bytes):
    lote_root = etree.fromstring(lote_xml)

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
                'cancelada': tipo == 'EVENTO_CANCELAMENTO',
            }
        )


def extrair_chave_do_xml(xml: str):
    try:
        root = etree.fromstring(xml.encode('utf-8'))

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
    setting = DfeSetting.objects.first()

    if not setting:
        raise Exception(
            'Nenhuma configuração DfeSetting encontrada. Cadastre o certificado e as configurações da SEF/SC.'
        )

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
        ver_aplic=setting.ver_aplic,
    )

    response_xml = enviar_requisicao(
        xml_dist=xml_dist,
        cert_pfx_path=setting.cert_path,
        cert_password=setting.cert_password,
    )

    root = etree.fromstring(response_xml)

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
    setting = DfeSetting.objects.first()

    xml_dist = build_xml_sol_dfe(
        cnpj=cnpj,
        chave_acesso=chave_acesso,
        ver_aplic=setting.ver_aplic,
    )

    response_xml = enviar_requisicao(
        xml_dist=xml_dist,
        cert_pfx_path=setting.cert_path,
        cert_password=setting.cert_password,
    )

    root = etree.fromstring(response_xml)

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