#!/usr/bin/env python3
"""Review Pulse for Steam: posts each NEW Steam review of one game to a Discord webhook.

Free and MIT-licensed. No dependencies beyond the Python standard library.

  python review_pulse.py --app 620            # one check, then exit
  python review_pulse.py --app 620 --every 15 # check every 15 minutes

Settings can also come from environment variables or a .env file next to this script:
STEAM_APP_ID, DISCORD_WEBHOOK_URL, CHECK_EVERY_MINUTES, STATE_FILE, FIRST_RUN_POSTS, MAX_POSTS_PER_RUN.
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

__version__ = "1.0.0"
UA = {"User-Agent": "review-pulse/" + __version__}
STEAM = "https://store.steampowered.com"
GREEN, RED = 0x2ECC71, 0xE74C3C


# ---------- small helpers ----------
def load_dotenv(path=".env"):
    """Read KEY=VALUE lines into os.environ without overriding variables that are already set."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if v and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


def http_json(url, params=None, data=None, timeout=20):
    """GET (or POST JSON when data is given). Returns (status, parsed_json_or_None, headers)."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={**UA, **({"Content-Type": "application/json"} if body else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            j = json.loads(raw) if raw else None
        except ValueError:
            j = None
        return e.code, j, dict(e.headers or {})


# ---------- Steam ----------
def fetch_summary(appid, get=http_json):
    """Steam's own query_summary for all languages and purchase types (num_per_page=0 returns only the summary)."""
    st, j, _ = get(f"{STEAM}/appreviews/{appid}", dict(json=1, language="all", purchase_type="all",
                                                     num_per_page=0, filter="all"))
    if st != 200 or not j or j.get("success") != 1:
        raise RuntimeError(f"Steam review summary request failed (HTTP {st})")
    return j.get("query_summary", {})


def fetch_recent(appid, pages=3, get=http_json):
    """Newest reviews first (filter=recent), up to pages x 100."""
    out, cursor = [], "*"
    for _ in range(pages):
        st, j, _ = get(f"{STEAM}/appreviews/{appid}", dict(json=1, filter="recent", language="all",
                                                         purchase_type="all", num_per_page=100, cursor=cursor))
        if st != 200 or not j or j.get("success") != 1:
            raise RuntimeError(f"Steam reviews request failed (HTTP {st})")
        batch = j.get("reviews", [])
        out += batch
        nxt = j.get("cursor")
        if not batch or not nxt or nxt == cursor:
            break
        cursor = nxt
    return out


def app_name(appid, get=http_json):
    try:
        st, j, _ = get(f"{STEAM}/api/appdetails", {"appids": appid, "filters": "basic"})
        return j[str(appid)]["data"]["name"]
    except Exception:
        return f"App {appid}"


# ---------- formatting (pure) ----------
def excerpt(text, n=300):
    t = re.sub(r"\[/?[^\]]{1,20}\]", "", text or "")   # drop Steam BBCode tags like [b] [/i] [h1]
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def hours(minutes):
    if minutes is None:
        return "n/a"
    h = minutes / 60
    return f"{h:.1f} h" if h < 100 else f"{h:,.0f} h"


def summary_line(s):
    """Only numbers Steam returns. '% positive' is total_positive / total_reviews from the same response."""
    total, pos = s.get("total_reviews", 0), s.get("total_positive", 0)
    pct = f"{pos / total * 100:.0f}% positive" if total else "no reviews yet"
    return f"Steam totals: {total:,} reviews · {pct} · {s.get('review_score_desc', 'n/a')}"


def review_url(r, appid):
    return f"https://steamcommunity.com/profiles/{r['author']['steamid']}/recommended/{appid}/"


def embed(r, appid, game, summ):
    up = bool(r.get("voted_up"))
    return {
        "title": f"{'👍 Recommended' if up else '👎 Not recommended'} · {game}"[:256],
        "url": review_url(r, appid),
        "description": excerpt(r.get("review", "")) or "(no text)",
        "color": GREEN if up else RED,
        "fields": [
            {"name": "Playtime at review", "value": hours(r.get("author", {}).get("playtime_at_review")), "inline": True},
            {"name": "Language", "value": str(r.get("language", "n/a")), "inline": True},
        ],
        "footer": {"text": summary_line(summ)},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r.get("timestamp_created", 0))),
    }


# ---------- state + selection (pure) ----------
def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(path, st):
    st["seen"] = st["seen"][-5000:]          # keep the file small
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def select_new(reviews, state, first_run_posts=3):
    """Returns (new_reviews_oldest_first, state). On the first run, only the newest `first_run_posts`
    reviews count as new; anything older than them is the baseline and is never posted."""
    reviews = sorted({r["recommendationid"]: r for r in reviews}.values(), key=lambda r: r.get("timestamp_created", 0))
    if state is None:
        keep = reviews[-first_run_posts:] if first_run_posts > 0 else []
        baseline = keep[0]["timestamp_created"] if keep else (reviews[-1]["timestamp_created"] + 1 if reviews else 0)
        state = {"since": baseline, "seen": []}
    seen = set(state["seen"])
    new = [r for r in reviews if r["recommendationid"] not in seen and r.get("timestamp_created", 0) >= state["since"]]
    return new, state


# ---------- Discord ----------
def post(hook, embeds, tries=4, sleep=time.sleep, send=http_json):
    """True only when Discord answers 2xx. 429 waits Retry-After and retries. Mentions are never pinged."""
    for attempt in range(tries):
        try:
            st, j, h = send(hook + ("&" if "?" in hook else "?") + "wait=true",
                            data={"embeds": embeds, "allowed_mentions": {"parse": []}})
        except Exception as e:
            print(f"  ! could not reach Discord ({type(e).__name__})")
            return False
        if 200 <= st < 300:
            return True
        if st == 429 and attempt < tries - 1:
            try:
                wait = float((j or {}).get("retry_after") or h.get("Retry-After") or 1)
            except (TypeError, ValueError):
                wait = 1.0
            sleep(min(max(wait, 0.5), 60))
            continue
        hint = {401: "the webhook link is invalid", 404: "webhook not found (deleted or wrong link)"}.get(st, "")
        print(f"  ! Discord answered HTTP {st} {hint}".rstrip())
        return False
    return False


def run_once(cfg, get=http_json, send=http_json, sleep=time.sleep):
    appid, hook = cfg["app"], cfg["hook"]
    summ = fetch_summary(appid, get)
    reviews = fetch_recent(appid, cfg.get("pages", 3), get)
    state = load_state(cfg["state"])
    new, state = select_new(reviews, state, cfg.get("first_run_posts", 3))
    print(summary_line(summ))
    new = new[: cfg.get("max_posts", 20)]          # the rest stays unseen and goes out next check
    if not new:
        print("No new reviews.")
        save_state(cfg["state"], state)
        return 0
    if not hook:
        for r in new:
            print(f"  {'+' if r.get('voted_up') else '-'} {excerpt(r.get('review', ''), 90)}")
        print(f"{len(new)} new review(s). Set DISCORD_WEBHOOK_URL to post them (not marked as seen).")
        save_state(cfg["state"], state)
        return 0
    game, posted = app_name(appid, get), 0
    for i in range(0, len(new), 10):                  # Discord allows 10 embeds per message
        chunk = new[i:i + 10]
        if not post(hook, [embed(r, appid, game, summ) for r in chunk], sleep=sleep, send=send):
            print("  ! Not posted; these reviews will be retried on the next check.")
            break
        state["seen"] += [r["recommendationid"] for r in chunk]   # only after Discord accepted them
        save_state(cfg["state"], state)
        posted += len(chunk)
    save_state(cfg["state"], state)
    print(f"Posted {posted} new review(s) to Discord.")
    return posted


def main(argv=None):
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    ap = argparse.ArgumentParser(description="Post new Steam reviews of one game to a Discord webhook.")
    ap.add_argument("--app", default=os.getenv("STEAM_APP_ID"), help="Steam app ID (the number in the store URL)")
    ap.add_argument("--every", type=float, default=float(os.getenv("CHECK_EVERY_MINUTES") or 0),
                    help="check every N minutes; 0 = one check, then exit (default)")
    ap.add_argument("--state", default=os.getenv("STATE_FILE", "review_pulse_state.json"))
    ap.add_argument("--first-run-posts", type=int, default=int(os.getenv("FIRST_RUN_POSTS") or 3),
                    help="on the very first run, post this many of the newest reviews (default 3)")
    ap.add_argument("--max-posts", type=int, default=int(os.getenv("MAX_POSTS_PER_RUN") or 20))
    a = ap.parse_args(argv)
    if not a.app or not str(a.app).isdigit():
        sys.exit("Set STEAM_APP_ID (or --app) to your game's Steam app ID, for example 620.")
    if os.getenv("DATA_DIR"):
        a.state = os.path.join(os.environ["DATA_DIR"], os.path.basename(a.state))
    cfg = dict(app=str(a.app), hook=os.getenv("DISCORD_WEBHOOK_URL", ""), state=a.state,
               first_run_posts=a.first_run_posts, max_posts=a.max_posts)
    while True:
        try:
            run_once(cfg)
        except Exception as e:                      # one bad check never stops the loop
            print(f"  ! check failed: {e}")
            if not a.every:
                return 1
        if not a.every:
            return 0
        time.sleep(a.every * 60)


if __name__ == "__main__":
    sys.exit(main())
