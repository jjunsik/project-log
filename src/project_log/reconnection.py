"""Read-only Git continuity checks; observations and target repositories are never repaired."""

import hashlib
import json

import psycopg

from project_log.db import Row
from project_log.git import CollectionError, Git


def review(conn: psycopg.Connection[Row], project: Row, git: Git, info: Row, branch: str) -> Row:
    if conn.execute(
        "SELECT 1 FROM projects WHERE id<>%s AND (path=%s OR repository_key=%s)",
        (project["id"], info["path"], info["repository_key"]),
    ).fetchone():
        raise CollectionError("duplicate", "이미 등록된 Repository입니다.")
    latest = conn.execute(
        "SELECT snapshot FROM collections WHERE project_id=%s AND state='completed' "
        "ORDER BY created_at DESC,id DESC LIMIT 1",
        (project["id"],),
    ).fetchone()
    evidence = latest["snapshot"] if latest else project["repository_info"]
    anchor = evidence.get("head")
    if not anchor or evidence.get("object_format") != info["object_format"]:
        raise CollectionError(
            "continuity_unavailable",
            "기준 Commit 또는 Git object format 근거가 없어 이력 연속성을 확인할 수 없습니다.",
        )
    try:
        git.object_read("cat-file", "commit", anchor)
        stored = conn.execute(
            "SELECT metadata FROM git_commits WHERE project_id=%s AND oid=%s",
            (project["id"], anchor),
        ).fetchone()
        if stored:
            from project_log.collector import commit_record

            meta, _, _ = commit_record(git, anchor)
            if any(meta.get(k) != stored["metadata"].get(k) for k in ("tree", "parents")):
                raise CollectionError(
                    "continuity_unavailable", "기준 Commit의 관측 정보가 일치하지 않습니다."
                )
        refs = git.refs()
        tips = [
            r["commit"] for r in refs if (r["name"] or "").startswith("refs/heads/") and r["commit"]
        ]
        if not any(
            git.run(
                "merge-base",
                "--is-ancestor",
                anchor,
                tip,
                optional=True,
                optional_false=b"not_ancestor",
            )
            == b""
            for tip in tips
        ):
            raise CollectionError(
                "continuity_unavailable",
                "최신 관측 Commit이 현재 local 브랜치의 이력에 연결되지 않습니다.",
            )
        if not git.run("merge-base", anchor, f"refs/heads/{branch}", optional=True):
            raise CollectionError(
                "continuity_unavailable",
                "기준 브랜치와 기존 관측 이력의 연결을 확인할 수 없습니다.",
            )
        # Do not use --lost-found: it writes into the target. No network or repair.
        local = git.run("config", "--name-only", "--get-regexp", r"^fsck\.", optional=True)
        overrides = {
            key: "error" for key in local.decode().splitlines() if key.lower() != "fsck.skiplist"
        }
        overrides["fsck.skipList"] = "/dev/null"
        git.run("fsck", "--full", "--no-reflogs", "--no-dangling", timeout=60, config=overrides)
        # fsck can accept promised-but-absent objects in a partial clone. Current
        # reachable objects must be locally readable without fetching them.
        git.run(
            "rev-list",
            "--objects",
            "--all",
            "--missing=error",
            *(("HEAD",) if info.get("head") else ()),
            timeout=60,
        )
        git.status()
        git.index()
    except CollectionError as exc:
        if exc.code == "continuity_unavailable":
            raise
        raise CollectionError(
            "repository_invalid",
            "Git 관리 정보·현재 객체 또는 연속성 기준 객체를 읽을 수 없습니다. "
            "자동 복구하지 않습니다.",
        ) from exc
    objects: set[str] = set()
    for row in conn.execute(
        "SELECT oid,metadata FROM git_commits WHERE project_id=%s", (project["id"],)
    ):
        objects.add(row["oid"])
        if row["metadata"].get("tree"):
            objects.add(row["metadata"]["tree"])
    for row in conn.execute("SELECT metadata FROM git_files WHERE project_id=%s", (project["id"],)):
        if row["metadata"].get("oid") and row["metadata"].get("mode") != "160000":
            objects.add(row["metadata"]["oid"])
    for row in conn.execute(
        "SELECT metadata FROM git_changes WHERE project_id=%s", (project["id"],)
    ):
        for side in ("old", "new"):
            oid = row["metadata"].get(side + "_oid")
            if oid and set(oid) != {"0"} and row["metadata"].get(side + "_mode") != "160000":
                objects.add(oid)
    missing: list[str] = []
    if objects:
        lines = (
            git.run(
                "cat-file",
                "--batch-check=%(objectname) %(objecttype)",
                input_bytes=("\n".join(sorted(objects)) + "\n").encode(),
                timeout=60,
            )
            .decode()
            .splitlines()
        )
        missing = sorted(line.split()[0] for line in lines if line.endswith(" missing"))
    token = (
        hashlib.sha256(
            json.dumps(
                {
                    "project": str(project["id"]),
                    "updated_at": str(project["updated_at"]),
                    "path": info["path"],
                    "anchor": anchor,
                    "branch": branch,
                    "refs": refs,
                    "missing": missing,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if missing
        else None
    )
    return {
        "path": info["path"],
        "anchor": anchor,
        "refs": refs,
        "missing_count": len(missing),
        "missing_objects": missing[:20],
        "warning_token": token,
        "message": "기존 프로젝트와 Git 이력의 연속성이 확인됐습니다."
        + (
            " 일부 과거 Git 객체가 없어 원본 조회가 제한될 수 있습니다. "
            "이미 보존된 수집 기록과 자료는 유지됩니다."
            if missing
            else ""
        ),
    }
