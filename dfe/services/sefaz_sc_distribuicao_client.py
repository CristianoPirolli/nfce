import logging
from pathlib import Path
import time

from django.conf import settings
from django.utils import timezone
from lxml import etree
from requests import exceptions as req_exc
from requests_pkcs12 import post

logger = logging.getLogger(__name__)


NAMESPACE = 'http://www.satnfce.sef.sc.gov.br/ws/distribuicao-v1'
URL_PRODUCAO = 'https://dfe.sat.sef.sc.gov.br/nfce/ws/distribuicao/DistribuicaoNfceDownload.asmx'

# Política de retry para falhas transitórias (timeout, conexão, HTTP 5xx).
TENTATIVAS_PADRAO = 3
BACKOFF_INICIAL_S = 1.0


class SefazError(Exception):
    """Erro de comunicação com o web service da SEFAZ-SC."""


class SefazTransitorioError(SefazError):
    """Falha provavelmente temporária (timeout, conexão, HTTP 5xx). Vale re-tentar."""


class SefazPermanenteError(SefazError):
    """Falha que não deve ser re-tentada automaticamente (HTTP 4xx, SSL, payload)."""


def build_xml_dist_nsu(cnpj: str, ult_nsu: int, ver_aplic: str = 'NFCE-DJANGO-1.0') -> bytes:
    root = etree.Element(
        'distNFCeSC',
        versao='1.00',
        nsmap={None: NAMESPACE}
    )

    etree.SubElement(root, 'tpAmb').text = '1'
    etree.SubElement(root, 'verAplic').text = ver_aplic
    etree.SubElement(root, 'cUF').text = '42'
    etree.SubElement(root, 'CNPJ').text = cnpj

    sol_rel = etree.SubElement(root, 'solRel')
    etree.SubElement(sol_rel, 'indXML').text = '1'
    etree.SubElement(sol_rel, 'indAtor').text = '1'
    etree.SubElement(sol_rel, 'ultNuNSU').text = str(ult_nsu)

    return etree.tostring(root, encoding='utf-8', xml_declaration=False)


def build_xml_sol_dfe(cnpj: str, chave_acesso: str, ver_aplic: str = 'NFCE-DJANGO-1.0') -> bytes:
    root = etree.Element(
        'distNFCeSC',
        versao='1.00',
        nsmap={None: NAMESPACE}
    )

    etree.SubElement(root, 'tpAmb').text = '1'
    etree.SubElement(root, 'verAplic').text = ver_aplic
    etree.SubElement(root, 'cUF').text = '42'
    etree.SubElement(root, 'CNPJ').text = cnpj

    sol_dfe = etree.SubElement(root, 'solDFe')
    etree.SubElement(sol_dfe, 'chAcesso').text = chave_acesso

    return etree.tostring(root, encoding='utf-8', xml_declaration=False)


def build_soap_envelope(xml_dist: bytes) -> bytes:
    xml_text = xml_dist.decode('utf-8')

    soap = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">'
        '<soap:Body>'
        '<nfceDownloadContab xmlns="http://www.satnfce.sef.sc.gov.br/ws/distribuicao-v1">'
        f'{xml_text}'
        '</nfceDownloadContab>'
        '</soap:Body>'
        '</soap:Envelope>'
    )

    return soap.encode('utf-8')


def _gravar_debug(nome: str, conteudo: bytes) -> None:
    """Grava o payload de debug sem deixar uma falha de I/O derrubar a captura."""
    try:
        with open(nome, 'wb') as f:
            f.write(conteudo)
    except OSError as exc:
        logger.warning('Não foi possível gravar %s: %s', nome, exc)

_SAFE_XML_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
    load_dtd=False,
)


def _texto(xml_root, tag_name):
    item = xml_root.find(f'.//{{*}}{tag_name}')
    return item.text if item is not None else None


def _int_ou_none(valor):
    if valor in (None, ''):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _metadados_xml_dist(xml_dist: bytes) -> dict:
    dados = {
        'cnpj': '',
        'ambiente': 'producao',
        'ult_nsu_enviado': None,
        'filtro': '',
    }
    try:
        root = etree.fromstring(xml_dist, parser=_SAFE_XML_PARSER)
    except Exception:
        return dados

    dados['cnpj'] = _texto(root, 'CNPJ') or ''
    tp_amb = _texto(root, 'tpAmb')
    dados['ambiente'] = 'producao' if tp_amb == '1' else f'ambiente_{tp_amb or ""}'
    ult_nsu = _texto(root, 'ultNuNSU')
    chave = _texto(root, 'chAcesso')
    if ult_nsu is not None:
        dados['ult_nsu_enviado'] = _int_ou_none(ult_nsu)
        dados['filtro'] = 'solRel'
    elif chave:
        dados['filtro'] = f'solDFe:{chave}'
    return dados


def _metadados_response(response_xml: bytes) -> dict:
    dados = {
        'cstat': '',
        'xmotivo': '',
        'ult_nsu_ret': None,
        'qt_dfe_ret': None,
    }
    try:
        root = etree.fromstring(response_xml, parser=_SAFE_XML_PARSER)
    except Exception:
        return dados

    dados['cstat'] = _texto(root, 'cStat') or ''
    dados['xmotivo'] = _texto(root, 'xMotivo') or ''
    dados['ult_nsu_ret'] = _int_ou_none(_texto(root, 'ultNuNSURet'))
    dados['qt_dfe_ret'] = _int_ou_none(_texto(root, 'qtDfeRet'))
    return dados


def _gravar_payload_historico(cnpj: str, ult_nsu, quando, sufixo: str, conteudo: bytes) -> str:
    pasta = Path(settings.BASE_DIR) / 'logs' / 'sefaz_sc_nfce' / quando.strftime('%Y-%m-%d')
    nsu = 'semnsu' if ult_nsu is None else str(ult_nsu)
    nome = f'{cnpj or "semcnpj"}_{nsu}_{quando.strftime("%Y%m%d_%H%M%S_%f")}_{sufixo}.xml'
    caminho = pasta / nome
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        caminho.write_bytes(conteudo)
    except OSError as exc:
        logger.warning('NÃ£o foi possÃ­vel gravar histÃ³rico SOAP %s: %s', caminho, exc)
        return ''
    return str(caminho)


def _registrar_chamada(
    *,
    xml_dist: bytes,
    soap: bytes,
    response_xml: bytes = b'',
    cert_identificador: str = '',
    tipo_execucao: str = '',
    tentativa: int = 1,
    http_status=None,
    tempo_resposta_ms=None,
    erro_tecnico: str = '',
):
    from dfe.models import SefazScChamada

    meta_req = _metadados_xml_dist(xml_dist)
    meta_resp = _metadados_response(response_xml) if response_xml else {}
    quando = timezone.now()
    request_path = _gravar_payload_historico(
        meta_req['cnpj'], meta_req['ult_nsu_enviado'], quando, 'request', soap
    )
    response_path = ''
    if response_xml:
        response_path = _gravar_payload_historico(
            meta_req['cnpj'], meta_req['ult_nsu_enviado'], quando, 'response', response_xml
        )

    try:
        SefazScChamada.objects.create(
            cnpj=meta_req['cnpj'],
            certificado=cert_identificador or '',
            ambiente=meta_req['ambiente'],
            tipo_execucao=tipo_execucao or '',
            filtro=meta_req['filtro'],
            ult_nsu_enviado=meta_req['ult_nsu_enviado'],
            cstat=meta_resp.get('cstat', ''),
            xmotivo=meta_resp.get('xmotivo', ''),
            ult_nsu_ret=meta_resp.get('ult_nsu_ret'),
            qt_dfe_ret=meta_resp.get('qt_dfe_ret'),
            http_status=http_status,
            tempo_resposta_ms=tempo_resposta_ms,
            erro_tecnico=erro_tecnico,
            request_path=request_path,
            response_path=response_path,
            tentativa=tentativa,
        )
    except Exception:
        logger.exception('Falha ao registrar histÃ³rico da chamada SEFAZ-SC.')


def enviar_requisicao(
    xml_dist: bytes,
    cert_pfx_path: str,
    cert_password: str,
    timeout: int = 60,
    tentativas: int = TENTATIVAS_PADRAO,
    cert_identificador: str = '',
    tipo_execucao: str = '',
) -> bytes:
    """Envia o SOAP à SEFAZ-SC com retry/backoff em falhas transitórias.

    Levanta:
      - SefazPermanenteError: falha que não vale re-tentar (HTTP 4xx, SSL).
      - SefazTransitorioError: falha transitória após esgotar as tentativas.
    """
    soap = build_soap_envelope(xml_dist)
    _gravar_debug('debug_request_soap.xml', soap)

    headers = {
        'Content-Type': 'application/soap+xml; charset=utf-8',
    }

    espera = BACKOFF_INICIAL_S
    ultimo_erro = None

    for tentativa in range(1, tentativas + 1):
        inicio = time.monotonic()
        try:
            response = post(
                URL_PRODUCAO,
                data=soap,
                headers=headers,
                pkcs12_filename=cert_pfx_path,
                pkcs12_password=cert_password,
                timeout=timeout,
            )
        except req_exc.SSLError as exc:
            tempo_ms = int((time.monotonic() - inicio) * 1000)
            _registrar_chamada(
                xml_dist=xml_dist,
                soap=soap,
                cert_identificador=cert_identificador,
                tipo_execucao=tipo_execucao,
                tentativa=tentativa,
                tempo_resposta_ms=tempo_ms,
                erro_tecnico=f'Erro de SSL/certificado: {exc}',
            )
            # Problema de certificado/handshake — não adianta re-tentar.
            raise SefazPermanenteError(
                f'Erro de SSL/certificado na conexão com a SEFAZ-SC: {exc}'
            ) from exc
        except (req_exc.ConnectionError, req_exc.Timeout) as exc:
            tempo_ms = int((time.monotonic() - inicio) * 1000)
            _registrar_chamada(
                xml_dist=xml_dist,
                soap=soap,
                cert_identificador=cert_identificador,
                tipo_execucao=tipo_execucao,
                tentativa=tentativa,
                tempo_resposta_ms=tempo_ms,
                erro_tecnico=f'Falha de conexÃ£o/timeout: {exc}',
            )
            ultimo_erro = SefazTransitorioError(
                f'Falha de conexão/timeout com a SEFAZ-SC: {exc}'
            )
        except req_exc.RequestException as exc:
            tempo_ms = int((time.monotonic() - inicio) * 1000)
            _registrar_chamada(
                xml_dist=xml_dist,
                soap=soap,
                cert_identificador=cert_identificador,
                tipo_execucao=tipo_execucao,
                tentativa=tentativa,
                tempo_resposta_ms=tempo_ms,
                erro_tecnico=f'Falha na requisiÃ§Ã£o: {exc}',
            )
            ultimo_erro = SefazTransitorioError(
                f'Falha na requisição à SEFAZ-SC: {exc}'
            )
        else:
            tempo_ms = int((time.monotonic() - inicio) * 1000)
            status = response.status_code
            _registrar_chamada(
                xml_dist=xml_dist,
                soap=soap,
                response_xml=response.content,
                cert_identificador=cert_identificador,
                tipo_execucao=tipo_execucao,
                tentativa=tentativa,
                http_status=status,
                tempo_resposta_ms=tempo_ms,
                erro_tecnico='' if status < 400 else f'HTTP {status}',
            )
            if status >= 500:
                _gravar_debug('debug_response_soap.xml', response.content)
                ultimo_erro = SefazTransitorioError(
                    f'SEFAZ-SC indisponível (HTTP {status}).'
                )
            elif status >= 400:
                _gravar_debug('debug_response_soap.xml', response.content)
                raise SefazPermanenteError(
                    f'SEFAZ-SC rejeitou a requisição (HTTP {status}).'
                )
            else:
                _gravar_debug('debug_response_soap.xml', response.content)
                return response.content

        logger.warning(
            'Tentativa %d/%d falhou ao contatar a SEFAZ-SC: %s',
            tentativa, tentativas, ultimo_erro,
        )
        if tentativa < tentativas:
            time.sleep(espera)
            espera *= 2

    raise ultimo_erro or SefazTransitorioError(
        'Falha desconhecida ao contatar a SEFAZ-SC.'
    )
