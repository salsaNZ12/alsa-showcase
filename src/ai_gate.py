"""
Opt-in gate for the per-project "AI Analyzer".

Rules enforced here (and used by EVERY /ai/* endpoint in main.py that reads project data):

  * The on/off state lives on the SERVER (table project_ai_settings), not in the browser.
    The browser only displays the state; the server decides.
  * Default is OFF. A project with no row in the table is OFF.
  * When OFF, an endpoint MUST NOT put project data (task names, deadlines, dependencies,
    assignees, attachment summaries, activity logs) into a prompt sent to the AI provider.
    The check runs BEFORE the prompt is assembled, not via instructions inside the prompt.
  * Fail-closed: an empty or unknown project_id, or a user without view permission, is treated as OFF.

The module is deliberately small and depends only on models (not main.py), which keeps it easy to test.
"""
from typing import Optional, Set

import models

# User-facing messages, kept in the product's two primary languages.
_OFF_MESSAGES = {
    "project": {
        "id": ("AI Analyzer sedang mati untuk project ini, jadi AI tidak membaca data project ini sama sekali. "
               "Nyalakan lewat tombol “AI Analyzer” di halaman project kalau mau memakai fitur AI."),
        "en": ("AI Analyzer is off for this project, so the AI is not reading any data from it. "
               "Turn it on with the “AI Analyzer” button on the project page to use AI features."),
    },
    "dashboard": {
        "id": ("AI Dashboard sedang mati untuk akunmu, jadi AI tidak membaca data project apa pun lewat dashboard. "
               "Nyalakan dengan tombol “Nyalakan AI” di jendela AI Chat dashboard."),
        "en": ("AI Dashboard is off for your account, so the AI is not reading any project data through the dashboard. "
               "Turn it on with the “Turn on AI” button in the dashboard AI Chat window."),
    },
    "none_enabled": {
        "id": ("Belum ada project yang AI Analyzer-nya aktif, jadi AI tidak membaca data project apa pun. "
               "Nyalakan lewat tombol “AI Analyzer” di halaman project yang kamu mau."),
        "en": ("No project has AI Analyzer turned on, so the AI is not reading any project data. "
               "Turn it on with the “AI Analyzer” button on the project you want."),
    },
}


def off_message(lang_code: str = "id", scope: str = "project") -> str:
    """Message shown to the user when AI is off. Any language other than 'id' falls back to English."""
    table = _OFF_MESSAGES.get(scope, _OFF_MESSAGES["project"])
    return table["id"] if lang_code == "id" else table["en"]


def is_enabled(db, workspace_id: int, project_id) -> bool:
    """True only if a row exists for (workspace, project) and analyzer_enabled is True."""
    pid = str(project_id or "").strip()
    if not pid:
        return False
    row = db.query(models.ProjectAISetting).filter(
        models.ProjectAISetting.workspace_id == workspace_id,
        models.ProjectAISetting.project_id == pid,
    ).first()
    return bool(row and row.analyzer_enabled)


def enabled_project_ids(db, workspace_id: int) -> Set[str]:
    """Ids of every project in this workspace with AI Analyzer ON (a set of str)."""
    rows = db.query(models.ProjectAISetting.project_id).filter(
        models.ProjectAISetting.workspace_id == workspace_id,
        models.ProjectAISetting.analyzer_enabled == True,  # noqa: E712 (SQLAlchemy needs == True)
    ).all()
    return {str(r[0]) for r in rows}


def set_enabled(db, workspace_id: int, project_id, enabled: bool, user_id: Optional[int]) -> bool:
    """
    Turn the Analyzer on or off for one project. Returns True if the value CHANGED.
    Does not commit; it joins the caller's transaction.
    """
    pid = str(project_id).strip()
    row = db.query(models.ProjectAISetting).filter(
        models.ProjectAISetting.workspace_id == workspace_id,
        models.ProjectAISetting.project_id == pid,
    ).first()
    if row is None:
        if not enabled:
            return False  # already treated as off; no row needed
        db.add(models.ProjectAISetting(workspace_id=workspace_id, project_id=pid,
                                       analyzer_enabled=True, updated_by_user_id=user_id))
        return True
    if bool(row.analyzer_enabled) == bool(enabled):
        return False
    row.analyzer_enabled = bool(enabled)
    row.updated_by_user_id = user_id
    return True


# ---------------------------------------------------------------------------
# AI Dashboard (dashboard AI Chat + dashboard Insight panel): opt-in PER USER, per workspace.
# Independent of the per-project AI Analyzer above; neither switch enables or disables the other.
# Same guarantees as the Analyzer: stored on the SERVER, default OFF, fail-closed,
# checked BEFORE the prompt is assembled.
# ---------------------------------------------------------------------------
def is_dashboard_enabled(db, workspace_id: int, user_id) -> bool:
    """True only if a row exists for (workspace, user) and dashboard_enabled is True."""
    if not user_id:
        return False
    row = db.query(models.UserDashboardAISetting).filter(
        models.UserDashboardAISetting.workspace_id == workspace_id,
        models.UserDashboardAISetting.user_id == user_id,
    ).first()
    return bool(row and row.dashboard_enabled)


def set_dashboard_enabled(db, workspace_id: int, user_id: int, enabled: bool) -> bool:
    """
    Turn the AI Dashboard on or off for one user. Returns True if the value CHANGED.
    Does not commit; it joins the caller's transaction.
    """
    row = db.query(models.UserDashboardAISetting).filter(
        models.UserDashboardAISetting.workspace_id == workspace_id,
        models.UserDashboardAISetting.user_id == user_id,
    ).first()
    if row is None:
        if not enabled:
            return False  # already treated as off; no row needed
        db.add(models.UserDashboardAISetting(workspace_id=workspace_id, user_id=user_id, dashboard_enabled=True))
        return True
    if bool(row.dashboard_enabled) == bool(enabled):
        return False
    row.dashboard_enabled = bool(enabled)
    return True