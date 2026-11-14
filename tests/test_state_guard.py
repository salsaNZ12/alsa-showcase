"""
Tes unit state_guard (fungsi murni, tanpa database sama sekali -- aman dijalankan di mana pun).

    python test_state_guard.py
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tempfile  # noqa: E402
# database.py butuh nilai saat di-import (tidak dipakai di tes ini). Dipaksa ke file sementara
# supaya tidak pernah menyentuh database asli walau .env / environment berisi DATABASE_URL.
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "unused.db")
import state_guard as G  # noqa: E402

failed = 0
def check(name, cond):
    global failed
    print(("  OK    " if cond else "  GAGAL ") + name)
    if not cond:
        failed += 1

def blob():
    return {
        "projectsData": {
            "active": [{"id": "p1", "name": "One"}, {"id": "p2", "name": "Two"}, {"id": "p3", "name": "Three"}],
            "archived": [{"id": "p4", "name": "Four"}],
        },
        "tasksByProject": {"p1": ["t1"], "p2": ["t2"], "p3": ["t3"], "p4": ["t4"]},
        "currentProjectId": "p1",
    }

def ids(b):
    return G.project_ids_in(b)

print("project_ids_in / visible_ids")
check("urut & unik, active lalu archived", ids(blob()) == ["p1", "p2", "p3", "p4"])
check("id angka dinormalisasi jadi string", G.project_ids_in({"projectsData": {"active": [{"id": 7}], "archived": []}}) == ["7"])
check("blob rusak -> kosong (tidak crash)", G.project_ids_in(None) == [] and G.project_ids_in({"projectsData": "x"}) == [] and G.project_ids_in({"projectsData": {"active": "x"}}) == [])
check("visible_ids: viewer/editor/manager boleh lihat, level rusak tidak", G.visible_ids(["p1", "p2", "p3", "p4"], {"p1": "viewer", "p2": "editor", "p3": "manager", "p4": "aneh"}) == {"p1", "p2", "p3"})

print("filter_state_for_member")
b = blob(); orig = copy.deepcopy(b)
out = G.filter_state_for_member(b, {"p1", "p4"})
check("hanya p1 & p4 (termasuk yang di archived)", ids(out) == ["p1", "p4"])
check("blob asli tidak diubah", b == orig)
check("tasksByProject tersaring", set(out["tasksByProject"]) == {"p1", "p4"})
check("filter dengan visible kosong -> tanpa project", ids(G.filter_state_for_member(blob(), set())) == [])
check("bukan dict dikembalikan apa adanya", G.filter_state_for_member(None, {"p1"}) is None)
b2 = blob(); b2["recentlyAccessed"] = [{"id": "p1"}, {"id": "hantu"}, {"id": "p2"}, {"note": "tanpa id"}]
check("recentlyAccessed: daftar putih (id tak dikenal & tersembunyi dibuang)", G.filter_state_for_member(b2, {"p1"})["recentlyAccessed"] == [{"id": "p1"}, {"note": "tanpa id"}])

print("merge_member_payload")
# a) member hanya lihat p1 (editor); kirim balik versi terfilter dengan perubahan
old = blob()
payload = G.filter_state_for_member(old, {"p1"}); payload["tasksByProject"]["p1"].append("t1-baru")
res = G.merge_member_payload(old, payload, {"p1": "editor"})
check("perubahan p1 diterima", payload["tasksByProject"]["p1"] == ["t1", "t1-baru"])
check("p2,p3,p4 dipulihkan utuh", ids(payload) == ["p1", "p2", "p3", "p4"] and payload["tasksByProject"]["p2"] == ["t2"] and payload["tasksByProject"]["p4"] == ["t4"])
check("p4 kembali ke bucket archived", [p["id"] for p in payload["projectsData"]["archived"]] == ["p4"])
check("tidak dianggap 'dikoreksi' (yang hilang cuma yang tersembunyi)", res.corrected is False)
check("visible_ids hasil = {p1}", res.visible_ids == {"p1"})

# b) urutan posisi project tersembunyi dipertahankan
old = blob()
payload = G.filter_state_for_member(old, {"p1", "p3"})
G.merge_member_payload(old, payload, {"p1": "editor", "p3": "editor"})
check("urutan active asli p1,p2,p3 dipertahankan", [p["id"] for p in payload["projectsData"]["active"]] == ["p1", "p2", "p3"])

# c) viewer: edit dibatalkan + corrected
old = blob()
payload = G.filter_state_for_member(old, {"p1"}); payload["tasksByProject"]["p1"] = ["diedit"]
res = G.merge_member_payload(old, payload, {"p1": "viewer"})
check("viewer: task kembali ke asli", payload["tasksByProject"]["p1"] == ["t1"])
check("viewer: corrected=True (client perlu disinkron)", res.corrected is True)
payload = G.filter_state_for_member(old, {"p1"})
res = G.merge_member_payload(old, payload, {"p1": "viewer"})
check("viewer tidak mengubah apa-apa: corrected=False", res.corrected is False)

# d) hapus project yang boleh dilihat -> dipulihkan + corrected
old = blob()
payload = G.filter_state_for_member(old, {"p1"}); payload["projectsData"]["active"] = []; payload["tasksByProject"] = {}
res = G.merge_member_payload(old, payload, {"p1": "editor"})
check("p1 dipulihkan (member tak bisa hapus)", "p1" in ids(payload) and payload["tasksByProject"]["p1"] == ["t1"])
check("corrected=True", res.corrected is True)

# e) serangan: mengaku punya project tersembunyi
old = blob()
payload = G.filter_state_for_member(old, {"p1"})
payload["projectsData"]["active"].append({"id": "p2", "name": "DIBAJAK"}); payload["tasksByProject"]["p2"] = ["INJEKSI"]
res = G.merge_member_payload(old, payload, {"p1": "editor"})
check("p2 tersembunyi tidak bisa ditimpa", [p["name"] for p in payload["projectsData"]["active"] if p["id"] == "p2"] == ["Two"] and payload["tasksByProject"]["p2"] == ["t2"])
check("p2 tidak dianggap project baru buatan member", "p2" not in res.created_ids)

# f) project baru
old = blob()
payload = G.filter_state_for_member(old, {"p1"})
payload["projectsData"]["active"].insert(0, {"id": "pNEW", "name": "Baru"}); payload["tasksByProject"]["pNEW"] = ["x"]
res = G.merge_member_payload(old, payload, {"p1": "editor"})
check("pNEW dikenali sebagai baru", res.created_ids == ["pNEW"])
check("pNEW ikut tersimpan & terlihat oleh pembuatnya", "pNEW" in ids(payload) and "pNEW" in res.visible_ids and payload["tasksByProject"]["pNEW"] == ["x"])

# g) currentProjectId ke project tersembunyi
old = blob()
payload = G.filter_state_for_member(old, {"p1"}); payload["currentProjectId"] = "p2"
G.merge_member_payload(old, payload, {"p1": "editor"})
check("currentProjectId ke project tersembunyi di-null-kan", payload["currentProjectId"] is None)

# h) payload jelek/ekstrem tidak bikin crash & fail-closed
for label, bad in [("projectsData bukan dict", {"projectsData": "x"}), ("kosong", {}), ("active bukan list", {"projectsData": {"active": 5}}),
                   ("tasksByProject bukan dict", {"tasksByProject": [1, 2]}), ("entri project bukan dict", {"projectsData": {"active": ["x", 3, None]}})]:
    old = blob()
    try:
        pl = copy.deepcopy(bad)
        G.merge_member_payload(old, pl, {"p1": "editor"})
        check(f"payload '{label}': tidak crash, semua project lama tetap ada", ids(pl) == ["p1", "p2", "p3", "p4"])
    except Exception as e:
        check(f"payload '{label}': tidak crash ({type(e).__name__}: {e})", False)

# i) tanpa blob lama (workspace baru)
payload = {"projectsData": {"active": [{"id": "pX"}], "archived": []}}
res = G.merge_member_payload(None, payload, {})
check("tanpa blob lama: semua project dianggap baru", res.created_ids == ["pX"] and "pX" in res.visible_ids)

# j) id angka di blob, key string di dict (JSON selalu string)
old = {"projectsData": {"active": [{"id": 5, "name": "Lima"}], "archived": []}, "tasksByProject": {"5": ["t"]}}
payload = {"projectsData": {"active": [], "archived": []}, "tasksByProject": {}}
G.merge_member_payload(old, payload, {"5": "editor"})
check("id angka: dipulihkan & data per-project cocok lewat key string", ids(payload) == ["5"] and payload["tasksByProject"]["5"] == ["t"])

print("\n" + ("SEMUA TES LULUS" if failed == 0 else f"{failed} TES GAGAL"))
sys.exit(1 if failed else 0)