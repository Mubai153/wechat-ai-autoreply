import json

from web_search import SearXNGSearch


def test_search_formats_limited_results(monkeypatch):
    payload = {
        "results": [
            {"title": "第一条", "content": "第一段摘要", "url": "https://example.com/one"},
            {"title": "第二条", "content": "第二段摘要", "url": "https://example.com/two"},
        ]
    }

    class Response:
        def read(self):
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    requested = []

    def fake_urlopen(url, timeout):
        requested.append((url, timeout))
        return Response()

    monkeypatch.setattr("web_search.urlopen", fake_urlopen)
    result = SearXNGSearch("http://127.0.0.1:8080/", max_results=1, timeout_seconds=9).search("今天天气")

    assert "第一条" in result
    assert "第二条" not in result
    assert "format=json" in requested[0][0]
    assert "q=%E4%BB%8A%E5%A4%A9%E5%A4%A9%E6%B0%94" in requested[0][0]
    assert requested[0][1] == 9


def test_search_returns_safe_message_when_service_is_unavailable(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("web_search.urlopen", unavailable)

    result = SearXNGSearch("http://127.0.0.1:8080").search("新闻")

    assert "暂时不可用" in result
