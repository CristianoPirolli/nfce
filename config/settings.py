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
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# === Estáticos ===
STATIC_URL = 'static/'


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
