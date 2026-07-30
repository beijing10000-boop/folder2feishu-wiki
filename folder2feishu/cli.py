"""Windows entry point for interactive and scheduled migration runs."""

from __future__ import annotations

import argparse
import ctypes
import logging
import socket
import sys
import threading
import webbrowser
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from . import __version__
from .api import create_app
from .application import ApplicationServices
from .core import JobRun, MigrationState, RunStatus, RunType
from .executor import ExecutionResult
from .logging_config import configure_logging
from .runtime import RuntimePaths
from .scheduler import remove_schedule

LOGGER = logging.getLogger(__name__)


def _local_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _show_startup_error(message: str) -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "Folder2Feishu Wiki",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        LOGGER.debug("无法显示 Windows 启动错误对话框", exc_info=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Folder2Feishu",
        description="把 Windows 本地目录按原层级安全迁移到飞书知识库",
    )
    parser.add_argument(
        "--run-project",
        metavar="PROJECT_ID",
        help="无界面执行一次盘点、差异计划与安全增量迁移",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动本地控制台时不自动打开浏览器",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--remove-all-schedules",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _latest_resumable_run(
    services: ApplicationServices,
    project_id: str,
) -> JobRun | None:
    """Return only the latest interrupted migration, never an older superseded run."""

    latest_migration = next(
        (
            run
            for run in services.store.list_job_runs(project_id)
            if run.run_type == RunType.MIGRATION
        ),
        None,
    )
    if (
        latest_migration is None
        or latest_migration.status in {RunStatus.COMPLETE, RunStatus.CANCELLED}
        or not latest_migration.plan_id
    ):
        return None
    actions = services.store.list_plan_actions(
        project_id,
        plan_id=latest_migration.plan_id,
    )
    if not actions or all(action.state == MigrationState.DONE for action in actions):
        return None
    return latest_migration


def _execution_exit_code(result: ExecutionResult) -> int:
    if result.quota_paused:
        LOGGER.warning("达到每日上传预算，已保留断点等待下一次运行")
        return 0
    if result.failed or result.conflicts:
        LOGGER.error(
            "运行结束但仍有异常：failed=%s conflicts=%s",
            result.failed,
            result.conflicts,
        )
        return 5
    LOGGER.info(
        "运行完成：completed=%s skipped=%s",
        result.completed,
        result.skipped,
    )
    return 0


def run_project(services: ApplicationServices, project_id: str) -> int:
    project = services.store.get_project(project_id)
    resumable = _latest_resumable_run(services, project_id)
    if resumable is not None:
        if not resumable.scan_id or resumable.scan_id != project.current_scan_id:
            LOGGER.error("未完成运行绑定的盘点已被替换，拒绝执行旧计划；请确认当前差异计划")
            return 4
        actions = services.store.list_plan_actions(
            project_id,
            plan_id=resumable.plan_id,
        )
        if any(
            action.state in {MigrationState.CONFLICT, MigrationState.MANUAL_ACTION}
            for action in actions
        ):
            LOGGER.error("断点计划包含冲突或人工处理项，未自动继续")
            return 4
        preflight = services.preflight(project_id)
        if not preflight.ready:
            failed = [check["message"] for check in preflight.checks if check.get("blocking")]
            LOGGER.error("断点恢复预检未通过：%s", "；".join(failed))
            return 3
        LOGGER.info(
            "发现未完成运行 %s，按原 plan_id=%s 恢复，不重新扫描或建计划",
            resumable.id,
            resumable.plan_id,
        )
        return _execution_exit_code(services.executor().execute(project_id, run_id=resumable.id))

    LOGGER.info("开始计划任务扫描：%s", project.name)
    scan = services.scanner.scan(project_id)
    if not scan.complete:
        LOGGER.error("扫描不完整，已停止：blocking=%s", scan.blocking_issues)
        return 2
    preflight = services.preflight(project_id)
    if not preflight.ready:
        failed = [check["message"] for check in preflight.checks if check.get("blocking")]
        LOGGER.error("预检未通过：%s", "；".join(failed))
        return 3
    plan = services.planner.build(project_id)
    if plan.blocked:
        LOGGER.error("安全增量计划包含冲突或人工处理项")
        return 4
    services.confirm_latest_plan(project_id)
    result = services.executor().execute(project_id)
    return _execution_exit_code(result)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = RuntimePaths.discover(args.runtime_dir).ensure()
    configure_logging(paths)
    services = ApplicationServices(paths=paths)
    if args.remove_all_schedules:
        try:
            for spec in services.schedules.list_all():
                remove_schedule(spec.project_id)
            LOGGER.info("已清理 Folder2Feishu Windows 计划任务")
            return 0
        except Exception:
            LOGGER.exception("清理 Windows 计划任务失败")
            return 1
        finally:
            services.close()
    if args.run_project:
        try:
            return run_project(services, args.run_project)
        except Exception:
            LOGGER.exception("无界面迁移运行失败")
            return 1
        finally:
            services.close()

    settings = services.settings_store.load()
    # Public settings validation guarantees this is localhost-only.
    address = f"http://127.0.0.1:{settings.port}"
    if not _local_port_available(settings.port):
        message = (
            f"本机端口 {settings.port} 已被占用，迁移作业台无法启动。\n\n"
            "请先关闭另一个 Folder2Feishu 实例或占用该端口的程序，"
            f"再重新启动。\n\n日志：{paths.logs / 'folder2feishu.log'}"
        )
        LOGGER.error(message.replace("\n", " "))
        _show_startup_error(message)
        services.close()
        return 6
    if settings.open_browser and not args.no_browser:
        timer = threading.Timer(1.0, webbrowser.open, args=(address,))
        timer.daemon = True
        timer.start()
    LOGGER.info("控制台已启动：%s", address)
    try:
        uvicorn.run(
            create_app(services),
            host="127.0.0.1",
            port=settings.port,
            log_level="info",
            # A windowed PyInstaller executable has no stderr stream. Uvicorn's
            # default formatter probes stderr.isatty() and would abort startup.
            log_config=None,
            access_log=False,
        )
    except Exception:
        LOGGER.exception("控制台启动失败")
        _show_startup_error(
            f"迁移作业台启动失败。请查看日志后重试：\n\n{paths.logs / 'folder2feishu.log'}"
        )
        return 1
    finally:
        services.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
