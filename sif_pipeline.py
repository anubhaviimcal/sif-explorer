#!/usr/bin/env python3
"""SIF data pipeline.

Downloads AMFI SIF NAV files, keeps a local NAV history cache, calculates
1/3/5-year CAGR as of the previous month-end for Regular-plan Growth schemes,
merges the fields you maintain by hand (manual_fields.csv) and writes
output/sif.json in the format the SIF Explorer app's Data tab accepts.

Commands:
  python sif_pipeline.py selftest            check the logic offline
  python sif_pipeline.py inspect SOURCE      show how a URL or file parses
  python sif_pipeline.py run                 build output/sif.json
Standard library only (Python 3.9+).
"""
import argparse
import csv
import json
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
CACHE = BASE / "cache" / "nav_cache.json"
META = BASE / "cache" / "meta.json"
MANUAL = BASE / "manual_fields.csv"
OUT = BASE / "output"
MANUAL_COLS = ["scheme_code", "name", "amc", "cat", "launch", "exp", "exit", "mgr", "src"]
DATE_FORMATS = ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d %b %Y")
DEFAULT_HDR = ["scheme code", "isin div payout/ isin growth", "isin div reinvestment",
               "scheme name", "plan", "option", "net asset value", "date"]
DEFAULT_CONFIG = {
    "latest_nav_url": "",
    "history_url_template": "",
    "history_days": 800,
    "stale_days": 7,
    "plan_filter": {"include": ["regular", "growth"],
                    "exclude": ["direct", "idcw", "dividend", "bonus"]},
}


def parse_date(s):
    s = (s or "").strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def fetch(url, retries=3, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (sif-pipeline)"})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError):
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def read_source(src):
    if re.match(r"https?://", src):
        return fetch(src)
    return Path(src).read_text(encoding="utf-8", errors="replace")


def _col(hdr, *keys):
    for i, h in enumerate(hdr):
        if any(k in h for k in keys):
            return i
    return None


def parse_amfi(text):
    """Parse AMFI semicolon-delimited NAV text (latest or history).

    Returns rows: {code, name, nav, date, amc, cat}. AMC and category come from
    the heading lines AMFI places between data rows; override them in
    manual_fields.csv if the headings are not what you want.
    """
    rows, hdr, amc, cat = [], None, "", ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if ";" not in line:
            if "(" in line and line.endswith(")"):
                cat = line[line.find("(") + 1:-1].strip().split(" - ")[-1].strip()
            elif re.match(r"(open|close|interval)\b", line, re.I):
                cat = line
            else:
                amc = line
            continue
        parts = [p.strip() for p in line.split(";")]
        low = [p.lower() for p in parts]
        if "scheme code" in low and any("net asset value" in p for p in low):
            hdr = low
            continue
        h = hdr or DEFAULT_HDR
        ic, iname, inav = _col(h, "scheme code"), _col(h, "scheme name"), _col(h, "net asset value")
        idate = h.index("date") if "date" in h else len(h) - 1
        iplan = h.index("plan") if "plan" in h else None
        iopt = h.index("option") if "option" in h else None
        if max(ic, iname, inav, idate) >= len(parts):
            continue
        try:
            nav = float(parts[inav].replace(",", ""))
        except ValueError:
            continue
        d = parse_date(parts[idate])
        if not parts[ic] or " " in parts[ic] or d is None or nav <= 0:
            continue
        get = lambda i: parts[i] if i is not None and i < len(parts) else ""
        isin = next((parts[i] for i, x in enumerate(h) if "isin" in x and i < len(parts)
                     and parts[i].startswith("INF")), "")
        rows.append({"code": parts[ic], "name": parts[iname], "nav": nav, "date": d,
                     "amc": amc, "cat": cat, "plan": get(iplan), "option": get(iopt), "isin": isin})
    return rows


def plan_ok(r, flt):
    n = " ".join([r["name"], r.get("plan", ""), r.get("option", "")]).lower()
    return all(t in n for t in flt["include"]) and not any(t in n for t in flt["exclude"])


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def add_rows(cache, rows, codes):
    for r in rows:
        if r["code"] in codes:
            c = cache.setdefault(r["code"], {"name": r["name"], "navs": {}})
            c["name"] = r["name"]
            c["navs"][r["date"].isoformat()] = r["nav"]


def date_ranges(start, end, step=89):
    d = start
    while d <= end:
        e = min(d + timedelta(days=step), end)
        yield d, e
        d = e + timedelta(days=1)


def years_back(d, n):
    try:
        return d.replace(year=d.year - n)
    except ValueError:
        return d.replace(year=d.year - n, day=28)


def nav_at(navs, d, tol=7):
    """Latest NAV on or before d, no more than tol days earlier."""
    best = None
    for dt, v in navs:
        if dt <= d:
            best = (dt, v)
        else:
            break
    return best if best and (d - best[0]).days <= tol else None


def cagr(navs, asof, n):
    """CAGR over n whole years ending at asof; None if the period is not complete."""
    end, start = nav_at(navs, asof), nav_at(navs, years_back(asof, n))
    if not end or not start:
        return None
    pct = round(((end[1] / start[1]) ** (1 / n) - 1) * 100, 2)
    return pct, start[1], start[0], end[1], end[0]


def load_manual():
    if not MANUAL.exists():
        return {}
    with open(MANUAL, newline="", encoding="utf-8-sig") as fh:
        return {r["scheme_code"].strip(): {k: (v or "").strip() for k, v in r.items() if k}
                for r in csv.DictReader(fh) if (r.get("scheme_code") or "").strip()}


def sync_manual(schemes, manual):
    """Append a blank row for every scheme not yet in manual_fields.csv."""
    new = [c for c in schemes if c not in manual]
    if not new:
        return 0
    exists = MANUAL.exists()
    needs_nl = exists and not MANUAL.read_bytes().endswith(b"\n")
    with open(MANUAL, "a", newline="", encoding="utf-8") as fh:
        if needs_nl:
            fh.write("\n")
        w = csv.writer(fh)
        if not exists:
            w.writerow(MANUAL_COLS)
        for c in new:
            w.writerow([c, schemes[c]["name"]] + [""] * (len(MANUAL_COLS) - 2))
    return len(new)


def build_funds(schemes, cache, manual, asof, today, stale_days=7, hist_from=None):
    funds, warns, audit, no_launch, no_manual = [], [], [], [], []
    for code, s in sorted(schemes.items()):
        navs = sorted((date.fromisoformat(d), v) for d, v in cache.get(code, {}).get("navs", {}).items())
        label = f"{code} {s['name']}"
        if not navs:
            warns.append(f"{label}: no NAV history")
            continue
        m = manual.get(code, {})
        launch = parse_date(m.get("launch", ""))
        if m.get("launch") and not launch:
            warns.append(f"{label}: could not read launch date '{m['launch']}' in manual_fields.csv")
        if not launch and hist_from and navs[0][0] > hist_from + timedelta(days=10):
            launch = navs[0][0]  # history reaches back before this scheme, so its first NAV is a good proxy
        if not launch:
            no_launch.append(label)
        f = {"id": code, "name": s["name"],
             "amc": m.get("amc") or s.get("amc") or "Unknown AMC",
             "cat": m.get("cat") or s.get("cat") or "Uncategorised"}
        if launch:
            f["launch"] = launch.isoformat()
        if s.get("isin"):
            f["isin"] = s["isin"]
        if m.get("exp"):
            try:
                f["exp"] = float(m["exp"])
            except ValueError:
                warns.append(f"{label}: expense ratio '{m['exp']}' is not a number")
        for k in ("exit", "mgr", "src"):
            if m.get(k):
                f[k] = m[k]
        if any(k not in f for k in ("exp", "exit", "mgr")):
            no_manual.append(label)
        if (today - navs[-1][0]).days > stale_days:
            warns.append(f"{label}: latest NAV is {navs[-1][0]}, which looks stale")
        for n in (1, 3, 5):
            if launch and (asof - launch).days < n * 365:
                continue
            res = cagr(navs, asof, n)
            if res:
                f[f"r{n}"] = res[0]
                audit.append(f"{label} {n}Y: {res[1]} on {res[2]} -> {res[3]} on {res[4]} = {res[0]}%")
                if abs(res[0]) > 100:
                    warns.append(f"{label}: {n}Y CAGR of {res[0]}% looks extreme, please check the NAVs")
            elif launch:
                warns.append(f"{label}: no NAV near the {n}Y start date, {n}Y return left out")
        funds.append(f)
    if no_launch:
        warns.append(f"{len(no_launch)} schemes have no launch date (set it in manual_fields.csv, or run the "
                     f"history download so it can be worked out): " + "; ".join(no_launch))
    if no_manual:
        warns.append(f"{len(no_manual)} schemes are missing expense ratio, exit load or fund manager in manual_fields.csv")
    return funds, warns, audit


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG, {}))
    return cfg


def run(args):
    cfg, today = load_config(), date.today()
    asof = parse_date(args.asof) if args.asof else today.replace(day=1) - timedelta(days=1)
    if args.latest_file:
        text = read_source(args.latest_file)
    elif cfg["latest_nav_url"]:
        text = fetch(cfg["latest_nav_url"])
    else:
        sys.exit("Set latest_nav_url in config.json (see README) or pass --latest-file.")
    latest = parse_amfi(text)
    schemes = {r["code"]: r for r in latest if plan_ok(r, cfg["plan_filter"])}
    if not schemes:
        sys.exit(f"Parsed {len(latest)} rows but none are Regular-plan Growth schemes. "
                 "Run `inspect` on the source and check plan_filter in config.json.")
    cache = load_json(CACHE, {})
    meta = load_json(META, {})
    known = [date.fromisoformat(d) for c in cache.values() for d in c["navs"]]
    codes = set(schemes)
    add_rows(cache, latest, codes)
    hist_rows = 0
    for hf in args.history_file or []:
        rows = parse_amfi(read_source(hf))
        hist_rows += len(rows)
        add_rows(cache, rows, codes)
    if cfg["history_url_template"] and not args.history_file:
        incremental = len(known) >= 2 and not args.full
        start = (max(known) - timedelta(days=7)) if incremental \
            else today - timedelta(days=int(cfg["history_days"]))
        for a, b in date_ranges(start, today):
            url = cfg["history_url_template"].format(**{"from": a.strftime("%d-%b-%Y"),
                                                        "to": b.strftime("%d-%b-%Y")})
            rows = parse_amfi(fetch(url))
            hist_rows += len(rows)
            add_rows(cache, rows, codes)
        if not incremental:
            prev = parse_date(meta.get("hist_from", ""))
            meta["hist_from"] = (min(prev, start) if prev else start).isoformat()
    CACHE.parent.mkdir(exist_ok=True)
    META.write_text(json.dumps(meta), encoding="utf-8")
    CACHE.write_text(json.dumps(cache), encoding="utf-8")
    manual = load_manual()
    added = sync_manual(schemes, manual)
    if added:
        manual = load_manual()
    funds, warns, audit = build_funds(schemes, cache, manual, asof, today, cfg["stale_days"],
                                      parse_date(meta.get("hist_from", "")))
    for nm in sorted({r["name"] for r in latest if not r["plan"] and not r["option"]
                      and "regular" not in r["name"].lower() and "direct" not in r["name"].lower()}):
        warns.append(f"AMFI does not state the plan or option for '{nm}', so it was left out")
    OUT.mkdir(exist_ok=True)
    (OUT / "sif.json").write_text(json.dumps({"asOf": asof.isoformat(), "funds": funds},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
    report = [f"Run date {today}, returns as of {asof}",
              f"{len(funds)} Regular-plan Growth schemes, {hist_rows} history rows fetched, "
              f"{added} new rows added to manual_fields.csv", "", "WARNINGS"]
    report += warns or ["none"]
    report += ["", "CAGR AUDIT (check a few against AMC factsheets)"] + (audit or ["no scheme has completed a year yet"])
    (OUT / "report.txt").write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report[:2]), f"\n{len(warns)} warnings, see output/report.txt")


def inspect(args):
    text = read_source(args.source)
    print("--- first 25 lines ---")
    print("\n".join(text.splitlines()[:25]))
    rows = parse_amfi(text)
    print(f"\n--- parsed {len(rows)} rows ---")
    print("AMCs:", sorted({r['amc'] for r in rows})[:15])
    print("Categories:", sorted({r['cat'] for r in rows})[:15])
    for r in rows[:5]:
        print(r)
    reg = [r for r in rows if plan_ok(r, load_config()["plan_filter"])]
    print(f"Regular-plan Growth rows by current plan_filter: {len(reg)}")


def selftest(_args=None):
    # Sample copied from the layout of AMFI's real SIF_NAVAll.txt
    latest = (
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date\n \n"
        "Open Ended Schemes(Equity Oriented Investment Strategies - Equity Ex-Top 100 Long-Short Fund)\n \nAltiva SIF\n \n"
        "SIF-120;INF754K30094;-;Altiva Equity Ex- Top 100 Long - Short Fund;Direct Plan;Growth;10.4446;06-Nov-2026\n"
        "SIF-121;INF754K30102;INF754K30110;Altiva Equity Ex- Top 100 Long - Short Fund;Direct Plan;IDCW;10.4446;06-Nov-2026\n"
        "SIF-122;INF754K30136;-;Altiva Equity Ex- Top 100 Long - Short Fund;Regular Plan;Growth;10.3774;06-Nov-2026\n"
        "SIF-123;INF754K30144;-;Altiva Equity Ex- Top 100 Long - Short Fund;Regular Plan;IDCW;10.3771;06-Nov-2026\n \n"
        "DynaSIF SIF\n \n"
        "SIF-143;INF579M30133;-;DynaSIF Equity Ex-Top 100 Long - Short Fund;Regular Plan;GROWTH OPTION;11.0413;06-Nov-2026\n"
        "SIF-75;INF194K30424;INF194K30507;Arudha Equity Long-Short Fund;Regular Plan;Annual IDCW;10.09;06-Nov-2026\n \n"
        "iSIF SIF\n \n"
        "SIF-33;INF109K30042;-;iSIF Equity Ex-Top 100 Long-Short Fund;;;10.21;06-Nov-2026\n")
    hist = ("Scheme Code;Scheme Name;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;"
            "Repurchase Price;Sale Price;Date\n"
            "SIF-122;x;INF754K30136;-;10.0000;10;10;30-Oct-2025\n"
            "SIF-122;x;INF754K30136;-;N.A.;;;31-Oct-2025\n"
            "SIF-122;x;INF754K30136;-;11.2000;11.2;11.2;30-Oct-2026\n")
    rows = parse_amfi(latest)
    assert len(rows) == 7, len(rows)
    assert rows[0]["amc"] == "Altiva SIF" and rows[0]["cat"] == "Equity Ex-Top 100 Long-Short Fund"
    assert rows[2]["plan"] == "Regular Plan" and rows[2]["option"] == "Growth" and rows[2]["isin"] == "INF754K30136"
    flt = DEFAULT_CONFIG["plan_filter"]
    schemes = {r["code"]: r for r in rows if plan_ok(r, flt)}
    assert sorted(schemes) == ["SIF-122", "SIF-143"], sorted(schemes)  # not Direct, IDCW, or unlabeled
    cache = {}
    add_rows(cache, rows, set(schemes))
    h = parse_amfi(hist)
    assert len(h) == 2, "N.A. NAV rows must be skipped"
    add_rows(cache, h, set(schemes))
    asof, today = date(2026, 10, 31), date(2026, 11, 8)
    navs = sorted((date.fromisoformat(d), v) for d, v in cache["SIF-122"]["navs"].items())
    assert cagr(navs, asof, 1)[0] == 12.0, cagr(navs, asof, 1)
    assert cagr(navs, asof, 3) is None, "3Y must be None when history is short"
    assert years_back(date(2028, 2, 29), 1) == date(2027, 2, 28)
    f, w, _ = build_funds(schemes, cache, {"SIF-122": {"launch": "2025-10-15", "exp": "1.9",
                          "exit": "Nil", "mgr": "Manager One"}}, asof, today)
    x = next(i for i in f if i["id"] == "SIF-122")
    assert x["r1"] == 12.0 and "r3" not in x and x["exp"] == 1.9 and x["cat"] == "Equity Ex-Top 100 Long-Short Fund"
    f2, _, _ = build_funds(schemes, cache, {"SIF-122": {"launch": "2026-06-01"}}, asof, today)
    assert "r1" not in next(i for i in f2 if i["id"] == "SIF-122"), "a scheme under 1 year old must not report 1Y"
    f3, w3, _ = build_funds(schemes, cache, {}, asof, today)
    y = next(i for i in f3 if i["id"] == "SIF-122")
    assert "launch" not in y and y["r1"] == 12.0 and any("no launch date" in t for t in w3)
    f4, _, _ = build_funds(schemes, cache, {}, asof, today, hist_from=date(2024, 9, 1))
    assert next(i for i in f4 if i["id"] == "SIF-143")["launch"] == "2026-11-06"
    global MANUAL
    old, MANUAL = MANUAL, Path(tempfile.mkdtemp()) / "manual.csv"
    try:
        assert sync_manual(schemes, {}) == 2 and sync_manual(schemes, load_manual()) == 0
    finally:
        MANUAL = old
    print("selftest passed")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--latest-file", help="local copy of the latest NAV file instead of downloading")
    r.add_argument("--history-file", action="append", help="local NAV history file (repeatable)")
    r.add_argument("--asof", help="return date YYYY-MM-DD, default last day of previous month")
    r.add_argument("--full", action="store_true", help="re-download the whole history window")
    r.set_defaults(fn=run)
    i = sub.add_parser("inspect")
    i.add_argument("source", help="URL or file path")
    i.set_defaults(fn=inspect)
    sub.add_parser("selftest").set_defaults(fn=selftest)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
