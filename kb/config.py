"""站点配置的「有效值」读取层。

所有 LLM / Embedding / 检索 / MinerU 配置都通过这里读取：
- 优先取数据库 SiteConfig 中的非空字段
- 否则回退到 settings（.env）默认值

这样前端设置页保存后，下一次请求即生效，无需重启。
"""
from __future__ import annotations

from django.conf import settings

from .models import SiteConfig


def _eff(db_value, settings_default):
    """数据库值非空则用之，否则回退 settings 默认。"""
    if db_value in (None, ""):
        return settings_default
    return db_value


def get_config() -> SiteConfig:
    """获取单行 SiteConfig（不存在则创建空行）。"""
    return SiteConfig.get()


def llm_settings() -> dict:
    c = get_config()
    return {
        "base_url": _eff(c.llm_base_url, settings.LLM_BASE_URL),
        "api_key": _eff(c.llm_api_key, settings.LLM_API_KEY),
        "model": _eff(c.llm_model, settings.LLM_MODEL),
        "temperature": c.llm_temperature if c.llm_temperature is not None else settings.LLM_TEMPERATURE,
    }


def embedding_settings() -> dict:
    c = get_config()
    return {
        "base_url": _eff(c.embedding_base_url, settings.EMBEDDING_BASE_URL),
        "api_key": _eff(c.embedding_api_key, settings.EMBEDDING_API_KEY),
        "model": _eff(c.embedding_model, settings.EMBEDDING_MODEL),
        "dimensions": c.embedding_dimensions if c.embedding_dimensions is not None else settings.EMBEDDING_DIMENSIONS,
    }


def retrieval_settings() -> dict:
    c = get_config()
    return {
        "chunk_size": c.kb_chunk_size if c.kb_chunk_size is not None else settings.KB_CHUNK_SIZE,
        "chunk_overlap": c.kb_chunk_overlap if c.kb_chunk_overlap is not None else settings.KB_CHUNK_OVERLAP,
        "top_k": c.kb_top_k if c.kb_top_k is not None else settings.KB_TOP_K,
    }


def mineru_settings() -> dict:
    c = get_config()
    return {
        "api_base": _eff(c.mineru_api_base, settings.MINERU_API_BASE),
        "api_key": _eff(c.mineru_api_key, settings.MINERU_API_KEY),
        "backend": _eff(c.mineru_backend, settings.MINERU_BACKEND),
        "lang": _eff(c.mineru_lang, settings.MINERU_LANG),
    }
