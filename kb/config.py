"""站点配置的「有效值」读取层。

所有 LLM / Embedding / 检索 / MinerU 配置都通过这里读取：
- 优先取数据库 SiteConfig 中的非空字段
- 否则回退到 settings（.env）默认值

这样前端设置页保存后，下一次请求即生效，无需重启。
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from django.conf import settings

from .models import SiteConfig


def _eff(db_value, settings_default):
    """数据库值非空则用之，否则回退 settings 默认。"""
    if db_value in (None, ""):
        return settings_default
    return db_value


def get_config() -> SiteConfig:
    """获取单行 SiteConfig（不存在则创建空行）。"""
    c = SiteConfig.get()
    from fos_rag.offline import configure_api_endpoints
    urls = []
    if c.all_remote_apis_enabled or c.llm_remote_enabled:
        urls.append(_eff(c.llm_base_url, settings.LLM_BASE_URL))
    if c.all_remote_apis_enabled:
        urls.extend([
            _eff(c.embedding_base_url, settings.EMBEDDING_BASE_URL),
            _eff(c.rerank_base_url, getattr(settings, "RERANK_BASE_URL", "")),
            _eff(c.mineru_api_base, settings.MINERU_API_BASE),
        ])
    configure_api_endpoints(normalize_openai_base_url(url) for url in urls)
    return c


def normalize_openai_base_url(value: str, *, ollama: bool = False) -> str:
    """Return a usable OpenAI-compatible base URL.

    Local services are commonly entered as ``127.0.0.1:11434``.  httpx does
    not infer the scheme, and Ollama exposes its OpenAI-compatible endpoints
    below ``/v1``.  Normalize both cases in one place so connection tests and
    runtime clients always use the same URL.
    """
    value = (value or "").strip().replace("：//", "://")
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    if ollama and parts.port == 11434 and path in ("", "/"):
        path = "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def llm_settings() -> dict:
    c = get_config()
    base_url = normalize_openai_base_url(_eff(c.llm_base_url, settings.LLM_BASE_URL))
    vision = c.llm_vision or getattr(settings, "LLM_VISION", False)
    return {
        "base_url": base_url,
        "remote_enabled": c.llm_remote_enabled or c.all_remote_apis_enabled,
        "all_remote_enabled": c.all_remote_apis_enabled,
        "api_key": _eff(c.llm_api_key, settings.LLM_API_KEY),
        "model": _eff(c.llm_model, settings.LLM_MODEL),
        "temperature": c.llm_temperature if c.llm_temperature is not None else settings.LLM_TEMPERATURE,
        # 视觉能力：开启后检索命中图片时把图发给模型（多模态内容块）
        "vision": bool(vision),
        # 问答管线增强：回答前问题规划 + 回答后核实（核实不过拒答）
        "qa_enhance": bool(c.qa_enhance or getattr(settings, "QA_ENHANCE", False)),
    }


def embedding_settings() -> dict:
    c = get_config()
    return {
        "base_url": normalize_openai_base_url(
            _eff(c.embedding_base_url, settings.EMBEDDING_BASE_URL), ollama=True,
        ),
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


def rerank_settings() -> dict:
    c = get_config()
    base_url = normalize_openai_base_url(
        _eff(c.rerank_base_url, getattr(settings, "RERANK_BASE_URL", "")))
    api_key = _eff(c.rerank_api_key, getattr(settings, "RERANK_API_KEY", ""))
    if not api_key and "siliconflow" in base_url.lower():
        # 同供应商时空 key 复用 embedding 的 key——但要看 embedding 的【有效】端点：
        # 若 embedding 指向本地服务（key 是占位符），复用它会 401，回退 .env 云端 key。
        emb = embedding_settings()
        if "siliconflow" in emb["base_url"].lower():
            api_key = emb["api_key"] or ""  # _eff 已含 .env 回退
        else:
            api_key = settings.EMBEDDING_API_KEY or ""
    return {
        "enabled": bool(c.rerank_enabled),
        "base_url": base_url,
        "api_key": api_key,
        "model": _eff(c.rerank_model, getattr(settings, "RERANK_MODEL", "")),
    }


def validate_answer_endpoint(cfg):
    """Use the same explicit opt-in and URL rules for tests and generation."""
    import ipaddress
    parts = urlsplit(cfg["base_url"])
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("请填写有效的 API Base URL，不要在地址中放入密钥或查询参数")
    local = parts.hostname == "localhost"
    try:
        local = local or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        pass
    if not local and not cfg.get("remote_enabled"):
        raise ValueError("此地址不在本机，请在回答模型配置中开启‘允许远程回答 API’")


def answer_extra_body(cfg):
    # Local engine switches are not portable OpenAI API parameters.
    return {} if cfg.get("remote_enabled") else settings.LLM_EXTRA_BODY
