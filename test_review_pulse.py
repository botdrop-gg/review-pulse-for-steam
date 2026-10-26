import json, os, tempfile, unittest
import review_pulse as rp

def rev(i, ts, up=True, text="Great game", pt=120, lang="english"):
    return {"recommendationid": str(i), "timestamp_created": ts, "voted_up": up, "review": text,
            "language": lang, "author": {"steamid": "7656119800000000" + str(i % 10), "playtime_at_review": pt}}

SUMM = {"total_reviews": 200, "total_positive": 180, "total_negative": 20, "review_score_desc": "Very Positive"}

class FakeSteam:
    """Mimics IUserReviewsService/GetAppReviews: {"response": {query_summary, reviews, cursor}}."""
    def __init__(self, reviews, summ=SUMM): self.reviews, self.summ, self.queries = reviews, summ, []
    def __call__(self, url, params=None, data=None, timeout=20):
        if "appdetails" in url: return 200, {params["appids"]: {"data": {"name": "Test Game"}}}, {}
        assert url == rp.REVIEWS_API, url
        q = json.loads(params["input_json"]); self.queries.append(q)
        if q["cursor"] != "*": return 200, {"response": {"query_summary": self.summ, "reviews": [], "cursor": q["cursor"]}}, {}
        newest = sorted(self.reviews, key=lambda r: -r["timestamp_created"])[: q["num_per_page"]]
        return 200, {"response": {"query_summary": self.summ, "reviews": newest, "cursor": "next"}}, {}

class FakeDiscord:
    def __init__(self, codes=None): self.codes, self.calls = list(codes or []), []
    def __call__(self, url, params=None, data=None, timeout=20):
        self.calls.append(data); code = self.codes.pop(0) if self.codes else 204
        return code, ({"retry_after": 0.01} if code == 429 else None), {}

class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.state = os.path.join(self.d, "s.json")
        self.cfg = dict(app="620", hook="https://discord.com/api/webhooks/1/x", state=self.state, first_run_posts=2)

    def test_excerpt_strips_bbcode_and_cuts(self):
        self.assertEqual(rp.excerpt("[b]Hi[/b]\n\n there"), "Hi there")
        e = rp.excerpt("x" * 500, 300); self.assertEqual(len(e), 300); self.assertTrue(e.endswith("…"))

    def test_summary_line_uses_steam_numbers_only(self):
        self.assertEqual(rp.summary_line(SUMM), "Steam totals: 200 reviews · 90% positive · Very Positive")
        self.assertIn("no reviews yet", rp.summary_line({"total_reviews": 0, "review_score_desc": "No user reviews"}))

    def test_embed_fields(self):
        e = rp.embed(rev(1, 100, up=False, pt=90, lang="german"), "620", "Portal 2", SUMM)
        self.assertTrue(e["title"].startswith("👎")); self.assertEqual(e["color"], rp.RED)
        self.assertEqual(e["fields"][0]["value"], "1.5 h"); self.assertEqual(e["fields"][1]["value"], "german")
        self.assertTrue(e["url"].endswith("/recommended/620/")); self.assertIn("Very Positive", e["footer"]["text"])
        self.assertEqual(rp.hours(None), "n/a")

    def test_first_run_posts_only_newest_n_then_no_duplicates(self):
        steam = FakeSteam([rev(i, 1000 + i) for i in range(5)]); dc = FakeDiscord()
        self.assertEqual(rp.run_once(self.cfg, get=steam, send=dc), 2)
        titles = [e["description"] for e in dc.calls[0]["embeds"]]; self.assertEqual(len(titles), 2)
        self.assertEqual(rp.run_once(self.cfg, get=steam, send=dc), 0); self.assertEqual(len(dc.calls), 1)
        steam.reviews.append(rev(9, 2000, text="New one"))
        self.assertEqual(rp.run_once(self.cfg, get=steam, send=dc), 1)
        self.assertEqual(dc.calls[-1]["embeds"][0]["description"], "New one")

    def test_failed_post_is_not_marked_seen_and_is_retried(self):
        steam = FakeSteam([rev(i, 1000 + i) for i in range(3)])
        self.assertEqual(rp.run_once(self.cfg, get=steam, send=FakeDiscord([404])), 0)
        self.assertEqual(json.load(open(self.state))["seen"], [])
        dc = FakeDiscord(); self.assertEqual(rp.run_once(self.cfg, get=steam, send=dc), 2)

    def test_rate_limit_retries(self):
        dc = FakeDiscord([429, 204]); self.assertTrue(rp.post("h", [{}], sleep=lambda s: None, send=dc)); self.assertEqual(len(dc.calls), 2)

    def test_mentions_never_ping(self):
        steam = FakeSteam([rev(1, 1000, text="@everyone free keys")]); dc = FakeDiscord()
        rp.run_once(self.cfg, get=steam, send=dc); self.assertEqual(dc.calls[0]["allowed_mentions"], {"parse": []})

    def test_batches_of_ten_and_max_posts(self):
        steam = FakeSteam([rev(i, 1000 + i) for i in range(30)]); dc = FakeDiscord()
        cfg = dict(self.cfg, first_run_posts=25, max_posts=20)
        self.assertEqual(rp.run_once(cfg, get=steam, send=dc), 20); self.assertEqual([len(c["embeds"]) for c in dc.calls], [10, 10])
        self.assertEqual(rp.run_once(cfg, get=steam, send=dc), 5)   # the rest go out on the next check

    def test_oldest_first_and_dedupe_input(self):
        new, _ = rp.select_new([rev(2, 20), rev(1, 10), rev(2, 20)], None, 5)
        self.assertEqual([r["recommendationid"] for r in new], ["1", "2"])

    def test_no_hook_does_not_mark_seen(self):
        steam = FakeSteam([rev(i, 1000 + i) for i in range(3)])
        rp.run_once(dict(self.cfg, hook=""), get=steam, send=FakeDiscord())
        self.assertEqual(json.load(open(self.state))["seen"], [])

    def test_uses_new_steam_reviews_api(self):
        # Steam retired store.steampowered.com/appreviews on Oct 22, 2026; reviews now come from IUserReviewsService.
        self.assertEqual(rp.REVIEWS_API, "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/")
        steam = FakeSteam([rev(i, 1000 + i) for i in range(3)])
        self.assertEqual(rp.fetch_summary("620", get=steam), SUMM)
        self.assertEqual(len(rp.fetch_recent("620", 3, get=steam)), 3)
        for q in steam.queries:
            self.assertEqual((q["appid"], q["filter"], q["languages"], q["purchase_type"]), (620, 1, ["all"], 1))
        self.assertEqual([q["cursor"] for q in steam.queries[1:]], ["*", "next"])

    def test_old_endpoint_redirect_or_unknown_app_gives_clear_error(self):
        # the old address now answers with the store home page (HTML), parsed as no JSON
        with self.assertRaisesRegex(RuntimeError, "HTTP 200"):
            rp.fetch_summary("620", get=lambda *a, **k: (200, None, {}))
        with self.assertRaisesRegex(RuntimeError, "No Steam game found with app id 999999999"):
            rp.fetch_summary("999999999", get=lambda *a, **k: (200, {"response": {}}, {}))

if __name__ == "__main__": unittest.main()
