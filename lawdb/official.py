#!/usr/bin/env python3
"""Overlay the official current (현행) statutes from the law.go.kr Open API onto the mirror DB.

  official.py plan                 # list API (~57 calls) -> compare with mirror -> law_status
  official.py fetch --max 300      # download the bodies that differ (1 call each, resumable)
  official.py index                # parse official/raw/*.json -> docs(corpus='law_cur')
  official.py status

Statuses in law_status:
  current        mirror MST == official current MST (mirror text is the current text)
  differs        MST differs, official body not fetched yet
  upcoming       mirror holds a newer promulgated (not yet current) version; official current is in law_cur
  stale          mirror is older than official current; official current is in law_cur
  official_only  official has it, mirror does not; official current is in law_cur
  not_in_official_list  mirror has it, official current list does not (repealed / superseded)
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import lawdb
import verify

ROOT = lawdb.ROOT
RAW = ROOT / "official" / "raw"
LIST = ROOT / "official" / "list.json"
KIND_RANK = {"법률": 0, "대통령령": 1}


def call(endpoint: str, delay: float, **params) -> dict:
    last = None
    for attempt in range(3):
        try:
            return verify.call(endpoint, delay if attempt == 0 else 5, **params)
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def rejected(d: dict) -> bool:
    return isinstance(d, dict) and "사용자 정보 검증" in str(d.get("result", ""))


def as_list(x):
    return x if isinstance(x, list) else ([] if x in (None, "") else [x])


def texts(node) -> list[str]:
    if node is None:
        return []
    if isinstance(node, str):
        s = node.strip()
        return [s] if s else []
    if isinstance(node, list):
        out: list[str] = []
        for x in node:
            out += texts(x)
        return out
    if isinstance(node, dict):
        out = []
        for v in node.values():
            out += texts(v)
        return out
    return []


def article_text(u: dict) -> str:
    parts = texts(u.get("조문내용"))
    for h in as_list(u.get("항")):
        if not isinstance(h, dict):
            parts += texts(h)
            continue
        parts += texts(h.get("항내용"))
        for ho in as_list(h.get("호")):
            if not isinstance(ho, dict):
                parts += texts(ho)
                continue
            parts += texts(ho.get("호내용"))
            for mok in as_list(ho.get("목")):
                parts += texts(mok.get("목내용") if isinstance(mok, dict) else mok)
    return "\n".join(parts)


def plan(delay: float) -> None:
    con = lawdb.connect()
    rows, page = [], 1
    while True:
        d = call("lawSearch.do", delay, target="law", display=100, page=page)
        if rejected(d):
            sys.exit("API rejected the caller (IP registration?): " + str(d))
        s = d.get("LawSearch")
        if not s:
            sys.exit("unexpected response: " + str(d)[:300])
        batch = as_list(s.get("law"))
        rows += batch
        total = int(s["totalCnt"])
        print(f"list page {page}: {len(rows)}/{total}", flush=True)
        if len(rows) >= total or not batch:
            break
        page += 1
    LIST.parent.mkdir(parents=True, exist_ok=True)
    LIST.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    local = {}
    for ext, title, kind, mst, prom, eff in con.execute(
            "SELECT ext_id,title,kind,serial,date1,effective FROM docs WHERE corpus='law' AND ext_id!=''"):
        local[ext] = (title, kind, str(mst), prom, eff)
    official = {r["법령ID"]: r for r in rows}

    out = []
    for ext, r in official.items():
        o = (str(r["법령일련번호"]), verify.dash(r.get("공포일자")), verify.dash(r.get("시행일자")))
        if ext in local:
            t, k, m, p, e = local[ext]
            st = "current" if m == o[0] else "differs"
            out.append((ext, t, k, m, p, e, *o, st))
        else:
            out.append((ext, r.get("법령명한글"), r.get("법령구분명"), "", "", "", *o, "official_only"))
    for ext, (t, k, m, p, e) in local.items():
        if ext not in official:
            out.append((ext, t, k, m, p, e, "", "", "", "not_in_official_list"))
    con.execute("DELETE FROM law_status")
    con.executemany("INSERT INTO law_status(ext_id,title,kind,local_mst,local_prom,local_eff,cur_mst,cur_prom,cur_eff,status,checked_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))", out)
    con.commit()
    refresh(con)
    status(con)


def refresh(con) -> None:
    """Mark rows whose official body is already indexed, and classify differs/official_only."""
    have = {m: i for i, m in con.execute("SELECT id,serial FROM docs WHERE corpus='law_cur'")}
    for ext, cur, lm, lp, st in con.execute(
            "SELECT ext_id,cur_mst,local_mst,local_prom,status FROM law_status").fetchall():
        if cur not in have:
            continue
        new = st
        if st == "differs":
            cp = con.execute("SELECT cur_prom FROM law_status WHERE ext_id=?", (ext,)).fetchone()[0]
            local_newer = (lp, int(lm or 0)) > (cp, int(cur or 0))
            new = "upcoming" if local_newer else "stale"
        elif st == "official_only":
            new = "official_only"
        con.execute("UPDATE law_status SET has_cur=1,status=? WHERE ext_id=?", (new, ext))
    con.commit()


def status(con=None) -> None:
    con = con or lawdb.connect()
    for st, n, c in con.execute("SELECT status,count(*),sum(has_cur) FROM law_status GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {st:22} {n:5}  (official body indexed: {c})")
    need = con.execute("SELECT count(*) FROM law_status WHERE status IN ('differs','official_only') AND has_cur=0").fetchone()[0]
    print(f"  to fetch: {need}   raw files: {len(list(RAW.glob('*.json')))}")


def todo(con) -> list[tuple[str, str, str, str]]:
    rows = con.execute("SELECT ext_id,cur_mst,title,kind FROM law_status "
                       "WHERE status IN ('differs','official_only') AND has_cur=0").fetchall()
    rows = [r for r in rows if not (RAW / f"{r[1]}.json").exists()]
    rows.sort(key=lambda r: (KIND_RANK.get(r[3], 2), r[2]))
    return rows


def fetch(max_calls: int, delay: float) -> None:
    con = lawdb.connect()
    RAW.mkdir(parents=True, exist_ok=True)
    rows = todo(con)
    print(f"to fetch: {len(rows)}  (this run: up to {max_calls})", flush=True)
    done = fails = 0
    for ext, mst, title, kind in rows[:max_calls]:
        try:
            d = call("lawService.do", delay, target="law", MST=mst)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  fail {title} {mst}: {e!r}"[:200])
            if fails >= 5:
                sys.exit("5 consecutive failures; stopping")
            continue
        if rejected(d):
            sys.exit("API rejected the caller: " + str(d))
        if "법령" not in d:
            fails += 1
            print(f"  no body {title} {mst}: {str(d)[:150]}")
            if fails >= 5:
                sys.exit("5 consecutive failures; stopping")
            continue
        fails = 0
        (RAW / f"{mst}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        done += 1
        if done % 25 == 0:
            print(f"  fetched {done}/{min(len(rows), max_calls)}", flush=True)
    print(f"fetched {done}")


def index() -> None:
    con = lawdb.connect()
    wanted = {m: (ext, t) for ext, m, t in con.execute(
        "SELECT ext_id,cur_mst,title FROM law_status WHERE cur_mst!=''")}
    have = {i: (d, s) for d, i, s in con.execute("SELECT id,ext_id,serial FROM docs WHERE corpus='law_cur'")}
    done_mst = {s for _, s in have.values()}
    n = 0
    for f in sorted(RAW.glob("*.json")):
        mst = f.stem
        if mst not in wanted or mst in done_mst:
            continue
        L = json.loads(f.read_text(encoding="utf-8")).get("법령") or {}
        info = L.get("기본정보") or {}
        ext = info.get("법령ID") or wanted[mst][0]
        title = info.get("법령명_한글") or wanted[mst][1]
        for doc_id, (e, _) in list(have.items()):
            if e == ext:
                lawdb.drop_doc(con, doc_id)
        chunks: list[tuple[str, str, str]] = []
        for u in as_list((L.get("조문") or {}).get("조문단위")):
            body = article_text(u)
            if u.get("조문여부") == "조문":
                label = f"제{u.get('조문번호')}조" + (f"의{u.get('조문가지번호')}" if u.get("조문가지번호") else "")
                heading = label + (f" ({u['조문제목']})" if u.get("조문제목") else "")
                chunks.append((label, heading, body))
            elif body:
                chunks.append(("", body.split("\n")[0][:80], body))
        for b in as_list((L.get("부칙") or {}).get("부칙단위")):
            body = "\n".join(texts(b.get("부칙내용")))
            if body:
                chunks.append(("", body.split("\n")[0][:80], body))
        for b in as_list((L.get("별표") or {}).get("별표단위")):
            body = "\n".join(texts(b.get("별표내용")) + texts(b.get("별표서식파일링크")))
            chunks.append(("", f"별표 {b.get('별표번호', '')} {b.get('별표제목', '')}".strip(), body))
        kind = (info.get("법종구분") or {}).get("content", "")
        org = (info.get("소관부처") or {}).get("content", "")
        meta = {k: v for k, v in info.items() if k != "연락부서"}
        cur = con.execute(
            "INSERT INTO docs(corpus,path,blob,title,kind,serial,ext_id,date1,effective,status,org,source_url,meta)"
            " VALUES('law_cur',?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{ext}/{mst}", mst, title, kind, mst, ext, verify.dash(info.get("공포일자")),
             verify.dash(info.get("시행일자")), "현행", org, f"https://www.law.go.kr/법령/{title}",
             json.dumps(meta, ensure_ascii=False, default=str)))
        doc_id = cur.lastrowid
        con.execute("INSERT INTO docs_fts(rowid,title) VALUES(?,?)", (doc_id, title))
        for ord_, (label, heading, body) in enumerate(chunks):
            c = con.execute("INSERT INTO chunks(doc_id,ord,label,heading,body) VALUES(?,?,?,?,?)",
                            (doc_id, ord_, label, heading, body))
            con.execute("INSERT INTO chunks_fts(rowid,heading,body) VALUES(?,?,?)", (c.lastrowid, heading, body))
        n += 1
    con.commit()
    print(f"indexed {n} official bodies")
    refresh(con)
    status(con)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "fetch"):
        p = sub.add_parser(name)
        p.add_argument("--delay", type=float, default=1.2)
        if name == "fetch":
            p.add_argument("--max", type=int, default=300)
    sub.add_parser("index")
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "plan":
        plan(a.delay)
    elif a.cmd == "fetch":
        fetch(a.max, a.delay)
    elif a.cmd == "index":
        index()
    else:
        status()


if __name__ == "__main__":
    main()
