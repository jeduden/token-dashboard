import http.server
import json
import os
import socket
import sqlite3
import stat
import tempfile
import threading
import unittest
import urllib.request

from token_dashboard.db import init_db, project_summary
from token_dashboard.repo_resolve import resolve_paths, encode_slug
from token_dashboard.repos import repo_summary, annotate_projects
from token_dashboard.server import build_handler


def _w(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def make_repo(root):
    os.makedirs(os.path.join(root, ".git", "objects"))
    _w(os.path.join(root, ".git", "HEAD"), "ref: refs/heads/main\n")
    return root


def make_worktree(repo, wt, name, with_commondir=True):
    gd = os.path.join(repo, ".git", "worktrees", name)
    os.makedirs(gd)
    if with_commondir:
        _w(os.path.join(gd, "commondir"), "../..\n")
    os.makedirs(wt)
    _w(os.path.join(wt, ".git"), "gitdir: " + gd + "\n")
    return wt


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())

    def p(self, *a):
        return os.path.join(self.tmp, *a)

    def one(self, path, extra=()):
        return resolve_paths([path, *extra])[path]

    def test_normal_repo_and_subdir(self):
        r = make_repo(self.p("alpha"))
        os.makedirs(self.p("alpha", "src", "deep"))
        a = self.one(r)
        self.assertEqual((a["root"], a["name"], a["confidence"]), (r, "alpha", "git"))
        b = self.one(self.p("alpha", "src", "deep"))
        self.assertEqual(b["key"], a["key"])
        self.assertEqual(b["confidence"], "git")

    def test_linked_worktree_with_commondir(self):
        r = make_repo(self.p("alpha"))
        wt = make_worktree(r, self.p("wt", "feat"), "feat")
        got = resolve_paths([r, wt])
        self.assertEqual(got[wt]["key"], got[r]["key"])
        self.assertEqual(got[wt]["root"], r)
        self.assertEqual(got[wt]["confidence"], "git")

    def test_linked_worktree_without_commondir(self):
        r = make_repo(self.p("alpha"))
        wt = make_worktree(r, self.p("wt2"), "x", with_commondir=False)
        self.assertEqual(self.one(wt)["root"], r)

    def test_relative_gitdir(self):
        r = make_repo(self.p("alpha"))
        os.makedirs(os.path.join(r, ".git", "worktrees", "rel"))
        wt = self.p("alpha-rel")
        _w(os.path.join(wt, ".git"), "gitdir: ../alpha/.git/worktrees/rel\n")
        self.assertEqual(self.one(wt)["root"], r)

    def test_submodule_is_its_own_repo(self):
        r = make_repo(self.p("alpha"))
        os.makedirs(os.path.join(r, ".git", "modules", "sub"))
        sub = self.p("alpha", "vendor", "sub")
        _w(os.path.join(sub, ".git"), "gitdir: " + os.path.join(r, ".git", "modules", "sub") + "\n")
        got = resolve_paths([r, sub])
        self.assertNotEqual(got[sub]["key"], got[r]["key"])
        self.assertEqual(got[sub]["root"], sub)
        self.assertEqual(got[sub]["name"], "sub")

    def test_bare_repo_and_its_worktree(self):
        bare = self.p("srv", "proj.git")
        for d in ("objects", "refs"):
            os.makedirs(os.path.join(bare, d))
        _w(os.path.join(bare, "HEAD"), "ref: refs/heads/main\n")
        gd = os.path.join(bare, "worktrees", "w1")
        os.makedirs(gd)
        _w(os.path.join(gd, "commondir"), "../..\n")
        wt = self.p("checkouts", "w1")
        _w(os.path.join(wt, ".git"), "gitdir: " + gd + "\n")
        got = resolve_paths([bare, wt])
        self.assertEqual(got[bare]["key"], got[wt]["key"])
        self.assertEqual(got[bare]["name"], "proj")
        self.assertEqual(got[bare]["confidence"], "git")

    def test_deleted_claude_worktree_with_proven_base(self):
        r = make_repo(self.p("alpha"))
        gone = self.p("alpha", ".claude", "worktrees", "agent-1")
        got = resolve_paths([r, gone])
        self.assertEqual(got[gone]["key"], got[r]["key"])
        self.assertEqual(got[gone]["confidence"], "path-heuristic")

    def test_deleted_dot_worktrees_and_suffix_dir(self):
        r = make_repo(self.p("alpha"))
        a = self.p("alpha", ".worktrees", "b1", "src")
        b = self.p("alpha-worktrees", "b2")
        got = resolve_paths([r, a, b])
        for x in (a, b):
            self.assertEqual(got[x]["key"], got[r]["key"], x)
            self.assertEqual(got[x]["confidence"], "path-heuristic")

    def test_deleted_sibling_branch_dir(self):
        r = make_repo(self.p("alpha"))
        gone = self.p("alpha-fix-bug", "src")
        got = resolve_paths([r, gone])
        self.assertEqual(got[gone]["key"], got[r]["key"])
        self.assertEqual(got[gone]["confidence"], "path-heuristic")

    def test_no_merge_without_proof(self):
        gone = self.p("ghost", ".claude", "worktrees", "a1")
        sib = self.p("ghost-feature")
        got = resolve_paths([gone, sib])
        self.assertNotEqual(got[gone]["key"], got[sib]["key"])
        self.assertEqual(got[sib]["confidence"], "slug-only")

    def test_similar_names_do_not_merge_when_both_exist(self):
        a = make_repo(self.p("webapp"))
        b = make_repo(self.p("webapp-alert"))
        got = resolve_paths([a, b])
        self.assertNotEqual(got[a]["key"], got[b]["key"])

    def test_deleted_prefix_sibling_not_matched_by_substring(self):
        r = make_repo(self.p("alpha"))
        other = self.p("alphabet")   # shares prefix but no '-' boundary
        self.assertNotEqual(self.one(other, [r])["key"], self.one(r)["key"])

    def test_windows_paths(self):
        base = "C:\\Users\\me\\proj"
        wt = base + "\\.claude\\worktrees\\w1"
        got = resolve_paths([base, wt])
        # base is only a known cwd (not git): proof via another cwd
        self.assertEqual(got[wt]["key"], got[base]["key"])
        self.assertEqual(got[wt]["name"], "proj")
        self.assertEqual(got[wt]["confidence"], "path-heuristic")

    def test_empty_and_none(self):
        got = resolve_paths([None, "", "   "])
        self.assertEqual(got, {})

    def test_unreadable_git_file_is_safe(self):
        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("permissions not enforced")
        r = make_repo(self.p("outer"))
        inner = self.p("outer", "inner")
        gf = os.path.join(inner, ".git")
        _w(gf, "gitdir: x\n")
        os.chmod(gf, 0)
        try:
            got = self.one(inner)
        finally:
            os.chmod(gf, stat.S_IRUSR | stat.S_IWUSR)
        self.assertNotEqual(got["confidence"], "git")
        self.assertNotEqual(got["root"], r)

    def test_garbage_git_file(self):
        d = self.p("junk")
        _w(os.path.join(d, ".git"), "\x00\xff not a gitdir")
        got = self.one(d)
        self.assertNotEqual(got["confidence"], "git")

    def test_nul_byte_path(self):
        got = resolve_paths(["/tmp/a\x00b"])
        self.assertEqual(got["/tmp/a\x00b"]["confidence"], "slug-only")

    def test_plain_dir_is_slug_only_without_root(self):
        d = self.p("notgit")
        os.makedirs(d)
        got = self.one(d)
        self.assertEqual(got["confidence"], "slug-only")
        self.assertIsNone(got["root"])
        self.assertEqual(got["name"], "notgit")

    def test_encode_slug(self):
        self.assertEqual(encode_slug("/a/my.app x"), "-a-my-app-x")


def _insert(c, slug, cwd, sid, inp, out, ts="2026-04-19T00:00:00Z"):
    c.execute("INSERT INTO messages (uuid, session_id, project_slug, cwd, type, timestamp, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens) VALUES (?,?,?,?,?,?,0,0,0,0,0)",
              (sid + "u" + slug, sid, slug, cwd, "user", ts))
    c.execute("INSERT INTO messages (uuid, session_id, project_slug, cwd, type, timestamp, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens) VALUES (?,?,?,?,?,?,?,?,5,0,0)",
              (sid + "a" + slug, sid, slug, cwd, "assistant", ts, inp, out))


class AggregateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.repo = make_repo(os.path.join(self.tmp, "alpha"))
        self.wt = make_worktree(self.repo, os.path.join(self.tmp, "alpha-feat"), "feat")
        self.other = make_repo(os.path.join(self.tmp, "beta"))
        self.gone = os.path.join(self.tmp, "alpha", ".claude", "worktrees", "old")
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            _insert(c, encode_slug(self.repo), self.repo, "s1", 100, 50)
            _insert(c, encode_slug(self.wt), self.wt, "s2", 30, 20)
            _insert(c, encode_slug(self.gone), self.gone, "s3", 10, 5)
            _insert(c, encode_slug(self.other), self.other, "s4", 7, 3)
            _insert(c, "-no-cwd", None, "s5", 1, 1)
            c.commit()


class AggregateTests(AggregateBase):
    def test_rollup(self):
        repos = repo_summary(self.db)
        by_name = {r["repo_name"]: r for r in repos}
        a = by_name["alpha"]
        self.assertEqual(a["worktree_count"], 3)
        self.assertEqual(a["repo_root"], self.repo)
        self.assertEqual(a["repo_confidence"], "path-heuristic")  # includes deleted wt
        self.assertEqual(by_name["beta"]["repo_confidence"], "git")
        self.assertEqual(by_name["beta"]["worktree_count"], 1)
        self.assertEqual(by_name["cwd"]["repo_confidence"], "slug-only")
        self.assertEqual(len(repos), 3)

    def test_sums_equal_per_worktree_rows(self):
        repos = repo_summary(self.db)
        projects = {r["project_slug"]: r for r in project_summary(self.db)}
        for r in repos:
            for f in ("sessions", "turns", "input_tokens", "output_tokens",
                      "billable_tokens", "cache_read_tokens"):
                self.assertEqual(r[f], sum(projects[w["project_slug"]][f] or 0 for w in r["worktrees"]), f)
            self.assertAlmostEqual(sum(w["share"] for w in r["worktrees"]), 1.0, places=6)
            self.assertEqual(r["worktree_count"], len(r["worktrees"]))
        self.assertEqual(sum(r["billable_tokens"] for r in repos),
                         sum(p["billable_tokens"] or 0 for p in projects.values()))
        a = next(r for r in repos if r["repo_name"] == "alpha")
        self.assertEqual(a["billable_tokens"], 150 + 50 + 15)
        self.assertEqual(repos[0]["repo_name"], "alpha")  # sorted desc
        self.assertEqual({"project_slug", "project_name", "cwd", "sessions", "billable_tokens", "share"},
                         set(a["worktrees"][0]))

    def test_date_filter(self):
        repos = repo_summary(self.db, since="2027-01-01T00:00:00Z")
        self.assertEqual(repos, [])

    def test_annotate_projects(self):
        rows = annotate_projects(self.db, project_summary(self.db))
        self.assertTrue(all("repo" in r and "repo_name" in r for r in rows))
        by = {r["project_slug"]: r for r in rows}
        self.assertEqual(by[encode_slug(self.wt)]["repo"], by[encode_slug(self.repo)]["repo"])
        self.assertNotEqual(by[encode_slug(self.other)]["repo"], by[encode_slug(self.repo)]["repo"])
        self.assertEqual(by["-no-cwd"]["repo_name"], "cwd")

    def test_zero_billable_share(self):
        rows = [{"project_slug": "z", "project_name": "z", "sessions": 0, "turns": 0,
                 "input_tokens": 0, "output_tokens": 0, "billable_tokens": None, "cache_read_tokens": None}]
        from token_dashboard.repos import group_rows
        out = group_rows(rows, {})
        self.assertEqual(out[0]["worktrees"][0]["share"], 0.0)
        self.assertEqual(out[0]["billable_tokens"], 0)


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class EndpointTests(AggregateBase):
    def setUp(self):
        super().setUp()
        self.port = _free_port()
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()

    def _get(self, path):
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read())

    def test_repos_shape(self):
        body = self._get("/api/repos")
        self.assertIsInstance(body, list)
        r = body[0]
        for k in ("repo", "repo_name", "repo_root", "repo_confidence", "worktree_count",
                  "worktrees", "sessions", "turns", "billable_tokens", "cache_read_tokens"):
            self.assertIn(k, r)
        self.assertIn(r["repo_confidence"], ("git", "path-heuristic", "slug-only"))

    def test_repos_date_filter(self):
        self.assertEqual(self._get("/api/repos?since=2027-01-01T00:00:00Z"), [])

    def test_projects_unchanged_plus_repo(self):
        body = self._get("/api/projects")
        self.assertEqual(len(body), 5)
        self.assertIn("repo", body[0]); self.assertIn("repo_name", body[0])
        self.assertIn("project_name", body[0])


if __name__ == "__main__":
    unittest.main()
