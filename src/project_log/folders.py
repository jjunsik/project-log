"""Home-path authorization shared by native folder selection and Git validation."""

import os
import pwd
import threading
from pathlib import Path

from project_log.db import Database
from project_log.git import CollectionError


class Folders:
    def __init__(self, db: Database, home: Path | None = None):
        self.home = (home or Path(pwd.getpwuid(os.geteuid()).pw_dir)).resolve(strict=True)
        self.handles: dict[str, tuple[Path, int]] = {}
        self.lock = threading.RLock()

    def close(self) -> None:
        with self.lock:
            for _, fd in self.handles.values():
                os.close(fd)
            self.handles.clear()

    def _activate(self, key: str, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if key in self.handles:
            os.close(self.handles[key][1])
        self.handles[key] = path, fd

    def _open(self, key: str, relative: str) -> tuple[Path, int]:
        if "home" not in self.handles:
            self._activate("home", self.home)
        if key not in self.handles:
            raise CollectionError(
                "folder_denied",
                "이 위치를 명시적으로 허용하세요. 재시작 후에는 다시 확인해야 합니다.",
            )
        root, anchor = self.handles[key]
        if relative.startswith("/") or any(p in {"..", "."} for p in relative.split("/")):
            raise CollectionError("folder_denied", "허용 범위를 벗어난 경로입니다.")
        try:
            current, original = os.stat(root, follow_symlinks=False), os.fstat(anchor)
            if root.resolve(strict=True) != root:
                raise OSError()
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                raise OSError()
            fd = os.dup(anchor)
            try:
                for part in filter(None, relative.split("/")):
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
                    if os.fstat(fd).st_dev != original.st_dev:
                        raise OSError()
                return root / relative, fd
            except Exception:
                os.close(fd)
                raise
        except (OSError, ValueError) as exc:
            raise CollectionError(
                "folder_denied",
                "경로가 교체됐거나 접근할 수 없습니다. Symlink·mount는 별도로 허용하세요.",
            ) from exc

    def authorize(self, value: str) -> None:
        path = Path(value)
        if not path.is_absolute():
            raise CollectionError("folder_denied", "탐색기에서 실제 경로를 선택하세요.")
        with self.lock:
            if "home" not in self.handles:
                self._activate("home", self.home)
            for key, (root, _) in sorted(
                self.handles.items(), key=lambda item: len(str(item[1][0])), reverse=True
            ):
                if path.is_relative_to(root):
                    _, fd = self._open(key, str(path.relative_to(root)) if path != root else "")
                    os.close(fd)
                    return
        raise CollectionError("folder_denied", "홈 밖의 위치는 먼저 명시적으로 허용하세요.")
