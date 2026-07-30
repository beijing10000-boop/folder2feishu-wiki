from pathlib import Path

from app.feishu import parse_wiki_token
from app.migrator import parent_rel, safe_remote_name


def test_parse_wiki_url_and_token():
    token = "XdhSwsU7PiDZSak2WoIc2Qb8nDc"
    assert parse_wiki_token(
        f"https://example.feishu.cn/wiki/{token}?fromScene=spaceOverview"
    ) == token
    assert parse_wiki_token(token) == token


def test_parent_rel_uses_portable_separator():
    assert parent_rel("A/B/C.txt") == "A/B"
    assert parent_rel("A.txt") == ""


def test_safe_remote_name_preserves_extension_and_is_deterministic():
    name = "a" * 300 + ".xlsx"
    first = safe_remote_name(name)
    second = safe_remote_name(name)
    assert first == second
    assert len(first) <= 250
    assert first.endswith(".xlsx")
    assert "~" in first

