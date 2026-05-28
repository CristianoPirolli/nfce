from django.urls import path

from . import views


app_name = 'dfe'

urlpatterns = [
    # Certificados
    path('certificados/', views.lista_empresas, name='lista_empresas'),
    path('certificados/nova/', views.nova_empresa, name='nova_empresa'),
    path('certificados/<int:empresa_id>/upload/', views.upload_certificado, name='upload_certificado'),
    path('certificados/<int:empresa_id>/remover/', views.remover_certificado, name='remover_certificado'),

    # Consulta de NFC-e
    path('consulta/', views.consulta_index, name='consulta_index'),
    path('consulta/<int:empresa_id>/', views.consulta_empresa, name='consulta_empresa'),
    path('consulta/<int:empresa_id>/capturar/', views.consulta_capturar, name='consulta_capturar'),
    path('consulta/doc/<int:doc_id>/', views.consulta_documento, name='consulta_documento'),
    path('consulta/doc/<int:doc_id>/xml/', views.consulta_documento_xml, name='consulta_documento_xml'),
]
