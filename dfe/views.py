import zipfile
from datetime import datetime
from pathlib import Path

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from lxml import etree


def _ler_xml(doc) -> str:
    """Lê o XML do documento a partir do .zip mensal indicado em arquivo_zip."""
    if not doc.arquivo_zip or not doc.arquivo_entrada:
        return ''
    caminho = Path(doc.arquivo_zip)
    if not caminho.exists():
        return ''
    try:
        with zipfile.ZipFile(caminho, mode='r') as zf:
            return zf.read(doc.arquivo_entrada).decode('utf-8')
    except (KeyError, OSError, zipfile.BadZipFile):
        return ''

_SAFE_XML_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
    load_dtd=False,
)

from .forms import CertificadoUploadForm, EmpresaForm
from .models import Empresa, NfceDocumento


def lista_empresas(request):
    empresas = Empresa.objects.order_by('razao_social')
    return render(request, 'dfe/certificados/lista.html', {'empresas': empresas})


def nova_empresa(request):
    if request.method == 'POST':
        form = EmpresaForm(request.POST)
        if form.is_valid():
            empresa = form.save()
            messages.success(request, 'Empresa cadastrada. Agora envie o certificado.')
            return redirect('dfe:upload_certificado', empresa_id=empresa.id)
    else:
        form = EmpresaForm()

    return render(request, 'dfe/certificados/empresa_form.html', {'form': form})


def upload_certificado(request, empresa_id):
    empresa = get_object_or_404(Empresa, id=empresa_id)

    if request.method == 'POST':
        form = CertificadoUploadForm(request.POST, request.FILES, empresa=empresa)
        if form.is_valid():
            info = form.info_cert
            if empresa.cert_pfx:
                empresa.cert_pfx.delete(save=False)

            empresa.cert_pfx = form.cleaned_data['cert_pfx']
            empresa.set_cert_password(form.cleaned_data['cert_password'])
            empresa.cert_cnpj_titular = info['cnpj_titular']
            empresa.cert_validade = info['nao_depois']
            empresa.save()

            messages.success(
                request,
                f"Certificado válido até {info['nao_depois'].strftime('%d/%m/%Y')} foi salvo."
            )
            return redirect('dfe:lista_empresas')
    else:
        form = CertificadoUploadForm(empresa=empresa)

    return render(request, 'dfe/certificados/upload.html', {
        'form': form,
        'empresa': empresa,
    })


def remover_certificado(request, empresa_id):
    empresa = get_object_or_404(Empresa, id=empresa_id)
    if request.method == 'POST':
        if empresa.cert_pfx:
            empresa.cert_pfx.delete(save=False)
        empresa.cert_pfx = None
        empresa.set_cert_password('')
        empresa.cert_cnpj_titular = ''
        empresa.cert_validade = None
        empresa.save()
        messages.success(request, 'Certificado removido.')
    return redirect('dfe:lista_empresas')


def _resumo_nfce(doc: NfceDocumento) -> dict:
    """Extrai número, série, valor e data de emissão do XML para exibição."""
    out = {'numero': '', 'serie': '', 'valor': '', 'emitido_em': ''}
    xml = _ler_xml(doc)
    if not xml:
        return out
    try:
        root = etree.fromstring(xml.encode('utf-8'), parser=_SAFE_XML_PARSER)
    except Exception:
        return out

    def _t(tag):
        el = root.find(f'.//{{*}}{tag}')
        return el.text if el is not None and el.text else ''

    out['numero'] = _t('nNF')
    out['serie'] = _t('serie')
    out['valor'] = _t('vNF')
    dh = _t('dhEmi')
    if dh:
        try:
            out['emitido_em'] = datetime.fromisoformat(dh).strftime('%d/%m/%Y %H:%M')
        except ValueError:
            out['emitido_em'] = dh
    return out


def consulta_index(request):
    empresas = Empresa.objects.filter(ativa=True).order_by('razao_social')
    return render(request, 'dfe/consulta/index.html', {'empresas': empresas})


def consulta_empresa(request, empresa_id):
    empresa = get_object_or_404(Empresa, id=empresa_id)

    # Eventos de cancelamento ficam vinculados à NFC-e via chave_acesso;
    # a listagem mostra apenas a nota, com o status refletindo o cancelamento.
    qs = (
        NfceDocumento.objects
        .filter(empresa=empresa)
        .exclude(tipo_documento='EVENTO_CANCELAMENTO')
        .order_by('-emitido_em', '-capturado_em', '-nsu')
    )

    tipo = request.GET.get('tipo', '').strip()
    cancelada = request.GET.get('cancelada', '').strip()
    chave = request.GET.get('chave', '').strip()
    inicio = request.GET.get('inicio', '').strip()
    fim = request.GET.get('fim', '').strip()

    if tipo:
        qs = qs.filter(tipo_documento=tipo)
    if cancelada == 'sim':
        qs = qs.filter(cancelada=True)
    elif cancelada == 'nao':
        qs = qs.filter(cancelada=False)
    if chave:
        qs = qs.filter(chave_acesso__icontains=chave)
    if inicio:
        try:
            qs = qs.filter(emitido_em__date__gte=datetime.strptime(inicio, '%Y-%m-%d').date())
        except ValueError:
            pass
    if fim:
        try:
            qs = qs.filter(emitido_em__date__lte=datetime.strptime(fim, '%Y-%m-%d').date())
        except ValueError:
            pass

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page'))

    # Usa os campos denormalizados (numero_nfce / serie / valor_total / emitido_em)
    # direto do DB — não reabre o .zip para cada linha.
    linhas = [{'doc': d} for d in page.object_list]

    state = getattr(empresa, 'dfesyncstate', None)

    ultima_captura = request.session.pop('ultima_captura', None)
    if ultima_captura and ultima_captura.get('empresa_id') != empresa.id:
        ultima_captura = None

    return render(request, 'dfe/consulta/empresa.html', {
        'empresa': empresa,
        'linhas': linhas,
        'page': page,
        'state': state,
        'ultima_captura': ultima_captura,
        'filtros': {
            'tipo': tipo, 'cancelada': cancelada, 'chave': chave,
            'inicio': inicio, 'fim': fim,
        },
        'tipos': [
            c for c in NfceDocumento._meta.get_field('tipo_documento').choices
            if c[0] != 'EVENTO_CANCELAMENTO'
        ],
    })


def consulta_capturar(request, empresa_id):
    empresa = get_object_or_404(Empresa, id=empresa_id)
    if request.method != 'POST':
        return redirect('dfe:consulta_empresa', empresa_id=empresa.id)

    if not empresa.cert_pfx:
        messages.error(request, 'Empresa sem certificado cadastrado.')
        return redirect('dfe:consulta_empresa', empresa_id=empresa.id)

    from .services.sefaz_sc_capture import capturar_sc_para_cnpj
    try:
        resultado = capturar_sc_para_cnpj(empresa.cnpj)
    except Exception as exc:
        messages.error(request, f'Falha na captura: {exc}')
        return redirect('dfe:consulta_empresa', empresa_id=empresa.id)

    lotes = resultado.get('lotes') or []
    messages.success(
        request,
        f"Captura concluída — {len(lotes)} lote(s), {resultado.get('qt_dfe_ret') or 0} documento(s). "
        f"NSU {resultado.get('nsu_inicial')} → {resultado.get('ult_nsu')}. "
        f"cStat final {resultado.get('cstat')}: {resultado.get('xmotivo')} "
        f"(parou por: {resultado.get('parou_por')})."
    )
    request.session['ultima_captura'] = {
        'empresa_id': empresa.id,
        'lotes': lotes,
        'parou_por': resultado.get('parou_por'),
        'cstat': resultado.get('cstat'),
        'xmotivo': resultado.get('xmotivo'),
        'nsu_inicial': resultado.get('nsu_inicial'),
        'ult_nsu': resultado.get('ult_nsu'),
        'qt_dfe_ret': resultado.get('qt_dfe_ret'),
    }
    return redirect('dfe:consulta_empresa', empresa_id=empresa.id)


def _resumo_evento(doc: NfceDocumento) -> dict:
    """Extrai dados do <infEvento> de um evento de cancelamento."""
    out = {
        'cstat': '', 'xmotivo': '', 'tp_evento': '', 'n_seq_evento': '',
        'dh_reg_evento': '', 'n_prot': '', 'ver_aplic': '',
    }
    xml = _ler_xml(doc)
    if not xml:
        return out
    try:
        root = etree.fromstring(xml.encode('utf-8'), parser=_SAFE_XML_PARSER)
    except Exception:
        return out

    def _t(tag):
        el = root.find(f'.//{{*}}{tag}')
        return el.text if el is not None and el.text else ''

    out['cstat'] = _t('cStat')
    out['xmotivo'] = _t('xMotivo')
    out['tp_evento'] = _t('tpEvento')
    out['n_seq_evento'] = _t('nSeqEvento')
    out['n_prot'] = _t('nProt')
    out['ver_aplic'] = _t('verAplic')
    dh = _t('dhRegEvento')
    if dh:
        try:
            out['dh_reg_evento'] = datetime.fromisoformat(dh).strftime('%d/%m/%Y %H:%M')
        except ValueError:
            out['dh_reg_evento'] = dh
    return out


def consulta_documento(request, doc_id):
    doc = get_object_or_404(NfceDocumento, id=doc_id)

    evento = None
    evento_resumo = None
    if doc.tipo_documento == 'NFE_PROC' and doc.cancelada:
        evento = (
            NfceDocumento.objects
            .filter(
                empresa=doc.empresa,
                chave_acesso=doc.chave_acesso,
                tipo_documento='EVENTO_CANCELAMENTO',
            )
            .order_by('-nsu')
            .first()
        )
        if evento is not None:
            evento_resumo = _resumo_evento(evento)

    return render(request, 'dfe/consulta/documento.html', {
        'doc': doc,
        'doc_xml': _ler_xml(doc),
        'resumo': _resumo_nfce(doc),
        'evento': evento,
        'evento_xml': _ler_xml(evento) if evento else '',
        'evento_resumo': evento_resumo,
    })


def consulta_documento_xml(request, doc_id):
    doc = get_object_or_404(NfceDocumento, id=doc_id)
    xml = _ler_xml(doc)
    if not xml:
        raise Http404('XML não disponível no disco.')
    nome = doc.arquivo_entrada or f'{doc.chave_acesso}.xml'
    response = HttpResponse(xml, content_type='application/xml; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{nome}"'
    return response
