"""
Per-project guard for the /state blob.

AppState.data is ONE JSON blob per workspace that holds every project. This module
makes per-project permissions apply inside that blob without restructuring the data
(the lightweight approach):

  filter_state_for_member()  -> a copy of the blob containing ONLY the projects the
                                 member may view (used by GET /state, the POST /state
                                 response, and report export).
  merge_member_payload()     -> merges a payload submitted by a member with the existing
                                 server-side blob, so that:
                                   * projects the member cannot see are NOT deleted when
                                     the member saves the filtered blob back,
                                   * edits to 'viewer'-level projects are discarded,
                                   * members cannot delete projects through /state
                                     (the legacy behavior is preserved),
                                   * projects newly created by the member are recognized.

The first half of this file is pure functions (no database) so they are easy to test.
The second half is bookkeeping for the project_members table (default grants, cleanup).

DEFAULT RULES (configurable via the constants below):
  * A new project is automatically shared with every regular member at
    DEFAULT_NEW_PROJECT_LEVEL ('editor'), matching the behavior from before
    per-project permissions existed. Restrictions only apply once an owner/admin
    revokes or lowers someone's access. Set it to None for "private by default".
  * The creator of a project, if a regular member, becomes its 'manager'.
"""
import copy
import json
from typing import Dict, Iterable, List, Optional, Set

import models
from permissions import level_allows, log_audit

BUCKETS = ("active", "archived")

# Default level for regular members when a project is created / a new member joins.
# None = private by default (other members get no access until granted).
DEFAULT_NEW_PROJECT_LEVEL: Optional[str] = "editor"
CREATOR_LEVEL = "manager"


# ===========================================================================
# PART 1: pure functions (no database)
# ===========================================================================
def project_ids_in(blob) -> List[str]:
    """Every project id (active + archived) in the blob, in order, unique, as strings."""
    if not isinstance(blob, dict):
        return []
    pd = blob.get("projectsData")
    if not isinstance(pd, dict):
        return []
    seen, out = set(), []
    for bucket in BUCKETS:
        lst = pd.get(bucket)
        if not isinstance(lst, list):
            continue
        for p in lst:
            if isinstance(p, dict) and p.get("id") is not None:
                pid = str(p["id"])
                if pid not in seen:
                    seen.add(pid)
                    out.append(pid)
    return out


def visible_ids(all_ids: Iterable, levels: Dict[str, str]) -> Set[str]:
    """From a list of project ids, those the member may VIEW, given the member's {project_id: level} map."""
    return {str(pid) for pid in all_ids if level_allows(levels.get(str(pid)), "view")}


def filter_state_for_member(blob, visible: Iterable):
    """
    A copy of the blob containing only the projects in `visible`. What gets filtered:
      * projectsData.active / archived
      * every top-level dict keyed by project_id (tasksByProject, groupByTagMap, ...;
        handled generically, the same pattern save_state uses when restoring)
      * recentlyAccessed (a list of {id, ts})
      * currentProjectId, if it points at a hidden project
    The original blob is not modified.
    """
    if not isinstance(blob, dict):
        return blob
    out = copy.deepcopy(blob)
    visible = {str(v) for v in visible}
    hidden = set(project_ids_in(out)) - visible

    pd = out.get("projectsData")
    if isinstance(pd, dict):
        for bucket in BUCKETS:
            if isinstance(pd.get(bucket), list):
                pd[bucket] = [
                    p for p in pd[bucket]
                    if not (isinstance(p, dict) and p.get("id") is not None and str(p["id"]) in hidden)
                ]

    for key, val in list(out.items()):
        if key == "projectsData":
            continue
        if isinstance(val, dict):
            for pid in hidden:
                val.pop(pid, None)
        elif key == "recentlyAccessed" and isinstance(val, list):
            # Allow-list: entries pointing at a project the member may NOT view (including
            # deleted or unknown projects) are dropped. Entries without an id are kept.
            out[key] = [
                e for e in val
                if not (isinstance(e, dict) and e.get("id") is not None and str(e["id"]) not in visible)
            ]

    cur = out.get("currentProjectId")
    if cur is not None and str(cur) in hidden:
        out["currentProjectId"] = None
    return out


class MergeResult:
    """Result of merge_member_payload."""
    def __init__(self, corrected: bool, created_ids: List[str], visible_ids: Set[str]):
        self.corrected = corrected          # the server overrode a change the member could actually see
        self.created_ids = created_ids      # ids of projects this member newly created (not in the old blob)
        self.visible_ids = visible_ids      # ids the member may view (used to filter the response)


def merge_member_payload(old_blob, payload: dict, levels: Dict[str, str]) -> MergeResult:
    """
    Merge a payload submitted by a REGULAR MEMBER with the complete existing blob on the server.
    `payload` is MODIFIED IN PLACE into the final version to be stored (complete, including
    hidden projects). `levels` = this member's {project_id: level} map.

    Rules for each project that ALREADY exists in the old blob:
      * not viewable                      -> restored intact from the old blob, whatever the client sent
      * 'viewer' only                     -> restored intact from the old blob (the edit is discarded)
      * editable but missing from payload -> restored (members cannot delete projects)
      * editable and present in payload   -> the client's version is accepted
    A project that is not in the old blob is treated as newly created by this member.
    """
    old = old_blob if isinstance(old_blob, dict) else {}
    old_pd = old.get("projectsData") if isinstance(old.get("projectsData"), dict) else {}

    # --- index of old projects: pid -> (bucket, index, entry)
    old_index = {}
    for bucket in BUCKETS:
        lst = old_pd.get(bucket)
        if not isinstance(lst, list):
            continue
        for i, p in enumerate(lst):
            if isinstance(p, dict) and p.get("id") is not None:
                old_index.setdefault(str(p["id"]), (bucket, i, p))

    # --- make sure projectsData in the payload is well-formed
    new_pd = payload.get("projectsData")
    if not isinstance(new_pd, dict):
        new_pd = {}
    for bucket in BUCKETS:
        if not isinstance(new_pd.get(bucket), list):
            new_pd[bucket] = []
    payload["projectsData"] = new_pd

    client_entries = {}
    for bucket in BUCKETS:
        for p in new_pd[bucket]:
            if isinstance(p, dict) and p.get("id") is not None:
                client_entries.setdefault(str(p["id"]), p)
    client_ids_ordered = project_ids_in(payload)

    # --- classify existing projects
    locked = {}  # pid -> "hidden" | "readonly" | "restored"
    for pid in old_index:
        lvl = levels.get(pid)
        if not level_allows(lvl, "view"):
            locked[pid] = "hidden"
        elif not level_allows(lvl, "edit"):
            locked[pid] = "readonly"
        elif pid not in client_entries:
            locked[pid] = "restored"

    corrected = False

    # --- 1) the project entries themselves
    for pid, kind in locked.items():
        if kind == "restored":
            corrected = True
        elif kind == "readonly":
            if pid not in client_entries or client_entries[pid] != old_index[pid][2]:
                corrected = True

    locked_pids = set(locked)
    if locked_pids:
        for bucket in BUCKETS:
            new_pd[bucket] = [
                p for p in new_pd[bucket]
                if not (isinstance(p, dict) and p.get("id") is not None and str(p["id"]) in locked_pids)
            ]
        # re-insert the old version at its original position (ascending order so indexes don't shift)
        for pid in sorted(locked_pids, key=lambda x: (old_index[x][0], old_index[x][1])):
            bucket, idx, entry = old_index[pid]
            lst = new_pd[bucket]
            lst.insert(min(idx, len(lst)), copy.deepcopy(entry))

    # --- 2) other data keyed by project_id (tasksByProject, etc.; handled generically)
    dict_keys = {k for k, v in old.items() if isinstance(v, dict)} | {k for k, v in payload.items() if isinstance(v, dict)}
    dict_keys.discard("projectsData")
    for key in dict_keys:
        old_val = old.get(key) if isinstance(old.get(key), dict) else {}
        new_val = payload.get(key)
        if not isinstance(new_val, dict):
            if not any(pid in old_val for pid in locked_pids):
                continue
            new_val = {}
            payload[key] = new_val
        for pid, kind in locked.items():
            if pid in old_val:
                if kind == "readonly" and new_val.get(pid) != old_val[pid]:
                    corrected = True
                new_val[pid] = copy.deepcopy(old_val[pid])
            elif pid in new_val:
                if kind == "readonly":
                    corrected = True
                new_val.pop(pid, None)

    # --- 3) 'recently accessed' list and active-project pointer: neither drop nor leak hidden projects
    hidden_pids = {pid for pid, kind in locked.items() if kind == "hidden"}
    if hidden_pids:
        old_ra = old.get("recentlyAccessed")
        new_ra = payload.get("recentlyAccessed")
        if isinstance(new_ra, list):
            new_ra = [e for e in new_ra if not (isinstance(e, dict) and e.get("id") is not None and str(e["id"]) in hidden_pids)]
        else:
            new_ra = []
        if isinstance(old_ra, list):
            new_ra += [e for e in old_ra if isinstance(e, dict) and e.get("id") is not None and str(e["id"]) in hidden_pids]
        if new_ra or "recentlyAccessed" in payload:
            payload["recentlyAccessed"] = new_ra
        cur = payload.get("currentProjectId")
        if cur is not None and str(cur) in hidden_pids:
            payload["currentProjectId"] = None

    # --- projects newly created by this member
    created_ids = [pid for pid in client_ids_ordered if pid not in old_index]

    # --- ids the member may view in the final state
    final_ids = project_ids_in(payload)
    vis = {pid for pid in final_ids if pid in created_ids or level_allows(levels.get(pid), "view")}

    return MergeResult(corrected=corrected, created_ids=created_ids, visible_ids=vis)


class OwnerMergeResult:
    """Result of restore_disappeared_projects."""
    def __init__(self, corrected: bool, restored_ids: List[str]):
        self.corrected = corrected      # some projects were silently restored
        self.restored_ids = restored_ids


def restore_disappeared_projects(old_blob, payload: dict) -> "OwnerMergeResult":
    """
    Extension of merge_member_payload, for OWNERS/ADMINS only.

    WHY THIS EXISTS: a project that "disappears" from a /state payload used to count as an
    intentional delete only when the sender was a regular member (see merge_member_payload,
    kind "restored"). For owners/admins, save_state() trusted the payload fully and really
    deleted the project (via forget_removed_projects) after taking a snapshot. That is
    dangerous. The only path users actually use to delete a project is the separate
    DELETE /projects/{id} endpoint (see project_access.py), which edits the server blob
    directly. So by the time /state next reads old_data, a project deleted that way is
    already gone from old_data too and is never detected as "missing" here. Hence, if /state
    sees a project missing compared with old_data, it is almost certainly NOT an intentional
    delete; it signals a stale or corrupt client (race condition, polling bug, an old tab
    that never synced). Owners/admins deserve the same protection regular members already have.

    Unlike merge_member_payload there is no "hidden"/"readonly" state, because owners/admins
    can view and edit EVERY project (no per-project level applies to them). EVERY project
    missing from the payload is restored intact from old_blob. `payload` is modified in place.

    If a future "delete many projects at once" feature needs to go through /state (rather than
    one-by-one DELETE /projects/{id}), give this function an explicit new path (e.g. a separate
    payload field that is signed or validated as deletion intent). Do NOT loosen the
    "missing from payload" detection here, because that is exactly the path that was the
    race-condition hole.
    """
    old = old_blob if isinstance(old_blob, dict) else {}
    old_pd = old.get("projectsData") if isinstance(old.get("projectsData"), dict) else {}

    old_index = {}
    for bucket in BUCKETS:
        lst = old_pd.get(bucket)
        if not isinstance(lst, list):
            continue
        for i, p in enumerate(lst):
            if isinstance(p, dict) and p.get("id") is not None:
                old_index.setdefault(str(p["id"]), (bucket, i, p))

    new_pd = payload.get("projectsData")
    if not isinstance(new_pd, dict):
        new_pd = {}
    for bucket in BUCKETS:
        if not isinstance(new_pd.get(bucket), list):
            new_pd[bucket] = []
    payload["projectsData"] = new_pd

    client_ids = set(project_ids_in(payload))
    missing = [pid for pid in old_index if pid not in client_ids]
    if not missing:
        return OwnerMergeResult(corrected=False, restored_ids=[])

    # re-insert the old version at its original position (ascending order so indexes don't shift)
    for pid in sorted(missing, key=lambda x: (old_index[x][0], old_index[x][1])):
        bucket, idx, entry = old_index[pid]
        lst = new_pd[bucket]
        lst.insert(min(idx, len(lst)), copy.deepcopy(entry))

    # other data keyed by project_id (tasksByProject, groupByTagMap, etc.; handled generically,
    # same as step 2 in merge_member_payload)
    missing_set = set(missing)
    for key, old_val in old.items():
        if key == "projectsData" or not isinstance(old_val, dict):
            continue
        if not any(pid in old_val for pid in missing_set):
            continue
        new_val = payload.get(key)
        if not isinstance(new_val, dict):
            new_val = {}
            payload[key] = new_val
        for pid in missing_set:
            if pid in old_val:
                new_val[pid] = copy.deepcopy(old_val[pid])

    return OwnerMergeResult(corrected=True, restored_ids=missing)


# ===========================================================================
# PART 2: project_members bookkeeping (needs the database)
# ===========================================================================
def load_state_blob(db, workspace_id: int):
    """The workspace state blob as a dict, or None if missing or corrupt."""
    st = db.query(models.AppState).filter(models.AppState.workspace_id == workspace_id).first()
    if not st or not st.data:
        return None
    try:
        blob = json.loads(st.data)
    except (TypeError, ValueError):
        return None
    return blob if isinstance(blob, dict) else None


def _existing_pairs(db, workspace_id: int, user_ids: Iterable[int]) -> Set[tuple]:
    user_ids = list(user_ids)
    if not user_ids:
        return set()
    rows = db.query(models.ProjectMember).filter(
        models.ProjectMember.workspace_id == workspace_id,
        models.ProjectMember.user_id.in_(user_ids),
    ).all()
    return {(r.project_id, r.user_id) for r in rows}


def grant_access_to_all_current_projects(db, workspace_id: int, user_id: int,
                                         level: Optional[str] = None,
                                         granted_by_user_id: Optional[int] = None) -> int:
    """
    Give the user access to EVERY project that currently exists in the workspace, for projects
    that have no access row yet. Used when a new member joins and when an admin is demoted to
    member (open by default, matching legacy behavior).
    Returns the number of new rows. Does not commit; it joins the caller's transaction.
    """
    level = level or DEFAULT_NEW_PROJECT_LEVEL
    if level is None:
        return 0
    pids = project_ids_in(load_state_blob(db, workspace_id))
    have = _existing_pairs(db, workspace_id, [user_id])
    added = 0
    for pid in pids:
        if (pid, user_id) in have:
            continue
        db.add(models.ProjectMember(workspace_id=workspace_id, project_id=pid, user_id=user_id,
                                    permission=level, granted_by_user_id=granted_by_user_id))
        added += 1
    return added


def register_new_projects(db, workspace_id: int, new_project_ids: Iterable[str],
                          creator_user_id: int, creator_is_member: bool) -> int:
    """
    Create access rows for projects that newly appeared in the blob:
      * the creator, if a regular member -> 'manager'
      * other active regular members     -> DEFAULT_NEW_PROJECT_LEVEL (unless None)
    Owners/admins need no row (they have full access through their workspace role).
    Does not commit; it joins the caller's transaction. Returns the number of new rows.
    """
    new_project_ids = [str(p) for p in new_project_ids]
    if not new_project_ids:
        return 0

    members = db.query(models.WorkspaceMember).filter(
        models.WorkspaceMember.workspace_id == workspace_id,
        models.WorkspaceMember.status == "active",
        models.WorkspaceMember.role == "member",
        models.WorkspaceMember.user_id.isnot(None),
    ).all()
    member_ids = {m.user_id for m in members}
    if creator_is_member:
        member_ids.add(creator_user_id)

    have = _existing_pairs(db, workspace_id, member_ids)
    added = 0
    for pid in new_project_ids:
        for uid in member_ids:
            if (pid, uid) in have:
                continue
            if creator_is_member and uid == creator_user_id:
                level = CREATOR_LEVEL
            else:
                level = DEFAULT_NEW_PROJECT_LEVEL
            if level is None:
                continue
            db.add(models.ProjectMember(workspace_id=workspace_id, project_id=pid, user_id=uid,
                                        permission=level, granted_by_user_id=creator_user_id))
            added += 1
    return added


def forget_removed_projects(db, workspace_id: int, removed_project_ids: Iterable[str]) -> int:
    """Delete access rows for projects that no longer exist in the blob. Does not commit."""
    removed = [str(p) for p in removed_project_ids]
    if not removed:
        return 0
    return db.query(models.ProjectMember).filter(
        models.ProjectMember.workspace_id == workspace_id,
        models.ProjectMember.project_id.in_(removed),
    ).delete(synchronize_session=False)


def revoke_all_project_access(db, workspace_id: int, user_id: int) -> int:
    """Delete ALL of a user's access rows in the workspace (when removed or leaving). Does not commit."""
    return db.query(models.ProjectMember).filter(
        models.ProjectMember.workspace_id == workspace_id,
        models.ProjectMember.user_id == user_id,
    ).delete(synchronize_session=False)


# ===========================================================================
# PART 3: safety snapshots and restoring deleted projects
# ===========================================================================
SNAPSHOT_KEEP = 30   # number of most recent snapshots kept per workspace


def snapshot_before_removal(db, workspace_id: int, actor_user_id: Optional[int], old_data_json: Optional[str],
                            removed_ids: Iterable[str], keep: int = SNAPSHOT_KEEP):
    """
    Save a copy of the blob BEFORE a save that removes projects. Called by save_state right
    before the blob is overwritten. Snapshots beyond the latest `keep` are pruned.
    Does not commit; it joins the caller's transaction (kept or rolled back together with the main save).
    """
    if not old_data_json:
        return None
    snap = models.AppStateSnapshot(
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        reason="projects_removed",
        removed_project_ids=",".join(sorted(str(r) for r in removed_ids)),
        data=old_data_json,
    )
    db.add(snap)
    db.flush()
    stale = [r[0] for r in db.query(models.AppStateSnapshot.id)
             .filter(models.AppStateSnapshot.workspace_id == workspace_id)
             .order_by(models.AppStateSnapshot.id.desc()).offset(keep).all()]
    if stale:
        db.query(models.AppStateSnapshot).filter(models.AppStateSnapshot.id.in_(stale)).delete(synchronize_session=False)
    return snap


def restore_removed_projects(db, snapshot_id: int, actor_user_id: Optional[int] = None) -> List[str]:
    """
    Restore the projects that were removed by the save that produced snapshot `snapshot_id`.
    It ONLY adds those projects back (with their per-project data, e.g. tasks) into the CURRENT
    blob; any other change made after the snapshot is left untouched. Projects that exist again
    are skipped. Before changing anything, the current state is saved ('before_restore').
    Restored projects are shared with members under the same default rules as a new project.
    Does not commit. Returns the list of restored ids.
    """
    snap = db.query(models.AppStateSnapshot).filter(models.AppStateSnapshot.id == snapshot_id).first()
    if not snap:
        raise ValueError(f"Snapshot id={snapshot_id} tidak ditemukan.")
    ws_id = snap.workspace_id
    try:
        old = json.loads(snap.data)
    except (TypeError, ValueError):
        raise ValueError("Isi snapshot rusak (bukan JSON valid).")
    st = db.query(models.AppState).filter(models.AppState.workspace_id == ws_id).first()
    if not st:
        raise ValueError(f"Workspace {ws_id} tidak punya baris data (app_states); tidak bisa dipulihkan otomatis.")
    try:
        cur = json.loads(st.data) if st.data else {}
    except (TypeError, ValueError):
        raise ValueError("Data workspace sekarang rusak (bukan JSON valid); berhenti demi keamanan.")
    if not isinstance(cur, dict):
        cur = {}

    old_index = {}
    for bucket in BUCKETS:
        lst = (old.get("projectsData") or {}).get(bucket) if isinstance(old.get("projectsData"), dict) else None
        for p in (lst if isinstance(lst, list) else []):
            if isinstance(p, dict) and p.get("id") is not None:
                old_index.setdefault(str(p["id"]), (bucket, p))

    have = set(project_ids_in(cur))
    todo = [p for p in (snap.removed_project_ids or "").split(",") if p and p not in have and p in old_index]
    if not todo:
        return []

    # save the CURRENT state first (so this restore can itself be undone)
    db.add(models.AppStateSnapshot(workspace_id=ws_id, actor_user_id=actor_user_id,
                                   reason="before_restore", removed_project_ids=None, data=st.data))

    pd = cur.get("projectsData")
    if not isinstance(pd, dict):
        pd = {}
        cur["projectsData"] = pd
    for bucket in BUCKETS:
        if not isinstance(pd.get(bucket), list):
            pd[bucket] = []

    for pid in todo:
        bucket, entry = old_index[pid]
        pd[bucket].append(copy.deepcopy(entry))
        for key, val in old.items():
            if key == "projectsData" or not isinstance(val, dict) or pid not in val:
                continue
            if not isinstance(cur.get(key), dict):
                cur[key] = {}
            cur[key][pid] = copy.deepcopy(val[pid])

    st.data = json.dumps(cur)
    register_new_projects(db, ws_id, todo, creator_user_id=actor_user_id or snap.actor_user_id or 0,
                          creator_is_member=False)
    for pid in todo:
        log_audit(db, ws_id, actor_user_id, "project.restored", "project", pid, {"snapshot_id": snapshot_id})
    return todo