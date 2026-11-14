"""
Tests for permissions.py. SAFE: forces a temporary SQLite database, so it never touches
Supabase or alsa.db even if the environment sets DATABASE_URL.

Run from the repository root:
    PYTHONPATH=src python tests/test_permissions.py             (macOS / Linux)
    $env:PYTHONPATH = "src"; python tests\test_permissions.py   (PowerShell)
"""
import os
import sys
import tempfile

# MUST come before importing database/models: force a temporary database.
_tmp = os.path.join(tempfile.mkdtemp(), "test_permissions.db")
os.environ["DATABASE_URL"] = "sqlite:///" + _tmp

from database import Base, engine, SessionLocal  # noqa: E402
import models  # noqa: E402
import permissions as P  # noqa: E402
from fastapi import HTTPException  # noqa: E402

assert engine.url.drivername == "sqlite", "STOP: this test must never run against a non-SQLite database!"

Base.metadata.create_all(bind=engine)
db = SessionLocal()

failed = 0


def check(name, cond):
    global failed
    print(("  OK   " if cond else "  FAIL ") + name)
    if not cond:
        failed += 1


# --- Test data ---
OWNER, ADMIN, EDITOR, VIEWER, MANAGER, NOACCESS, OUTSIDER = 1, 2, 3, 4, 5, 6, 7
for uid in range(1, 8):
    db.add(models.User(id=uid, name=f"U{uid}", email=f"u{uid}@x.com", hashed_password="x"))
db.commit()
db.add(models.Workspace(id=1, name="WS", owner_id=OWNER))
db.add(models.Workspace(id=2, name="Other WS", owner_id=OUTSIDER))
db.commit()
for uid, role in [(OWNER, "owner"), (ADMIN, "admin"), (EDITOR, "member"), (VIEWER, "member"),
                  (MANAGER, "member"), (NOACCESS, "member")]:
    db.add(models.WorkspaceMember(workspace_id=1, user_id=uid, role=role, status="active"))
db.add(models.WorkspaceMember(workspace_id=2, user_id=OUTSIDER, role="owner", status="active"))
db.add(models.WorkspaceMember(workspace_id=1, user_id=None, role="member", status="pending", invited_email="p@x.com"))
db.add(models.ProjectMember(workspace_id=1, project_id="proj_A", user_id=EDITOR, permission="editor"))
db.add(models.ProjectMember(workspace_id=1, project_id="proj_A", user_id=VIEWER, permission="viewer"))
db.add(models.ProjectMember(workspace_id=1, project_id="proj_A", user_id=MANAGER, permission="manager"))
db.add(models.ProjectMember(workspace_id=1, project_id="proj_B", user_id=EDITOR, permission="editor"))
db.add(models.ProjectMember(workspace_id=1, project_id="proj_A", user_id=NOACCESS, permission="bogus"))  # corrupt level
db.commit()

print("Owner & admin: full access to every project")
for u in (OWNER, ADMIN):
    for act in ("view", "edit", "delete", "manage_access"):
        check(f"user {u} may {act} proj_A", P.can(db, u, 1, "proj_A", act))
    check(f"user {u} may delete a project that has no project_members row (proj_Z)", P.can(db, u, 1, "proj_Z", "delete"))

print("Editor: may view and edit, may not delete or manage access")
check("view", P.can(db, EDITOR, 1, "proj_A", "view"))
check("edit", P.can(db, EDITOR, 1, "proj_A", "edit"))
check("cannot delete", not P.can(db, EDITOR, 1, "proj_A", "delete"))
check("cannot manage_access", not P.can(db, EDITOR, 1, "proj_A", "manage_access"))

print("Viewer: view only")
check("view", P.can(db, VIEWER, 1, "proj_A", "view"))
check("cannot edit", not P.can(db, VIEWER, 1, "proj_A", "edit"))
check("cannot delete", not P.can(db, VIEWER, 1, "proj_A", "delete"))

print("Per-project manager (regular member)")
for act in ("view", "edit", "delete", "manage_access"):
    check(f"may {act}", P.can(db, MANAGER, 1, "proj_A", act))

print("Member without an access row = no access at all")
check("EDITOR cannot view proj_C (not listed)", not P.can(db, EDITOR, 1, "proj_C", "view"))
check("VIEWER cannot view proj_B", not P.can(db, VIEWER, 1, "proj_B", "view"))

print("Fail-closed")
check("corrupt level 'bogus' grants no permission", not P.can(db, NOACCESS, 1, "proj_A", "view"))
check("unknown action is denied", not P.can(db, EDITOR, 1, "proj_A", "hack"))
check("non-member of the workspace is denied", not P.can(db, OUTSIDER, 1, "proj_A", "view"))
check("owner of another workspace cannot reach workspace 1", not P.can(db, OUTSIDER, 1, "proj_A", "edit"))

print("require(): the 403 messages are distinguished")
# The 403 messages are user-facing UI text and stay in Indonesian ("akses" = access, "izin" = permission).
try:
    P.require(db, EDITOR, 1, "proj_C", "view")
    check("require without access must raise", False)
except HTTPException as e:
    check("no access -> 403 'no access'", e.status_code == 403 and "akses" in e.detail)
try:
    P.require(db, EDITOR, 1, "proj_A", "delete")
    check("require with insufficient level must raise", False)
except HTTPException as e:
    check("insufficient level -> 403 'no permission'", e.status_code == 403 and "izin" in e.detail)
check("require succeeds and returns the level", P.require(db, EDITOR, 1, "proj_A", "edit") == "editor")

print("accessible_project_ids(): the basis of the /state filter")
allp = ["proj_A", "proj_B", "proj_C"]
check("owner sees all", P.accessible_project_ids(db, OWNER, 1, allp) == {"proj_A", "proj_B", "proj_C"})
check("admin sees all", P.accessible_project_ids(db, ADMIN, 1, allp) == {"proj_A", "proj_B", "proj_C"})
check("editor sees A and B only", P.accessible_project_ids(db, EDITOR, 1, allp) == {"proj_A", "proj_B"})
check("viewer sees A only", P.accessible_project_ids(db, VIEWER, 1, allp) == {"proj_A"})
check("non-member: empty", P.accessible_project_ids(db, OUTSIDER, 1, allp) == set())
check("numeric project ids still match (normalized to strings)", P.accessible_project_ids(db, OWNER, 1, [1, 2]) == {"1", "2"})

print("log_audit()")
P.log_audit(db, 1, OWNER, "project_access.grant", "project", "proj_A", {"user_id": EDITOR, "level": "editor"}, commit=True)
row = db.query(models.AuditLog).first()
check("audit row is recorded completely", row and row.action == "project_access.grant" and row.target_id == "proj_A" and row.actor_user_id == OWNER)

db.close()
print("\n" + ("ALL TESTS PASSED" if failed == 0 else f"{failed} TESTS FAILED"))
sys.exit(1 if failed else 0)