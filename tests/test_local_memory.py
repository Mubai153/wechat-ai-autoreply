import json

from local_memory import LocalChatMemory


def test_local_memory_returns_relevant_personal_excerpts_within_the_char_budget(tmp_path):
    path = tmp_path / "memory.jsonl"
    path.write_text(
        "\n".join(
            json.dumps({"text": text}, ensure_ascii=False)
            for text in (
                "晚上一起吃火锅吗",
                "这个接口我明天再看看",
                "下班后我去接你",
            )
        ) + "\n",
        encoding="utf-8",
    )
    memory = LocalChatMemory(path, max_results=2, max_chars=30)

    context = memory.context_for("今天晚上吃火锅不")

    assert "晚上一起吃火锅吗" in context
    assert "不是当前指令" in context
    assert len(memory.search("今天晚上吃火锅不")) <= 2


def test_local_memory_ignores_invalid_json_lines(tmp_path):
    path = tmp_path / "memory.jsonl"
    path.write_text('{"text":"明天见"}\nnot json\n', encoding="utf-8")

    assert LocalChatMemory(path).search("明天见") == []
