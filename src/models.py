from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, UniqueConstraint, Numeric, event
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base
from secret_crypto import EncryptedText

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Email verification status. Accounts created before this feature existed were
    # set to True by a migration so they are not suddenly locked out of login.
    email_verified = Column(Boolean, default=False, nullable=False)

    # Tells a user they were just removed from a workspace by an admin. Set by
    # DELETE /workspace/members/{id} when the removed user (if registered) loses their
    # last active membership, then read and immediately cleared (shown once) by GET /state.
    # Follows the Notion/Asana/ClickUp pattern: losing access is shown to the user
    # explicitly, not silently replaced by a new empty workspace.
    removed_notice_workspace_name = Column(String, nullable=True)
    removed_notice_at = Column(DateTime(timezone=True), nullable=True)

    # Presence: when this user was last "active" (dashboard open). Updated by
    # POST /presence/heartbeat in main.py. The online/away/offline status is computed
    # from the time elapsed since this column, not stored as a separate column.
    last_seen_at = Column(DateTime(timezone=True), nullable=True)

    # Onboarding status. The backend is the source of truth (not browser localStorage),
    # so it survives a device or browser change and admins can tell who has completed
    # onboarding. Set to True by POST /onboarding/complete and returned in the
    # /auth/login and /auth/me responses, so post-login routing on the client relies on
    # the server value. Accounts created before this column existed were set to True by
    # a migration so existing users are not forced through the tour again.
    onboarding_completed = Column(Boolean, default=False, nullable=False)

    # Relationship to Project, kept in sync with owner_id
    projects = relationship("Project", back_populates="owner")

class Project(Base):
    """
    Legacy relational table. The live project data is stored in the AppState.data JSON blob;
    this table is no longer read or written by the application and remains only for historical
    data.
    """
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"))

    # Back-reference to User
    owner = relationship("User", back_populates="projects")

    # AI columns provisioned from the start (AI-native design)
    ai_analyzer_active = Column(Boolean, default=False)   # AI Project Analyzer toggle
    ai_health_summary = Column(String, nullable=True)       # Project Health Insight result

    tasks = relationship("Task", back_populates="project")

class Task(Base):
    """
    Legacy relational table, see Project. Live task data lives in the AppState.data JSON blob
    (tasksByProject).
    """
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    deadline = Column(DateTime, nullable=True)
    project_id = Column(Integer, ForeignKey("projects.id"))

    # AI columns provisioned from the start (AI-native design)
    ai_tooltip_context = Column(String, nullable=True)     # for the Contextual AI Tooltip
    ai_suggested_deadline = Column(DateTime, nullable=True) # for Soft Deadline Suggestion

    project = relationship("Project", back_populates="tasks")


class Workspace(Base):
    """
    Shared container for team collaboration. A workspace has many members (via
    WorkspaceMember) and exactly one AppState (the dashboard data) shared by all of them.

    Every user gets one personal workspace automatically (at signup, or by migration for older
    accounts). Collaboration happens when other users are invited (see WorkspaceInvite) and
    join the same workspace.
    """
    __tablename__ = "workspaces"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("User")
    members = relationship("WorkspaceMember", back_populates="workspace")


class WorkspaceMember(Base):
    """
    Membership row: who belongs to which workspace, with what role and status. This is what
    makes "team" a real data concept instead of a client-side list.

    role: "owner" (workspace creator; cannot be removed or demoted through the regular
    endpoints), "admin" (can invite and remove members), "member" (read/write data, cannot
    manage members).

    status: "active" (joined) or "pending" (invited, not yet accepted). A "pending" row is
    created when an invite is sent and may not point to a valid user_id until the recipient
    registers or logs in with the invite token (see WorkspaceInvite).
    """
    __tablename__ = "workspace_members"
    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    role = Column(String, nullable=False, default="member")  # owner | admin | member
    status = Column(String, nullable=False, default="active")  # active | pending
    invited_email = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    workspace = relationship("Workspace", back_populates="members")
    user = relationship("User")


class WorkspaceInvite(Base):
    """
    Single-use token for joining a workspace, following the same pattern as PasswordResetToken
    and EmailVerificationToken: unique token, expiry time, and a used flag. The email is sent
    through the same SMTP helper (send_otp_email).

    Created by POST /workspace/invite and consumed (marked used=True) by POST
    /workspace/accept-invite in main.py.
    """
    __tablename__ = "workspace_invites"
    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    invited_email = Column(String, nullable=False)
    role = Column(String, nullable=False, default="member")
    token = Column(String, unique=True, index=True, nullable=False)
    invited_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    workspace = relationship("Workspace")
    invited_by = relationship("User")


class AppState(Base):
    """
    The whole dashboard state (tasksByProject, projectsData, templates, access history, quick
    capture, and so on) stored as one JSON blob per WORKSPACE, not per individual user (see
    Workspace).

    The legacy owner_id column is kept (nullable, no longer used by the /state endpoint) so
    historical data is not lost and the change stays reversible. New code treats workspace_id
    as the source of truth.
    """
    __tablename__ = "app_states"
    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), unique=True, nullable=True)
    data = Column(String, nullable=False, default="{}")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    owner = relationship("User")
    workspace = relationship("Workspace")


class PasswordResetToken(Base):
    """
    Single-use token for the "Forgot password" flow. Created by POST /auth/forgot-password and
    consumed (marked used=True) by POST /auth/reset-password in main.py.

    A separate table, so the User structure is untouched.
    """
    __tablename__ = "password_reset_tokens"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token = Column(String, unique=True, index=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")


class EmailVerificationToken(Base):
    """
    Single-use OTP code that verifies email ownership at signup. Created by POST /auth/signup
    and matched (then marked used=True) by POST /auth/verify-email in main.py.

    How the code is delivered is independent of this table: only the "send by email" step
    depends on SMTP configuration, while the table structure and the verification endpoint
    stay the same.
    """
    __tablename__ = "email_verification_tokens"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    code = Column(String, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")


class TeamNote(Base):
    """
    Team notes (the social Context Bar) as their own table. They used to live inside
    AppState.data (one JSON blob per workspace, saved via POST /state), where each note's
    `authorEmail` was trusted verbatim from the client payload, so anyone with DevTools could
    impersonate another member (write or delete notes in their name).

    As a separate table (POST/GET/DELETE /notes in main.py), `author_email` and `author_name`
    are ALWAYS taken from the logged-in user (a server-verified JWT), never from
    client-supplied fields. The author also drives delete authorization: only the original
    author may delete a note.

    workspace_id (FK to workspaces, required) scopes access, like AppState.workspace_id and
    ActivityLog.workspace_id.

    tab: "foryou" (notes that mention specific members) or "announce" (general announcement,
    no mention). Decided SERVER-SIDE at creation (see POST /notes) from mentions that actually
    match workspace members, not trusted from the client.

    mentions: a comma-separated email string (e.g. "a@x.com,b@x.com"); a short list that does
    not need its own relation table.
    """
    __tablename__ = "team_notes"
    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    tab = Column(String, nullable=False, default="announce")  # foryou | announce
    author_name = Column(String, nullable=False)
    author_email = Column(String, nullable=False)
    text = Column(String, nullable=False)
    mentions = Column(String, nullable=True)  # comma-separated emails, may be empty
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    workspace = relationship("Workspace")


class ActivityLog(Base):
    """
    Real per-project activity: who completed which task, new comments, approaching deadlines,
    unassigned tasks, and so on. It is the raw data source for the Context Bar / AI summary in
    the Notifications tab.

    Principle: the raw activity stored here is always real data and is never invented by the
    AI. The AI's role (see POST /ai/context-bar) is only to weave these log rows into one
    narrative paragraph, and it must not add facts beyond what the table records.

    actor_name is stored as plain text (not a ForeignKey to users) so the log stays readable
    as written even if membership or the user's name changes later, the same way
    WorkspaceMember.invited_email keeps a copy of the email.

    project_id is NOT a ForeignKey to `projects`: that relational table is legacy, and the
    real project/task data lives as a JSON blob in AppState.data. Project ids used by the
    dashboard are strings such as "proj_1722789123456", so the column is a plain String.

    workspace_id (FK to workspaces, required) scopes access, like AppState.workspace_id,
    because a project is only a free-form string inside one workspace's JSON, not an
    independent row that could be a foreign key.

    Types in use: task_completed, comment_added, deadline_warning, unassigned_alert,
    status_changed, recurring_generated (written by the frontend each time a recurring task is
    completed and its next instance is created).
    """
    __tablename__ = "activity_logs"
    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    project_id = Column(String, nullable=False)
    type = Column(String, nullable=False)
    actor_name = Column(String, nullable=True)
    task_name = Column(String, nullable=True)
    detail = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    workspace = relationship("Workspace")


class AutomationRule(Base):
    """
    An automation rule ("when X happens, do Y") owned by one workspace. A generic trigger ->
    condition -> action pattern, so adding a trigger or action type later does not require a
    schema redesign.

    v1 is deliberately limited: 1 rule = 1 trigger + AT MOST 1 condition (a single AND, no
    nested AND/OR) + 1 action. The JSON text columns trigger_config / condition_config /
    action_config can hold richer structures later (multiple conditions, multiple actions,
    branching) without a schema migration; only the backend parsing changes, as with
    AppState.data.

    Supported trigger_type values in v1:
      - "task_status_changed": event-based; detected by diffing the old and new blob during
        POST /state, because Task/Project are legacy and no longer the real data source
      - "task_assigned": event-based, same detection mechanism
      - "task_overdue": time-based; checked periodically by the backend scheduler, not
        during /state
      - "deadline_approaching": time-based, same mechanism

    trigger_config: JSON text whose content depends on trigger_type, e.g. {"to_status":
    "Done"} for task_status_changed, {"days_before": 1} for deadline_approaching.

    condition_config: OPTIONAL, may be NULL (no extra condition beyond the trigger itself).
    JSON text holding a single condition, e.g. {"field": "priority", "operator": "equals",
    "value": "High"}.

    Supported action_type values in v1: "send_email", "change_status", "assign_user".
    action_config is JSON text whose content depends on action_type.

    project_id_filter: OPTIONAL. If set, the rule applies to one specific project only (a
    string id like ActivityLog.project_id; NOT a ForeignKey, since projects live only as keys
    inside the AppState.data JSON). If NULL, the rule applies to every project in the
    workspace.

    last_triggered_at: used by automation_engine as part of the anti-loop / anti-spam guard
    for TIME-BASED triggers. A rule whose condition stays true (for example a task that is
    already overdue) does not fire again on every scheduler run; it fires once, and again only
    after the condition flips back and becomes true again.
    """
    __tablename__ = "automation_rules"
    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    name = Column(String, nullable=False)

    trigger_type = Column(String, nullable=False)
    trigger_config = Column(String, nullable=True)  # JSON text

    condition_config = Column(String, nullable=True)  # JSON text, may be NULL

    action_type = Column(String, nullable=False)
    action_config = Column(String, nullable=True)  # JSON text

    project_id_filter = Column(String, nullable=True)

    enabled = Column(Boolean, nullable=False, default=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)

    workspace = relationship("Workspace")
    created_by = relationship("User")


class AutomationLog(Base):
    """
    History of every time an AutomationRule fired: it evaluated TRUE and ran its action,
    failed, or was deliberately skipped by the anti-loop guard. It serves two purposes:
      1. An audit trail for users ("why did this task change status by itself?" -> look at
        this log instead of guessing).
      2. Debugging material for developers, and input for the anti infinite-loop guard (see
        AutomationRule.last_triggered_at).

    workspace_id is copied directly (not just derived from rule_id) so "all automation logs in
    this workspace" needs no JOIN to automation_rules, the same pattern as
    ActivityLog.workspace_id.

    rule_name and task_name are stored as TEXT SNAPSHOTS, not just rule_id, so the log stays
    readable as written even if the rule is later renamed or deleted, the same pattern as
    actor_name in ActivityLog.

    rule_id is kept (nullable) for per-rule queries and filters while the rule exists. When a
    rule is deleted its old log rows are NOT deleted (history stays intact); handling orphaned
    rule_id values is the job of the DELETE rule endpoint in main.py, not a database
    constraint.

    result: "executed" | "skipped_loop_guard" | "failed".
    """
    __tablename__ = "automation_logs"
    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    rule_id = Column(Integer, ForeignKey("automation_rules.id"), nullable=True)
    rule_name = Column(String, nullable=False)
    project_id = Column(String, nullable=True)
    task_name = Column(String, nullable=True)
    result = Column(String, nullable=False)  # executed | skipped_loop_guard | failed
    detail = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    workspace = relationship("Workspace")


class ProjectMember(Base):
    """
    Per-project access for regular members.

    permission: "viewer" (view only) | "editor" (view + edit) | "manager" (view + edit +
    delete + manage project access).

    Workspace owners and admins are NOT stored here: they are automatically 'manager' on every
    project through WorkspaceMember.role.

    project_id is a string because project ids in the AppState blob are generated by the
    frontend ('proj_' + Date.now()).
    """
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("workspace_id", "project_id", "user_id", name="uq_project_member"),)

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    permission = Column(String, nullable=False, default="editor")  # viewer | editor | manager
    granted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Milestone(Base):
    """
    A major checkpoint within one project, separate from regular Tasks. It marks an important
    phase or release (for example "Beta Launch" or "Investor Demo") whose progress is computed
    automatically from the Tasks assigned to it.

    Own SQL table (not stacked into the AppState.data JSON blob), scoped to workspace_id +
    project_id. project_id is a String, NOT a ForeignKey, since projects are only keys in the
    blob, the same as project_id_filter in AutomationRule and project_id in ActivityLog.

    The link to Tasks is NOT stored here. Tasks live in tasksByProject[project_id] inside the
    AppState.data blob, so each task in this milestone stores milestoneId = str(milestone.id)
    on the task object itself (the same pattern as task.priority and task.deadline). GET
    /milestones (main.py) computes progress by reading the blob and counting tasks with this
    milestoneId whose status is 'done' against the total. The MVP deliberately does not store
    progress as a column, to avoid two sources of truth that could drift apart.

    A Task belongs to at most ONE Milestone (MVP decision): task.milestoneId is a single
    value, not an array.

    deadline: optional target date for this checkpoint.
    """
    __tablename__ = "milestones"
    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    deadline = Column(DateTime, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    workspace = relationship("Workspace")
    created_by = relationship("User")


class ProjectBudget(Base):
    """
    Total budget for one project. One row per (workspace, project). Same pattern as Milestone:
    its own SQL table, project_id as a String (not a ForeignKey; projects are only keys in the
    AppState.data blob), created automatically by create_all.
    Who may change it: the project manager level (the 'manage_access' action).
    """
    __tablename__ = "project_budgets"
    __table_args__ = (UniqueConstraint("workspace_id", "project_id", name="uq_project_budget"),)

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    total_amount = Column(Numeric(16, 2), nullable=False, default=0)
    currency = Column(String, nullable=False, default="IDR")
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BudgetEntry(Base):
    """
    One expense record in a project.

    Total spent, remaining amount, percentage and status are NOT stored as columns; they are
    computed from these rows (GET /budget), so there is no second source of truth.

    task_name: an optional text label ("which task is this expense for"). Deliberately text
    rather than a reference, because tasks in the blob have no stable id.
    """
    __tablename__ = "budget_entries"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    amount = Column(Numeric(16, 2), nullable=False)
    description = Column(String, nullable=False)
    task_name = Column(String, nullable=True)
    entry_date = Column(DateTime, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    created_by = relationship("User")


class AuditLog(Base):
    """
    Record of permission and membership changes: who (actor) did what (action, e.g.
    'project_access.grant') to whom or what (target_type + target_id), plus extra detail as
    JSON text.
    """
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String, nullable=False)
    target_type = Column(String, nullable=True)
    target_id = Column(String, nullable=True)
    detail = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FieldChangeLog(Base):
    """
    Per-field audit trail: records each time a Task/Project field changes (which field, old
    value, new value, who did it, and when). It differs from the two other log tables:
      - AuditLog: only permission and membership changes.
      - ActivityLog: a short narrative for notifications and the AI Context Bar, not a
        technical per-field history.
    This table backs the "History" panel in Task Detail, answering "when and by whom was this
    field changed, and from what to what".

    entity_name (not entity_id) identifies the task/project, because tasks have no stable id
    in the AppState blob. The consequence: if a task is renamed, history rows from before the
    rename keep the old entity_name, so the history is split by name rather than lost.
    """
    __tablename__ = "field_change_log"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    entity_type = Column(String, nullable=False)   # 'task' | 'project'
    entity_name = Column(String, nullable=False)   # name of the task/project at the time of the change
    field_name = Column(String, nullable=False)    # 'title'|'status'|'priority'|'deadline'|'category'|'assignee'|...
    old_value = Column(String, nullable=True)
    new_value = Column(String, nullable=True)
    actor_name = Column(String, nullable=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class AppStateSnapshot(Base):
    """
    Safety copy of the AppState blob BEFORE a save removes projects.

    Created automatically by POST /state (main.py) whenever a save makes a project disappear
    from the blob, including when the cause is a stale or empty browser tab overwriting server
    data. Used to restore deleted projects. Only the latest 30 snapshots per workspace are
    kept; older ones are pruned automatically.

    reason: "projects_removed" (automatic) | "before_restore" (taken before a restore).
    removed_project_ids: comma-separated ids of the projects that disappeared in that save.
    """
    __tablename__ = "app_state_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reason = Column(String, nullable=False, default="projects_removed")
    removed_project_ids = Column(String, nullable=True)
    data = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class WorkspaceInviteAccess(Base):
    """
    The project selection an inviter gave for ONE invitation.

    One row per invitation (invite_id is unique), created ONLY if the inviter explicitly lists
    projects in POST /workspace/invite:
      * no row: legacy behavior. When the invite is accepted, the member gets access to
        every existing project (open by default).
      * a row: when the invite is accepted, the member gets ONLY the projects in `grants`.
        An empty list '[]' means deliberately no access.

    grants: JSON text, e.g. [{"project_id": "proj_123", "permission": "editor"}].
    A separate table (not a new column on workspace_invites) so an already-running database
    only needs a new table through create_all, with no ALTER TABLE.
    """
    __tablename__ = "workspace_invite_access"

    id = Column(Integer, primary_key=True, index=True)
    invite_id = Column(Integer, ForeignKey("workspace_invites.id"), nullable=False, unique=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    grants = Column(String, nullable=False, default="[]")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Integration(Base):
    """
    Connection to a third-party service: Slack, GitHub, Google Calendar, and so on. One row
    per (workspace, provider). Connections are scoped to the WORKSPACE, not a project: connect
    once through OAuth, then every project in the workspace can use it. "Which channel does
    this project notify" is stored in default_channel_id for the MVP (one default channel per
    workspace); per-project mapping can come later through a field in the AppState.data blob
    (projects exist only as JSON keys, not independent table rows, which is also why Milestone
    and BudgetEntry reference project_id as a String, not a ForeignKey).

    access_token / refresh_token: encrypted at rest with the EncryptedText column type
    (secret_crypto.py, Fernet). In Python they read as plain text; in the database they are
    stored as "enc1:...".

    connected_by_user_id: who performed the OAuth connect, recorded for the audit trail. It is
    NOT the "owner" of the token: the token belongs to the workspace, and any owner/admin may
    disconnect, not only whoever connected first.

    status: "active" | "revoked" (manual disconnect by an owner/admin) | "error" (the token
    expired or was revoked on the provider side, flagged automatically when an API call to the
    provider returns an auth error such as Slack token_revoked/invalid_auth; also set when the
    stored token can no longer be decrypted, see _tandai_integrasi_tak_terbaca below).
    """
    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("workspace_id", "provider", name="uq_integration_workspace_provider"),)

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    provider = Column(String, nullable=False)  # "slack" | "github" | "google_calendar"

    # Transparently encrypted (secret_crypto.EncryptedText): plain text in Python, "enc1:..." in the database.
    access_token = Column(EncryptedText, nullable=False)
    refresh_token = Column(EncryptedText, nullable=True)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)

    external_account_name = Column(String, nullable=True)   # e.g. Slack workspace name / GitHub org
    default_channel_id = Column(String, nullable=True)      # Slack: default channel for notifications
    default_channel_name = Column(String, nullable=True)    # display name, e.g. "#general"
    extra_meta = Column(String, nullable=True)               # JSON text, provider-specific

    status = Column(String, nullable=False, default="active")  # active | revoked | error
    connected_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    workspace = relationship("Workspace")
    connected_by = relationship("User")


class WeeklyNote(Base):
    """
    Team weekly note for the Reports page (Weekly Summary). One row per (workspace, week). Its
    own SQL table, NOT stacked into the AppState.data blob, so a bug or save in this feature
    can never corrupt or overwrite task/project data and is not subject to state_guard rules.
    Created automatically by create_all (no ALTER TABLE).

    week_start: the MONDAY of that week, format 'YYYY-MM-DD' (validated by the server).
    version: increments by 1 on each save; PUT /weekly-notes uses it as a guard against two
    people editing at once (base_version must match, otherwise 409).
    locked / locked_by_user_id: a locked note can only be changed or unlocked by whoever
    locked it or by a workspace owner/admin.
    """
    __tablename__ = "weekly_notes"
    __table_args__ = (UniqueConstraint("workspace_id", "week_start", name="uq_weekly_note"),)

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    week_start = Column(String, nullable=False)
    body = Column(String, nullable=False, default="")
    version = Column(Integer, nullable=False, default=1)
    locked = Column(Boolean, nullable=False, default=False)
    locked_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WeeklySummary(Base):
    """
    CUSTOM text of the "Weekly Summary" on the Reports page. One row per (workspace, week).
    Its own SQL table, NOT in the AppState.data blob, so it cannot corrupt task/project data
    and is not subject to state_guard rules. Created automatically by create_all (no ALTER
    TABLE), the same pattern as WeeklyNote.

    week_start: the MONDAY of that week, 'YYYY-MM-DD' (validated by the server, same as
    WeeklyNote).
    body_html: HTML that the server has ALREADY sanitized (html_sanitizer.py); never write raw
    HTML here.
    version: increments by 1 on each save; guards against concurrent edits (base_version must
    match, otherwise 409).
    Only workspace owners/admins may write or delete; every active member may read. A deleted
    row means that week goes back to the Auto text.
    """
    __tablename__ = "weekly_summaries"
    __table_args__ = (UniqueConstraint("workspace_id", "week_start", name="uq_weekly_summary"),)

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    week_start = Column(String, nullable=False)
    body_html = Column(String, nullable=False, default="")
    version = Column(Integer, nullable=False, default=1)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())



class ReportSnapshot(Base):
    """
    Daily snapshot of Reports metrics per workspace (historical Period and Trend). One row per
    (workspace, date). Its own SQL table, NOT in the AppState.data blob, so it cannot corrupt
    task/project data and is not subject to state_guard rules. Created automatically by
    create_all (no ALTER TABLE).

    snap_date: 'YYYY-MM-DD' in the REPORT_SNAPSHOT_TZ time zone (default Asia/Jakarta).
    source: "daily" (computed by the server from the blob) | "import" (a one-time import of
    the old metricHistory in the blob; contains only workspace-wide totals).
    data: JSON text. daily -> {"v":1,"projects":[one row per project]}; import ->
    {"v":1,"legacy":true,...}.
    """
    __tablename__ = "report_snapshots"
    __table_args__ = (UniqueConstraint("workspace_id", "snap_date", name="uq_report_snapshot"),)

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    snap_date = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False, default="daily")
    data = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ReleaseNote(Base):
    """
    "What's New" announcements: a list of feature updates shown to all users. GLOBAL (not per
    workspace): one list for the whole application. Only emails listed in the
    RELEASE_NOTES_ADMINS environment variable may add or delete entries (see POST/DELETE
    /releases in main.py).

    category: "feature" | "improvement" | "fix".
    The per-user "already read" state is kept in the browser (the highest release id seen), so
    this table needs no read-tracking column or table. Created automatically by create_all (no
    ALTER TABLE).
    """
    __tablename__ = "release_notes"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    body = Column(String, nullable=False, default="")
    category = Column(String, nullable=False, default="feature")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    published_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class ProjectAISetting(Base):
    """
    Per-project opt-in for the AI Analyzer: the ONLY source of truth for whether the AI may
    read this project's data. The setting lives on the server, not in the browser, so
    "Analyzer off = the AI reads no data at all" is something the backend can actually
    guarantee. Every /ai/* endpoint that reads project data checks this table through
    ai_gate.py BEFORE assembling the prompt.

    No row = OFF (default). A row is created the first time the Analyzer is turned on.
    project_id is a String (not a ForeignKey); projects are only keys in the AppState.data
    blob, the same as Milestone and ProjectBudget. Created automatically by create_all (no
    ALTER TABLE).
    """
    __tablename__ = "project_ai_settings"
    __table_args__ = (UniqueConstraint("workspace_id", "project_id", name="uq_project_ai_setting"),)

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    analyzer_enabled = Column(Boolean, nullable=False, default=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class UserDashboardAISetting(Base):
    """
    Per-USER (per workspace) opt-in for the "AI Dashboard": the switch for the dashboard AI
    Chat and the dashboard Insight panel (Risk Alert / Recommendation). SEPARATE from
    ProjectAISetting (the AI Analyzer inside a project): turning one on does not turn the
    other on.

    No row = OFF (default). A row is created the first time it is turned on. Created
    automatically by create_all (no ALTER TABLE), the same as ProjectAISetting.
    """
    __tablename__ = "user_dashboard_ai_settings"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_user_dashboard_ai_setting"),)

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    dashboard_enabled = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AIChatConversation(Base):
    """
    AI chat history per USER, stored on the server (not in the browser). One row = one
    conversation. scope "dashboard" = the dashboard AI chat (project_id empty); scope
    "project" = the AI chat inside one project. The title and message text are encrypted
    (EncryptedText, the same as integration tokens). Only the owner can read them: every query
    in main.py filters by user_id. Old conversations are deleted automatically (see
    _CHAT_HIST_RETENTION_DAYS). Created automatically by create_all (no ALTER TABLE).
    """
    __tablename__ = "ai_chat_conversations"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    scope = Column(String, nullable=False)
    project_id = Column(String, nullable=True, index=True)
    title = Column(EncryptedText, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AIChatMessage(Base):
    """
    One message inside an AIChatConversation. Images are NOT stored: only file names
    (img_names, JSON text), so the history knows an image attachment was there. The message
    text is encrypted.
    """
    __tablename__ = "ai_chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("ai_chat_conversations.id"), nullable=False, index=True)
    role = Column(String, nullable=False)
    text = Column(EncryptedText, nullable=True)
    img_names = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# Safeguard for a changed or lost encryption key: if access_token cannot be decrypted,
# EncryptedText returns None (see secret_crypto.py). The column is NOT NULL, so None means
# "unreadable". The in-memory row is marked status="error" so that (1) the Integrations panel
# shows "reconnect", (2) automation_engine, which checks status == "active", does not use an
# empty token and records a visible "failed" log entry instead, and (3) the disconnect and
# reconnect buttons keep working. The status is persisted on the next commit; the token itself
# is never rewritten.
def _tandai_integrasi_tak_terbaca(target, *args):
    if target.access_token is None and target.status != "error":
        target.status = "error"


event.listen(Integration, "load", _tandai_integrasi_tak_terbaca)
event.listen(Integration, "refresh", _tandai_integrasi_tak_terbaca)

class PushSubscription(Base):
    """
    Browser push notification (Web Push) subscription for one DEVICE/browser of one user.

    A user may have many rows (laptop, phone, another browser). A row is created by POST
    /push/subscribe after the user clicks "Enable desktop notifications" and grants the
    browser permission. It is deleted by POST /push/unsubscribe, or automatically by
    push_service when the push service (Google/Apple/Mozilla) reports that the subscription is
    no longer valid (HTTP 404/410).

    endpoint: the unique subscription address from the browser (used as a unique key; do not
    share it).
    p256dh, auth: the browser's message-encryption keys. Both are only useful together with
    the endpoint.
    Created automatically by create_all (no ALTER TABLE).
    """
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    endpoint = Column(String, nullable=False, unique=True, index=True)
    p256dh = Column(String, nullable=False)
    auth = Column(String, nullable=False)
    user_agent = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())