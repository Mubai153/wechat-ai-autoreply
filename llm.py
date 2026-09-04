from __future__ import annotations

import os
import base64
import mimetypes
import shutil
import subprocess
import tempfile
from pathlib import Path

from openai import OpenAI

from config import PROJECT_DIR, Settings


class ReplyGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.persona_prompt = self._load_persona_prompt()
        self.client = None
        if settings.llm_provider == "codex_cli":
            if not shutil.which(settings.codex_command) and not Path(settings.codex_command).exists():
                raise RuntimeError(
                    f"找不到 Codex CLI：{settings.codex_command}。请确认已安装并加入 PATH。"
                )
        else:
            self.client = OpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
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
            response = self.client.chat.completions.create(
                model=self.settings.llm_model,
                messages=messages,
                temperature=0.7,
                max_tokens=self.settings.max_reply_chars * 2,
            )
            reply = (response.choices[0].message.content or "").strip()
        if not reply:
            raise RuntimeError("模型返回了空回复")
        return reply[: self.settings.max_reply_chars]

    def _generate_with_codex(
        self,
        history: list[dict[str, str]],
        message: str,
        image_path: str | None = None,
    ) -> str:
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
