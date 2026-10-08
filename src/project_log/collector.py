import base64
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from project_log.db import CollectionStopped, Database, Row, put_content
from project_log.documents import scan_documents
from project_log.git import (
    CollectionError,
    Git,
    diagnose,
    index_entries,
    read_working,
    status_entries,
)
from project_log.policy import (
    DOCUMENT_ROOT,
    MAX_BODY,
    SNAPSHOT_BYTES,
    SNAPSHOT_SECONDS,
    content_reason,
    kind_hint,
    path_info,
    sha256,
)
from project_log.self_workflow import pathspecs


def now() -> str:
    return datetime.now(UTC).isoformat()


def capture(
    db: Database, collection: str, git: Git, info: Row, *, expected_branch: str | None = None
) -> None:
    start = time.monotonic()
    snapshot: Row = {**info, "started_at": now(), "complete": False}

    # Preserve partial observation even if a later read fails.
    def save() -> None:
        db.write_collection(
            collection,
            "UPDATE collections SET snapshot=%s WHERE id=%s",
            (Jsonb(snapshot), collection),
        )

    pending: list[tuple[str, str, Jsonb, bytes | None, str | None, str | None, str]] = []
    project = str(
        db.one("SELECT project_id FROM collections WHERE id=%s", (collection,))["project_id"]
    )
    save()
    try:
        refs = git.refs()
        head = git.head()
        branch = git.symbolic_head()
        if expected_branch is not None:
            expected_ref = ("refs/heads/" + expected_branch).encode()
            if branch != expected_ref:
                raise CollectionError(
                    "checkout_changed_before_capture",
                    "수집 시작 직전 checkout이 기준 브랜치에서 바뀌었습니다. "
                    "기준 브랜치로 checkout한 뒤 지금 수집을 다시 시도하세요.",
                )
            tip = next(
                (r["commit"] for r in refs if r["name_b64"] == path_info(expected_ref)["path_b64"]),
                None,
            )
            if tip != head:
                raise CollectionError(
                    "repository_changed",
                    "수집 시작 직전 branch tip/HEAD가 바뀌었습니다. 다시 수집하세요.",
                )
        snapshot["refs"] = refs
        snapshot["head"] = head
        snapshot["branch"] = branch.removeprefix(b"refs/heads/").decode("utf-8", "replace") or None
        snapshot["branch_ref_b64"] = path_info(branch)["path_b64"] if branch else None
        before = git.status()
        index_raw = git.index()
        index = index_entries(index_raw)
        snapshot["status_sha256"] = sha256(before)
        snapshot["index_listing_sha256"] = sha256(index_raw)
        entries = status_entries(before)
        snapshot["status_count"] = len(entries)
        present = {base64.b64decode(entry["path_b64"]) for entry in entries}
        entries.extend({**path_info(path), "status": "  "} for path in index if path not in present)
        submodules = {
            path for path, items in index.items() if any(item["mode"] == "160000" for item in items)
        }
        documents, document_signature, document_issues = scan_documents(git.root, submodules, start)
        snapshot["document_roots"] = [path_info(DOCUMENT_ROOT)]
        snapshot["documents_listing_sha256"] = document_signature
        for issue in document_issues:
            db.issue(
                collection,
                "snapshot",
                issue["document_reason"],
                "문서 관측을 완료하지 못했습니다.",
                issue,
            )
        merged: list[Row] = []
        for entry in entries:
            path = base64.b64decode(entry["path_b64"])
            if path.rstrip(b"/") == DOCUMENT_ROOT or path.startswith(DOCUMENT_ROOT + b"/"):
                if entry["status"] == "!!":
                    continue
                entry["document_root"] = path_info(DOCUMENT_ROOT)
            merged.append(entry)
        observed_paths = {base64.b64decode(entry["path_b64"]) for entry in merged}
        for entry in documents:
            path = base64.b64decode(entry["path_b64"])
            if path not in observed_paths:
                # Git only reported a collapsed directory, not this path's individual status.
                merged.append({**entry, "status": None})
                observed_paths.add(path)
        entries = merged
        snapshot["working_scope"] = "all_index_and_tracked_working_paths"
        save()
        if len(entries) > 20000:
            raise CollectionError(
                "snapshot_entry_limit",
                "상태 항목이 20,000개를 초과해 초기 snapshot을 확보하지 못했습니다.",
            )
        used = 0
        meta: Row
        for entry in entries:
            path = base64.b64decode(entry["path_b64"])
            status = entry["status"]
            observed = now()
            if status == "!!" and "document_root" not in entry:
                _working(
                    pending, collection, "ignored", entry, None, "ignored_metadata_only", observed
                )
                continue
            for index_entry in index.get(path, []):
                budget = time.monotonic() - start >= SNAPSHOT_SECONDS or used >= SNAPSHOT_BYTES
                try:
                    data, reason, size = (
                        (None, "snapshot_budget", None)
                        if budget
                        else git.blob(index_entry["oid"], entry["path"], index_entry["mode"])
                    )
                except CollectionError as exc:
                    data, reason, size = None, exc.code, None
                    db.issue(
                        collection, "snapshot", exc.code, exc.message, {**entry, **index_entry}
                    )
                if data is not None and used + len(data) > SNAPSHOT_BYTES:
                    data, reason = None, "snapshot_budget"
                used += len(data or b"")
                if reason == "snapshot_budget":
                    db.issue(
                        collection,
                        "snapshot",
                        reason,
                        "일부 staged 본문을 확보하지 못했습니다.",
                        entry,
                    )
                _working(
                    pending,
                    collection,
                    "index",
                    {**entry, **index_entry, "size": size},
                    data,
                    reason,
                    observed,
                )
            budget = time.monotonic() - start >= SNAPSHOT_SECONDS or used >= SNAPSHOT_BYTES
            document_reason = entry.pop("document_reason", None)
            if document_reason:
                data, reason, meta = None, document_reason, {}
            elif budget:
                data, reason, meta = None, "snapshot_budget", {}
            elif any(item["mode"] == "160000" for item in index.get(path, [])):
                data, reason, meta = None, "submodule", {}
            else:
                data, reason, meta = read_working(git.root, path)
            if data is not None and used + len(data) > SNAPSHOT_BYTES:
                data, reason = None, "snapshot_budget"
            used += len(data or b"")
            expected_missing = status is not None and (status[1] == "D" or status == "D ")
            if reason == "missing" and expected_missing:
                reason = "deleted"
            layer = (
                "document"
                if "document_root" in entry and status in {"??", None} and path not in index
                else "untracked"
                if status == "??"
                else "working"
            )
            _working(pending, collection, layer, {**entry, **meta}, data, reason, observed)
            if reason in {
                "changed_during_capture",
                "unreadable_or_symlink_parent",
                "snapshot_budget",
            } or (reason == "missing" and not expected_missing):
                db.issue(
                    collection,
                    "snapshot",
                    reason or "missing",
                    "일부 미커밋 본문을 확보하지 못했습니다.",
                    entry,
                )
        snapshot["body_bytes"] = used
        snapshot["complete"] = not db.all(
            "SELECT id FROM collection_issues WHERE collection_id=%s AND phase='snapshot'",
            (collection,),
        )
        if (
            before != git.status()
            or index_raw != git.index()
            or snapshot["head"] != git.head()
            or snapshot["refs"] != git.refs()
            or branch != git.symbolic_head()
        ):
            snapshot["complete"] = False
            db.issue(
                collection,
                "snapshot",
                "repository_changed",
                "관측 중 Repository 상태가 바뀌었습니다. 단일 시점 snapshot이 아닙니다.",
            )
        _, after_document_signature, after_document_issues = scan_documents(
            git.root, submodules, start
        )
        if document_signature != after_document_signature or after_document_issues:
            snapshot["complete"] = False
            db.issue(
                collection,
                "snapshot",
                "documents_changed_or_incomplete",
                "문서 관측 중 변경이 감지됐거나 재관측을 완료하지 못했습니다.",
            )
    except CollectionError as exc:
        db.issue(collection, "snapshot", exc.code, exc.message)
    finally:
        snapshot["finished_at"] = now()
        # Batch Metadata writes; a large untracked set must not open one DB connection per path.
        if pending:
            with db.writer(collection) as conn:
                for cid, layer, metadata, data, digest, reason, observed in pending:
                    assert data is None or isinstance(data, bytes)
                    content = put_content(conn, project, data)
                    conn.execute(
                        """INSERT INTO working_entries
                        (collection_id,project_id,layer,metadata,content_id,sha256,body_reason,
                        observed_at,body_observed_at,provenance)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            cid,
                            project,
                            layer,
                            metadata,
                            content,
                            digest,
                            reason,
                            observed,
                            observed if data is not None else None,
                            Jsonb({"kind": "collection_capture"}),
                        ),
                    )
        save()
    db.summarize(collection)
    db.write_collection(
        collection, "UPDATE collections SET state='queued' WHERE id=%s", (collection,)
    )


def _working(
    pending: list[tuple[str, str, Jsonb, bytes | None, str | None, str | None, str]],
    collection: str,
    layer: str,
    metadata: Row,
    data: bytes | None,
    reason: str | None,
    observed: str,
) -> None:
    pending.append(
        (
            collection,
            layer,
            Jsonb(metadata),
            data,
            sha256(data) if data is not None else None,
            reason,
            observed,
        )
    )


def commit_record(git: Git, oid: str) -> tuple[Row, bytes | None, str | None]:
    raw = git.object_read("cat-file", "commit", oid, limit=MAX_BODY * 2)
    header, _, message = raw.partition(b"\n\n")
    metadata: Row = {"parents": []}
    for line in header.splitlines():
        key, _, value = line.partition(b" ")
        if key == b"tree":
            metadata["tree"] = git.oid(value)
        elif key == b"parent":
            metadata["parents"].append(git.oid(value))
        elif key in {b"author", b"committer"}:
            # Preserve original zone and raw date, do not infer identity/contribution.
            match = re.fullmatch(rb"(.*) <(.*)> (-?\d+) ([+-]\d{4})", value)
            if match and not content_reason(value):
                name, email, timestamp, zone = match.groups()
                metadata[key.decode()] = {
                    "name": name.decode(),
                    "email": email.decode(),
                    "timestamp": int(timestamp),
                    "timezone": zone.decode(),
                }
            else:
                metadata[key.decode()] = {"unavailable": "unparseable_or_restricted"}
        elif key == b"encoding":
            metadata["encoding"] = value.decode("ascii", errors="replace")
    reason = content_reason(raw)
    metadata["message"] = message.decode() if not content_reason(message) else None
    metadata["message_reason"] = content_reason(message)
    return metadata, None if reason else raw, reason


def changes(git: Git, oid: str, parent: str | None) -> list[Row]:
    args: list[str | bytes] = [
        "diff-tree",
        "--no-commit-id",
        "--raw",
        "-r",
        "-z",
        "--no-abbrev",
        "-M",
        "--no-ext-diff",
        "--no-textconv",
    ]
    args.extend([parent, oid] if parent else ["--root", oid])
    exclusions = pathspecs(git.root)
    if exclusions:
        args.extend(["--", *exclusions])
    tokens = iter(git.run(*args).split(b"\0"))
    result: list[Row] = []
    for token in tokens:
        if not token:
            continue
        old_mode, new_mode, old_oid, new_oid, status = token.lstrip(b":").split()
        old_path = next(tokens)
        new_path = next(tokens) if status[:1] in {b"R", b"C"} else old_path
        result.append(
            {
                "old": path_info(old_path),
                "new": path_info(new_path),
                "old_mode": old_mode.decode(),
                "new_mode": new_mode.decode(),
                "old_oid": None if set(old_oid) == {48} else git.oid(old_oid),
                "new_oid": None if set(new_oid) == {48} else git.oid(new_oid),
                "status": status.decode(),
                "kind_hint": kind_hint(new_path.decode("utf-8", "replace")),
            }
        )
    return result


def patch(git: Git, oid: str, parent: str | None, change: Row) -> tuple[bytes | None, str | None]:
    for side in ("old", "new"):
        blob = change[f"{side}_oid"]
        if blob:
            _, reason, size = git.blob(blob, change[side]["path"], change[f"{side}_mode"])
            change[f"{side}_size"] = size
            if reason:
                return None, reason
    path_args = [
        b":(literal)" + base64.b64decode(change[side]["path_b64"]) for side in ("old", "new")
    ]
    # A deleted/renamed file can become a directory. A literal Git pathspec still recurses;
    # exclude descendants so this patch contains only the exact, screened file paths.
    path_args.extend(
        b":(exclude,literal)" + base64.b64decode(change[side]["path_b64"]) + b"/"
        for side in ("old", "new")
    )
    args: list[str | bytes] = [
        "diff-tree",
        "--no-commit-id",
        "--patch",
        "--full-index",
        "-r",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-renames",
    ]
    args.extend([parent, oid] if parent else ["--root", oid])
    data = git.run(*args, "--", *path_args, limit=4 * MAX_BODY)
    # Full old/new blobs were screened first; do not persist partial/redacted diffs.
    reason = content_reason(data) if len(data) <= MAX_BODY else "large_diff"
    return None if reason else data, reason


def collect(db: Database, collection: str, stopped: Callable[[], bool]) -> None:
    row = db.one(
        """SELECT c.*, p.path, p.repository_key FROM collections c
        JOIN projects p ON p.id=c.project_id WHERE c.id=%s""",
        (collection,),
    )
    git, current = diagnose(row["path"])
    if current["repository_key"] != row["repository_key"]:
        raise CollectionError("repository_replaced", "등록한 Repository의 Git 경로가 달라졌습니다.")
    snapshot = row["snapshot"]
    if not snapshot.get("complete") and not db.all(
        "SELECT id FROM collection_issues WHERE collection_id=%s AND phase='snapshot'",
        (collection,),
    ):
        db.issue(
            collection,
            "snapshot",
            "snapshot_incomplete",
            "등록 시점 snapshot이 끝까지 저장되지 않았습니다. "
            "재시도로 당시 미커밋 자료를 복원할 수 없습니다.",
        )
    if "refs" not in snapshot:
        raise CollectionError(
            "missing_refs", "등록 당시 Git refs를 확보하지 못해 History 범위를 확정할 수 없습니다."
        )
    tips = {ref["commit"] for ref in snapshot["refs"] if ref["commit"]}
    if snapshot.get("head"):
        tips.add(snapshot["head"])
    if snapshot.get("shallow"):
        db.issue(
            collection,
            "history",
            "shallow_history",
            "Shallow Repository이므로 누락된 과거 History가 있습니다.",
        )
    if current["shallow"] != snapshot.get("shallow"):
        db.issue(
            collection,
            "history",
            "history_boundary_changed",
            "등록 이후 shallow 상태가 바뀌었습니다.",
        )
    history = (
        git.run(
            "rev-list",
            "--topo-order",
            "--reverse",
            "--stdin",
            input_bytes="\n".join(sorted(tips)).encode(),
            timeout=60,
        )
        if tips
        else b""
    )
    history_oids = [git.oid(value) for value in history.splitlines()]
    history_set = set(history_oids)
    for oid in history_oids:
        if stopped():
            raise CollectionError("interrupted", "수집이 중단됐습니다.")
        try:
            metadata, raw, reason = commit_record(git, oid)
            db.observe_commit(collection, str(row["project_id"]), oid, metadata, raw, reason)
            missing_parents = [p for p in metadata["parents"] if p not in history_set]
            if missing_parents:
                db.issue(
                    collection,
                    "history",
                    "history_gap",
                    "기록된 부모 Commit 중 이번 History에서 확인할 수 없는 항목이 있습니다.",
                    {"commit": oid, "parents": missing_parents},
                )
            for parent in metadata["parents"] or [None]:
                try:
                    for change in changes(git, oid, parent):
                        try:
                            diff, excluded = patch(git, oid, parent, change)
                        except CollectionError as exc:
                            diff, excluded = None, exc.code
                            db.issue(
                                collection,
                                "diff",
                                exc.code,
                                exc.message,
                                {"commit": oid, "parent": parent, "path": change["new"]},
                            )
                        db.observe_change(
                            collection, str(row["project_id"]), oid, parent, change, diff, excluded
                        )
                except CollectionError as exc:
                    db.issue(
                        collection,
                        "history",
                        exc.code,
                        exc.message,
                        {"commit": oid, "parent": parent},
                    )
            db.summarize(collection)
        except CollectionError as exc:
            db.issue(collection, "commit", exc.code, exc.message, {"commit": oid})
    if snapshot.get("head"):
        for file in git.tree(snapshot["head"]):
            if stopped():
                raise CollectionStopped()
            file["kind_hint"] = kind_hint(file["path"])
            file["commit"] = snapshot["head"]
            db.observe_head(collection, str(row["project_id"]), file)
            if file.get("size_reason") == "git_object_unavailable":
                db.issue(
                    collection,
                    "head",
                    "git_object_unavailable",
                    "HEAD blob 일부를 읽지 못했습니다. 경로·object locator는 보존했습니다.",
                    file,
                )
    db.summarize(collection)
    errors = db.one(
        "SELECT count(*) AS n FROM collection_issues WHERE collection_id=%s", (collection,)
    )["n"]
    db.write_collection(
        collection,
        "UPDATE collections SET state=%s,finished_at=now() WHERE id=%s",
        ("cleanup_pending" if errors else "completed", collection),
    )
