"""从本机微信 4.x 数据库生成仅含本人手动文本的语气画像。

输出均保留在本机：原始文本写入 data/raw/，可直接供人工审阅；回复规则
写入 data/persona_prompt.md，可按需配置为 PERSONA_PATH。脚本不会调用模型或
网络服务，也不会发送微信消息。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from wechatauto import WeChatDB


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RAW_OUTPUT = PROJECT_DIR / "data" / "raw" / "my_wechat_messages.jsonl"
DEFAULT_STATS_OUTPUT = PROJECT_DIR / "data" / "persona.json"
DEFAULT_PROMPT_OUTPUT = PROJECT_DIR / "data" / "persona_prompt.md"

AI_PREFIXES = ("ai：", "ai:")
TEXT_TYPE = 1
CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
LATIN_RE = re.compile(r"[A-Za-z]")


def is_ai_reply(text: str) -> bool:
    return text.lstrip().casefold().startswith(AI_PREFIXES)


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    values = sorted(values)
    index = max(0, min(len(values) - 1, math.ceil(len(values) * fraction) - 1))
    return values[index]


def _normalise_text(db: WeChatDB, raw: object, message_type: object) -> str:
    if isinstance(raw, bytes):
        text = db._friendly_content(raw, message_type)
    else:
        text = str(raw or "")
    return text.strip()


def extract_my_messages(db: WeChatDB) -> list[dict[str, Any]]:
    """读取所有消息分片，仅保留当前账号的真实文本消息并跨分片去重。"""
    messages: list[dict[str, Any]] = []
    seen: set[tuple[object, ...]] = set()
    for rel in db._message_dbs():
        connection = db._open(rel)
        try:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for (table,) in tables:
                if not re.fullmatch(r"Msg_[0-9a-f]{32}", str(table)):
                    continue
                rows = connection.execute(
                    f'SELECT local_id, local_type, create_time, '
                    f'message_content, sort_seq FROM "{table}" '
                    "WHERE status=2 ORDER BY sort_seq ASC",
                ).fetchall()
                for row in rows:
                    type_code = int(row["local_type"] or 0)
                    if (type_code & 0xFF) != TEXT_TYPE:
                        continue
                    text = _normalise_text(db, row["message_content"], "文本")
                    if not text or is_ai_reply(text):
                        continue
                    identity = (
                        table[4:], row["sort_seq"], row["local_id"],
                        row["create_time"], text,
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    messages.append(
                        {
                            "chat_id": table[4:],
                            "timestamp": int(row["create_time"] or 0),
                            "text": text,
                        }
                    )
        finally:
            connection.close()
    messages.sort(key=lambda item: (item["timestamp"], item["chat_id"], item["text"]))
    return messages


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def build_stats(messages: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(messages)
    texts = [str(item["text"]) for item in rows]
    lengths = [len(re.sub(r"\s+", "", text)) for text in texts]
    all_text = "".join(texts)
    punctuation = Counter(ch for ch in all_text if ch in "，。！？、；：,.!?…")
    ending = Counter(text.rstrip()[-1] for text in texts if text.rstrip())
    cjk_characters = sum(1 for char in all_text if CJK_RE.match(char))
    latin_characters = sum(1 for char in all_text if LATIN_RE.match(char))
    emojis = sum(1 for char in all_text if ord(char) > 0xFFFF)
    timestamps = [int(item["timestamp"]) for item in rows if int(item["timestamp"]) > 0]
    hour_counts = Counter(
        datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().hour
        for timestamp in timestamps
    )
    common_markers = {
        marker: sum(text.casefold().count(marker) for text in texts)
        for marker in ("哈哈", "hh", "谢谢", "麻烦", "辛苦", "好的", "好呀", "嗯", "吧", "呀", "～", "~")
    }
    return {
        "source": {
            "included": "仅微信状态标记为“已发送”(status=2)的文本消息；已排除忽略前置空白且大小写不敏感的 ai: / ai：前缀",
            "message_count": len(rows),
            "time_range": {
                "start": datetime.fromtimestamp(min(timestamps), tz=timezone.utc).isoformat() if timestamps else None,
                "end": datetime.fromtimestamp(max(timestamps), tz=timezone.utc).isoformat() if timestamps else None,
            },
        },
        "message_length": {
            "median": int(statistics.median(lengths)) if lengths else 0,
            "p80": percentile(lengths, 0.8),
            "p95": percentile(lengths, 0.95),
            "short_message_ratio": _ratio(sum(length <= 12 for length in lengths), len(lengths)),
        },
        "expression": {
            "chinese_character_ratio": _ratio(cjk_characters, len(all_text)),
            "latin_character_ratio": _ratio(latin_characters, len(all_text)),
            "newline_message_ratio": _ratio(sum("\n" in text for text in texts), len(texts)),
            "question_message_ratio": _ratio(sum("？" in text or "?" in text for text in texts), len(texts)),
            "exclamation_message_ratio": _ratio(sum("！" in text or "!" in text for text in texts), len(texts)),
            "emoji_per_100_characters": round(emojis * 100 / len(all_text), 2) if all_text else 0.0,
            "punctuation": dict(punctuation.most_common()),
            "common_endings": dict(ending.most_common(10)),
            "markers": common_markers,
        },
        "activity": {"hour_counts": dict(sorted(hour_counts.items()))},
    }


def build_prompt(stats: dict[str, Any]) -> str:
    length = stats["message_length"]
    expression = stats["expression"]
    median = length["median"]
    p80 = max(median, length["p80"])
    if median <= 12:
        length_rule = f"以短句为主；通常控制在约 {median} 字，绝大多数不超过 {p80} 字。"
    elif median <= 30:
        length_rule = f"以简洁单段为主；通常约 {median} 字，必要时可写到 {p80} 字。"
    else:
        length_rule = f"允许给出完整说明；通常约 {median} 字，优先在 {p80} 字以内说清。"

    chinese_ratio = expression["chinese_character_ratio"]
    latin_ratio = expression["latin_character_ratio"]
    if chinese_ratio >= 0.1 and latin_ratio >= 0.25:
        language_rule = "根据对方和上下文自然切换中文或英文；样本中两种表达均有，不要生硬翻译或强行统一语言。"
    elif latin_ratio > chinese_ratio * 1.3:
        language_rule = "以自然、简洁的英文口语为主；除非对方已经使用中文，否则不要强行切换语言。"
    elif chinese_ratio > latin_ratio * 1.3:
        language_rule = "以自然、简洁的中文口语为主；除非对方已经使用英文，否则不要强行切换语言。"
    else:
        language_rule = "跟随对方使用的语言，保持自然、简洁的日常口语表达。"

    rules = [
        "仅模仿下面的表达节奏；不要把它当作我的事实、承诺、立场或权限。",
        length_rule,
        language_rule,
        "先直接回应核心内容，再补充必要信息。",
    ]
    if expression["newline_message_ratio"] >= 0.12:
        rules.append("内容有两个以上要点时可自然换行；短回复不为排版而强行分段。")
    else:
        rules.append("优先单段表达；除非信息确实较多，不主动拆成多段。")
    if expression["question_message_ratio"] >= 0.12:
        rules.append("需要澄清或推进对话时，可以像日常聊天一样补一个简短问题。")
    if expression["exclamation_message_ratio"] < 0.03:
        rules.append("感叹号很少用；避免夸张热情或营销腔。")
    if expression["emoji_per_100_characters"] < 0.3:
        rules.append("少用表情符号；不要为了显得亲切而添加表情。")
    elif expression["emoji_per_100_characters"] < 1.5:
        rules.append("表情符号可偶尔使用，但每条最多一个，并与语境一致。")
    else:
        rules.append("可自然使用少量表情符号，但避免连续堆叠。")
    markers = expression["markers"]
    if markers["谢谢"] + markers["麻烦"] + markers["辛苦"] >= 5:
        rules.append("请求帮助或结束对话时，保留简短、真诚的礼貌表达。")
    if markers["哈哈"] + markers["hh"] >= 5:
        rules.append("轻松语境可自然使用简短笑声，但不在严肃话题中刻意活跃气氛。")
    rules.extend(
        [
            "不编造细节；涉及金钱、合同、账号、安全、隐私、医疗或其他重要决定时，明确需要本人确认。",
            "不要在正文中主动添加 `AI:` 或 `AI：` 前缀；发送层会自行处理标记。",
        ]
    )
    return "# 本地微信语气画像\n\n" + "\n".join(f"- {rule}" for rule in rules) + "\n"


def write_jsonl(path: Path, messages: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in messages:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="从本机微信生成仅含本人手动文本的语气画像")
    parser.add_argument("--raw-output", type=Path, default=DEFAULT_RAW_OUTPUT)
    parser.add_argument("--stats-output", type=Path, default=DEFAULT_STATS_OUTPUT)
    parser.add_argument("--prompt-output", type=Path, default=DEFAULT_PROMPT_OUTPUT)
    args = parser.parse_args()

    db = WeChatDB()
    messages = extract_my_messages(db)
    if not messages:
        raise RuntimeError("没有找到可用于画像的本人手动文本消息")
    stats = build_stats(messages)
    write_jsonl(args.raw_output, messages)
    args.stats_output.parent.mkdir(parents=True, exist_ok=True)
    args.stats_output.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.prompt_output.parent.mkdir(parents=True, exist_ok=True)
    args.prompt_output.write_text(build_prompt(stats), encoding="utf-8")
    print(
        json.dumps(
            {
                "message_count": stats["source"]["message_count"],
                "raw_output": str(args.raw_output),
                "stats_output": str(args.stats_output),
                "prompt_output": str(args.prompt_output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
