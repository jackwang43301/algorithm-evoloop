from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# 结构化输出降级链：优先 json_schema，失败后退到 json_object，最后退到纯文本自行解析
STRUCTURED_OUTPUT_MODES = ("json_schema", "json_object", "text")
# 只有这些 HTTP 状态码被视为可重试（限流、网关抖动、服务端临时故障）
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
ENV_PREFIX = "LLM_"


class LLMConfigError(RuntimeError):
    """大模型配置错误：配置文件缺失/非法、必填项缺失、字段类型或取值不受支持时抛出。"""

    pass


class LLMRequestError(RuntimeError):
    """大模型请求错误：携带 HTTP 状态码 status、截断后的响应体 body 与逐次尝试记录 attempts。"""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        body: str = "",
        attempts: list[dict[str, Any]] | None = None,
    ):
        """记录失败上下文；status 为 None 表示网络层或解析层失败，attempts 缺省时置为空列表。"""
        self.status = status
        self.body = body
        self.attempts = attempts or []
        super().__init__(message)


@dataclass
class LLMConfig:
    """可配置的对话补全接入参数，适配任意 OpenAI 兼容接口：地址、模型、超时、重试与结构化输出模式。"""

    base_url: str
    model: str
    api_key: str = ""
    chat_path: str = "/chat/completions"
    timeout_seconds: int = 180
    temperature: float = 0.0
    max_retries: int = 2
    structured_output: str = "json_schema"
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        config_path: Path | None = None,
        overrides: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
    ) -> "LLMConfig":
        """按“配置文件 < 环境变量 < overrides”的优先级合并出配置；缺少 base_url/model 或字段非法时抛
        LLMConfigError。"""
        env = dict(os.environ if env is None else env)
        data: dict[str, Any] = {}
        path = config_path or (Path(env["LLM_CONFIG"]) if env.get("LLM_CONFIG") else None)
        if path is not None:
            data.update(cls._from_file(path))
        data.update(cls._from_env(env))
        for key, value in (overrides or {}).items():
            if value is not None:
                data[key] = value
        data["api_key"] = cls._resolve_api_key(data, env)
        # api_key_env 只是取值来源的指示项，不是 dataclass 字段，合并完必须丢弃
        data.pop("api_key_env", None)
        unknown = sorted(set(data) - set(cls.__dataclass_fields__))
        if unknown:
            raise LLMConfigError(f"未知的大模型配置项：{', '.join(unknown)}")
        for key in ("base_url", "model"):
            if not data.get(key):
                raise LLMConfigError(
                    "缺少大模型接口配置：base_url 和 model 必填。"
                    "可通过 --llm-config 配置文件、LLM_BASE_URL/LLM_MODEL 环境变量或命令行参数提供。"
                )
        config = cls(**data)
        if config.structured_output not in STRUCTURED_OUTPUT_MODES:
            raise LLMConfigError(
                f"structured_output 只支持 {', '.join(STRUCTURED_OUTPUT_MODES)}"
            )
        if not isinstance(config.extra_headers, dict) or not isinstance(config.extra_body, dict):
            raise LLMConfigError("extra_headers 和 extra_body 必须是对象")
        return config

    @staticmethod
    def _from_file(path: Path) -> dict[str, Any]:
        """读取 JSON 配置文件并返回字典；文件不存在、内容非法 JSON 或顶层不是对象时抛 LLMConfigError。"""
        path = path.expanduser()
        if not path.is_file():
            raise LLMConfigError(f"大模型配置文件不存在：{path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LLMConfigError(f"大模型配置文件不是合法 JSON：{path}") from exc
        if not isinstance(data, dict):
            raise LLMConfigError(f"大模型配置文件必须是 JSON 对象：{path}")
        return data

    @staticmethod
    def _from_env(env: dict[str, str]) -> dict[str, Any]:
        """从 LLM_ 前缀环境变量提取配置：文本原样、整数与浮点转型、extra_headers/extra_body 解析为 JSON。"""
        text_keys = ("base_url", "model", "api_key", "api_key_env", "chat_path", "structured_output")
        int_keys = ("timeout_seconds", "max_retries")
        json_keys = ("extra_headers", "extra_body")
        data: dict[str, Any] = {}
        for key in text_keys:
            value = env.get(ENV_PREFIX + key.upper())
            if value:
                data[key] = value
        for key in int_keys:
            value = env.get(ENV_PREFIX + key.upper())
            if value:
                data[key] = int(value)
        value = env.get(ENV_PREFIX + "TEMPERATURE")
        if value:
            data["temperature"] = float(value)
        for key in json_keys:
            value = env.get(ENV_PREFIX + key.upper())
            if value:
                try:
                    data[key] = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise LLMConfigError(f"{ENV_PREFIX + key.upper()} 不是合法 JSON") from exc
        return data

    @staticmethod
    def _resolve_api_key(data: dict[str, Any], env: dict[str, str]) -> str:
        """按“显式 api_key → api_key_env 指向的环境变量 → LLM_API_KEY”取密钥；指定的环境变量为空则报错。"""
        if data.get("api_key"):
            return str(data["api_key"])
        key_env = data.get("api_key_env")
        if key_env:
            value = env.get(str(key_env), "")
            if not value:
                raise LLMConfigError(f"环境变量 {key_env} 未设置大模型接口密钥")
            return value
        return env.get(ENV_PREFIX + "API_KEY", "")

    @property
    def endpoint(self) -> str:
        """拼出完整请求地址：chat_path 为空时直接用 base_url，否则规整斜杠后拼接，避免出现双斜杠。"""
        if not self.chat_path:
            return self.base_url
        return self.base_url.rstrip("/") + "/" + self.chat_path.lstrip("/")

    def headers(self) -> dict[str, str]:
        """构造请求头：固定 JSON 内容类型，有密钥时附加 Bearer 认证，最后允许 extra_headers 覆盖。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def redacted(self) -> dict[str, Any]:
        """返回可安全写入日志的配置快照：密钥脱敏成 ***，自定义头只保留键名不带取值。"""
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "structured_output": self.structured_output,
            "api_key": "***" if self.api_key else "",
            "extra_header_keys": sorted(self.extra_headers),
        }


def parse_json_object(text: str) -> dict[str, Any]:
    """从模型回复中抽取第一个 JSON 对象，容忍代码围栏包裹；无对象或解析失败时抛 LLMRequestError。"""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        # 去掉首行 ```lang 与结尾围栏，只留中间正文
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    if start < 0:
        raise LLMRequestError("大模型返回内容中没有 JSON 对象")
    try:
        obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        raise LLMRequestError(f"大模型返回的 JSON 无法解析：{exc}") from exc
    if not isinstance(obj, dict):
        raise LLMRequestError("大模型返回的 JSON 不是对象")
    return obj


class LLMClient:
    """仅依赖标准库 urllib 的极简对话补全客户端，内置结构化输出降级与状态码退避重试。"""

    def __init__(self, config: LLMConfig, opener: Any = None):
        """保存配置；opener 缺省为 urllib.request.urlopen，测试可注入伪造实现以避免真实网络请求。"""
        self.config = config
        self._opener = opener or urllib.request.urlopen

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any] | None = None,
        schema_name: str = "role_record",
    ) -> dict[str, Any]:
        """完成一次角色对话，返回 {"record": 解析出的 JSON 对象, "text": 原始回复, "attempts": 尝试记录,
        "mode": 生效模式}；沿降级链逐个模式尝试，全部失败则抛 LLMRequestError 并带上累计 attempts。"""
        modes = self._mode_chain(schema)
        attempts: list[dict[str, Any]] = []
        last_error: LLMRequestError | None = None
        for mode in modes:
            payload = self._payload(system_prompt, user_prompt, schema, schema_name, mode)
            try:
                body = self._post_with_retry(payload, attempts, mode)
            except LLMRequestError as exc:
                # 该模式的请求彻底失败，记下错误并降级到下一个模式
                last_error = exc
                continue
            text = self._extract_text(body)
            try:
                record = parse_json_object(text)
            except LLMRequestError as exc:
                # 请求成功但正文不是可用 JSON，同样降级重试
                last_error = exc
                attempts.append({"mode": mode, "error": str(exc)})
                continue
            return {"record": record, "text": text, "attempts": attempts, "mode": mode}
        raise LLMRequestError(
            f"大模型接口调用失败：{last_error}",
            status=getattr(last_error, "status", None),
            body=getattr(last_error, "body", ""),
            attempts=attempts,
        )

    def _mode_chain(self, schema: dict[str, Any] | None) -> tuple[str, ...]:
        """给出本次可用的模式序列：无 schema 时只用 text，否则从配置的起点截取降级链的剩余部分。"""
        if schema is None:
            return ("text",)
        start = STRUCTURED_OUTPUT_MODES.index(self.config.structured_output)
        return STRUCTURED_OUTPUT_MODES[start:]

    def _payload(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any] | None,
        schema_name: str,
        mode: str,
    ) -> dict[str, Any]:
        """组装请求体：system/user 两条消息，按 mode 决定是否附带 response_format，最后并入 extra_body。"""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if schema is not None and mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": False},
            }
        elif schema is not None and mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        payload.update(self.config.extra_body)
        return payload

    def _post_with_retry(
        self,
        payload: dict[str, Any],
        attempts: list[dict[str, Any]],
        mode: str,
    ) -> dict[str, Any]:
        """发起 POST 并按需重试，成功返回解析后的响应体；每次尝试都追加到 attempts，最终失败抛
        LLMRequestError。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last: LLMRequestError | None = None
        for attempt in range(self.config.max_retries + 1):
            request = urllib.request.Request(
                self.config.endpoint,
                data=data,
                headers=self.config.headers(),
                method="POST",
            )
            try:
                with self._opener(request, timeout=self.config.timeout_seconds) as response:
                    body = response.read().decode("utf-8", errors="replace")
                attempts.append({"mode": mode, "attempt": attempt + 1, "status": 200})
                return json.loads(body)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                last = LLMRequestError(
                    f"接口返回状态码 {exc.code}", status=exc.code, body=detail[:2000]
                )
                attempts.append(
                    {"mode": mode, "attempt": attempt + 1, "status": exc.code, "error": detail[:500]}
                )
                if exc.code not in RETRY_STATUS:
                    # 4xx 等确定性错误重试无意义，直接跳出交由上层降级
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = LLMRequestError(f"接口请求失败：{exc}")
                attempts.append({"mode": mode, "attempt": attempt + 1, "error": str(exc)})
            if attempt < self.config.max_retries:
                # 指数退避，上限 8 秒
                time.sleep(min(2 ** attempt, 8))
        raise last or LLMRequestError("接口请求失败")

    @staticmethod
    def _extract_text(body: dict[str, Any]) -> str:
        """取出首个 choice 的正文：支持字符串或分段内容拼接，正文为空时回退到工具调用参数，均无则报错。"""
        choices = body.get("choices") or []
        if not choices:
            raise LLMRequestError("接口响应缺少 choices 字段")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            ]
            content = "".join(parts)
        if isinstance(content, str) and content.strip():
            return content
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            arguments = (tool_calls[0].get("function") or {}).get("arguments")
            if isinstance(arguments, str) and arguments.strip():
                return arguments
        raise LLMRequestError("接口响应没有可用的文本内容")

