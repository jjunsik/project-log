"""Only the fixed macOS folder chooser can execute; no request supplies commands."""

import secrets
import subprocess
import sys
import threading
from pathlib import Path

from project_log.db import Row
from project_log.folders import Folders
from project_log.git import CollectionError

SCRIPT = (
    "return POSIX path of (choose folder with prompt "
    '"Project Log: Git Repository의 최상위 폴더를 선택하세요.")'
)


class NativePicker:
    def __init__(self, folders: Folders):
        self.folders = folders
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.selected: dict[str, tuple[int, int]] = {}

    def choose(self) -> Row:
        if sys.platform != "darwin":
            raise CollectionError("picker_unavailable", "macOS 네이티브 폴더 선택이 필요합니다.")
        if not self.lock.acquire(blocking=False):
            raise CollectionError("conflict", "이미 폴더 선택창이 열려 있습니다.")
        try:
            try:
                result = subprocess.run(
                    ["/usr/bin/osascript", "-e", SCRIPT],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=180,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CollectionError(
                    "picker_unavailable", "폴더 선택창을 열 수 없거나 선택 시간이 초과됐습니다."
                ) from exc
            if result.returncode:
                if b"(-128)" in result.stderr:
                    return {"cancelled": True, "path": None}
                raise CollectionError(
                    "picker_unavailable", "macOS 폴더 선택 권한과 실행 상태를 확인하세요."
                )
            try:
                value = result.stdout.decode("utf-8").removesuffix("\n")
                path = Path(value)
                if not path.is_absolute():
                    raise ValueError()
                path = path.resolve(strict=True)
                if not path.is_dir() or len(str(path)) > 4096:
                    raise ValueError()
                info = path.stat()
                self.selected[str(path)] = info.st_dev, info.st_ino
            except (ValueError, UnicodeError, OSError) as exc:
                raise CollectionError("invalid_path", "선택한 폴더에 접근할 수 없습니다.") from exc
            return {"cancelled": False, "path": str(path)}
        finally:
            self.lock.release()

    def authorize(self, value: str) -> None:
        if value in self.selected:
            try:
                path = Path(value)
                info = path.stat()
                if path.resolve(strict=True) == path and path.is_dir():
                    if (info.st_dev, info.st_ino) == self.selected[value]:
                        return
            except OSError:
                pass
            raise CollectionError(
                "folder_denied", "선택한 경로가 교체됐습니다. 다시 찾아보기를 실행하세요."
            )
        self.folders.authorize(value)
