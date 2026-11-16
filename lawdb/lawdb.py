#!/usr/bin/env python3
"""Local Korean law DB built from the legalize-kr Git mirrors.

  lawdb.py build [--corpus law admrule ordinance precedent]
  lawdb.py stats
  lawdb.py search "확정일자" [--corpus law] [--title 임대차] [--limit 10]
  lawdb.py get 주택임대차보호법 [--kind 법률] [--article 제3조의2]

Content is read straight from Git objects (no dependence on the working tree),
and each file's blob hash is stored so a rebuild only touches changed files.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import yaml

try:
    _Loader = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _Loader = yaml.SafeLoader

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "law.db"
MIRROR = ROOT / "mirror"

CORPORA = {
    "law": "legalize-kr",
    "admrule": "admrule-kr",
    "ordinance": "ordinance-kr",
    "precedent": "precedent-kr",
}

# corpus -> (title, kind, serial, ext_id, date1, effective, status, org)
FIELDS = {
    "law": ("제목", "법령구분", "법령MST", "법령ID", "공포일자", "시행일자", "상태", "소관부처"),
    "admrule": ("행정규칙명", "행정규칙종류", "행정규칙일련번호", "행정규칙ID", "발령일자", "시행일자", "현행여부", "소관부처명"),
    "ordinance": ("자치법규명", "자치법규종류", "자치법규일련번호", "자치법규ID", "공포일자", "시행일자", "", "지자체기관명"),
    "precedent": ("사건명", "사건종류", "판례일련번호", "사건번호", "선고일자", "", "", "법원명"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
  id INTEGER PRIMARY KEY,
  corpus TEXT NOT NULL, path TEXT NOT NULL, blob TEXT NOT NULL,
  title TEXT, kind TEXT, serial TEXT, ext_id TEXT,
  date1 TEXT, effective TEXT, status TEXT, org TEXT, source_url TEXT,
  meta TEXT,
  UNIQUE(corpus, path));
CREATE INDEX IF NOT EXISTS docs_title ON docs(title);
CREATE INDEX IF NOT EXISTS docs_serial ON docs(corpus, serial);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL, ord INTEGER NOT NULL,
  label TEXT, heading TEXT, body TEXT);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id, ord);
CREATE INDEX IF NOT EXISTS chunks_label ON chunks(doc_id, label);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  heading, body, content='chunks', content_rowid='id', tokenize='trigram');
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
  title, content='docs', content_rowid='id', tokenize='trigram');
CREATE TABLE IF NOT EXISTS sync(corpus TEXT PRIMARY KEY, head TEXT, head_date TEXT, synced_at TEXT);
CREATE TABLE IF NOT EXISTS law_status(
  ext_id TEXT PRIMARY KEY, title TEXT, kind TEXT,
  local_mst TEXT, local_prom TEXT, local_eff TEXT,
  cur_mst TEXT, cur_prom TEXT, cur_eff TEXT,
  status TEXT, has_cur INTEGER DEFAULT 0, checked_at TEXT);
"""

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
ARTICLE = re.compile(r"^(제\s*\d+\s*조(?:의\s*\d+)?)")
RAW_ARTICLE = re.compile(r"^(제\d+조(?:의\d+)?)\s*[\(（]")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-400000;")
    con.executescript(SCHEMA)
    return con


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def tree(repo: Path) -> dict[str, str]:
    out = git(repo, "ls-tree", "-r", "-z", "HEAD")
    files = {}
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, typ, sha = meta.split()
        p = path.decode("utf-8", "replace")
        if typ == b"blob" and p.endswith(".md") and "/" in p and not p.startswith("."):
            files[p] = sha.decode()
    return files


class CatFile:
    def __init__(self, repo: Path):
        self.p = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, sha: str) -> str:
        self.p.stdin.write(sha.encode() + b"\n")
        self.p.stdin.flush()
        header = self.p.stdout.readline().split()
        size = int(header[2])
        data = self.p.stdout.read(size)
        self.p.stdout.read(1)
        return data.decode("utf-8", "replace")

    def close(self):
        self.p.stdin.close()
        self.p.wait()


def split_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                meta = yaml.load(text[3:end], Loader=_Loader) or {}
            except yaml.YAMLError:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), text[end + 4:]
    return {}, text


def norm_label(s: str) -> str:
    return re.sub(r"\s+", "", s)


def chunk(body: str) -> list[tuple[str, str, str]]:
    """Split into (label, heading, text). Splits on markdown headings, and on
    raw '제N조(' line starts inside code fences (ordinance source-preserving format)."""
    chunks: list[tuple[str, str, str]] = []
    label, heading, buf = "", "", []
    section = ""
    in_fence = False

    def flush():
        text = "\n".join(buf).strip()
        if text or label:
            chunks.append((label, heading, text))

    for line in body.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            m = HEADING.match(line)
            if m:
                flush()
                buf = []
                h = m.group(2).strip()
                a = ARTICLE.match(h)
                if a:
                    label, heading = norm_label(a.group(1)), h
                else:
                    section = h
                    label, heading = "", h
                continue
        else:
            m = RAW_ARTICLE.match(line.strip())
            if m:
                flush()
                buf = []
                label = m.group(1)
                heading = line.strip()[:80]
        buf.append(line)
    flush()
    return chunks


def scalar(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


def build(corpora: list[str]) -> None:
    con = connect()
    for corpus in corpora:
        repo = MIRROR / CORPORA[corpus]
        if not (repo / ".git").exists():
            print(f"[{corpus}] mirror missing: {repo}")
            continue
        t0 = time.time()
        files = tree(repo)
        have = {p: (i, b) for i, p, b in con.execute("SELECT id, path, blob FROM docs WHERE corpus=?", (corpus,))}
        gone = [i for p, (i, b) in have.items() if p not in files]
        todo = [(p, s) for p, s in files.items() if p not in have or have[p][1] != s]
        stale = gone + [have[p][0] for p, _ in todo if p in have]
        for i in stale:
            drop_doc(con, i)
        print(f"[{corpus}] files={len(files)} changed/new={len(todo)} removed={len(gone)}", flush=True)

        cat = CatFile(repo)
        f = FIELDS[corpus]
        n = 0
        for path, sha in todo:
            meta, body = split_frontmatter(cat.read(sha))
            get = lambda k: scalar(meta.get(k)) if k else ""
            title = get(f[0]) or Path(path).parent.name
            kind = get(f[1])
            if corpus == "law":
                kind = kind or Path(path).stem
            cur = con.execute(
                "INSERT INTO docs(corpus,path,blob,title,kind,serial,ext_id,date1,effective,status,org,source_url,meta)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (corpus, path, sha, title, kind, get(f[2]), get(f[3]), get(f[4]), get(f[5]), get(f[6]), get(f[7]),
                 scalar(meta.get("출처")), json.dumps(meta, ensure_ascii=False, default=str)))
            doc_id = cur.lastrowid
            con.execute("INSERT INTO docs_fts(rowid,title) VALUES(?,?)", (doc_id, title))
            for ord_, (label, heading, text) in enumerate(chunk(body)):
                c = con.execute("INSERT INTO chunks(doc_id,ord,label,heading,body) VALUES(?,?,?,?,?)",
                                (doc_id, ord_, label, heading, text))
                con.execute("INSERT INTO chunks_fts(rowid,heading,body) VALUES(?,?,?)", (c.lastrowid, heading, text))
            n += 1
            if n % 5000 == 0:
                con.commit()
                print(f"[{corpus}] {n}/{len(todo)} {time.time()-t0:.0f}s", flush=True)
        cat.close()
        head = git(repo, "log", "-1", "--format=%H %cI").decode().split()
        con.execute("INSERT OR REPLACE INTO sync VALUES(?,?,?,datetime('now'))", (corpus, head[0], head[1]))
        con.commit()
        print(f"[{corpus}] done {n} docs in {time.time()-t0:.0f}s", flush=True)
    con.close()


def drop_doc(con: sqlite3.Connection, doc_id: int) -> None:
    row = con.execute("SELECT title FROM docs WHERE id=?", (doc_id,)).fetchone()
    if row:
        con.execute("INSERT INTO docs_fts(docs_fts,rowid,title) VALUES('delete',?,?)", (doc_id, row[0]))
    for cid, h, b in con.execute("SELECT id,heading,body FROM chunks WHERE doc_id=?", (doc_id,)).fetchall():
        con.execute("INSERT INTO chunks_fts(chunks_fts,rowid,heading,body) VALUES('delete',?,?,?)", (cid, h, b))
    con.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    con.execute("DELETE FROM docs WHERE id=?", (doc_id,))


def fts_query(q: str) -> str:
    terms = [t for t in q.split() if t]
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)


def cmd_stats(_):
    con = connect()
    for row in con.execute("SELECT corpus, count(*) FROM docs GROUP BY corpus"):
        print(row)
    print("chunks", con.execute("SELECT count(*) FROM chunks").fetchone()[0])
    for row in con.execute("SELECT * FROM sync"):
        print(row)
    print("db_size_MB", round(DB_PATH.stat().st_size / 1e6))


def status_tag(corpus: str, st: str | None) -> str:
    if corpus == "law_cur":
        return "공식 현행본"
    if corpus != "law":
        return ""
    return {"current": "현행 확인", "differs": "현행 미확인",
            "not_in_official_list": "⚠ 공식 현행목록에 없음(폐지/구본 가능)"}.get(st or "", "")


HIDE_SUPERSEDED = ("NOT (d.corpus='law' AND EXISTS (SELECT 1 FROM law_status s2 WHERE s2.ext_id=d.ext_id "
                   "AND s2.has_cur=1 AND s2.status IN ('upcoming','stale')))")


def cmd_search(a):
    con = connect()
    terms = a.query.split()
    where, params = [], []
    if all(len(t) >= 3 for t in terms):
        sql = ("SELECT d.corpus,d.title,d.kind,c.label,c.heading,d.effective,"
               "snippet(chunks_fts,1,'[',']','…',24),s.status FROM chunks_fts "
               "JOIN chunks c ON c.id=chunks_fts.rowid JOIN docs d ON d.id=c.doc_id "
               "LEFT JOIN law_status s ON s.ext_id=d.ext_id WHERE chunks_fts MATCH ?")
        params.append(fts_query(a.query))
        order = " ORDER BY rank"
    else:  # trigram index needs >=3 chars per term; fall back to a scan
        sql = ("SELECT d.corpus,d.title,d.kind,c.label,c.heading,d.effective,substr(c.body,1,160),s.status "
               "FROM chunks c JOIN docs d ON d.id=c.doc_id "
               "LEFT JOIN law_status s ON s.ext_id=d.ext_id WHERE 1=1")
        for t in terms:
            where.append("c.body LIKE ?")
            params.append(f"%{t}%")
        order = ""
    if a.corpus == "law":  # statutes = mirror + official current bodies
        where.append("d.corpus IN ('law','law_cur')")
    elif a.corpus:
        where.append("d.corpus=?")
        params.append(a.corpus)
    if not a.include_upcoming:  # default: official current text, not the not-yet-effective mirror copy
        where.append(HIDE_SUPERSEDED)
    if a.title:
        where.append("d.title LIKE ?")
        params.append(f"%{a.title}%")
    if where:
        sql += " AND " + " AND ".join(where)
    for r in con.execute(sql + order + " LIMIT ?", (*params, a.limit)):
        tag = status_tag(r[0], r[7])
        print(f"[{'law' if r[0] == 'law_cur' else r[0]}{' · ' + tag if tag else ''}] "
              f"{r[1]} ({r[2]}, 시행 {r[5]}) {r[4]}\n    {r[6].strip()}\n")


def cmd_get(a):
    con = connect()
    corpora = ("law_cur", "law") if a.corpus == "law" else (a.corpus,)
    marks = ",".join("?" * len(corpora))
    base = ("SELECT d.id,d.title,d.kind,d.serial,d.date1,d.effective,d.status,d.source_url,d.corpus,d.ext_id,"
            "s.status,s.has_cur FROM docs d LEFT JOIN law_status s ON s.ext_id=d.ext_id "
            f"WHERE d.corpus IN ({marks}) AND ")
    kind_sql, kind_params = (" AND d.kind=?", [a.kind]) if a.kind else ("", [])
    docs = con.execute(base + "d.title=?" + kind_sql, [*corpora, a.name, *kind_params]).fetchall()
    if not docs:
        docs = con.execute(base + "replace(d.title,' ','') LIKE ?" + kind_sql + " LIMIT 20",
                           [*corpora, f"%{a.name.replace(' ', '')}%", *kind_params]).fetchall()
    # mirror copies superseded by an official current body are hidden unless --upcoming asks for them
    hidden = lambda d: d[8] == "law" and d[11] and d[10] in ("upcoming", "stale")
    notes = {d[9]: d for d in docs if hidden(d) and d[10] == "upcoming"}
    if a.upcoming:
        docs = [d for d in docs if hidden(d) and d[10] == "upcoming"]
    else:
        docs = [d for d in docs if not hidden(d)]
    for d in docs:
        tag = "시행예정본(공포됐으나 아직 현행 아님)" if a.upcoming else status_tag(d[8], d[10])
        print(f"# {d[1]} [{d[2]}] 일련번호={d[3]} 공포/발령={d[4]} 시행={d[5]} · {tag}\n# {d[7]}")
        n = notes.get(d[9])
        if n and not a.upcoming:
            print(f"# ※ 공포됐으나 미시행인 개정본 있음(일련번호 {n[3]}, 공포 {n[4]}, 시행 {n[5]}) — --upcoming 으로 조회")
        if not a.article and len(docs) > 1:
            continue
        q, p = "SELECT heading,body FROM chunks WHERE doc_id=?", [d[0]]
        if a.article:
            q += " AND label=?"
            p.append(norm_label(a.article))
        for h, b in con.execute(q + " ORDER BY ord", p):
            print(f"\n## {h}\n{b}")
        print()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--corpus", nargs="*", default=list(CORPORA), choices=list(CORPORA))
    sub.add_parser("stats")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--corpus", choices=list(CORPORA))
    s.add_argument("--title")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--include-upcoming", action="store_true", help="also search not-yet-effective mirror copies")
    g = sub.add_parser("get")
    g.add_argument("--upcoming", action="store_true", help="show the promulgated-but-not-yet-current version")
    g.add_argument("name")
    g.add_argument("--corpus", default="law", choices=list(CORPORA))
    g.add_argument("--kind")
    g.add_argument("--article")
    a = ap.parse_args()
    {"build": lambda a: build(a.corpus), "stats": cmd_stats, "search": cmd_search, "get": cmd_get}[a.cmd](a)


if __name__ == "__main__":
    main()
