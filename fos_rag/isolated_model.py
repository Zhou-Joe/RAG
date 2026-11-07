"""Read the configured answer model without modifying the retrieval process."""
import json
import os
from pathlib import Path
import subprocess
import sys

from django.conf import settings


class AnalysisError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def read_model_config():
    from .interface_language import response_language_instruction
    # Avoid SiteConfig.get() and kb.config.llm_settings(): the former can write
    # and the latter reconfigures the retrieval process's global allowlist.
    from kb.models import SiteConfig
    from kb.config import normalize_openai_base_url, validate_answer_endpoint
    fields = ("llm_base_url", "llm_api_key", "llm_model", "llm_temperature",
              "llm_remote_enabled", "all_remote_apis_enabled")
    row = SiteConfig.objects.values(*fields).first() or {}
    cfg = {
        "base_url": normalize_openai_base_url(row.get("llm_base_url") or settings.LLM_BASE_URL),
        "api_key": row.get("llm_api_key") or settings.LLM_API_KEY or "local-no-key",
        "model": row.get("llm_model") or settings.LLM_MODEL,
        "language_instruction": response_language_instruction(),
        "temperature": row.get("llm_temperature") if row.get("llm_temperature") is not None else settings.LLM_TEMPERATURE,
        "remote_enabled": bool(row.get("llm_remote_enabled") or row.get("all_remote_apis_enabled")),
        # 与 kb/agent.py 的回答使用同一个输出预算；上限仅防止异常环境变量把子进程无限挂住。
        "max_tokens": min(max(int(settings.LLM_MAX_OUTPUT_TOKENS), 256), 32768),
    }
    if not cfg["base_url"] or not cfg["model"]:
        raise AnalysisError("请先在站点配置中选择可用的回答模型。", 503)
    try:
        validate_answer_endpoint(cfg)
    except ValueError as exc:
        raise AnalysisError(str(exc), 503) from exc
    cfg["extra"] = {} if cfg["remote_enabled"] else getattr(settings, "LLM_EXTRA_BODY", {})
    return cfg


def child_environment():
    """Environment for isolated helpers: same interpreter libraries and installed packages, nothing else."""
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "DYLD_FRAMEWORK_PATH", "DYLD_LIBRARY_PATH") if key in os.environ}
    # Worker JSON uses UTF-8 regardless of the Windows console's legacy encoding.
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    # Deployments may run the bundled interpreter with packages supplied via PYTHONPATH rather than a venv.
    packages = [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry]
    packages += [entry for entry in sys.path if entry and Path(entry).name in ("site-packages", "dist-packages")]
    if packages:
        environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(packages))
    return environment


def call_model(config, messages):
    environment = child_environment()
    try:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-s", "-P", str(Path(__file__).with_name("isolated_model_worker.py"))],
            input=json.dumps({"config": config, "messages": messages}, ensure_ascii=False),
            text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=165, env=environment, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError("本次分析超时，请缩小范围后重试。", 504) from exc
    # 16K 输出预算对应约 24000 字（中文近 1 字 1 token）；stdout 上限留出 JSON 与 UTF-8 开销。
    if result.returncode or len(result.stdout) > 160_000:
        raise AnalysisError("回答模型暂时无法完成分析，请稍后重试。", 503)
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise AnalysisError("模型未返回有效内容，请重试。", 502) from exc
    if not data.get("ok"):
        raise AnalysisError(str(data.get("message") or "")[:200] or "模型连接失败。", 503)
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise AnalysisError("模型未返回分析内容，请重试。", 502)
    return content[:24000]
