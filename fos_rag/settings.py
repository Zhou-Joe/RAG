"""
Django settings for FOS_RAG project.

复用上层 MedicalAgent/.venv（Django 6.0 + LangChain + ChromaDB 等）。
独立的 SQLite 数据库、独立端口运行，与父项目互不干扰。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（FOS_RAG/）
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


SECRET_KEY = _env("DJANGO_SECRET_KEY", "fos-rag-dev-insecure-change-me")
DEBUG = _env("DJANGO_DEBUG", "True").lower() in ("1", "true", "yes")
ALLOWED_HOSTS = [h for h in _env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h]

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 本项目 app
    "accounts",
    "kb",
    "dashboard",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "fos_rag.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "fos_rag.wsgi.application"
ASGI_APPLICATION = "fos_rag.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 6}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_REDIRECT_URL = "/"
LOGIN_URL = "/accounts/login/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# 用户上传文件
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# CSRF 信任来源（含 Cloudflare Tunnel）
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in _env(
        "CSRF_TRUSTED_ORIGINS",
        "http://localhost,http://127.0.0.1,https://*.trycloudflare.com",
    ).split(",") if o.strip()
]

# ============================================================
# RAG 配置（独立读取 .env，不依赖父项目 common.config）
# ============================================================
# MinerU OCR
MINERU_API_BASE = _env("MINERU_API_BASE", "http://localhost:8888").rstrip("/")
MINERU_API_KEY = _env("MINERU_API_KEY", "")
MINERU_BACKEND = _env("MINERU_BACKEND", "pipeline")
MINERU_LANG = _env("MINERU_LANG", "ch")

# Embedding
EMBEDDING_BASE_URL = _env("EMBEDDING_BASE_URL", "").rstrip("/")
EMBEDDING_API_KEY = _env("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = _env("EMBEDDING_MODEL", "")
EMBEDDING_DIMENSIONS = int(_env("EMBEDDING_DIMENSIONS", "1024"))

# LLM
LLM_BASE_URL = _env("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = _env("LLM_API_KEY", "")
LLM_MODEL = _env("LLM_MODEL", "")
LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", "0.2"))

# 检索参数
KB_CHUNK_SIZE = int(_env("KB_CHUNK_SIZE", "800"))
KB_CHUNK_OVERLAP = int(_env("KB_CHUNK_OVERLAP", "150"))
KB_TOP_K = int(_env("KB_TOP_K", "5"))

# 数据目录
DATA_DIR = BASE_DIR / "data"
CHROMA_ROOT = DATA_DIR / "chroma"       # 每个向量库一个子目录
MD_ROOT = DATA_DIR / "md"               # OCR 输出的 Markdown
