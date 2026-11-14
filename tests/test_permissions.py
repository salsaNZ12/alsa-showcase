"""
Tes untuk permissions.py. AMAN: memaksa database SQLite sementara, jadi tidak
pernah menyentuh Supabase/alsa.db walaupun .env berisi DATABASE_URL.

Jalankan dari folder backend:
    python test_permissions.py
"""
import os
import sys
import tempfile

# WAJIB sebelum import database/models: paksa DB sementara.
_tmp = os.path.join(tempfile.mkdtemp(), "test_permissions.db")
os.environ["DATABASE_URL"] = "sqlite:///" + _tmp

from database import Base, engine, SessionLocal  # noqa: E402
import models  # noqa: E402
import permissions as P  # noqa: E402
from fastapi import HTTPException  # noqa: E402

assert engine.url.drivername == "sqlite", "STOP: tes ini tidak boleh jalan di database non-SQLite!"

Base.metadata.create_all(bind=engine)
db = SessionLocal()

failed = 0


def check(name, cond):
    global failed
    print(("  OK   " if cond else "  GAGAL ") + name)
    if not cond:
        failed += 1


# --- Data uji ---
OWNER, ADMIN, EDITOR, VIEWER, MANAGER, NOACCESS, OUTSIDER = 1, 2, 3, 4, 5, 6, 7
for uid in range(1, 8):
    db.add(models.User(id=uid, name=f"U{uid}", email=f"u{uid}@x.com", hashed_password="x"))
db.commit()
db.add(models.Workspace(id=1, name="WS", owner_id=OWNER))
db.add(models.Workspace(id=2, name="WS lain", owner_id=OUTSIDER))
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
db.add(models.ProjectMember(workspace_id=1, project_id="proj_A", user_id=NOACCESS, permission="ngawur"))  # level rusak
db.commit()

print("Owner & admin: akses penuh ke semua project")
for u in (OWNER, ADMIN):
    for act in ("view", "edit", "delete", "manage_access"):
        check(f"user {u} boleh {act} proj_A", P.can(db, u, 1, "proj_A", act))
    check(f"user {u} boleh delete project yang tidak ada di project_members (proj_Z)", P.can(db, u, 1, "proj_Z", "delete"))

print("Editor: boleh lihat+edit, tidak boleh hapus/atur akses")
check("view", P.can(db, EDITOR, 1, "proj_A", "view"))
check("edit", P.can(db, EDITOR, 1, "proj_A", "edit"))
check("TIDAK boleh delete", not P.can(db, EDITOR, 1, "proj_A", "delete"))
check("TIDAK boleh manage_access", not P.can(db, EDITOR, 1, "proj_A", "manage_access"))

print("Viewer: hanya lihat")
check("view", P.can(db, VIEWER, 1, "proj_A", "view"))
check("TIDAK boleh edit", not P.can(db, VIEWER, 1, "proj_A", "edit"))
check("TIDAK boleh delete", not P.can(db, VIEWER, 1, "proj_A", "delete"))

print("Manager per project (member biasa)")
for act in ("view", "edit", "delete", "manage_access"):
    check(f"boleh {act}", P.can(db, MANAGER, 1, "proj_A", act))

print("Member tanpa baris akses = tidak punya akses sama sekali")
check("EDITOR tidak boleh view proj_C (tidak terdaftar)", not P.can(db, EDITOR, 1, "proj_C", "view"))
check("VIEWER tidak boleh view proj_B", not P.can(db, VIEWER, 1, "proj_B", "view"))

print("Fail-closed")
check("level rusak 'ngawur' tidak punya izin apa pun", not P.can(db, NOACCESS, 1, "proj_A", "view"))
check("action tidak dikenal ditolak", not P.can(db, EDITOR, 1, "proj_A", "hack"))
check("bukan anggota workspace -> tidak boleh", not P.can(db, OUTSIDER, 1, "proj_A", "view"))
check("owner workspace lain tidak bisa menembus workspace 1", not P.can(db, OUTSIDER, 1, "proj_A", "edit"))

print("require(): pesan 403 dibedakan")
try:
    P.require(db, EDITOR, 1, "proj_C", "view")
    check("require tanpa akses harus raise", False)
except HTTPException as e:
    check("tanpa akses -> 403 'tidak punya akses'", e.status_code == 403 and "akses" in e.detail)
try:
    P.require(db, EDITOR, 1, "proj_A", "delete")
    check("require level kurang harus raise", False)
except HTTPException as e:
    check("level kurang -> 403 'tidak punya izin'", e.status_code == 403 and "izin" in e.detail)
check("require sukses mengembalikan level", P.require(db, EDITOR, 1, "proj_A", "edit") == "editor")

print("accessible_project_ids(): dasar filter /state")
allp = ["proj_A", "proj_B", "proj_C"]
check("owner lihat semua", P.accessible_project_ids(db, OWNER, 1, allp) == {"proj_A", "proj_B", "proj_C"})
check("admin lihat semua", P.accessible_project_ids(db, ADMIN, 1, allp) == {"proj_A", "proj_B", "proj_C"})
check("editor lihat A & B saja", P.accessible_project_ids(db, EDITOR, 1, allp) == {"proj_A", "proj_B"})
check("viewer lihat A saja", P.accessible_project_ids(db, VIEWER, 1, allp) == {"proj_A"})
check("bukan anggota: kosong", P.accessible_project_ids(db, OUTSIDER, 1, allp) == set())
check("id project angka tetap cocok (dinormalisasi ke string)", P.accessible_project_ids(db, OWNER, 1, [1, 2]) == {"1", "2"})

print("log_audit()")
P.log_audit(db, 1, OWNER, "project_access.grant", "project", "proj_A", {"user_id": EDITOR, "level": "editor"}, commit=True)
row = db.query(models.AuditLog).first()
check("baris audit tercatat lengkap", row and row.action == "project_access.grant" and row.target_id == "proj_A" and row.actor_user_id == OWNER)

db.close()
print("\n" + ("SEMUA TES LULUS" if failed == 0 else f"{failed} TES GAGAL"))
sys.exit(1 if failed else 0)