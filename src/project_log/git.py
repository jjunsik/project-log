"""Bounded Git plumbing with explicit hook/filter/diff/fetch controls; no shell."""

import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from project_log.policy import MAX_BODY, content_reason, path_info, sensitive_path
from project_log.self_workflow import is_self_repository, pathspecs, workflow_path


class CollectionError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class Git:
    def __init__(self, root: Path):
        self.root = root

    def run(
        self,
        *args: str | bytes,
        input_bytes: bytes = b"",
        timeout: float = 15,
        limit: int = 32 * 1024 * 1024,
        optional: bool = False,
    ) -> bytes:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_NO_LAZY_FETCH": "1",
                "LC_ALL": "C",
            }
        )
        command: list[str | bytes] = [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.pager=cat",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.allow=never",
            "-C",
            str(self.root),
            *args,
        ]
        with tempfile.TemporaryFile() as stdin:
            stdin.write(input_bytes)
            stdin.seek(0)
            try:
                proc = subprocess.Popen(
                    command,
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
            except OSError as exc:
                raise CollectionError("git_unavailable", "Git을 실행할 수 없습니다.") from exc
            assert proc.stdout is not None
            output = bytearray()
            deadline = time.monotonic() + timeout
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise CollectionError(
                                "git_timeout", "Git 조회 제한 시간을 초과했습니다."
                            )
                        data = os.read(proc.stdout.fileno(), 65536)
                        if not data:
                            break
                        output.extend(data)
                        if len(output) > limit:
                            raise CollectionError(
                                "git_output_limit", "Git 조회 결과 용량 제한을 초과했습니다."
                            )
                code = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
                if code != 0:
                    if optional and code == 1:
                        return b""
                    raise CollectionError(
                        "git_read_failed",
                        "Git 자료를 읽지 못했습니다. 경로·권한·object를 확인하세요.",
                    )
                return bytes(output)
            except subprocess.TimeoutExpired as exc:
                raise CollectionError("git_timeout", "Git 조회 제한 시간을 초과했습니다.") from exc
            finally:
                if proc.poll() is None:
                    # Terminate only our direct Git child; process-group signals may be denied
                    # and must not replace the original timeout/output-limit error.
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                proc.wait()
                proc.stdout.close()

    def oid(self, value: bytes) -> str:
        text = value.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text):
            raise CollectionError("invalid_oid", "Git object 식별자가 유효하지 않습니다.")
        return text

    def head(self) -> str | None:
        raw = self.run("rev-parse", "--verify", "--quiet", "HEAD", optional=True)
        return self.oid(raw) if raw else None

    def symbolic_head(self) -> bytes:
        return self.run("symbolic-ref", "--quiet", "HEAD", optional=True).rstrip(b"\n")

    def validate_base_branch(self, name: str | None) -> None:
        if name is None or not name.strip():
            raise CollectionError(
                "base_branch_required",
                "기준 브랜치가 미설정입니다. 설정에서 기준 브랜치를 입력하세요.",
            )
        try:
            # --branch also rejects leading '-', unlike checking a full ref alone.
            checked = self.run("check-ref-format", "--branch", name).rstrip(b"\n")
            if checked != name.encode("utf-8"):
                # Do not accept Git's @{-n} expansion as the literal branch setting.
                raise CollectionError("git_read_failed", "")
        except (UnicodeError, ValueError) as exc:
            raise CollectionError(
                "invalid_base_branch", "Git에서 유효한 기준 브랜치 이름을 입력하세요."
            ) from exc
        except CollectionError as exc:
            if exc.code != "git_read_failed":
                raise
            raise CollectionError(
                "invalid_base_branch", "Git에서 유효한 기준 브랜치 이름을 입력하세요."
            ) from exc
        ref = "refs/heads/" + name
        tip = self.run("rev-parse", "--verify", "--quiet", ref + "^{commit}", optional=True)
        if tip:
            return
        # An unborn HEAD is allowed only when the repository has no reachable commits.
        if self.symbolic_head() == ref.encode() and not self.run(
            "rev-list", "--all", "--max-count=1"
        ):
            return
        raise CollectionError(
            "base_branch_not_found",
            f'local 기준 브랜치 "{name}"가 없습니다. 존재하는 local 브랜치 이름으로 수정하세요.',
        )

    def collection_branch(self, name: str | None) -> dict[str, Any]:
        self.validate_base_branch(name)
        assert name is not None
        ref = ("refs/heads/" + name).encode()
        branch = self.symbolic_head()
        head = self.head()
        current = self.symbolic_head()
        if branch != current:
            raise CollectionError(
                "repository_changed",
                "기준 브랜치 검사 중 checkout이 바뀌었습니다. "
                "기준 브랜치가 checkout되어 있는지 확인한 뒤 다시 수집하세요.",
            )
        if current != ref:
            label = (
                current.removeprefix(b"refs/heads/").decode("utf-8", "replace")
                if current.startswith(b"refs/heads/")
                else "분리된 HEAD"
            )
            raise CollectionError(
                "checkout_mismatch",
                f'기준 브랜치: "{name}" · 현재 checkout: "{label}". '
                "기준 브랜치로 checkout한 뒤 다시 수집하세요. "
                "Project Log는 branch를 변경하지 않습니다.",
            )
        return {"branch": name, "branch_ref_b64": path_info(ref)["path_b64"], "head": head}

    def suggested_base_branch(self) -> str | None:
        for name in ("main", "master"):
            if self.run(
                "rev-parse", "--verify", "--quiet", f"refs/heads/{name}^{{commit}}", optional=True
            ):
                return name
        branch = self.symbolic_head()
        if branch.startswith(b"refs/heads/") and not self.run("rev-list", "--all", "--max-count=1"):
            try:
                name = branch.removeprefix(b"refs/heads/").decode("utf-8")
                self.validate_base_branch(name)
                return name
            except (UnicodeError, CollectionError):
                pass
        return None

    def refs(self) -> list[dict[str, str | None]]:
        result = []
        for line in self.run(
            "for-each-ref", "--format=%(refname)%00%(objectname)%00%(objecttype)"
        ).splitlines():
            name, oid, kind = line.split(b"\0")
            value = self.oid(oid)
            tip = self.run("rev-parse", "--verify", "--quiet", f"{value}^{{commit}}", optional=True)
            result.append(
                {
                    "name": name.decode("utf-8", errors="replace"),
                    "name_b64": path_info(name)["path_b64"],
                    "oid": value,
                    "kind": kind.decode(),
                    "commit": self.oid(tip) if tip else None,
                }
            )
        return result

    def status(self) -> bytes:
        scope_args: list[str | bytes] = (
            ["--", *pathspecs(self.root)] if is_self_repository(self.root) else []
        )
        # status can execute clean/process filters while comparing equal-size working files.
        # Read effective config names (including local includes), then override those drivers.
        keys = self.run(
            "config",
            "--null",
            "--name-only",
            "--get-regexp",
            r"^filter\..*\.(clean|smudge|process|required)$",
            optional=True,
        )
        overrides: list[str | bytes] = []
        for key in sorted(set(keys.split(b"\0")) - {b""}):
            overrides.extend(["-c", key + (b"=false" if key.endswith(b".required") else b"=")])
        raw = self.run(
            *overrides,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=all",
            *scope_args,
        )
        if not scope_args:
            return raw
        # Git may emit collapsed ignored directory markers despite an exclude pathspec.
        tokens = iter(raw.split(b"\0"))
        retained = []
        for token in tokens:
            if not token:
                continue
            previous = next(tokens) if b"R" in token[:2] or b"C" in token[:2] else None
            if workflow_path(token[3:]):
                continue
            if previous is not None and workflow_path(previous):
                raise CollectionError(
                    "workflow_boundary_unavailable",
                    "개인 workflow 경계를 기존 상태 표현으로 분리하지 못했습니다.",
                )
            retained.append(token)
            if previous is not None:
                retained.append(previous)
        return b"\0".join(retained) + (b"\0" if retained else b"")

    def index(self) -> bytes:
        scope_args: list[str | bytes] = (
            ["--", *pathspecs(self.root)] if is_self_repository(self.root) else []
        )
        return self.run(
            "ls-files",
            "--stage",
            "-z",
            *scope_args,
        )

    def blob(
        self, oid: str, path: str, mode: str = "100644"
    ) -> tuple[bytes | None, str | None, int | None]:
        if mode == "160000":
            return None, "submodule", None
        if mode == "120000":
            return None, "symlink", None
        size = int(self.object_read("cat-file", "-s", oid))
        if sensitive_path(path):
            return None, "sensitive_path", size
        if size > MAX_BODY:
            return None, "large", size
        raw = self.object_read("cat-file", "blob", oid, limit=MAX_BODY)
        reason = content_reason(raw)
        return (None if reason else raw), reason, size

    def object_read(self, *args: str | bytes, limit: int = 32 * 1024 * 1024) -> bytes:
        try:
            return self.run(*args, limit=limit)
        except CollectionError as exc:
            if exc.code != "git_read_failed":
                raise
            raise CollectionError(
                "git_object_unavailable",
                "Git object를 읽을 수 없습니다. 원본 object의 존재 여부와 접근 권한을 확인하세요.",
            ) from exc

    def tree(self, commit: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        paths: list[bytes] = []
        if is_self_repository(self.root):
            # Enumerate only the Git tree root; never descend into the excluded subtree.
            for record in self.object_read("ls-tree", "-z", commit).split(b"\0"):
                if record:
                    entry, path = record.split(b"\t", 1)
                    if not workflow_path(path) or (
                        path == b"current_status.md" and entry.split()[1] == b"tree"
                    ):
                        paths.append(path)
            if not paths:
                return result
        scope_args: list[str | bytes] = ["--", *paths] if paths else []
        for record in self.object_read("ls-tree", "-r", "-l", "-z", commit, *scope_args).split(
            b"\0"
        ):
            if not record:
                continue
            info, path = record.split(b"\t", 1)
            mode, kind, oid, size = info.split()
            result.append(
                {
                    **path_info(path),
                    "mode": mode.decode(),
                    "type": kind.decode(),
                    "oid": self.oid(oid),
                    "size": None if size in {b"-", b"BAD"} else int(size),
                    **({"size_reason": "git_object_unavailable"} if size == b"BAD" else {}),
                }
            )
        return result


def diagnose(value: str) -> tuple[Git, dict[str, Any]]:
    try:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            raise OSError()
    except (OSError, RuntimeError, ValueError) as exc:
        raise CollectionError(
            "invalid_path", "존재하고 접근 가능한 프로젝트 폴더를 입력하세요."
        ) from exc
    git = Git(path)
    root = Path(os.fsdecode(git.run("rev-parse", "--show-toplevel").rstrip(b"\n"))).resolve()
    if root != path:
        raise CollectionError("not_root", "Git Repository의 최상위 폴더를 입력하세요.")
    common = git.run("rev-parse", "--path-format=absolute", "--git-common-dir").rstrip(b"\n")
    branch = git.symbolic_head()
    return git, {
        "path": str(root),
        "repository_key": str(Path(os.fsdecode(common)).resolve()),
        "suggested_name": root.name,
        "head": git.head(),
        "branch": branch.removeprefix(b"refs/heads/").decode("utf-8", errors="replace") or None,
        "branch_ref_b64": path_info(branch)["path_b64"] if branch else None,
        "object_format": git.run("rev-parse", "--show-object-format").decode().strip(),
        "shallow": git.run("rev-parse", "--is-shallow-repository").strip() == b"true",
    }


def status_entries(raw: bytes) -> list[dict[str, Any]]:
    tokens = iter(raw.split(b"\0"))
    records = []
    for token in tokens:
        if not token:
            continue
        status_code = token[:2].decode("ascii")
        row = {**path_info(token[3:]), "status": status_code}
        if "R" in status_code or "C" in status_code:
            row["previous"] = path_info(next(tokens))
        records.append(row)
    return records


def index_entries(raw: bytes) -> dict[bytes, list[dict[str, Any]]]:
    result: dict[bytes, list[dict[str, Any]]] = {}
    for record in raw.split(b"\0"):
        if record:
            info, path = record.split(b"\t", 1)
            mode, oid, stage = info.split()
            result.setdefault(path, []).append(
                {"mode": mode.decode(), "oid": oid.decode(), "stage": int(stage)}
            )
    return result


def read_working(root: Path, raw_path: bytes) -> tuple[bytes | None, str | None, dict[str, Any]]:
    """Open each component without following symlinks (including parent-directory races)."""
    parts = raw_path.split(b"/")
    if any(p in {b"", b".", b".."} for p in parts):
        return None, "unsafe_path", {}
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        meta = {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "mode": info.st_mode}
        if not stat.S_ISREG(info.st_mode):
            return None, "symlink" if stat.S_ISLNK(info.st_mode) else "non_regular", meta
        if sensitive_path(os.fsdecode(raw_path)):
            return None, "sensitive_path", meta
        if info.st_size > MAX_BODY:
            return None, "large", meta
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None, "non_regular", meta
            raw = stream.read(MAX_BODY + 1)
            after = os.fstat(stream.fileno())
        if (info.st_ino, info.st_mtime_ns, info.st_size) != (
            before.st_ino,
            before.st_mtime_ns,
            before.st_size,
        ) or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None, "changed_during_capture", meta
        reason = content_reason(raw)
        return (None if reason else raw), reason, meta
    except FileNotFoundError:
        return None, "missing", {}
    except OSError:
        return None, "unreadable_or_symlink_parent", {}
    finally:
        os.close(fd)
