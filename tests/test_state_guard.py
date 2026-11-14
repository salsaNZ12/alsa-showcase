"""
Unit tests for state_guard (pure functions, no database at all, safe to run anywhere).

    PYTHONPATH=src python tests/test_state_guard.py
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tempfile  # noqa: E402
# database.py needs a value when it is imported (not used in this test). It is forced to a
# temporary file so it never touches a real database, even if .env or the environment sets DATABASE_URL.
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "unused.db")
import state_guard as G  # noqa: E402

failed = 0
def check(name, cond):
    global failed
    print(("  OK    " if cond else "  FAIL  ") + name)
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
check("ordered and unique, active first then archived", ids(blob()) == ["p1", "p2", "p3", "p4"])
check("numeric ids are normalized to strings", G.project_ids_in({"projectsData": {"active": [{"id": 7}], "archived": []}}) == ["7"])
check("corrupt blob -> empty (no crash)", G.project_ids_in(None) == [] and G.project_ids_in({"projectsData": "x"}) == [] and G.project_ids_in({"projectsData": {"active": "x"}}) == [])
check("visible_ids: viewer/editor/manager may view, a corrupt level may not", G.visible_ids(["p1", "p2", "p3", "p4"], {"p1": "viewer", "p2": "editor", "p3": "manager", "p4": "bogus"}) == {"p1", "p2", "p3"})

print("filter_state_for_member")
b = blob(); orig = copy.deepcopy(b)
out = G.filter_state_for_member(b, {"p1", "p4"})
check("only p1 and p4 (including the archived one)", ids(out) == ["p1", "p4"])
check("original blob is not modified", b == orig)
check("tasksByProject is filtered", set(out["tasksByProject"]) == {"p1", "p4"})
check("filtering with an empty visible set -> no projects", ids(G.filter_state_for_member(blob(), set())) == [])
check("non-dict is returned as-is", G.filter_state_for_member(None, {"p1"}) is None)
b2 = blob(); b2["recentlyAccessed"] = [{"id": "p1"}, {"id": "ghost"}, {"id": "p2"}, {"note": "no id"}]
check("recentlyAccessed: allow-list (unknown and hidden ids are dropped)", G.filter_state_for_member(b2, {"p1"})["recentlyAccessed"] == [{"id": "p1"}, {"note": "no id"}])

print("merge_member_payload")
# a) the member can only see p1 (editor); sends back the filtered version with a change
old = blob()
payload = G.filter_state_for_member(old, {"p1"}); payload["tasksByProject"]["p1"].append("t1-new")
res = G.merge_member_payload(old, payload, {"p1": "editor"})
check("change to p1 is accepted", payload["tasksByProject"]["p1"] == ["t1", "t1-new"])
check("p2, p3, p4 are restored intact", ids(payload) == ["p1", "p2", "p3", "p4"] and payload["tasksByProject"]["p2"] == ["t2"] and payload["tasksByProject"]["p4"] == ["t4"])
check("p4 goes back to the archived bucket", [p["id"] for p in payload["projectsData"]["archived"]] == ["p4"])
check("not treated as 'corrected' (only hidden projects were missing)", res.corrected is False)
check("resulting visible_ids = {p1}", res.visible_ids == {"p1"})

# b) the position of hidden projects is preserved
old = blob()
payload = G.filter_state_for_member(old, {"p1", "p3"})
G.merge_member_payload(old, payload, {"p1": "editor", "p3": "editor"})
check("original active order p1,p2,p3 is preserved", [p["id"] for p in payload["projectsData"]["active"]] == ["p1", "p2", "p3"])

# c) viewer: the edit is reverted and the result is flagged as corrected
old = blob()
payload = G.filter_state_for_member(old, {"p1"}); payload["tasksByProject"]["p1"] = ["edited"]
res = G.merge_member_payload(old, payload, {"p1": "viewer"})
check("viewer: task is reverted to the original", payload["tasksByProject"]["p1"] == ["t1"])
check("viewer: corrected=True (the client needs to resync)", res.corrected is True)
payload = G.filter_state_for_member(old, {"p1"})
res = G.merge_member_payload(old, payload, {"p1": "viewer"})
check("viewer changes nothing: corrected=False", res.corrected is False)

# d) deleting a project the member can see -> restored and flagged as corrected
old = blob()
payload = G.filter_state_for_member(old, {"p1"}); payload["projectsData"]["active"] = []; payload["tasksByProject"] = {}
res = G.merge_member_payload(old, payload, {"p1": "editor"})
check("p1 is restored (members cannot delete)", "p1" in ids(payload) and payload["tasksByProject"]["p1"] == ["t1"])
check("corrected=True", res.corrected is True)

# e) attack: claiming to own a hidden project
old = blob()
payload = G.filter_state_for_member(old, {"p1"})
payload["projectsData"]["active"].append({"id": "p2", "name": "HIJACKED"}); payload["tasksByProject"]["p2"] = ["INJECTED"]
res = G.merge_member_payload(old, payload, {"p1": "editor"})
check("hidden p2 cannot be overwritten", [p["name"] for p in payload["projectsData"]["active"] if p["id"] == "p2"] == ["Two"] and payload["tasksByProject"]["p2"] == ["t2"])
check("p2 is not treated as a new project created by the member", "p2" not in res.created_ids)

# f) new project
old = blob()
payload = G.filter_state_for_member(old, {"p1"})
payload["projectsData"]["active"].insert(0, {"id": "pNEW", "name": "New"}); payload["tasksByProject"]["pNEW"] = ["x"]
res = G.merge_member_payload(old, payload, {"p1": "editor"})
check("pNEW is recognized as new", res.created_ids == ["pNEW"])
check("pNEW is stored and visible to its creator", "pNEW" in ids(payload) and "pNEW" in res.visible_ids and payload["tasksByProject"]["pNEW"] == ["x"])

# g) currentProjectId pointing at a hidden project
old = blob()
payload = G.filter_state_for_member(old, {"p1"}); payload["currentProjectId"] = "p2"
G.merge_member_payload(old, payload, {"p1": "editor"})
check("currentProjectId pointing at a hidden project is set to None", payload["currentProjectId"] is None)

# h) malformed or extreme payloads must not crash and must fail closed
for label, bad in [("projectsData is not a dict", {"projectsData": "x"}), ("empty", {}), ("active is not a list", {"projectsData": {"active": 5}}),
                   ("tasksByProject is not a dict", {"tasksByProject": [1, 2]}), ("project entry is not a dict", {"projectsData": {"active": ["x", 3, None]}})]:
    old = blob()
    try:
        pl = copy.deepcopy(bad)
        G.merge_member_payload(old, pl, {"p1": "editor"})
        check(f"payload '{label}': no crash, all existing projects remain", ids(pl) == ["p1", "p2", "p3", "p4"])
    except Exception as e:
        check(f"payload '{label}': no crash ({type(e).__name__}: {e})", False)

# i) no previous blob (new workspace)
payload = {"projectsData": {"active": [{"id": "pX"}], "archived": []}}
res = G.merge_member_payload(None, payload, {})
check("no previous blob: every project counts as new", res.created_ids == ["pX"] and "pX" in res.visible_ids)

# j) numeric id in the blob, string key in the dict (JSON keys are always strings)
old = {"projectsData": {"active": [{"id": 5, "name": "Five"}], "archived": []}, "tasksByProject": {"5": ["t"]}}
payload = {"projectsData": {"active": [], "archived": []}, "tasksByProject": {}}
G.merge_member_payload(old, payload, {"5": "editor"})
check("numeric id: restored, and per-project data matches via the string key", ids(payload) == ["5"] and payload["tasksByProject"]["5"] == ["t"])

print("\n" + ("ALL TESTS PASSED" if failed == 0 else f"{failed} TESTS FAILED"))
sys.exit(1 if failed else 0)