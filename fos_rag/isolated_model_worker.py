"""Isolated OpenAI-compatible HTTP call; no Django or retrieval imports."""
import json
import sys

import httpx


def complete(value):
    config = value["config"]
    payload = {key: value for key, value in config.get("extra", {}).items()
               if key not in {"messages", "model", "tools", "tool_choice", "stream", "max_tokens"}}
    payload.update(model=config["model"], messages=value["messages"], temperature=config["temperature"],
                   max_tokens=config["max_tokens"], stream=False)
    if config.get("language_instruction"):
        payload["messages"] = [{"role": "system", "content": config["language_instruction"]}, *payload["messages"]]
    try:
        with httpx.Client(timeout=httpx.Timeout(150, connect=5), trust_env=False, follow_redirects=False) as client:
            response = client.post(config["base_url"].rstrip("/") + "/chat/completions",
                                   headers={"Authorization": "Bearer " + config["api_key"]}, json=payload)
        if response.status_code in (401, 403):
            return {"ok": False, "message": "当前回答模型认证失败，请检查站点中的模型连接。"}
        if response.status_code != 200:
            return {"ok": False, "message": "回答模型暂时不可用，请稍后重试。"}
        content = response.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "\n".join(row.get("text", "") for row in content if isinstance(row, dict))
        # 上限与 fos_rag/isolated_model.call_model 保持一致。
        return {"ok": True, "content": content[:24000] if isinstance(content, str) else ""}
    except httpx.TimeoutException:
        return {"ok": False, "message": "回答模型响应超时，请缩小范围后重试。"}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        return {"ok": False, "message": "未能从当前回答模型取得有效内容，请稍后重试。"}


def main():
    try:
        result = complete(json.loads(sys.stdin.read(192 * 1024)))
    except Exception:
        result = {"ok": False, "message": "回答模型连接未能完成，请稍后重试。"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
