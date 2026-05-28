"""Inspeção de certificados PKCS#12 (.pfx) para validação no cadastro."""
import re
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID


class CertificadoInvalido(Exception):
    pass


def inspecionar_pfx(pfx_bytes: bytes, senha: str) -> dict:
    """Abre o .pfx e extrai CNPJ do titular e data de validade.

    Lança CertificadoInvalido se a senha estiver errada ou o arquivo for inválido.
    """
    try:
        _, cert, _ = pkcs12.load_key_and_certificates(
            pfx_bytes,
            senha.encode('utf-8'),
        )
    except Exception as exc:
        raise CertificadoInvalido(f'Não foi possível abrir o certificado: {exc}')

    if cert is None:
        raise CertificadoInvalido('Certificado não encontrado dentro do .pfx.')

    subject_cn = ''
    for attr in cert.subject:
        if attr.oid == NameOID.COMMON_NAME:
            subject_cn = attr.value
            break

    cnpj = ''
    match = re.search(r'(\d{14})', subject_cn or '')
    if match:
        cnpj = match.group(1)

    return {
        'cnpj_titular': cnpj,
        'subject_cn': subject_cn,
        'nao_antes': cert.not_valid_before_utc,
        'nao_depois': cert.not_valid_after_utc,
    }
