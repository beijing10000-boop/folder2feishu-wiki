from __future__ import annotations

import json

import httpx
import pytest

from folder2feishu.feishu import (
    FeishuAPIClient,
    RateLimitSet,
    ReconcileStatus,
    WikiMoveTaskFailedError,
    WikiService,
    parse_wiki_reference,
    parse_wiki_token,
)


class CapturingHooks:
    def __init__(self):
        self.events = []

    def on_upload_session(self, session):
        self.events.append(("session", session))

    def on_upload_part(self, upload_id, sequence):
        self.events.append(("part", upload_id, sequence))

    def on_file_token(self, file_token):
        self.events.append(("file", file_token))

    def on_wiki_task(self, task_id):
        self.events.append(("task", task_id))

    def on_wiki_token(self, wiki_token):
        self.events.append(("wiki", wiki_token))


def _service(handler):
    api = FeishuAPIClient(
        lambda: "u-fixed-user",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limits=RateLimitSet.disabled(),
        sleeper=lambda _: None,
    )
    return WikiService(api, sleeper=lambda _: None)


def test_parse_wiki_url_and_reject_non_feishu_hosts():
    token = "XdhSwsU7PiDZSak2WoIc2Qb8nDc"
    value = f"https://pg6xd0yqgm.feishu.cn/wiki/{token}?fromScene=spaceOverview"
    assert parse_wiki_token(value) == token
    assert parse_wiki_reference(value).host == "pg6xd0yqgm.feishu.cn"
    assert parse_wiki_token(token) == token
    with pytest.raises(ValueError):
        parse_wiki_token(f"https://evil.example/wiki/{token}")


def test_create_and_list_docx_folder_nodes_return_tokens():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload == {
                "obj_type": "docx",
                "node_type": "origin",
                "parent_node_token": "parent",
                "title": "原目录",
            }
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "node": {
                            "space_id": "space",
                            "node_token": "created-node",
                            "obj_token": "docx-token",
                            "obj_type": "docx",
                            "title": "原目录",
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [{"node_token": "created-node", "title": "原目录"}],
                    "has_more": False,
                },
            },
        )

    wiki = _service(handler)
    node = wiki.create_docx_node("space", "原目录", "parent")
    assert node["node_token"] == "created-node"
    assert wiki.list_children("space", "parent")[0]["node_token"] == "created-node"
    assert calls[1].url.params["parent_node_token"] == "parent"


def test_ensure_docx_node_reconciles_ambiguous_create_without_duplicate():
    lists = 0
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal lists, posts
        if request.method == "GET":
            lists += 1
            items = (
                []
                if lists == 1
                else [
                    {
                        "node_token": "committed-node",
                        "obj_token": "docx-token",
                        "obj_type": "docx",
                        "title": "原目录",
                        "space_id": "space",
                        "parent_node_token": "parent",
                    }
                ]
            )
            return httpx.Response(
                200,
                json={"code": 0, "data": {"items": items, "has_more": False}},
            )
        posts += 1
        raise httpx.ReadTimeout("response lost after commit", request=request)

    node = _service(handler).ensure_docx_node("space", "原目录", "parent")
    assert node["node_token"] == "committed-node"
    assert (lists, posts) == (2, 1)


def test_move_file_to_wiki_handles_synchronous_result_and_persists_token():
    hooks = CapturingHooks()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/nodes/move_docs_to_wiki")
        assert json.loads(request.content) == {
            "parent_wiki_token": "wiki-parent",
            "obj_type": "file",
            "obj_token": "file-token",
            "apply": False,
        }
        return httpx.Response(200, json={"code": 0, "data": {"wiki_token": "wiki-file"}})

    token = _service(handler).move_file_to_wiki(
        "space",
        file_token="file-token",
        parent_wiki_token="wiki-parent",
        hooks=hooks,
    )
    assert token == "wiki-file"
    assert hooks.events == [("wiki", "wiki-file")]


def test_move_file_to_wiki_handles_async_task_and_persists_ids_in_order():
    hooks = CapturingHooks()
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "task-1"}})
        assert request.url.params["task_type"] == "move"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "task": {
                        "move_result": [
                            {
                                "status": 0,
                                "status_msg": "success",
                                "node": {"node_token": "wiki-async"},
                            }
                        ]
                    }
                },
            },
        )

    token = _service(handler).move_file_to_wiki(
        "space",
        file_token="file-token",
        parent_wiki_token="wiki-parent",
        hooks=hooks,
    )
    assert token == "wiki-async"
    assert hooks.events == [("task", "task-1"), ("wiki", "wiki-async")]
    assert paths == [
        "/open-apis/wiki/v2/spaces/space/nodes/move_docs_to_wiki",
        "/open-apis/wiki/v2/tasks/task-1",
    ]


def test_existing_task_id_resumes_poll_without_reposting_move():
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert request.url.path.endswith("/wiki/v2/tasks/already-persisted")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "task": {
                        "move_result": [
                            {
                                "status": 0,
                                "node": {
                                    "node_token": "wiki-resumed",
                                    "space_id": "space",
                                    "parent_node_token": "wiki-parent",
                                },
                            }
                        ]
                    }
                },
            },
        )

    token = _service(handler).move_file_to_wiki(
        "space",
        file_token="file-token",
        parent_wiki_token="wiki-parent",
        existing_task_id="already-persisted",
    )
    assert token == "wiki-resumed"
    assert methods == ["GET"]


def test_move_task_status_one_is_processing_not_failure():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        result = (
            {"status": 1, "status_msg": "processing"}
            if calls == 1
            else {
                "status": 0,
                "status_msg": "success",
                "node": {"node_token": "wiki-after-processing"},
            }
        )
        return httpx.Response(
            200,
            json={"code": 0, "data": {"task": {"move_result": [result]}}},
        )

    token = _service(handler).move_file_to_wiki(
        "space",
        file_token="file-token",
        parent_wiki_token="wiki-parent",
        existing_task_id="processing-task",
    )

    assert token == "wiki-after-processing"
    assert calls == 2


def test_retryable_failed_task_reconciles_then_replaces_once():
    methods_and_paths: list[tuple[str, str]] = []
    hooks = CapturingHooks()

    def handler(request: httpx.Request) -> httpx.Response:
        methods_and_paths.append((request.method, request.url.path))
        if request.url.path.endswith("/wiki/v2/tasks/failed-task"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"task": {"move_result": [{"status": -1, "status_msg": "failure"}]}},
                },
            )
        if request.method == "GET":
            assert request.url.params["token"] == "file-token"
            return httpx.Response(
                400,
                json={"code": 131005, "msg": "node not found"},
            )
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={"code": 0, "data": {"wiki_token": "wiki-replacement"}},
        )

    token = _service(handler).move_file_to_wiki(
        "space",
        file_token="file-token",
        parent_wiki_token="wiki-parent",
        existing_task_id="failed-task",
        hooks=hooks,
    )

    assert token == "wiki-replacement"
    assert methods_and_paths == [
        ("GET", "/open-apis/wiki/v2/tasks/failed-task"),
        ("GET", "/open-apis/wiki/v2/spaces/get_node"),
        ("POST", "/open-apis/wiki/v2/spaces/space/nodes/move_docs_to_wiki"),
    ]
    assert hooks.events == [("task", ""), ("wiki", "wiki-replacement")]


def test_nonretryable_failed_task_never_reposts_move():
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path.endswith("/wiki/v2/tasks/permission-task"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "task": {"move_result": [{"status": -1, "status_msg": "permission denied"}]}
                    },
                },
            )
        assert request.method == "GET"
        return httpx.Response(
            400,
            json={"code": 131005, "msg": "node not found"},
        )

    with pytest.raises(WikiMoveTaskFailedError) as caught:
        _service(handler).move_file_to_wiki(
            "space",
            file_token="file-token",
            parent_wiki_token="wiki-parent",
            existing_task_id="permission-task",
        )

    assert caught.value.retryable is False
    assert methods == ["GET", "GET"]


def test_ambiguous_move_reconciles_by_drive_object_without_reposting():
    methods = []
    hooks = CapturingHooks()

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            raise httpx.ReadTimeout("move response lost", request=request)
        assert request.url.path.endswith("/wiki/v2/spaces/get_node")
        assert request.url.params["token"] == "file-token"
        assert request.url.params["obj_type"] == "file"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "node": {
                        "node_token": "wiki-reconciled",
                        "obj_token": "file-token",
                        "obj_type": "file",
                        "space_id": "space",
                        "parent_node_token": "wiki-parent",
                    }
                },
            },
        )

    token = _service(handler).move_file_to_wiki(
        "space",
        file_token="file-token",
        parent_wiki_token="wiki-parent",
        hooks=hooks,
    )
    assert token == "wiki-reconciled"
    assert methods == ["POST", "GET"]
    assert hooks.events == [("wiki", "wiki-reconciled")]


def test_move_5xx_reconciles_committed_object_without_reposting():
    methods = []
    hooks = CapturingHooks()

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                503,
                json={"code": 500001, "msg": "response lost after commit"},
            )
        assert request.url.params["token"] == "file-token"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "node": {
                        "node_token": "wiki-after-5xx",
                        "obj_token": "file-token",
                        "obj_type": "file",
                        "space_id": "space",
                        "parent_node_token": "wiki-parent",
                    }
                },
            },
        )

    token = _service(handler).move_file_to_wiki(
        "space",
        file_token="file-token",
        parent_wiki_token="wiki-parent",
        hooks=hooks,
    )

    assert token == "wiki-after-5xx"
    assert methods == ["POST", "GET"]
    assert hooks.events == [("wiki", "wiki-after-5xx")]


def test_move_1061045_retries_then_succeeds_once():
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        assert request.method == "POST"
        posts += 1
        if posts == 1:
            return httpx.Response(
                200,
                json={"code": 1061045, "msg": "server busy"},
            )
        return httpx.Response(
            200,
            json={"code": 0, "data": {"wiki_token": "wiki-after-retry"}},
        )

    token = _service(handler).move_file_to_wiki(
        "space",
        file_token="file-token",
        parent_wiki_token="wiki-parent",
    )

    assert token == "wiki-after-retry"
    assert posts == 2


def test_move_archive_and_rename_return_explicit_node_token():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/move"):
            assert json.loads(request.content) == {
                "target_parent_token": "history-parent",
                "target_space_id": "space",
            }
            return httpx.Response(
                200,
                json={"code": 0, "data": {"node": {"node_token": "old-node"}}},
            )
        assert request.url.path.endswith("/update_title")
        assert json.loads(request.content) == {"title": "新名称"}
        return httpx.Response(200, json={"code": 0, "data": {}})

    wiki = _service(handler)
    assert (
        wiki.archive_file_node("space", "old-node", history_parent_token="history-parent")
        == "old-node"
    )
    assert wiki.rename_node("space", "old-node", "新名称") == "old-node"
    assert len(requests) == 2


def test_reconcile_detects_match_conflict_and_missing():
    responses = [
        httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "node": {
                        "node_token": "node",
                        "space_id": "space",
                        "parent_node_token": "parent",
                        "title": "name",
                        "obj_token": "file",
                    }
                },
            },
        ),
        httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "node": {
                        "node_token": "node",
                        "space_id": "space",
                        "parent_node_token": "someone-moved-it",
                        "title": "name",
                        "obj_token": "file",
                    }
                },
            },
        ),
        httpx.Response(400, json={"code": 131005, "msg": "node not found"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return responses.pop(0)

    wiki = _service(handler)
    expected = {
        "expected_space_id": "space",
        "expected_parent_token": "parent",
        "expected_title": "name",
        "expected_obj_token": "file",
    }
    assert wiki.reconcile_node("node", **expected).status == ReconcileStatus.MATCH
    conflict = wiki.reconcile_node("node", **expected)
    assert conflict.status == ReconcileStatus.CONFLICT
    assert conflict.differences == ("parent_node_token",)
    assert wiki.reconcile_node("node").status == ReconcileStatus.MISSING
