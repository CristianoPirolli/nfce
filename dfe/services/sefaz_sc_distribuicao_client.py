from lxml import etree
from requests_pkcs12 import post


NAMESPACE = 'http://www.satnfce.sef.sc.gov.br/ws/distribuicao-v1'
URL_PRODUCAO = 'https://dfe.sat.sef.sc.gov.br/nfce/ws/distribuicao/DistribuicaoNfceDownload.asmx'


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


def enviar_requisicao(
    xml_dist: bytes,
    cert_pfx_path: str,
    cert_password: str,
    timeout: int = 60
) -> bytes:
    soap = build_soap_envelope(xml_dist)

    with open('debug_request_soap.xml', 'wb') as f:
        f.write(soap)

    headers = {
        'Content-Type': 'application/soap+xml; charset=utf-8',
    }

    response = post(
        URL_PRODUCAO,
        data=soap,
        headers=headers,
        pkcs12_filename=cert_pfx_path,
        pkcs12_password=cert_password,
        timeout=timeout,
    )

    with open('debug_response_soap.xml', 'wb') as f:
        f.write(response.content)

    response.raise_for_status()
    return response.content