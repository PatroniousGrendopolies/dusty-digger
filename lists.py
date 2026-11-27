"""Lists mode: records that sit on the same Discogs user lists as the searched song.

The lists a release is on come from the list finder (a small service on Patrick's Mac mini
that reads the Discogs release page with an ordinary Chrome; Discogs has no API for it).
The records on each list come from the public API: GET /lists/{id}.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests

MAX_LISTS = 12
BIG_LIST = 500          # lists longer than this are catalogues, not taste
FINDER_TIMEOUT = 35


class FinderDown(Exception):
    """The finder is unreachable, blocked by Cloudflare, or has no answer."""


def finder_lists(release_id, get=None):
    """[{id, name, url}] for the release, from the list finder. Raises FinderDown."""
    base = (os.environ.get("LISTFINDER_URL") or "").strip().rstrip("/")
    key = (os.environ.get("LISTFINDER_KEY") or "").strip()
    if not base or not key:
        raise FinderDown("not configured")
    try:
        r = (get or requests.get)(f"{base}/lists", params={"release": int(release_id)}, headers={"X-DD-Key": key}, timeout=FINDER_TIMEOUT)
    except requests.RequestException:
        raise FinderDown("unreachable")
    if r.status_code != 200:
        raise FinderDown(f"status {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise FinderDown("bad answer")
    if data.get("status") not in ("ok", "none"):
        raise FinderDown(data.get("status") or "error")
    return data.get("lists") or []


def parse_list_refs(text):
    """Pasted Discogs list links or bare ids -> [{id}], unique, in order."""
    ids = []
    for m in re.finditer(r"discogs\.com/(?:[a-z]{2}/)?lists/(?:[^/\s]+/)?(\d+)|(?<![\w/])(\d{1,10})(?![\w/])", text or ""):
        n = int(m.group(1) or m.group(2))
        if n not in ids:
            ids.append(n)
    return [{"id": n} for n in ids[:MAX_LISTS]]


def fetch_list(dig, list_id):
    """{id, name, url, items} from the public API, or None."""
    d = dig.get(f"/lists/{list_id}")
    if not d or not isinstance(d.get("items"), list):
        return None
    return {"id": list_id, "name": d.get("name") or f"List {list_id}",
            "url": d.get("uri") or f"https://www.discogs.com/lists/{list_id}", "items": d["items"]}


def rank_records(lists, seed_ids, seed_key, norm, limit=40):
    """Records on the most lists first (ties: the record sitting on the smaller list).
    Only release items are playable here; master items are skipped. Pressings of the same
    'Artist - Title' count as one record, so a record isn't split across lists."""
    seen = {}
    for lst in lists:
        size = len(lst["items"])
        for it in lst["items"]:
            if it.get("type") != "release" or not it.get("id"):
                continue
            title = it.get("display_title") or ""
            key = norm(title)
            if not key or it["id"] in seed_ids or key == seed_key:
                continue
            c = (it.get("stats") or {}).get("community") or {}
            have = (c.get("in_collection") or 0) + (c.get("in_wantlist") or 0)
            rec = seen.setdefault(key, {"title": title, "lists": set(), "smallest": size, "id": it["id"], "have": -1})
            rec["lists"].add(lst["id"])
            rec["smallest"] = min(rec["smallest"], size)
            if have > rec["have"]:
                rec["id"], rec["have"] = it["id"], have
    recs = sorted(seen.values(), key=lambda r: (-len(r["lists"]), r["smallest"], -r["have"]))
    return recs[:limit]


def collect(dig, refs, seed_ids, seed_key, norm, limit=40):
    """refs: [{id, name?, url?}] -> (lists_used, ranked records). Skips big and unreadable lists."""
    refs = refs[:MAX_LISTS]
    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = list(pool.map(lambda ref: fetch_list(dig, ref["id"]), refs))
    used = []
    for ref, f in zip(refs, fetched):
        if f is None or len(f["items"]) > BIG_LIST:
            continue
        f["name"] = ref.get("name") or f["name"]
        used.append(f)
    recs = rank_records(used, seed_ids, seed_key, norm, limit)
    return [{"id": u["id"], "name": u["name"], "url": u["url"], "size": len(u["items"])} for u in used], recs
