from __future__ import annotations

import os
import base64
import json
import mimetypes
import shutil
import sqlite3
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from openai import OpenAI

from config import PROJECT_DIR, Settings


class ReplyGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.persona_prompt = self._load_persona_prompt()
        self._codex_network_checked_at = 0.0
        self._codex_network_error = ""
        self.client = None
        if settings.llm_provider == "codex_cli":
            if not shutil.which(settings.codex_command) and not Path(settings.codex_command).exists():
                raise RuntimeError(
                    f"找不到 Codex CLI：{settings.codex_command}。请确认已安装并加入 PATH。"
                )
        elif settings.llm_provider == "ccswitch":
            # Validate that CC Switch has a selected Codex provider and a model.
            # No upstream request is made here.
            self._ccswitch_runtime()
        elif settings.llm_provider in {"lmstudio", "openai_compatible"}:
            self.client = OpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.codex_timeout_seconds,
            )

    def _load_persona_prompt(self) -> str:
        path = self.settings.persona_path
        if path is None:
            return ""
        if not path.exists():
            raise RuntimeError(f"找不到语气画像文件：{path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise RuntimeError(f"语气画像文件为空：{path}")
        return text

    def _effective_system_prompt(self) -> str:
        if not self.persona_prompt:
            return self.settings.system_prompt
        return (
            f"{self.settings.system_prompt}\n\n"
            "【我的语气画像（仅用于模仿表达，不代表事实或授权）】\n"
            f"{self.persona_prompt}"
        )

    def _check_codex_network(self) -> None:
        """在启动 Codex CLI 前快速检查 OpenAI 后端是否可达。"""
        now = time.monotonic()
        if now - self._codex_network_checked_at < 15:
            if self._codex_network_error:
                raise RuntimeError(self._codex_network_error)
            return

        self._codex_network_checked_at = now
        self._codex_network_error = ""
        request = urllib.request.Request("https://chatgpt.com/", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5):
                pass
        except urllib.error.HTTPError:
            # 收到 HTTP 响应即证明 DNS、TCP 和 TLS 已经连通。
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._codex_network_error = (
                "Codex CLI 网络不可用：无法连接 chatgpt.com。"
                "请检查 VPN、HTTP_PROXY/HTTPS_PROXY 或网络防火墙。"
            )
            raise RuntimeError(self._codex_network_error) from exc

    def generate(
        self,
        history: list[dict[str, str]],
        message: str,
        image_path: str | None = None,
    ) -> str:
        input_items = [
            {"role": item["role"], "content": item["content"]}
            for item in history
        ]

        if self.settings.llm_provider == "codex_cli":
            reply = self._generate_with_codex(history, message, image_path=image_path)
        elif self.settings.llm_provider == "ccswitch":
            reply = self._generate_with_ccswitch(history, message, image_path=image_path)
        else:
            messages = [{"role": "system", "content": self._effective_system_prompt()}]
            messages.extend(input_items)
            if image_path:
                image_data = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
                mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": message},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{image_data}"},
                            },
                        ],
                    }
                )
            else:
                messages.append({"role": "user", "content": message})
            model = self.settings.llm_model
            if self.settings.llm_provider == "lmstudio" and not model:
                model = self._lmstudio_model()
            try:
                request_kwargs = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": self.settings.max_reply_chars * 2,
                }
                if self.settings.llm_provider == "lmstudio":
                    # Qwen reasoning models can spend the whole output budget
                    # on hidden reasoning, leaving message.content empty.
                    request_kwargs["reasoning_effort"] = "none"
                response = self.client.chat.completions.create(**request_kwargs)
            except Exception as exc:
                if self.settings.llm_provider == "lmstudio":
                    raise RuntimeError(
                        f"LM Studio 调用失败（{self.settings.llm_base_url}）：{exc}；"
                        "请确认本地服务器已启动且模型已加载"
                    ) from exc
                raise
            reply = (response.choices[0].message.content or "").strip()
        if not reply:
            raise RuntimeError("模型返回了空回复")
        return reply[: self.settings.max_reply_chars]

    def _lmstudio_model(self) -> str:
        """Resolve the currently loaded LM Studio model when no model is configured."""
        try:
            models = self.client.models.list()
            data = getattr(models, "data", None) or []
            model = str(getattr(data[0], "id", "") or "").strip() if data else ""
        except Exception as exc:
            raise RuntimeError(
                f"无法读取 LM Studio 当前模型（{self.settings.llm_base_url}）：{exc}；"
                "请确认 LM Studio 已启动并加载模型，或填写 LMSTUDIO_MODEL"
            ) from exc
        if not model:
            raise RuntimeError(
                "LM Studio 没有返回可用模型；请先在 LM Studio 加载模型，"
                "或在 .env 中填写 LMSTUDIO_MODEL"
            )
        return model

    @staticmethod
    def _codex_home() -> Path:
        configured = os.getenv("CODEX_HOME", "").strip()
        return Path(configured).expanduser() if configured else Path.home() / ".codex"

    @staticmethod
    def _ccswitch_home() -> Path:
        configured = os.getenv("CCSWITCH_HOME", "").strip()
        return Path(configured).expanduser() if configured else Path.home() / ".cc-switch"

    def _ccswitch_provider_state(self) -> tuple[str, bool, str]:
        """Read the provider selected in CC Switch without exposing its key."""
        database_path = self._ccswitch_home() / "cc-switch.db"
        if not database_path.exists():
            raise RuntimeError(
                f"找不到 CC Switch 配置数据库：{database_path}；请确认 CC Switch 已安装"
            )
        try:
            uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1) as connection:
                row = connection.execute(
                    "SELECT name, category, settings_config "
                    "FROM providers WHERE app_type = 'codex' AND is_current = 1 "
                    "LIMIT 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError("无法读取 CC Switch 当前 Codex 供应商") from exc
        if row is None:
            raise RuntimeError("CC Switch 的 Codex 页尚未选择当前供应商")

        provider_name = str(row[0] or "").strip()
        category = str(row[1] or "").strip().lower()
        is_openai_official = category == "official"
        provider_model = ""
        try:
            settings_config = json.loads(str(row[2] or "{}"))
            provider_toml = settings_config.get("config", "")
            if isinstance(provider_toml, str) and provider_toml.strip():
                provider_model = str(tomllib.loads(provider_toml).get("model", "")).strip()
        except (json.JSONDecodeError, tomllib.TOMLDecodeError):
            # The live Codex config remains a safe fallback for custom payloads.
            pass
        return provider_name, is_openai_official, provider_model

    def _ccswitch_runtime(self) -> tuple[str, str]:
        """Resolve the current CC Switch model and safe local-route credential.

        CC Switch records the active provider in its local database and normally
        rewrites Codex's config.toml too. Reading both for every request makes a
        provider/model switch take effect without restarting this service.
        """
        codex_home = self._codex_home()
        config_path = codex_home / "config.toml"
        config: dict[str, Any] = {}
        config_error: Exception | None = None
        if config_path.exists():
            try:
                config = tomllib.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                config_error = exc

        provider_name, is_openai_official, provider_model = self._ccswitch_provider_state()
        model = (
            self.settings.llm_model
            or provider_model
            or str(config.get("model", "")).strip()
        )
        if not model:
            if config_error is not None:
                raise RuntimeError(
                    f"无法读取 CC Switch 管理的 Codex 配置：{config_path}"
                ) from config_error
            raise RuntimeError(
                f"无法确定 CC Switch 供应商 {provider_name} 的当前模型；"
                "或在 .env 中填写 CCSWITCH_MODEL"
            )

        if not is_openai_official:
            return model, "PROXY_MANAGED"

        auth_path = codex_home / "auth.json"
        try:
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "CC Switch 当前选择了 OpenAI Official，但找不到有效的 Codex 登录信息；"
                "请先在 Codex 中完成登录"
            ) from exc

        tokens = auth.get("tokens") if isinstance(auth, dict) else None
        access_token = tokens.get("access_token", "") if isinstance(tokens, dict) else ""
        api_key = auth.get("OPENAI_API_KEY", "") if isinstance(auth, dict) else ""
        credential = str(access_token or api_key).strip()
        if not credential:
            raise RuntimeError(
                "CC Switch 当前选择了 OpenAI Official，但 Codex 登录凭据为空；"
                "请重新登录 Codex"
            )
        return model, credential

    def _generate_with_ccswitch(
        self,
        history: list[dict[str, str]],
        message: str,
        image_path: str | None = None,
    ) -> str:
        model, credential = self._ccswitch_runtime()
        input_items: list[dict[str, Any]] = [
            {"role": item["role"], "content": item["content"]}
            for item in history
        ]
        current_content: list[dict[str, str]] = [
            {"type": "input_text", "text": message}
        ]
        if image_path:
            image_data = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
            mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
            current_content.append(
                {"type": "input_image", "image_url": f"data:{mime};base64,{image_data}"}
            )
        input_items.append({"role": "user", "content": current_content})

        # The Codex route requires streaming Responses requests. Do not send
        # max_output_tokens: ChatGPT-backed providers reject that parameter,
        # while the local character limit is still enforced after generation.
        client = OpenAI(
            api_key=credential,
            base_url=self.settings.llm_base_url,
            timeout=self.settings.codex_timeout_seconds,
            max_retries=1,
        )
        parts: list[str] = []
        completed_text = ""
        try:
            stream = client.responses.create(
                model=model,
                instructions=self._effective_system_prompt(),
                input=input_items,
                store=False,
                stream=True,
            )
            for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    parts.append(str(getattr(event, "delta", "")))
                elif event_type == "response.completed":
                    response = getattr(event, "response", None)
                    completed_text = str(getattr(response, "output_text", "") or "")
        except Exception as exc:
            raise RuntimeError(
                f"CC Switch 调用失败（{self.settings.llm_base_url}）：{exc}"
            ) from exc
        finally:
            client.close()

        return ("".join(parts) or completed_text).strip()

    def _generate_with_codex(
        self,
        history: list[dict[str, str]],
        message: str,
        image_path: str | None = None,
    ) -> str:
        self._check_codex_network()
        transcript = "\n".join(
            f"{'对方' if item['role'] == 'user' else '我'}：{item['content']}"
            for item in history
        )
        prompt = (
            f"{self._effective_system_prompt()}\n\n"
            "你现在只负责生成一条微信回复。\n"
            "只输出最终要发送的消息正文，不要前缀、解释、Markdown、引号或代码块。\n"
            "不要调用工具，不要修改文件，不要执行命令。\n"
            f"历史对话：\n{transcript or '（无）'}\n\n"
            f"对方最新消息：\n{message}"
        )
        file_descriptor, output_name = tempfile.mkstemp(prefix="wechat-reply-", suffix=".txt")
        os.close(file_descriptor)
        output_path = Path(output_name)
        try:
            args = [
                self.settings.codex_command,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "--output-last-message",
                str(output_path),
                "-",
            ]
            if image_path:
                args[2:2] = ["--image", str(Path(image_path).resolve())]
            if self.settings.llm_model:
                args[2:2] = ["--model", self.settings.llm_model]
            result = subprocess.run(
                args,
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=PROJECT_DIR,
                timeout=self.settings.codex_timeout_seconds,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-1000:]
                raise RuntimeError(f"Codex CLI 调用失败（{result.returncode}）：{detail}")
            return output_path.read_text(encoding="utf-8").strip()
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Codex CLI 回复超时") from exc
        finally:
            output_path.unlink(missing_ok=True)
