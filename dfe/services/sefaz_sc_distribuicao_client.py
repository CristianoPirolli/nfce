import logging
import time

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


def enviar_requisicao(
    xml_dist: bytes,
    cert_pfx_path: str,
    cert_password: str,
    timeout: int = 60,
    tentativas: int = TENTATIVAS_PADRAO,
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
            # Problema de certificado/handshake — não adianta re-tentar.
            raise SefazPermanenteError(
                f'Erro de SSL/certificado na conexão com a SEFAZ-SC: {exc}'
            ) from exc
        except (req_exc.ConnectionError, req_exc.Timeout) as exc:
            ultimo_erro = SefazTransitorioError(
                f'Falha de conexão/timeout com a SEFAZ-SC: {exc}'
            )
        except req_exc.RequestException as exc:
            ultimo_erro = SefazTransitorioError(
                f'Falha na requisição à SEFAZ-SC: {exc}'
            )
        else:
            status = response.status_code
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