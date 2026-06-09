"""
Django settings for config project.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Carrega variáveis do .env (override=False para não sobrescrever env do SO).
load_dotenv(BASE_DIR / '.env', override=False)


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ('1', 'true', 'yes', 'on')


def _env_list(name: str, default=None):
    val = os.environ.get(name, '')
    if not val:
        return default or []
    return [item.strip() for item in val.split(',') if item.strip()]


# === Segurança ===
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    raise ImproperlyConfigured(
        'DJANGO_SECRET_KEY não definida. Configure no arquivo .env.'
    )

DEBUG = _env_bool('DJANGO_DEBUG', False)

ALLOWED_HOSTS = _env_list('DJANGO_ALLOWED_HOSTS', ['127.0.0.1', 'localhost'])


# === Apps ===
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'core',
    'dfe',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# === Database ===
# Se DB_HOST estiver definido no .env, usa Postgres; senão cai pro SQLite local.
if os.environ.get('DB_HOST'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'HOST': os.environ['DB_HOST'],
            'PORT': os.environ.get('DB_PORT', '5432'),
            'NAME': os.environ['DB_NAME'],
            'USER': os.environ['DB_USER'],
            'PASSWORD': os.environ['DB_PASSWORD'],
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# === Senhas ===
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# === i18n ===
LANGUAGE_CODE = 'pt-br'
TIME_ZONE = 'America/Sao_Paulo'
USE_I18N = True
USE_TZ = True


# === Estáticos ===
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'


# === Certificados ===
# Diretório privado, NÃO servido por HTTP.
PRIVATE_CERTS_ROOT = BASE_DIR / 'private_certs'

# Chave Fernet para criptografar senhas dos certificados.
CERT_SECRET_KEY = os.environ.get('CERT_SECRET_KEY')
if not CERT_SECRET_KEY:
    raise ImproperlyConfigured(
        'CERT_SECRET_KEY não definida. Configure no arquivo .env '
        '(chave Fernet de 32 bytes url-safe base64).'
    )

# Raiz onde os XMLs capturados são gravados em disco (local ou UNC).
# Vazio/None = não grava em disco, mantém apenas no banco.
_xml_root = os.environ.get('NFCE_XML_ROOT', '').strip()
NFCE_XML_ROOT = Path(_xml_root) if _xml_root else None


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


# === Worker assíncrono de captura ===
# Threads simultâneas (CNPJs capturados em paralelo).
DFE_WORKER_CONCURRENCY = _env_int('DFE_WORKER_CONCURRENCY', 5)
# Intervalo entre ciclos do loop do worker, em segundos.
DFE_WORKER_CICLO_SEGUNDOS = _env_int('DFE_WORKER_CICLO_SEGUNDOS', 30)
# Cadência da verificação global de cancelamentos pendentes, em minutos.
DFE_CANCELAMENTOS_INTERVALO_MIN = _env_int('DFE_CANCELAMENTOS_INTERVALO_MIN', 60)
# Cadência do resync por empresa, em horas.
DFE_RESYNC_INTERVALO_HORAS = _env_int('DFE_RESYNC_INTERVALO_HORAS', 24)
# Idade (min) que torna um claim de execução "órfão" e re-reivindicável.
DFE_CLAIM_ORFAO_MIN = _env_int('DFE_CLAIM_ORFAO_MIN', 30)
# Piso de segurança (min): nunca inicia nova captura de um CNPJ cuja última
# consulta válida (ultima_captura) foi há menos que isto — rede de proteção
# extra contra consumo indevido, independente do agendamento normal.
DFE_MIN_INTERVALO_CAPTURA_MIN = _env_int('DFE_MIN_INTERVALO_CAPTURA_MIN', 30)
DFE_LOCK_TTL_MIN = _env_int('DFE_LOCK_TTL_MIN', 30)
