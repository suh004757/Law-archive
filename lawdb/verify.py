#!/usr/bin/env python3
"""Spot-check the local law DB against the official law.go.kr Open API.

Deliberately small: one list call per sampled statute (metadata only), plus a
handful of full-text comparisons. Requests are spaced out to stay polite.

  verify.py [--random 16] [--fulltext 4] [--delay 1.2]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "https://www.law.go.kr/DRF"

FIXED = [
    # 부동산
    "주택임대차보호법", "상가건물 임대차보호법", "공인중개사법", "부동산 거래신고 등에 관한 법률",
    "집합건물의 소유 및 관리에 관한 법률", "주택법", "건축법", "도시 및 주거환경정비법",
    "부동산등기법", "민법", "종합부동산세법", "전세사기피해자 지원 및 주거안정에 관한 특별법",
    # 제약
    "약사법", "마약류 관리에 관한 법률", "첨단재생의료 및 첨단바이오의약품 안전 및 지원에 관한 법률",
    "제약산업 육성 및 지원에 관한 특별법", "국민건강보험법", "의료기기법", "생명윤리 및 안전에 관한 법률",
    "화장품법", "건강기능식품에 관한 법률", "의료법", "독점규제 및 공정거래에 관한 법률", "개인정보 보호법",
]
FULLTEXT = ["주택임대차보호법", "공인중개사법", "약사법", "제약산업 육성 및 지원에 관한 특별법"]


def oc() -> str:
    value = os.environ.get("LAW_OC", "").strip()
    if value:
        return value
    env_file = ROOT.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("LAW_OC="):
                value = line.split("=", 1)[1].strip().strip(chr(34)).strip(chr(39))
                if value:
                    return value
    sys.exit("Set LAW_OC in the environment or repository-root .env")


CALLS = 0


def call(endpoint: str, delay: float, **params) -> dict:
    global CALLS
    CALLS += 1
    time.sleep(delay)
    url = f"{API}/{endpoint}?" + urllib.parse.urlencode({"OC": oc(), "type": "JSON", **params})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def squash(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣一-鿿]", "", s)


def dash(d: str) -> str:
    d = re.sub(r"\D", "", d or "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d


def find(target: str, title: str, delay: float) -> list[dict]:
    data = call("lawSearch.do", delay, target=target, query=title, display=100)
    rows = (data.get("LawSearch") or {}).get("law") or []
    if isinstance(rows, dict):
        rows = [rows]
    return [r for r in rows if squash(r.get("법령명한글", "")) == squash(title) and r.get("법령구분명") == "법률"]


def strings(node, out):
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for x in node:
            strings(x, out)
    elif isinstance(node, dict):
        for x in node.values():
            strings(x, out)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--random", type=int, default=16)
    ap.add_argument("--fulltext", type=int, default=4)
    ap.add_argument("--delay", type=float, default=1.2)
    a = ap.parse_args()
    con = sqlite3.connect(ROOT / "law.db")
    today = time.strftime("%Y-%m-%d")

    titles = list(FIXED)
    titles += [r[0] for r in con.execute(
        "SELECT title FROM docs WHERE corpus='law' AND kind='법률' ORDER BY random() LIMIT ?", (a.random,))]

    report, counts = [], {"match": 0, "ahead": 0, "stale": 0, "missing": 0, "error": 0}
    for t in titles:
        row = con.execute(
            "SELECT id,title,serial,date1,effective FROM docs WHERE corpus='law' AND kind='법률' "
            "AND replace(title,' ','')=?", (t.replace(" ", ""),)).fetchone()
        if not row:
            counts["missing"] += 1
            report.append({"title": t, "result": "missing_local"})
            continue
        _, title, mst, prom, eff = row
        try:
            hits = find("law", title, a.delay)
        except Exception as e:  # noqa: BLE001
            counts["error"] += 1
            report.append({"title": title, "result": "error", "detail": repr(e)[:200]})
            continue
        if not hits:
            counts["missing"] += 1
            report.append({"title": title, "result": "not_in_official_list", "local_mst": mst})
            continue
        h = hits[0]
        o_mst, o_prom, o_eff = str(h.get("법령일련번호")), dash(h.get("공포일자")), dash(h.get("시행일자"))
        if o_mst == str(mst):
            res = "match"
        elif prom > o_prom or (prom == o_prom and eff > o_eff):
            res = "ahead"   # local holds a newer promulgated version than the official "current" row
        else:
            res = "stale"
        counts[res] += 1
        report.append({"title": title, "result": res, "local": [str(mst), prom, eff],
                       "official": [o_mst, o_prom, o_eff], "local_not_yet_effective": eff > today})
        print(f"{res:6} {title}  local(MST {mst}, 공포 {prom}, 시행 {eff})  official(MST {o_mst}, 공포 {o_prom}, 시행 {o_eff})")

    print("\n--- full text ---")
    full = []
    for t in FULLTEXT[: a.fulltext]:
        row = con.execute("SELECT id,serial FROM docs WHERE corpus='law' AND kind='법률' AND replace(title,' ','')=?",
                          (t.replace(" ", ""),)).fetchone()
        if not row:
            continue
        try:
            data = call("lawService.do", a.delay, target="law", MST=row[1])
        except Exception as e:  # noqa: BLE001
            full.append({"title": t, "error": repr(e)[:200]})
            print(f"error  {t} {e!r}")
            continue
        out: list[str] = []
        strings((data.get("법령") or {}).get("조문"), out)
        blob = squash("".join(out))
        total = ok = 0
        bad = []
        for label, body in con.execute("SELECT label,body FROM chunks WHERE doc_id=? AND label!='' ORDER BY ord", (row[0],)):
            for para in re.split(r"\n\s*\n", body):
                p = squash(para)
                if len(p) < 12:
                    continue
                total += 1
                if p in blob:
                    ok += 1
                elif len(bad) < 3:
                    bad.append(f"{label}: {para.strip()[:70]}")
        full.append({"title": t, "mst": row[1], "paragraphs": total, "matched": ok, "official_chars": len(blob), "samples_unmatched": bad})
        print(f"{t}: {ok}/{total} paragraphs found verbatim in official text (official {len(blob)} chars)")
        for b in bad:
            print("   unmatched:", b)

    print("\nsummary", counts, "api_calls", CALLS)
    (ROOT / "verify-report.json").write_text(
        json.dumps({"date": today, "counts": counts, "api_calls": CALLS, "metadata": report, "fulltext": full},
                   ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
