from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from config import PROJECT_DIR, Settings


class ImageRecognitionError(RuntimeError):
    pass


class ImageRecognizer:
    """通过当前 LLM 后端对单张本地图片做客观描述和 OCR。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def recognize(self, image_path: str | Path) -> dict[str, object]:
        path = Path(image_path).resolve()
        if not path.exists() or not path.is_file():
            raise ImageRecognitionError(f"图片不存在：{path}")
        if self.settings.llm_provider != "codex_cli":
            raise ImageRecognitionError(
                "图片识别目前需要 LLM_PROVIDER=codex_cli；兼容接口请使用支持视觉输入的模型。"
            )

        output_fd, output_name = tempfile.mkstemp(prefix="wechat-image-", suffix=".json")
        Path(output_name).unlink(missing_ok=True)
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
                "--image",
                str(path),
                "--output-last-message",
                output_name,
                "-",
            ]
            if self.settings.llm_model:
                args[2:2] = ["--model", self.settings.llm_model]
            prompt = (
                "请客观识别附带图片，用于微信聊天上下文。严格只输出 JSON，格式为 "
                '{"description":"不超过120字的客观描述","ocr":"可见文字，没有则空字符串",'
                '"confidence":0到1的数字}。不要猜测人物身份、地点、隐私或图片外的信息。'
            )
            result = subprocess.run(
                args,
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=PROJECT_DIR,
                timeout=max(30, self.settings.codex_timeout_seconds),
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-1000:]
                raise ImageRecognitionError(f"图片识别失败（{result.returncode}）：{detail}")
            raw = Path(output_name).read_text(encoding="utf-8").strip()
            raw = raw.removeprefix("```json").removesuffix("```").strip()
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("模型返回的不是 JSON 对象")
            description = str(data.get("description", "")).strip()[:120]
            ocr = str(data.get("ocr", "")).strip()[:500]
            try:
                confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
            except (TypeError, ValueError):
                confidence = 0.0
            return {"description": description, "ocr": ocr, "confidence": confidence}
        except subprocess.TimeoutExpired as exc:
            raise ImageRecognitionError("图片识别超时") from exc
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ImageRecognitionError(f"图片识别结果无效：{exc}") from exc
        finally:
            Path(output_name).unlink(missing_ok=True)
            try:
                import os

                os.close(output_fd)
            except OSError:
                pass
