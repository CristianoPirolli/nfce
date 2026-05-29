import zipfile
from datetime import datetime
from pathlib import Path

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from lxml import etree


def _captura_liberada_em(state):
    """Datetime em que a captura volta a ser permitida, ou None se já liberada.

    Considera tanto o bloqueio duro do SEFAZ (bloqueado_ate — cStat 657, consumo
    indevido) quanto o agendamento suave (proxima_captura_em — cStat 110/117/118).
    Retorna o mais distante dos dois que ainda esteja no futuro.
    """
    if not state:
        return None
    agora = timezone.now()
    liberada_em = None
    for ts in (state.bloqueado_ate, state.proxima_captura_em):
        if ts and ts > agora and (liberada_em is None or ts > liberada_em):
            liberada_em = ts
    return liberada_em


def _status_captura(state):
    """Status agregado da captura para exibição. Retorna dict com:
    code, label e refresh (se a página deve auto-recarregar enquanto pendente).
    """
    if not state:
        return {'code': 'novo', 'label': 'Sem sincronização ainda', 'refresh': False}

    agora = timezone.now()

    if state.em_execucao:
        return {'code': 'executando', 'label': '🔄 Capturando agora…', 'refresh': True}

    if state.bloqueado_ate and state.bloqueado_ate > agora:
        return {'code': 'bloqueado', 'label': '🔒 Bloqueado', 'refresh': False}

    if state.proxima_captura_em is None or state.proxima_captura_em <= agora:
        return {'code': 'fila', 'label': '⏳ Na fila', 'refresh': True}

    return {'code': 'em_dia', 'label': '✓ Em dia', 'refresh': False}


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

from .forms import CertificadoContadorForm, CertificadoUploadForm, EmpresaForm
from .models import DfeSetting, Empresa, NfceDocumento


def _contador_cert_configurado() -> bool:
    """True se há um certificado do contador utilizável (upload ou legado)."""
    setting = DfeSetting.objects.first()
    if not setting:
        return False
    if setting.cert_pfx and setting.cert_password_encrypted:
        return True
    return bool(setting.cert_path and setting.cert_password)


def lista_empresas(request):
    empresas = Empresa.objects.order_by('razao_social')
    return render(request, 'dfe/certificados/lista.html', {
        'empresas': empresas,
        'contador_ok': _contador_cert_configurado(),
    })


def nova_empresa(request):
    if request.method == 'POST':
        form = EmpresaForm(request.POST)
        if form.is_valid():
            form.save()
            if _contador_cert_configurado():
                messages.success(
                    request,
                    'Empresa cadastrada. Ela usará o certificado do contador automaticamente.'
                )
            else:
                messages.success(
                    request,
                    'Empresa cadastrada. Configure o certificado do contador para habilitar as capturas.'
                )
            return redirect('dfe:lista_empresas')
    else:
        form = EmpresaForm()

    return render(request, 'dfe/certificados/empresa_form.html', {'form': form})


def certificado_contador(request):
    setting = DfeSetting.objects.first()

    if request.method == 'POST':
        form = CertificadoContadorForm(request.POST, request.FILES)
        if form.is_valid():
            info = form.info_cert
            if setting is None:
                setting = DfeSetting()
            if setting.cert_pfx:
                setting.cert_pfx.delete(save=False)

            setting.cert_pfx = form.cleaned_data['cert_pfx']
            setting.set_cert_password(form.cleaned_data['cert_password'])
            setting.cert_validade = info['nao_depois']
            if info['cnpj_titular']:
                setting.nfce_sc_cert_cnpj_contador = info['cnpj_titular']
            setting.save()

            messages.success(
                request,
                f"Certificado do contador válido até {info['nao_depois'].strftime('%d/%m/%Y')} salvo. "
                f"Todas as empresas sem certificado próprio passarão a usá-lo."
            )
            return redirect('dfe:lista_empresas')
    else:
        form = CertificadoContadorForm()

    return render(request, 'dfe/certificados/contador.html', {
        'form': form,
        'setting': setting,
    })


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
    empresas = list(
        Empresa.objects.filter(ativa=True)
        .select_related('dfesyncstate')
        .order_by('razao_social')
    )
    contador_ok = _contador_cert_configurado()
    for empresa in empresas:
        state = getattr(empresa, 'dfesyncstate', None)
        empresa.status = _status_captura(state)
        empresa.cert_ok = bool(empresa.cert_pfx) or contador_ok
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
        'captura_liberada_em': _captura_liberada_em(state),
        'captura_status': _status_captura(state),
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
    """Enfileira a captura (não-bloqueante). O worker em background processa.

    A web nunca chama o SEFAZ: aqui só marcamos proxima_captura_em=agora para
    que a empresa "fure a fila" e seja capturada no próximo ciclo do worker.
    """
    empresa = get_object_or_404(Empresa, id=empresa_id)
    if request.method != 'POST':
        return redirect('dfe:consulta_empresa', empresa_id=empresa.id)

    if not empresa.cert_pfx and not _contador_cert_configurado():
        messages.error(
            request,
            'Sem certificado disponível. Cadastre o certificado do contador ou um certificado próprio.'
        )
        return redirect('dfe:consulta_empresa', empresa_id=empresa.id)

    from .models import DfeSyncState
    state, _ = DfeSyncState.objects.get_or_create(empresa=empresa)

    if state.em_execucao:
        messages.info(request, 'Captura já em andamento para esta empresa.')
        return redirect('dfe:consulta_empresa', empresa_id=empresa.id)

    liberada_em = _captura_liberada_em(state)
    if liberada_em:
        messages.warning(
            request,
            f'Captura bloqueada até {timezone.localtime(liberada_em):%d/%m/%Y %H:%M} '
            f'(proteção contra consumo indevido do SEFAZ).'
        )
        return redirect('dfe:consulta_empresa', empresa_id=empresa.id)

    state.proxima_captura_em = timezone.now()
    state.ultimo_erro = ''
    state.ultimo_erro_em = None
    state.save(update_fields=['proxima_captura_em', 'ultimo_erro', 'ultimo_erro_em'])

    messages.success(request, 'Captura agendada — será processada em instantes pelo worker.')
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
