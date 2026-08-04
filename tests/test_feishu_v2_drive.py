from __future__ import annotations

import json

import httpx
import pytest

import folder2feishu.feishu.drive as drive_module
from folder2feishu.feishu import (
    DriveService,
    FeishuAPIClient,
    FeishuProtocolError,
    RateLimitSet,
    UploadSession,
    deterministic_staging_name,
    parse_drive_folder_token,
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


def _service(handler, *, now=lambda: 1000):
    api = FeishuAPIClient(
        lambda: "u-fixed-user",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limits=RateLimitSet.disabled(),
        sleeper=lambda _: None,
    )
    return DriveService(api, now=now)


def test_current_user_quota_uses_user_id_and_quota_v2_endpoint():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/authen/v1/user_info"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"user_id": "u-current", "name": "Owner"}},
            )
        if request.url.path.endswith("/drive/v2/quota_details/u-current"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "biz_infos": [
                            {
                                "name": "ccm",
                                "used": "100",
                                "quota": "1000",
                                "unlimited": False,
                            }
                        ],
                        "is_tenant_quota_exceeded": False,
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    quota = _service(handler).get_current_user_quota()

    assert quota["biz_infos"][0] == {
        "name": "ccm",
        "used": "100",
        "quota": "1000",
        "unlimited": False,
    }
    assert [request.url.path for request in seen] == [
        "/open-apis/authen/v1/user_info",
        "/open-apis/drive/v2/quota_details/u-current",
    ]


def test_current_user_quota_fails_closed_without_user_id_field_permission():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"name": "Owner"}})

    with pytest.raises(
        FeishuProtocolError,
        match="contact:user.employee_id:readonly",
    ):
        _service(handler).get_current_user_quota()


def test_rename_file_retries_legacy_rate_limit_in_drive_write_bucket():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "PATCH"
        if calls == 1:
            return httpx.Response(
                400,
                headers={"x-ogw-ratelimit-reset": "0"},
                json={"code": 99991400, "msg": "request trigger frequency limit"},
            )
        return httpx.Response(200, json={"code": 0})

    service = _service(handler)

    assert service.rename_file("file-token", "original.xlsx") == "file-token"
    assert calls == 2


def test_can_edit_wiki_checks_container_edit_permission():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/drive/v1/permissions/wiki_parent/members/auth")
        assert dict(request.url.params) == {"type": "wiki", "action": "edit"}
        return httpx.Response(
            200,
            json={"code": 0, "data": {"auth_result": True}},
        )

    assert _service(handler).can_edit_wiki("wiki_parent") is True


def test_drive_folder_url_and_bare_token_are_parsed() -> None:
    assert parse_drive_folder_token("DriveFolderToken99") == "DriveFolderToken99"
    assert (
        parse_drive_folder_token(
            "https://example.feishu.cn/drive/folder/DriveFolderToken99?from=space"
        )
        == "DriveFolderToken99"
    )
    with pytest.raises(ValueError, match="/drive/folder"):
        parse_drive_folder_token("https://example.feishu.cn/wiki/WikiToken99")


def test_move_file_uses_drive_destination_folder_and_returns_task_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/drive/v1/files/file-1/move")
        assert json.loads(request.content) == {
            "type": "file",
            "folder_token": "destination-folder",
        }
        return httpx.Response(200, json={"code": 0, "data": {"task_id": "task-1"}})

    task_id = _service(handler).move_file(
        "file-1",
        object_type="file",
        destination_folder_token="destination-folder",
    )
    assert task_id == "task-1"


def test_root_and_staging_folders_return_explicit_tokens():
    created = {}
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/root_folder/meta"):
            return httpx.Response(200, json={"code": 0, "data": {"token": "root-token"}})
        if request.method == "GET" and request.url.path.endswith("/drive/v1/files"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"files": [], "has_more": False}},
            )
        if request.url.path.endswith("/drive/v1/files/create_folder"):
            payload = json.loads(request.content)
            token = f"folder-{len(created) + 1}"
            created[payload["name"]] = (payload["folder_token"], token)
            return httpx.Response(200, json={"code": 0, "data": {"token": token}})
        raise AssertionError(request.url)

    service = _service(handler)
    location = service.ensure_staging("project-A", shard_index=7)
    assert location.root_token == "folder-1"
    assert location.project_token == "folder-2"
    assert location.shard_token == "folder-3"
    assert created["Folder2Feishu-Staging"][0] == "root-token"
    assert "project-" in next(name for name in created if name.startswith("project-"))
    assert created["shard-000007"][0] == "folder-2"

    first_request_count = len(requests)
    assert service.ensure_staging("project-A", shard_index=7) == location
    assert len(requests) == first_request_count

    next_shard = service.ensure_staging("project-A", shard_index=8)
    assert next_shard.root_token == location.root_token
    assert next_shard.project_token == location.project_token
    assert next_shard.shard_token == "folder-4"
    assert requests[first_request_count:] == [
        ("GET", "/open-apis/drive/v1/files"),
        ("POST", "/open-apis/drive/v1/files/create_folder"),
    ]


def test_upload_all_always_sends_nonempty_parent_and_persists_before_rename(tmp_path):
    source = tmp_path / "中文 & report.pdf"
    source.write_bytes(b"content")
    events = []
    seen_requests: list[httpx.Request] = []
    hooks = CapturingHooks()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"files": [], "has_more": False}},
            )
        if request.url.path.endswith("/upload_all"):
            content = request.read()
            assert b'name="parent_node"' in content
            assert b"staging-folder" in content
            assert b".f2fw-" in content
            events.append("uploaded")
            return httpx.Response(200, json={"code": 0, "data": {"file_token": "file-1"}})
        if request.method == "PATCH":
            # The file token must be durable before the title is restored.
            assert hooks.events[-1] == ("file", "file-1")
            assert request.url.path.endswith("/drive/v1/files/file-1")
            assert request.url.params["type"] == "file"
            assert json.loads(request.content)["new_title"] == source.name
            events.append("renamed")
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(request.url)

    staged = _service(handler).stage_file(
        source,
        parent_node="staging-folder",
        project_id="project-A",
        item_key="folder/中文 & report.pdf",
        hooks=hooks,
        probe_existing=False,
    )
    assert staged.file_token == "file-1"
    assert staged.final_name == source.name
    assert staged.internal_name == deterministic_staging_name(
        "project-A", "folder/中文 & report.pdf", source.name
    )
    assert events == ["uploaded", "renamed"]
    assert not any(request.method == "GET" for request in seen_requests)


def test_upload_all_5xx_is_reconciled_by_staging_name_without_reposting(tmp_path):
    source = tmp_path / "report.xlsx"
    source.write_bytes(b"committed despite response")
    hooks = CapturingHooks()
    internal_name = deterministic_staging_name(
        "project-A",
        "folder/report.xlsx",
        source.name,
    )
    lists = uploads = renames = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal lists, uploads, renames
        if request.method == "GET":
            lists += 1
            files = (
                []
                if lists == 1
                else [
                    {
                        "name": internal_name,
                        "type": "file",
                        "token": "committed-file",
                    }
                ]
            )
            return httpx.Response(
                200,
                json={"code": 0, "data": {"files": files, "has_more": False}},
            )
        if request.url.path.endswith("/upload_all"):
            uploads += 1
            return httpx.Response(
                503,
                json={"code": 500001, "msg": "response lost after commit"},
            )
        assert request.method == "PATCH"
        renames += 1
        return httpx.Response(200, json={"code": 0, "data": {}})

    staged = _service(handler).stage_file(
        source,
        parent_node="staging-folder",
        project_id="project-A",
        item_key="folder/report.xlsx",
        hooks=hooks,
    )

    assert staged.file_token == "committed-file"
    assert (uploads, lists, renames) == (1, 2, 1)
    assert hooks.events == [("file", "committed-file")]


def test_empty_parent_node_is_rejected_before_http(tmp_path):
    source = tmp_path / "file.txt"
    source.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"HTTP should not run: {request.url}")

    with pytest.raises(ValueError, match="parent_node"):
        _service(handler).upload_file(source, file_name=source.name, parent_node="")


def test_multipart_persists_session_parts_and_file_token(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_module, "SINGLE_UPLOAD_LIMIT", 1)
    monkeypatch.setattr(drive_module, "CHUNK_SIZE", 4)
    source = tmp_path / "large.bin"
    source.write_bytes(b"abcdefghij")

    class CountingHooks(CapturingHooks):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def on_upload_attempt(self):
            self.attempts += 1

    hooks = CountingHooks()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/upload_prepare"):
            payload = json.loads(request.content)
            assert payload["parent_node"] == "staging"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "upload_id": "upload-1",
                        "block_size": 4,
                        "block_num": 3,
                    },
                },
            )
        if request.url.path.endswith("/upload_part"):
            return httpx.Response(200, json={"code": 0, "data": None})
        if request.url.path.endswith("/upload_finish"):
            assert json.loads(request.content) == {
                "upload_id": "upload-1",
                "block_num": 3,
            }
            return httpx.Response(200, json={"code": 0, "data": {"file_token": "file-large"}})
        raise AssertionError(request.url)

    token = _service(handler).upload_file(
        source,
        file_name=source.name,
        parent_node="staging",
        hooks=hooks,
    )
    assert token == "file-large"
    assert hooks.events[0][0] == "session"
    session = hooks.events[0][1]
    assert session.upload_id == "upload-1"
    assert [event[2] for event in hooks.events if event[0] == "part"] == [0, 1, 2]
    assert hooks.events[-1] == ("file", "file-large")
    assert hooks.attempts == 5


def test_multipart_finish_5xx_reconciles_without_repeating_finish(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(drive_module, "SINGLE_UPLOAD_LIMIT", 1)
    monkeypatch.setattr(drive_module, "CHUNK_SIZE", 4)
    source = tmp_path / "ambiguous-finish.bin"
    source.write_bytes(b"abcdef")
    hooks = CapturingHooks()
    internal_name = deterministic_staging_name(
        "project-A",
        "item-A",
        source.name,
    )
    lists = finishes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal lists, finishes
        if request.method == "GET":
            lists += 1
            files = (
                []
                if lists == 1
                else [
                    {
                        "name": internal_name,
                        "type": "file",
                        "token": "finished-file",
                    }
                ]
            )
            return httpx.Response(
                200,
                json={"code": 0, "data": {"files": files, "has_more": False}},
            )
        if request.url.path.endswith("/upload_prepare"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "upload_id": "upload-ambiguous",
                        "block_size": 4,
                        "block_num": 2,
                    },
                },
            )
        if request.url.path.endswith("/upload_part"):
            return httpx.Response(200, json={"code": 0})
        if request.url.path.endswith("/upload_finish"):
            finishes += 1
            return httpx.Response(
                503,
                json={"code": 500001, "msg": "finish response lost"},
            )
        assert request.method == "PATCH"
        return httpx.Response(200, json={"code": 0})

    staged = _service(handler).stage_file(
        source,
        parent_node="staging",
        project_id="project-A",
        item_key="item-A",
        hooks=hooks,
    )

    assert staged.file_token == "finished-file"
    assert finishes == 1
    assert lists == 2
    assert hooks.events[-1] == ("file", "finished-file")


def test_multipart_resume_skips_completed_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_module, "SINGLE_UPLOAD_LIMIT", 1)
    monkeypatch.setattr(drive_module, "CHUNK_SIZE", 4)
    source = tmp_path / "resume.bin"
    source.write_bytes(b"abcdef")
    hooks = CapturingHooks()
    paths = []
    part_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/upload_part"):
            part_bodies.append(request.read())
            return httpx.Response(200, json={"code": 0})
        if request.url.path.endswith("/upload_finish"):
            return httpx.Response(200, json={"code": 0, "data": {"file_token": "resumed-file"}})
        raise AssertionError(request.url)

    session = UploadSession(
        upload_id="resume-id",
        parent_node="staging",
        file_name=source.name,
        size=6,
        block_size=4,
        block_num=2,
        completed_parts=frozenset({0}),
        expires_at=2000,
    )
    token = _service(handler).upload_file(
        source,
        file_name=source.name,
        parent_node="staging",
        resume_session=session,
        hooks=hooks,
    )
    assert token == "resumed-file"
    assert not any(path.endswith("/upload_prepare") for path in paths)
    assert len(part_bodies) == 1
    assert b'name="seq"\r\n\r\n1' in part_bodies[0]
    assert ("part", "resume-id", 1) in hooks.events


def test_staging_name_is_deterministic_unique_and_bounded():
    long_name = "文" * 240 + ".xlsx"
    first = deterministic_staging_name("project", "A/file", long_name)
    assert first == deterministic_staging_name("project", "A/file", long_name)
    assert first != deterministic_staging_name("project", "B/file", long_name)
    assert len(first) <= 250
    assert first.endswith(".xlsx")
