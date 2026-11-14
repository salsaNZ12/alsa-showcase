"""
Per-project permissions.

Two layers, following the model used by products like Asana, GitHub and Jira:
  1. Workspace role (workspace_members.role): owner | admin | member.
     Owners and admins are automatically 'manager' on EVERY project.
  2. Per-project level (project_members.permission) for regular members:
     viewer | editor | manager. A member with no row in project_members
     has NO access to that project at all.

Endpoints check a PERMISSION (an action), never a role name:
    if not can(db, user.id, ws.id, project_id, "delete"): ...
Adding a new level (e.g. commenter) or a custom role only requires one new
entry in PROJECT_PERMISSIONS; no endpoint has to change.

This module never mutates data except log_audit(), which appends a row to
audit_log. It fails closed: an unknown level grants no permission at all.
"""
import json
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models

# ---------------------------------------------------------------------------
# Permission definitions
# ---------------------------------------------------------------------------
PROJECT_PERMISSIONS = {
    "viewer":  {"view"},
    "editor":  {"view", "edit"},
    "manager": {"view", "edit", "delete", "manage_access"},
}

VALID_PROJECT_LEVELS = tuple(PROJECT_PERMISSIONS.keys())

# Workspace roles that automatically receive this level on every project.
WORKSPACE_FULL_ACCESS_ROLES = ("owner", "admin")
FULL_ACCESS_LEVEL = "manager"

# User-facing text for 403 responses (Indonesian, matching the product UI).
ACTION_LABELS = {
    "view": "melihat project ini",
    "edit": "mengubah isi project ini",
    "delete": "menghapus project ini",
    "manage_access": "mengatur akses project ini",
}


# ---------------------------------------------------------------------------
# Reading access levels
# ---------------------------------------------------------------------------
def get_workspace_role(db: Session, user_id: int, workspace_id: int) -> Optional[str]:
    """The user's workspace role ('owner'/'admin'/'member'), or None if not an active member."""
    m = db.query(models.WorkspaceMember).filter(
        models.WorkspaceMember.workspace_id == workspace_id,
        models.WorkspaceMember.user_id == user_id,
        models.WorkspaceMember.status == "active",
    ).first()
    return m.role if m else None


def get_project_permission(db: Session, user_id: int, workspace_id: int, project_id,
                           workspace_role: Optional[str] = None) -> Optional[str]:
    """
    The user's access level on one project: 'viewer' | 'editor' | 'manager' | None (no access).
    Pass workspace_role if the caller already loaded it, to avoid a second query.
    """
    role = workspace_role or get_workspace_role(db, user_id, workspace_id)
    if role is None:
        return None  # not an active member of this workspace
    if role in WORKSPACE_FULL_ACCESS_ROLES:
        return FULL_ACCESS_LEVEL

    row = db.query(models.ProjectMember).filter(
        models.ProjectMember.workspace_id == workspace_id,
        models.ProjectMember.project_id == str(project_id),
        models.ProjectMember.user_id == user_id,
    ).first()
    return row.permission if row else None


def get_user_project_levels(db: Session, user_id: int, workspace_id: int) -> dict:
    """
    All of a user's explicit grants in a workspace as {project_id: level}, in ONE query.
    Only meaningful for regular members; owners and admins have full access and don't need it.
    Used to filter the /state blob without issuing one query per project.
    """
    rows = db.query(models.ProjectMember).filter(
        models.ProjectMember.workspace_id == workspace_id,
        models.ProjectMember.user_id == user_id,
    ).all()
    return {r.project_id: r.permission for r in rows}


# ---------------------------------------------------------------------------
# Action checks
# ---------------------------------------------------------------------------
def level_allows(level: Optional[str], action: str) -> bool:
    """True if the level (e.g. 'editor') may perform the action (e.g. 'edit'). Unknown level = False."""
    if level is None:
        return False
    return action in PROJECT_PERMISSIONS.get(level, set())


def can(db: Session, user_id: int, workspace_id: int, project_id, action: str,
        workspace_role: Optional[str] = None) -> bool:
    level = get_project_permission(db, user_id, workspace_id, project_id, workspace_role)
    return level_allows(level, action)


def require(db: Session, user_id: int, workspace_id: int, project_id, action: str,
            workspace_role: Optional[str] = None) -> str:
    """
    Like can(), but raises 403 when the action is not allowed. Returns the level when it is.
    The error message distinguishes "no access at all" from "level too low".
    """
    level = get_project_permission(db, user_id, workspace_id, project_id, workspace_role)
    if level is None:
        raise HTTPException(status_code=403, detail="Kamu tidak punya akses ke project ini.")
    if not level_allows(level, action):
        raise HTTPException(
            status_code=403,
            detail=f"Kamu tidak punya izin untuk {ACTION_LABELS.get(action, action)}.",
        )
    return level


def accessible_project_ids(db: Session, user_id: int, workspace_id: int,
                           all_project_ids: Iterable, workspace_role: Optional[str] = None) -> set:
    """
    From every project id in the workspace, return those this user may VIEW (a set of str).
    Owner/admin: all of them. Member: only projects with a 'view'-capable row in project_members.
    Non-member: empty set.
    """
    all_ids = {str(p) for p in all_project_ids}
    role = workspace_role or get_workspace_role(db, user_id, workspace_id)
    if role is None:
        return set()
    if role in WORKSPACE_FULL_ACCESS_ROLES:
        return all_ids
    levels = get_user_project_levels(db, user_id, workspace_id)
    return {pid for pid in all_ids if level_allows(levels.get(pid), "view")}


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def log_audit(db: Session, workspace_id: int, actor_user_id: Optional[int], action: str,
              target_type: Optional[str] = None, target_id=None, detail: Optional[dict] = None,
              commit: bool = False) -> None:
    """
    Record one event in audit_log. Example actions: 'project_access.grant',
    'project_access.change', 'project_access.revoke'.
    commit=False (default): the row joins the caller's transaction, so it is saved
    or rolled back together with the change it describes. A failure to write the log
    must never fail the main action, so errors are swallowed and printed instead.
    """
    try:
        db.add(models.AuditLog(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=json.dumps(detail, ensure_ascii=False) if detail is not None else None,
        ))
        if commit:
            db.commit()
    except Exception as e:  # pragma: no cover
        print(f"[audit] Gagal mencatat audit_log ({action}): {e}")