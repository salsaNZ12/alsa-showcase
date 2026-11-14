"""
Per-project access management.

Complements permissions.py and state_guard.py with the entry points that CHANGE access:

  Endpoints (all enforced server-side via permissions.require, not merely hidden in the UI):
    GET    /projects/{project_id}/members            list members and their access level on this project
    PUT    /projects/{project_id}/members/{user_id}  grant / change a level  {"permission": "viewer|editor|manager"}
    DELETE /projects/{project_id}/members/{user_id}  revoke access
    DELETE /projects/{project_id}                    delete a project (workspace owner/admin or project manager)
    GET    /workspace/my-project-access              the logged-in user's access levels (drives the frontend UI)

  Helpers for invitations (called from main.py):
    normalize_invite_projects()   validate the project list in the POST /workspace/invite body
    store_invite_grants()         persist that selection in the workspace_invite_access table
    grant_for_accepted_invite()   when an invite is accepted, grant access according to that selection

Who may manage a project's access = whoever holds the 'manage_access' permission on it:
workspace owners/admins (automatically) and members who are 'manager' on that project.

The module is attached to main.py through build_router(...) to avoid a circular import
(get_current_user and resolve_workspace live in main.py).
"""
import json
from typing import Iterable, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

import models
import permissions
import state_guard

# Level for projects ticked in an invite when the inviter doesn't specify one.
# Intentionally stays 'editor' even if state_guard.DEFAULT_NEW_PROJECT_LEVEL becomes None:
# a project the inviter explicitly ticked is a deliberate choice, not an automatic default.
DEFAULT_INVITE_LEVEL = "editor"


# ===========================================================================
# PART 1: invitations with a project selection
# ===========================================================================
def normalize_invite_projects(raw, existing_project_ids: Iterable) -> Optional[List[dict]]:
    """
    Validate and normalize the `projects` list from the POST /workspace/invite body.

    raw   : None (key not sent), or a list of project ids (str/int) or
            {"project_id": ..., "permission": "viewer|editor|manager"}.
    Result: None          -> not specified: legacy behavior (open by default)
            empty list    -> specified but empty: deliberately no project access
            list of dicts -> [{"project_id": "proj_1", "permission": "editor"}], unique per project

    Malformed input / unknown id / invalid level -> HTTPException 400 (the invite is not created).
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="Format 'projects' tidak valid: harus berupa daftar.")

    existing = {str(p) for p in existing_project_ids}
    out = {}  # project_id -> level (last one wins)
    for item in raw:
        if isinstance(item, dict):
            pid = item.get("project_id", item.get("id"))
            level = item.get("permission") or DEFAULT_INVITE_LEVEL
        else:
            pid, level = item, DEFAULT_INVITE_LEVEL
        if isinstance(pid, bool) or pid is None or not isinstance(pid, (str, int)) or str(pid).strip() == "":
            raise HTTPException(status_code=400, detail="Format 'projects' tidak valid: id project kosong atau bukan teks/angka.")
        pid = str(pid)
        if level not in permissions.VALID_PROJECT_LEVELS:
            raise HTTPException(
                status_code=400,
                detail=f"Level akses '{level}' tidak valid. Pilih salah satu: {', '.join(permissions.VALID_PROJECT_LEVELS)}.",
            )
        if pid not in existing:
            raise HTTPException(status_code=400, detail=f"Project '{pid}' tidak ditemukan di workspace ini.")
        out[pid] = level
    return [{"project_id": pid, "permission": lvl} for pid, lvl in out.items()]


def store_invite_grants(db: Session, invite: "models.WorkspaceInvite", workspace_id: int,
                        grants: Optional[List[dict]]) -> None:
    """Persist the project selection for an invite. grants=None -> store nothing. Does not commit."""
    if grants is None:
        return
    db.flush()  # make sure invite.id exists
    db.add(models.WorkspaceInviteAccess(invite_id=invite.id, workspace_id=workspace_id,
                                        grants=json.dumps(grants, ensure_ascii=False)))


def grant_for_accepted_invite(db: Session, invite: "models.WorkspaceInvite", user_id: int) -> int:
    """
    Called when a regular member accepts an invite. Returns the number of new access rows.
      * invite without a project selection -> every project that currently exists (open by default, legacy behavior)
      * invite with a selection            -> only the selected projects that STILL EXIST
    Fail-closed: if the stored selection is corrupt, the member joins with no project access
    (an admin can grant it manually). Does not commit; it joins the caller's transaction.
    """
    row = db.query(models.WorkspaceInviteAccess).filter(
        models.WorkspaceInviteAccess.invite_id == invite.id
    ).first()
    if row is None:
        return state_guard.grant_access_to_all_current_projects(
            db, invite.workspace_id, user_id, granted_by_user_id=invite.invited_by_user_id)

    try:
        grants = json.loads(row.grants)
        if not isinstance(grants, list):
            raise ValueError("bukan list")
    except (TypeError, ValueError) as e:
        print(f"[invite] Pilihan project undangan id={invite.id} rusak ({e}); member dimasukkan tanpa akses project.")
        return 0

    existing_ids = set(state_guard.project_ids_in(state_guard.load_state_blob(db, invite.workspace_id)))
    have = state_guard._existing_pairs(db, invite.workspace_id, [user_id])
    added = 0
    for g in grants:
        if not isinstance(g, dict):
            continue
        pid = str(g.get("project_id", ""))
        level = g.get("permission")
        if pid not in existing_ids or level not in permissions.VALID_PROJECT_LEVELS or (pid, user_id) in have:
            continue  # project deleted since the invite / invalid level / row already exists
        db.add(models.ProjectMember(workspace_id=invite.workspace_id, project_id=pid, user_id=user_id,
                                    permission=level, granted_by_user_id=invite.invited_by_user_id))
        have.add((pid, user_id))
        added += 1
    return added


# ===========================================================================
# PART 2: router
# ===========================================================================
def _project_or_404(db: Session, workspace_id: int, project_id: str) -> None:
    if str(project_id) not in set(state_guard.project_ids_in(state_guard.load_state_blob(db, workspace_id))):
        raise HTTPException(status_code=404, detail="Project tidak ditemukan.")


def _remove_project_from_blob(blob: dict, project_id: str) -> None:
    """Remove a project and all of its per-project data from the blob, in place. Same pattern as filter_state_for_member."""
    pid = str(project_id)
    pd = blob.get("projectsData")
    if isinstance(pd, dict):
        for bucket in state_guard.BUCKETS:
            if isinstance(pd.get(bucket), list):
                pd[bucket] = [p for p in pd[bucket]
                              if not (isinstance(p, dict) and p.get("id") is not None and str(p["id"]) == pid)]
    for key, val in list(blob.items()):
        if key == "projectsData":
            continue
        if isinstance(val, dict):
            val.pop(pid, None)
        elif key == "recentlyAccessed" and isinstance(val, list):
            blob[key] = [e for e in val if not (isinstance(e, dict) and e.get("id") is not None and str(e["id"]) == pid)]
    if blob.get("currentProjectId") is not None and str(blob["currentProjectId"]) == pid:
        blob["currentProjectId"] = None


def build_router(get_current_user, get_db, resolve_workspace) -> APIRouter:
    router = APIRouter(tags=["project-access"])

    def _check_target(db: Session, ws, actor, target_user_id: int) -> models.WorkspaceMember:
        """Shared rules for PUT/DELETE on someone's access. Returns the target's active membership."""
        if target_user_id == actor.id:
            raise HTTPException(status_code=400, detail="Kamu tidak bisa mengubah aksesmu sendiri ke project ini.")
        target = db.query(models.WorkspaceMember).filter(
            models.WorkspaceMember.workspace_id == ws.id,
            models.WorkspaceMember.user_id == target_user_id,
            models.WorkspaceMember.status == "active",
        ).first()
        if not target:
            raise HTTPException(status_code=404, detail="Orang ini bukan anggota aktif workspace.")
        if target.role in permissions.WORKSPACE_FULL_ACCESS_ROLES:
            raise HTTPException(status_code=400,
                                detail="Owner dan admin workspace otomatis punya akses penuh ke semua project; aksesnya tidak bisa diatur per project.")
        return target

    # ------------------------------------------------ my access (for the frontend)
    @router.get("/workspace/my-project-access")
    def my_project_access(workspace_id: int = None,
                          current_user: models.User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
        """
        The logged-in user's access level on each project in this workspace, so the frontend
        knows which buttons to show (edit / delete / manage access). This is for DISPLAY only:
        the real enforcement stays on the server, in every endpoint and in /state.
          owner/admin -> full_access=True, empty levels (they are manager on every project)
          member      -> levels = {project_id: viewer|editor|manager}; a project that is not
                         in the map means no access.
        """
        ws = resolve_workspace(workspace_id, current_user, db)
        role = permissions.get_workspace_role(db, current_user.id, ws.id)
        if role is None:
            raise HTTPException(status_code=403, detail="Kamu bukan anggota aktif workspace ini.")
        full = role in permissions.WORKSPACE_FULL_ACCESS_ROLES
        levels = {} if full else permissions.get_user_project_levels(db, current_user.id, ws.id)
        return {"workspace_id": ws.id, "workspace_role": role, "full_access": full, "levels": levels}

    # ---------------------------------------------------------------- list
    @router.get("/projects/{project_id}/members")
    def list_project_members(project_id: str, workspace_id: int = None,
                             current_user: models.User = Depends(get_current_user),
                             db: Session = Depends(get_db)):
        ws = resolve_workspace(workspace_id, current_user, db)
        my_level = permissions.require(db, current_user.id, ws.id, project_id, "manage_access")
        _project_or_404(db, ws.id, project_id)

        members = db.query(models.WorkspaceMember).filter(
            models.WorkspaceMember.workspace_id == ws.id,
            models.WorkspaceMember.status == "active",
            models.WorkspaceMember.user_id.isnot(None),
        ).all()
        user_ids = [m.user_id for m in members]
        users = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(user_ids)).all()} if user_ids else {}
        rows = {r.user_id: r for r in db.query(models.ProjectMember).filter(
            models.ProjectMember.workspace_id == ws.id,
            models.ProjectMember.project_id == str(project_id),
        ).all()}

        order = {"owner": 0, "admin": 1, "member": 2}
        result = []
        for m in sorted(members, key=lambda x: (order.get(x.role, 3), (users.get(x.user_id).name or "").lower() if users.get(x.user_id) else "")):
            u = users.get(m.user_id)
            full = m.role in permissions.WORKSPACE_FULL_ACCESS_ROLES
            row = rows.get(m.user_id)
            result.append({
                "user_id": m.user_id,
                "member_id": m.id,
                "name": u.name if u else "?",
                "email": u.email if u else m.invited_email,
                "workspace_role": m.role,
                "permission": permissions.FULL_ACCESS_LEVEL if full else (row.permission if row else None),
                "source": "workspace_role" if full else ("project" if row else "none"),
                "editable": (not full) and m.user_id != current_user.id,
            })
        return {"project_id": str(project_id), "workspace_id": ws.id, "my_permission": my_level, "members": result}

    # ------------------------------------------------------- grant / change level
    @router.put("/projects/{project_id}/members/{user_id}")
    def set_project_member_access(project_id: str, user_id: int, payload: dict, workspace_id: int = None,
                                  current_user: models.User = Depends(get_current_user),
                                  db: Session = Depends(get_db)):
        ws = resolve_workspace(workspace_id, current_user, db)
        permissions.require(db, current_user.id, ws.id, project_id, "manage_access")
        _project_or_404(db, ws.id, project_id)

        level = payload.get("permission")
        if level not in permissions.VALID_PROJECT_LEVELS:
            raise HTTPException(status_code=400,
                                detail=f"Level akses tidak valid. Pilih salah satu: {', '.join(permissions.VALID_PROJECT_LEVELS)}.")
        _check_target(db, ws, current_user, user_id)

        row = db.query(models.ProjectMember).filter(
            models.ProjectMember.workspace_id == ws.id,
            models.ProjectMember.project_id == str(project_id),
            models.ProjectMember.user_id == user_id,
        ).first()
        if row is None:
            db.add(models.ProjectMember(workspace_id=ws.id, project_id=str(project_id), user_id=user_id,
                                        permission=level, granted_by_user_id=current_user.id))
            permissions.log_audit(db, ws.id, current_user.id, "project_access.grant", "project", project_id,
                                  {"user_id": user_id, "permission": level})
            changed = True
        elif row.permission == level:
            changed = False   # nothing changed -> nothing to write, nothing to audit
        else:
            old = row.permission
            row.permission = level
            permissions.log_audit(db, ws.id, current_user.id, "project_access.change", "project", project_id,
                                  {"user_id": user_id, "from": old, "to": level})
            changed = True
        try:
            db.commit()
        except IntegrityError:
            db.rollback()   # two concurrent requests created the same row
            raise HTTPException(status_code=409, detail="Ada perubahan bersamaan, coba lagi sebentar lagi.")
        except OperationalError:
            db.rollback()
            raise HTTPException(status_code=503, detail="Server database sedang bermasalah, coba lagi sebentar lagi.")
        return {"message": "Akses diperbarui." if changed else "Tidak ada perubahan.",
                "project_id": str(project_id), "user_id": user_id, "permission": level, "changed": changed}

    # ------------------------------------------------------------------ revoke
    @router.delete("/projects/{project_id}/members/{user_id}")
    def revoke_project_member_access(project_id: str, user_id: int, workspace_id: int = None,
                                     current_user: models.User = Depends(get_current_user),
                                     db: Session = Depends(get_db)):
        ws = resolve_workspace(workspace_id, current_user, db)
        permissions.require(db, current_user.id, ws.id, project_id, "manage_access")
        _project_or_404(db, ws.id, project_id)
        _check_target(db, ws, current_user, user_id)

        row = db.query(models.ProjectMember).filter(
            models.ProjectMember.workspace_id == ws.id,
            models.ProjectMember.project_id == str(project_id),
            models.ProjectMember.user_id == user_id,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Orang ini memang tidak punya akses ke project ini.")
        old = row.permission
        db.delete(row)
        permissions.log_audit(db, ws.id, current_user.id, "project_access.revoke", "project", project_id,
                              {"user_id": user_id, "was": old})
        try:
            db.commit()
        except OperationalError:
            db.rollback()
            raise HTTPException(status_code=503, detail="Server database sedang bermasalah, coba lagi sebentar lagi.")
        return {"message": "Akses dicabut.", "project_id": str(project_id), "user_id": user_id}

    # ------------------------------------------------------------ delete project
    @router.delete("/projects/{project_id}")
    def delete_project(project_id: str, workspace_id: int = None,
                       current_user: models.User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
        """
        Dedicated deletion path for project managers. POST /state deliberately cannot be used
        by members to delete projects (the server cannot tell an intentional delete from a
        stale client), so intentional deletion goes through this endpoint. A safety snapshot
        is still taken, exactly as when an owner/admin deletes through /state.
        """
        ws = resolve_workspace(workspace_id, current_user, db)
        permissions.require(db, current_user.id, ws.id, project_id, "delete")

        st = db.query(models.AppState).filter(models.AppState.workspace_id == ws.id).first()
        blob = None
        if st and st.data:
            try:
                blob = json.loads(st.data)
            except (TypeError, ValueError):
                blob = None
        if not isinstance(blob, dict) or str(project_id) not in set(state_guard.project_ids_in(blob)):
            raise HTTPException(status_code=404, detail="Project tidak ditemukan.")

        state_guard.snapshot_before_removal(db, ws.id, current_user.id, st.data, [str(project_id)])
        _remove_project_from_blob(blob, project_id)
        st.data = json.dumps(blob)
        state_guard.forget_removed_projects(db, ws.id, [str(project_id)])
        permissions.log_audit(db, ws.id, current_user.id, "project.deleted", "project", project_id,
                              {"via": "delete_endpoint"})
        try:
            db.commit()
        except OperationalError:
            db.rollback()
            raise HTTPException(status_code=503, detail="Server database sedang bermasalah, coba lagi sebentar lagi.")
        return {"message": "Project dihapus.", "project_id": str(project_id), "workspace_id": ws.id}

    return router