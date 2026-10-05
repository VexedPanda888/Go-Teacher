"""The live review page: dashboard_row (contract §1.25), the page's checksum, and the page itself in a
headless browser with a stand-in for the artifact db (skipped when node / Chrome are not installed)."""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from _helpers import make_tools, quiet_points  # noqa: E402  (also puts the package on sys.path)
from katago_mcp.tools import ToolError, dashboard_row_sha  # noqa: E402
from test_tools_mock import synthetic_game  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
TEMPLATE = os.path.join(REPO, "skills", "review-dashboard", "template", "dashboard.html")
BUILD = os.path.join(REPO, "skills", "review-dashboard", "scripts", "build_dashboard.py")
COLS = "ABCDEFGHJKLMNOPQRST"
CHROME = next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                           shutil.which("google-chrome") or "", shutil.which("chromium") or "",
                           shutil.which("chromium-browser") or "") if p and os.path.exists(p)), None)


def page_idx(pt: str) -> int:
    """The template's point index (row 0 = row 19)."""
    return (19 - int(pt[1:])) * 19 + COLS.index(pt[0])


class DashboardRowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tools = make_tools(self.tmp.name)
        os.makedirs(os.path.join(self.tmp.name, "games"), exist_ok=True)
        with open(os.path.join(self.tmp.name, "games", "g.sgf"), "w") as f:
            f.write(synthetic_game(40, seed=4))

    def tearDown(self):
        self.tools.close()
        self.tmp.cleanup()

    def board_at(self, n):
        return self.tools._resolve_position({"sgf": "g.sgf", "move_number": n}).board

    def test_game_row(self):
        r = self.tools.dashboard_row("game", "g.sgf")
        self.assertEqual((r["collection"], r["doc_id"]), ("review", "game"))
        row = r["row"]
        self.assertEqual(len(row["moves"]), 40)
        self.assertEqual(row["you"], "B")
        self.assertEqual(row["komi"], "6.5")                      # a string: no float formatting to disagree on
        self.assertEqual(row["sha256"], dashboard_row_sha(row))
        self.assertTrue(row["game_id"].startswith("ogs_"))

    def test_board_rows(self):
        a, b = quiet_points(self.board_at(11), 2)
        r = self.tools.dashboard_row("board", "g.sgf", {"title": "Move 12", "text": "What did you expect?", "at_move": 11,
                                                         "line": [a, b], "highlight": ["d4", "D4"], "ask": "line"})
        row = r["row"]
        self.assertEqual((r["collection"], r["doc_id"]), ("boards", "b01"))
        self.assertEqual(row["line"], ["W" + a, "B" + b])        # colours from the side to move after move 11
        self.assertEqual(row["highlight"], ["D4"])
        self.assertEqual(row["ask_color"], "W")                  # the answer continues after the shown line
        # title and text are free text: rewording them keeps the checksum
        self.assertEqual(dashboard_row_sha({**row, "title": "Reworded"}), row["sha256"])
        self.assertNotEqual(dashboard_row_sha({**row, "at_move": 12}), row["sha256"])
        r2 = self.tools.dashboard_row("board", "g.sgf", {"id": "q 1!", "title": "Again", "at_move": 0})
        self.assertEqual(r2["doc_id"], "q1")
        self.assertEqual(r2["row"]["seq"], 2)
        self.assertIsNone(r2["row"]["ask_color"])
        r3 = self.tools.dashboard_row("board", "g.sgf", {"title": "Third", "at_move": 5})
        self.assertEqual(r3["doc_id"], "b03")

    def test_board_errors(self):
        occupied = self.tools.dashboard_row("game", "g.sgf")["row"]["moves"][0][1:]
        for board, code in (({"title": "x", "at_move": 11, "line": [occupied]}, "illegal_move"),
                            ({"title": "x", "at_move": 99}, "bad_request"),
                            ({"title": "x", "at_move": 3, "ask": "essay"}, "bad_request"),
                            ({"at_move": 3}, "bad_request"),
                            ({"title": "x", "at_move": 3, "highlight": ["Z9"]}, "bad_request")):
            with self.assertRaises(ToolError) as cm:
                self.tools.dashboard_row("board", "g.sgf", board)
            self.assertEqual(cm.exception.code, code, board)
        with self.assertRaises(ToolError):
            self.tools.dashboard_row("chart", "g.sgf")

    def test_board_from_a_past_game(self):
        """A quiz on an old lesson: the row belongs to this review (its game_id, its numbering) and
        carries the past game's record up to at_move, which may lie beyond this game's last move."""
        old = synthetic_game(60, seed=9).replace("game/1009", "game/123456")
        with open(os.path.join(self.tmp.name, "games", "ogs_123456.sgf"), "w") as f:
            f.write(old)
        this = self.tools.dashboard_row("game", "g.sgf")["row"]
        self.tools.dashboard_row("board", "g.sgf", {"title": "Move 12", "at_move": 11})
        r = self.tools.dashboard_row("board", "g.sgf", {"title": "From an old lesson", "at_move": 50,
                                                         "from_game": "ogs_123456", "ask": "move"})
        row = r["row"]
        self.assertEqual(row["game_id"], this["game_id"])
        self.assertEqual(row["seq"], 2)
        fg = row["from_game"]
        self.assertEqual(fg["game_id"], "ogs_123456")
        self.assertEqual(len(fg["moves"]), 50)
        self.assertEqual(fg["you"], "B")
        self.assertEqual(row["ask_color"], "B")                 # 50 moves played from an even start: Black to move
        self.assertNotEqual(dashboard_row_sha({**row, "from_game": {**fg, "moves": fg["moves"][:-1]}}), row["sha256"])
        with self.assertRaises(ToolError):
            self.tools.dashboard_row("board", "g.sgf", {"title": "x", "at_move": 61, "from_game": "ogs_123456"})
        self.assertEqual(self.tools.sgf_summary("ogs_123456")["game_id"], "ogs_123456")   # memory's id form

    def test_rows_for_a_job_match_the_survey_game_id(self):
        jid = self.tools.start_game_analysis("g.sgf", options={"prefetch": False})["job_id"]
        self.tools.wait_for_job(jid, 0.05, timeout=30)
        by_job = self.tools.dashboard_row("game", jid)["row"]
        by_sgf = self.tools.dashboard_row("game", "g.sgf")["row"]
        self.assertEqual(by_job["game_id"], by_sgf["game_id"])
        self.assertEqual(by_job["sha256"], by_sgf["sha256"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PageChecksumTest(unittest.TestCase):
    """The page recomputes the server's SHA-256 in JavaScript; the two must agree byte for byte."""

    def test_page_and_server_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = make_tools(tmp)
            try:
                os.makedirs(os.path.join(tmp, "games"))
                sgf = synthetic_game(30, seed=9).replace("PW[rival]", 'PW[Łukasz 碁 "q"\\\\]')
                with open(os.path.join(tmp, "games", "g.sgf"), "w", encoding="utf-8") as f:
                    f.write(sgf)
                game = t.dashboard_row("game", "g.sgf")["row"]
                a, b = quiet_points(t._resolve_position({"sgf": "g.sgf", "move_number": 7}).board, 2)
                board = t.dashboard_row("board", "g.sgf", {"title": "T", "at_move": 7, "line": [a, b], "ask": "move"})["row"]
            finally:
                t.close()
            with open(TEMPLATE, encoding="utf-8") as f:
                core = re.search(r'<script id="core">(.*?)</script>', f.read(), re.S).group(1)
            with open(os.path.join(tmp, "core.js"), "w", encoding="utf-8") as f:
                f.write(core)
            with open(os.path.join(tmp, "rows.json"), "w", encoding="utf-8") as f:
                json.dump([game, board], f, ensure_ascii=False)
            js = """
if (!globalThis.crypto) globalThis.crypto = require("crypto").webcrypto;   // node 18: the browsers' Web Crypto, as in node 19+
const Core = require(process.argv[2]); const rows = require(process.argv[3]);
(async () => {
  const out = [];
  for (const r of rows) out.push(await Core.rowIntact(r));
  out.push(await Core.rowIntact(Object.assign({}, rows[1], { at_move: rows[1].at_move + 1 })));
  out.push(await Core.rowIntact(Object.assign({}, rows[1], { title: "reworded" })));
  const D = Core.fromGameRow(rows[0]);
  out.push(Core.replay(D.setup, D.moves).positions.length - 1, D.game.opponent);
  console.log(JSON.stringify(out));
})();"""
            with open(os.path.join(tmp, "t.js"), "w", encoding="utf-8") as f:
                f.write(js)
            res = subprocess.run(["node", os.path.join(tmp, "t.js"), os.path.join(tmp, "core.js"), os.path.join(tmp, "rows.json")],
                                 capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(json.loads(res.stdout), [True, True, False, True, 30, 'Łukasz 碁 "q"\\'])


FAKE_RUNTIME = """<script>
// stand-in for the artifact runtime: claude.use("db") / ("user") with an in-memory store and live snapshots
(function () {
  const S = window.__store = {}; const listeners = [];
  const clone = (d) => JSON.parse(JSON.stringify(d));
  const docSnap = (p) => ({ id: p.split("/").pop(), exists: S[p] !== undefined, data: () => S[p] === undefined ? undefined : clone(S[p]),
                            metadata: { fromCache: false, hasPendingWrites: false } });
  const colSnap = (p) => { const n = p.split("/").length + 1; const docs = Object.keys(S).filter((k) => k.startsWith(p + "/") && k.split("/").length === n).sort().map(docSnap);
                            return { docs, size: docs.length, empty: !docs.length, docChanges: () => [], metadata: { fromCache: false, hasPendingWrites: false } }; };
  const notify = () => { for (const l of listeners) setTimeout(() => l.fn(l.col ? colSnap(l.path) : docSnap(l.path)), 0); };
  window.__write = (p, d) => { S[p] = clone(d); notify(); };
  const db = {
    doc: (p) => ({ path: p, id: p.split("/").pop(), get: async () => docSnap(p), set: async (d) => window.__write(p, d),
                   update: async (d) => window.__write(p, Object.assign({}, S[p], d)), delete: async () => { delete S[p]; notify(); },
                   onSnapshot: (fn) => { listeners.push({ path: p, fn }); setTimeout(() => fn(docSnap(p)), 0); return () => {}; } }),
    collection: (p) => ({ path: p, doc: (id) => db.doc(p + "/" + id),
                          onSnapshot: (fn) => { listeners.push({ path: p, fn, col: true }); setTimeout(() => fn(colSnap(p)), 0); return () => {}; } }),
  };
  const user = { isOwner: async () => true, can: async () => true };
  window.claude = { use: (name) => new Promise((r) => setTimeout(() => r(name === "db" ? db : name === "user" ? user : null), 20)) };
  SEED
})();
</script>"""


def run_chrome(page: str, workdir: str, timeout: float = 90.0) -> str:
    """Headless Chrome's --dump-dom of a page. Chrome can linger after dumping (its updater), so read the
    dump as soon as it is complete and stop the whole process group."""
    out = os.path.join(workdir, "dump.html")
    with tempfile.TemporaryDirectory() as profile, open(out, "w") as fh:
        proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                                 "--disable-background-networking", "--disable-component-update", f"--user-data-dir={profile}",
                                 "--virtual-time-budget=6000", "--dump-dom", "file://" + page],
                                stdout=fh, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + timeout
        try:
            while time.time() < deadline and proc.poll() is None:
                with open(out, encoding="utf-8", errors="replace") as f:
                    if "</html>" in f.read():
                        break
                time.sleep(0.3)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(10)
    with open(out, encoding="utf-8", errors="replace") as f:
        return f.read()


@unittest.skipUnless(CHROME, "Chrome is not installed")
class LivePageBrowserTest(unittest.TestCase):
    """The page in headless Chrome: the live page during the review, and the final review keeping its boards."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tools = make_tools(cls.tmp.name)
        os.makedirs(os.path.join(cls.tmp.name, "games"))
        with open(os.path.join(cls.tmp.name, "games", "g.sgf"), "w") as f:
            f.write(synthetic_game(70, seed=7))

    @classmethod
    def tearDownClass(cls):
        cls.tools.close()
        cls.tmp.cleanup()

    def run_page(self, html_path: str, seed: dict, script: str) -> dict:
        with open(html_path, encoding="utf-8") as f:
            html = f.read()
        seed_js = "\n".join(f"S[{json.dumps(k)}] = {json.dumps(v, ensure_ascii=False)};" for k, v in seed.items())
        html = html.replace("<head>", "<head>" + FAKE_RUNTIME.replace("SEED", seed_js), 1)
        html = html.replace("</body>", "<script>" + script + "</script></body>", 1)
        page = os.path.join(self.tmp.name, "page.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(html)
        dump = run_chrome(page, self.tmp.name)
        m = re.search(r"<title>RESULT:(.*?)</title>", dump, re.S)
        self.assertIsNotNone(m, dump[-3000:])
        import html as htmllib
        return json.loads(htmllib.unescape(m.group(1)))

    def build(self, *args) -> str:
        out = os.path.join(self.tmp.name, "review.html")
        subprocess.run(["python3", BUILD, *args, "--out", out], check=True, capture_output=True)
        return out

    def test_live_page_shows_boards_and_saves_a_clicked_answer(self):
        t = self.tools
        game = t.dashboard_row("game", "g.sgf")
        first = t.dashboard_row("board", "g.sgf", {"title": "Move 30 for the self-review", "at_move": 30})
        g_board = t._resolve_position({"sgf": "g.sgf", "move_number": 40}).board
        played = quiet_points(g_board, 1)[0]
        ask = t.dashboard_row("board", "g.sgf", {"title": "Move 41: what did you expect?", "text": "Click the moves.",
                                                  "at_move": 40, "line": [played], "highlight": [played], "ask": "line"})
        after = t._resolve_position({"sgf": "g.sgf", "move_number": 40, "then": [ask["row"]["line"][0]]}).board
        r1, r2 = quiet_points(after, 3)[1:]
        stone = game["row"]["moves"][0][1:]
        tampered = dict(first["row"], id="bx", at_move=31)       # checksum no longer matches
        seed = {"review/game": game["row"], "boards/" + first["doc_id"]: first["row"], "boards/bx": tampered}
        script = f"""
const click = (pt) => document.querySelector('#hit rect[data-idx="' + pt + '"]').dispatchEvent(new MouseEvent('click', {{ bubbles: true }}));
const out = {{}};
setTimeout(() => {{ out.firstTitle = (document.querySelector('#boardbody h3') || {{}}).textContent || null;
                   out.graphHidden = document.getElementById('graphPanel').hidden; out.scoreHidden = document.getElementById('scoreBox').hidden;
                   window.__write('boards/{ask["doc_id"]}', {json.dumps(ask["row"])}); }}, 600);
setTimeout(() => {{ out.askTitle = (document.querySelector('#boardbody h3') || {{}}).textContent || null; click({page_idx(stone)}); }}, 1200);
setTimeout(() => {{ out.illegal = (document.querySelector('#boardbody .msg') || {{}}).textContent || null; click({page_idx(r1)}); }}, 1600);
setTimeout(() => {{ click({page_idx(r2)}); }}, 1900);
setTimeout(() => {{ out.labels = Array.from(document.querySelectorAll('#labels text')).map((t) => t.textContent);
                   document.querySelector('[data-a=send]').click(); }}, 2200);
setTimeout(() => {{ out.answer = window.__store['answers/{ask["doc_id"]}'] || null;
                   out.sentMsg = (document.querySelector('#boardbody .msg') || {{}}).textContent || null;
                   out.list = Array.from(document.querySelectorAll('#boardlist button')).map((b) => b.textContent);
                   document.title = 'RESULT:' + JSON.stringify(out); }}, 3000);
"""
        out = self.run_page(self.build("--live", "--title", "Go review: vs rival"), seed, script)
        self.assertEqual(out["firstTitle"], "Move 30 for the self-review")      # the newest intact board is shown
        self.assertTrue(out["graphHidden"])                                      # no engine data on the live page
        self.assertTrue(out["scoreHidden"])
        self.assertEqual(out["askTitle"], "Move 41: what did you expect?")       # a new board takes the focus
        self.assertIn("occupied", out["illegal"])
        self.assertEqual(out["labels"], ["1", "2", "3"])                         # the shown move, then the two clicked
        self.assertEqual(out["answer"]["moves"], ["W" + r1, "B" + r2])
        self.assertEqual(out["answer"]["board"], ask["doc_id"])
        self.assertIn("done", out["sentMsg"])
        self.assertTrue(any("did not arrive intact" in x for x in out["list"]))
        self.assertTrue(any("answered" in x for x in out["list"]))

    def test_live_page_shows_a_board_from_a_past_game(self):
        t = self.tools
        old = os.path.join(self.tmp.name, "games", "old.sgf")
        with open(old, "w") as f:
            f.write(synthetic_game(90, seed=11).replace("PW[rival]", "PW[oldrival]"))
        game = t.dashboard_row("game", "g.sgf")
        past = t.dashboard_row("board", "g.sgf", {"title": "An old lesson", "at_move": 80, "from_game": "old.sgf", "ask": "move"})
        script = """
setTimeout(() => { const out = {};
  out.title = (document.querySelector('#boardbody h3') || {}).textContent || null;
  out.meta = (document.querySelector('#boardbody .meta') || {}).textContent || null;
  out.list = Array.from(document.querySelectorAll('#boardlist button')).map((b) => b.textContent);
  document.title = 'RESULT:' + JSON.stringify(out); }, 1200);
"""
        out = self.run_page(self.build("--live", "--title", "Go review: vs rival"),
                            {"review/game": game["row"], "boards/" + past["doc_id"]: past["row"]}, script)
        self.assertEqual(out["title"], "An old lesson")
        self.assertIn("your game vs oldrival", out["meta"])
        self.assertIn("after move 80", out["meta"])               # beyond this game's 70 moves
        self.assertFalse(any("did not arrive intact" in x for x in out["list"]), out["list"])
        self.assertTrue(any("past game, move 80" in x for x in out["list"]), out["list"])

    def test_final_review_keeps_the_boards(self):
        t = self.tools
        jid = t.start_game_analysis("g.sgf", options={"prefetch": False})["job_id"]
        t.wait_for_job(jid, 0.05, timeout=30)
        m = t.job_results(jid)["moments"][0]
        n = m["move"]
        r = t.validate_variations(jid, [{"id": "M1", "moves": m["moves"], "title": "Test moment",
                                         "commentary": [{"at_move": n, "text": "Here."}], "takeaway": "Count first.",
                                         "branches": [{"id": "B1", "label": "As played", "from_move": n - 1,
                                                       "moves": [f"B{m['played']}"]}]}],
                                  {"story": "The headline.", "takeaways": [{"momentId": "M1", "takeaway": "Count first."}]})
        self.assertTrue(r["valid"], r["errors"])
        blob = os.path.join(self.tmp.name, "blob.json")
        with open(blob, "w", encoding="utf-8") as f:
            f.write(r["dashboard_data"])
        board = t.dashboard_row("board", jid, {"title": "Your expected line", "at_move": n - 1})
        script = """
setTimeout(() => { const out = {};
  out.headline = (document.querySelector('#summary .headline') || {}).textContent || null;
  out.liveHead = document.getElementById('liveHead').textContent; out.liveHidden = document.getElementById('live').hidden;
  out.moments = document.querySelectorAll('#eplist button').length; out.graph = !!document.querySelector('#graph path');
  out.takeaways = Array.from(document.querySelectorAll('#summary li')).map((li) => li.textContent);
  out.boxTakeaway = (document.querySelector('#epbody .principle') || {}).textContent || null;
  out.boards = Array.from(document.querySelectorAll('#boardlist button')).map((b) => b.textContent);
  const slider = document.getElementById('slider'); slider.value = N; slider.dispatchEvent(new Event('input'));
  const blue = () => document.querySelectorAll('#marks circle[fill="var(--best)"], #marks circle[stroke="var(--best)"]').length;
  out.best = document.getElementById('bestInfo').textContent; out.blue = blue();
  document.getElementById('tglBest').click();
  out.bestOff = document.getElementById('bestInfo').hidden; out.blueOff = blue();
  document.title = 'RESULT:' + JSON.stringify(out); }, 1500);
""".replace("N;", f"{n};")
        out = self.run_page(self.build("--blob", blob, "--sha", r["sha256"]), {"boards/" + board["doc_id"]: board["row"]}, script)
        self.assertEqual(out["headline"], "The headline.")
        self.assertEqual(out["moments"], 1)
        self.assertTrue(any("Count first." in x for x in out["takeaways"]), out["takeaways"])
        self.assertIn("Count first.", out["boxTakeaway"])
        self.assertTrue(out["graph"])
        self.assertFalse(out["liveHidden"])
        self.assertEqual(out["liveHead"], "During the review")
        self.assertEqual(len(out["boards"]), 1)
        # at the episode's move, the survey's best move is named and marked in blue; the toggle hides both
        self.assertIn("Best move: " + m["best"], out["best"])
        self.assertIn("lost", out["best"])
        self.assertEqual(out["blue"], 1)
        self.assertTrue(out["bestOff"])
        self.assertEqual(out["blueOff"], 0)


if __name__ == "__main__":
    unittest.main()


class SeedPageTest(unittest.TestCase):
    """The seed page: one page for many games, with the survey's engine data, the agreed records and per-game boards."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tools = t = make_tools(cls.tmp.name, seed__survey_visits=30, seed__min_moves=40)
        os.makedirs(os.path.join(cls.tmp.name, "games"))
        names = []
        for k, (n, seed) in enumerate(((70, 81), (60, 82))):
            name = f"ogs_{8100 + k}.sgf"
            with open(os.path.join(cls.tmp.name, "games", name), "w") as f:
                f.write(synthetic_game(n, seed=seed).replace(f"game/{1000 + seed}", f"game/{8100 + k}"))
            names.append(name)
        t.seed_start(games=names)
        deadline = time.time() + 30
        while time.time() < deadline and not all(g["state"] == "ready" for g in t.seed_status()["games"]):
            time.sleep(0.05)
        cls.g1, cls.g2 = t.seed_game(1), t.seed_game(2)          # the index of g2 has game 2 as the current one
        tm = cls.g1["top_moment"]
        cls.board1 = t.dashboard_row("board", cls.g1["job_id"], {"title": "Better", "at_move": tm["move"] - 1,
                                                                 "line": [tm["best"]]}, page="seed")
        cls.board2 = t.dashboard_row("board", cls.g2["job_id"], {"title": "Game two board", "at_move": 5}, page="seed")
        cls.rec1 = t.seed_record(1, "confirmed", story="Agreed story one.", feedback="Mostly right.",
                                 lesson={"move": tm["move"], "better": tm["best"], "title": "Check first",
                                         "takeaway": "Before X I check Y.", "theme": "checking first"})
        cls.idx_back = t.seed_game(1)["page_rows"][0]["row"]     # a signed index with game 1 as the current one

    @classmethod
    def tearDownClass(cls):
        cls.tools.close()
        cls.tmp.cleanup()

    def rows(self):
        out = {}
        for g, story in ((self.g1, "Story one."), (self.g2, "Story two.")):
            for pr in g["page_rows"]:
                row = dict(pr["row"])
                if pr["collection"] == "seedgames":
                    row["text"] = story
                out[pr["collection"] + "/" + pr["doc_id"]] = row
        out["boards/" + self.board1["doc_id"]] = self.board1["row"]
        out["boards/" + self.board2["doc_id"]] = self.board2["row"]
        out["seedrecords/" + self.rec1["page_row"]["doc_id"]] = self.rec1["page_row"]["row"]
        return out

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_page_and_server_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(TEMPLATE, encoding="utf-8") as f:
                core = re.search(r'<script id="core">(.*?)</script>', f.read(), re.S).group(1)
            with open(os.path.join(tmp, "core.js"), "w", encoding="utf-8") as f:
                f.write(core)
            rows = self.rows()
            with open(os.path.join(tmp, "rows.json"), "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
            js = """
if (!globalThis.crypto) globalThis.crypto = require("crypto").webcrypto;
const Core = require(process.argv[2]); const rows = require(process.argv[3]);
(async () => {
  const out = {};
  for (const k of Object.keys(rows)) out[k] = await Core.rowIntact(rows[k]);
  const g = rows["seedgames/ogs_8100"];
  out.tampered = await Core.rowIntact(Object.assign({}, g, { scores: g.scores.slice(1) }));
  const D = Core.fromSeedRow(g, rows["seedrecords/ogs_8100"]);
  out.shape = [D.scoreSeries.length, D.bestMoves.length, D.episodes.length > 0, D.seedInfo.story, D.seedRecord.status, D.game.you];
  console.log(JSON.stringify(out));
})();"""
            with open(os.path.join(tmp, "t.js"), "w", encoding="utf-8") as f:
                f.write(js)
            res = subprocess.run(["node", os.path.join(tmp, "t.js"), os.path.join(tmp, "core.js"), os.path.join(tmp, "rows.json")],
                                 capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(res.returncode, 0, res.stderr)
            out = json.loads(res.stdout)
            self.assertFalse(out.pop("tampered"))
            shape = out.pop("shape")
            self.assertTrue(all(out.values()), out)
            self.assertEqual(shape, [71, 70, True, "Story one.", "confirmed", "B"])

    @unittest.skipUnless(CHROME, "Chrome is not installed")
    def test_seed_page_in_a_browser(self):
        out_html = os.path.join(self.tmp.name, "seeding.html")
        subprocess.run(["python3", BUILD, "--seed", "--out", out_html], check=True, capture_output=True)
        with open(out_html, encoding="utf-8") as f:
            html = f.read()
        seed_js = "\n".join(f"S[{json.dumps(k)}] = {json.dumps(v, ensure_ascii=False)};" for k, v in self.rows().items())
        html = html.replace("<head>", "<head>" + FAKE_RUNTIME.replace("SEED", seed_js), 1)
        script = """
const out = {}; const txt = (sel) => (document.querySelector(sel) || {}).textContent || null;
const snap = (k) => { out[k] = { title: txt('#title'), story: txt('#epbody .commentary'), lesson: txt('#epbody .principle'),
  graph: !document.getElementById('graphPanel').hidden && !!document.querySelector('#graph path'),
  boards: Array.from(document.querySelectorAll('#boardlist button')).map((b) => b.textContent),
  tabs: Array.from(document.querySelectorAll('#seedbar button')).map((b) => b.textContent),
  slider: document.getElementById('slider').max }; };
setTimeout(() => { snap('first'); document.querySelector('#seedbar button').click(); }, 900);
setTimeout(() => { snap('clicked'); const sl = document.getElementById('slider'); sl.value = 3; sl.dispatchEvent(new Event('input')); out.blue = document.querySelectorAll('#marks circle[fill="var(--best)"], #marks circle[stroke="var(--best)"]').length; }, 1500);
setTimeout(() => { const g = Object.assign({}, window.__store['seedgames/ogs_8100'], { text: 'Rewritten story.' }); window.__write('seedgames/ogs_8100', g);
                   window.__write('seedrecords/ogs_8100', null); delete window.__store['seedrecords/ogs_8100']; }, 1900);
setTimeout(() => { snap('rewritten'); out.move = document.getElementById('moveNum').textContent;
                   const idx = Object.assign({}, window.__store['seed/index']); window.__write('seed/index', idx); }, 2400);
setTimeout(() => { snap('stays'); window.__write('seed/index', IDX_BACK); }, 2900);          // Claude goes back to game 1
setTimeout(() => { window.__write('seed/index', IDX_ON); }, 3300);                              // ... and on to game 2
setTimeout(() => { out.followsAgain = txt('#title'); document.title = 'RESULT:' + JSON.stringify(out); }, 3800);
"""
        script = script.replace("IDX_BACK", json.dumps(self.idx_back)).replace("IDX_ON", json.dumps(self.g2["page_rows"][0]["row"]))
        html = html.replace("</body>", "<script>" + script + "</script></body>", 1)
        page = os.path.join(self.tmp.name, "seedpage.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(html)
        dump = run_chrome(page, self.tmp.name)
        m = re.search(r"<title>RESULT:(.*?)</title>", dump, re.S)
        self.assertIsNotNone(m, dump[-3000:])
        import html as htmllib
        out = json.loads(htmllib.unescape(m.group(1)))
        first, clicked = out["first"], out["clicked"]
        self.assertTrue(first["title"].startswith("Game 2 of 2"))            # follows Claude: the index's current game
        self.assertIn("Story two.", first["story"])
        self.assertEqual(first["boards"], ["Game two boardafter move 5"])    # only this game's boards
        self.assertEqual(first["slider"], "60")
        self.assertTrue(any("✓ lesson" in t for t in first["tabs"]), first["tabs"])
        self.assertTrue(first["graph"])                                      # engine data is shown when seeding
        self.assertTrue(clicked["title"].startswith("Game 1 of 2"))
        self.assertIn("Agreed story one.", clicked["story"])                 # the record's story wins
        self.assertIn("Before X I check Y.", clicked["lesson"])
        self.assertEqual(len(clicked["boards"]), 1)
        self.assertIn("Better", clicked["boards"][0])
        self.assertEqual(out["blue"], 1)                                     # the best move at move 3
        self.assertIn("Rewritten story.", out["rewritten"]["story"])         # new text, same game, no rebuild
        self.assertEqual(out["move"], "3")
        self.assertTrue(out["stays"]["title"].startswith("Game 1 of 2"))     # the student's choice holds until Claude moves on
        self.assertTrue(out["followsAgain"].startswith("Game 2 of 2"))      # once Claude moves, the page follows again
