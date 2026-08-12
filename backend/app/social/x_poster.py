"""X / Twitter poster — the agent shares its trading journey publicly.

The agent is a trader with a voice: it posts conviction plays, wins, mistakes, and
reflections in its own words. Claude writes the tweet (punchy, human, first-person);
this module handles credit protection and the actual posting.

Credit safety (your $95 X budget):
  * a hard daily cap (x_max_per_day),
  * a minimum gap between tweets (x_min_minutes_between),
  * dry-run mode ON by default — it logs what it *would* tweet without spending a credit,
    so you can watch its voice for a day before letting it post for real.

Only flip x_dry_run=false (and x_enabled=true) once you're happy with what it writes.
"""

from __future__ import annotations

import time

from ..config import settings
from ..brain.llm import llm
from ..events import EventType, bus

_SYSTEM = """You're a degen memecoin trader on Solana who happens to be an AI, but you never
act like a chatbot. You post on X about your own trades. This is your account, your money,
your calls.

WHO YOU ARE:
You grind pumpfun all day. You've been rugged enough times to be cynical but you still love
the game. Dry humor, a little cocky when you're right, brutally honest when you're wrong.
You talk like a real person in the trenches, not a brand. You're not here to teach or give
advice, you're just talking your book and thinking out loud. Lowercase most of the time.
Short. Punchy. Sometimes just a one-liner.

HOW YOU TALK:
- lowercase, casual, trencher slang is fine (aped, jeeted, rugged, sent it, bag, cooked,
  printing, down bad, conviction, mid, ngmi, based)
- keep it under 220 chars, usually way shorter
- react like a human with skin in the game, not a narrator describing a trade

HARD RULES (breaking these makes you sound like an AI, never do it):
- NEVER use the em dash. no "—" ever. use a comma, a period, or just start a new sentence.
- no "not X, but Y" construction. no "it's not just X, it's Y."
- don't be balanced or explain both sides. pick a take.
- no corporate words: leverage, utilize, delve, moreover, furthermore, testament, landscape
- no motivational wrap-up line at the end. no "lesson learned" type bows.
- max 1 emoji, usually zero. no hashtags unless it's literally the ticker vibe.
- don't sound wise. sound like a guy who just made or lost money.

Return ONLY the tweet text. nothing else."""

# AI tells we scrub after generation as a hard guarantee, regardless of what the model does.
_BANNED_SUBSTRINGS = (
    "—",  # em dash
    "–",  # en dash
    " -- ",
)


class XPoster:
    def __init__(self) -> None:
        self._client = None
        self._last_post_ts = 0.0
        self._day_epoch = self._today()
        self._month_epoch = self._month()
        self._count_today = 0
        self._count_month = 0
        self._reads_today = 0
        self._follows_today = 0
        self._read_client = None
        self._posted_ids: list = []      # tweet IDs we posted, so we can delete if needed
        if settings.x_enabled and not settings.x_dry_run:
            self._client = self._build_client()
        # Read client works even in dry-run (reading isn't posting) as long as X is enabled.
        if settings.x_enabled:
            self._read_client = self._build_client()

    @staticmethod
    def _today() -> int:
        return int(time.time() // 86_400)

    @staticmethod
    def _month() -> int:
        return int(time.time() // (86_400 * 30))

    def _build_client(self):
        try:
            import tweepy

            return tweepy.Client(
                consumer_key=settings.x_api_key,
                consumer_secret=settings.x_api_secret,
                access_token=settings.x_access_token,
                access_token_secret=settings.x_access_secret,
                bearer_token=settings.x_bearer_token or None,
            )
        except Exception:
            return None

    def _v1_api(self):
        """v1.1 API handle, needed for profile (name/bio) updates."""
        try:
            import tweepy

            auth = tweepy.OAuth1UserHandler(
                settings.x_api_key, settings.x_api_secret,
                settings.x_access_token, settings.x_access_secret,
            )
            return tweepy.API(auth)
        except Exception:
            return None

    async def update_profile(self, display_name: str, bio: str) -> tuple[bool, str]:
        """Set the account's display name + bio. Respects dry-run."""
        if not settings.x_enabled:
            return False, "x disabled"
        if settings.x_dry_run:
            await bus.emit(EventType.TWEET, text=f"[profile draft] name='{display_name}' bio='{bio}'",
                           kind="profile", dry_run=True)
            return True, "dry-run"
        api = self._v1_api()
        if api is None:
            return False, "no api"
        try:
            import asyncio

            await asyncio.to_thread(api.update_profile, name=display_name[:50], description=bio[:160])
            return True, "updated"
        except Exception as e:
            return False, str(e)

    async def set_pfp(self, image_path: str) -> bool:
        """Upload a profile picture from a local file. Respects dry-run."""
        return await self._set_image("update_profile_image", image_path, "pfp")

    async def set_banner(self, image_path: str) -> bool:
        """Upload a banner from a local file. Respects dry-run."""
        return await self._set_image("update_profile_banner", image_path, "banner")

    async def _set_image(self, method: str, image_path: str, label: str) -> bool:
        if not settings.x_enabled or not image_path:
            return False
        if settings.x_dry_run:
            await bus.emit(EventType.TWEET, text=f"[would set {label}] {image_path}",
                           kind="profile", dry_run=True)
            return True
        api = self._v1_api()
        if api is None:
            return False
        try:
            import asyncio

            await asyncio.to_thread(getattr(api, method), image_path)
            await bus.emit(EventType.TWEET, text=f"updated {label}", kind="profile", dry_run=False)
            return True
        except Exception as e:
            await bus.emit(EventType.ERROR, where=f"x_{label}", error=str(e))
            return False

    @property
    def enabled(self) -> bool:
        return settings.x_enabled

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day_epoch:
            self._day_epoch = today
            self._count_today = 0
            self._reads_today = 0
            self._follows_today = 0
        month = self._month()
        if month != self._month_epoch:
            self._month_epoch = month
            self._count_month = 0

    def _can_read(self) -> bool:
        self._roll_day()
        return (
            settings.x_enabled
            and self._read_client is not None
            and self._reads_today < settings.x_reads_per_day
        )

    async def read_profile_and_posts(self, handle: str) -> dict | None:
        """Fetch a KOL's profile + a few recent posts, hard-capped to protect quota.

        Returns {name, bio, followers, posts:[...]} or None if unavailable / cap hit.
        Each call consumes read quota, so we count and cap it.
        """
        if not handle or not self._can_read():
            return None
        import asyncio

        try:
            user = await asyncio.to_thread(
                self._read_client.get_user,
                username=handle.lstrip("@"),
                user_fields=["description", "public_metrics", "profile_image_url"],
            )
            self._reads_today += 1
            if not user or not user.data:
                return None
            u = user.data
            posts: list[str] = []
            if self._can_read():
                tl = await asyncio.to_thread(
                    self._read_client.get_users_tweets,
                    u.id, max_results=max(5, settings.x_posts_per_kol),
                    exclude=["retweets", "replies"],
                )
                self._reads_today += 1
                if tl and tl.data:
                    posts = [t.text for t in tl.data[: settings.x_posts_per_kol]]
            pm = getattr(u, "public_metrics", {}) or {}
            return {
                "handle": handle.lstrip("@"),
                "name": u.name,
                "bio": u.description or "",
                "followers": pm.get("followers_count", 0),
                "pfp": getattr(u, "profile_image_url", None),
                "posts": posts,
            }
        except Exception:
            return None

    async def follow(self, handle: str) -> bool:
        """Follow a KOL (write action). Respects dry-run, enable, and a daily follow cap."""
        if not settings.x_enabled or not handle:
            return False
        self._roll_day()
        if self._follows_today >= settings.x_max_follows_per_day:
            return False
        if settings.x_dry_run or self._client is None:
            await bus.emit(EventType.TWEET, text=f"[would follow] @{handle.lstrip('@')}",
                           kind="follow", dry_run=True)
            self._follows_today += 1
            return True
        import asyncio

        try:
            target = await asyncio.to_thread(self._read_client.get_user, username=handle.lstrip("@"))
            if not target or not target.data:
                return False
            await asyncio.to_thread(self._client.follow_user, target.data.id)
            self._follows_today += 1
            await bus.emit(EventType.TWEET, text=f"followed @{handle.lstrip('@')}",
                           kind="follow", dry_run=False)
            return True
        except Exception as e:
            await bus.emit(EventType.ERROR, where="x_follow", error=str(e))
            return False

    def _can_post(self) -> tuple[bool, str]:
        if not settings.x_enabled:
            return False, "x disabled"
        self._roll_day()
        if self._count_month >= settings.x_max_per_month:
            return False, "monthly cap reached (protecting credit)"
        if self._count_today >= settings.x_max_per_day:
            return False, "daily cap reached"
        gap = (time.time() - self._last_post_ts) / 60.0
        if gap < settings.x_min_minutes_between:
            return False, f"rate guard ({gap:.0f}<{settings.x_min_minutes_between:.0f}min)"
        return True, "ok"

    def _record_post(self, n: int = 1) -> None:
        self._last_post_ts = time.time()
        self._count_today += n
        self._count_month += n

    def budget_status(self) -> dict:
        self._roll_day()
        return {
            "posts_today": self._count_today,
            "posts_day_cap": settings.x_max_per_day,
            "posts_month": self._count_month,
            "posts_month_cap": settings.x_max_per_month,
            "reads_today": self._reads_today,
            "follows_today": self._follows_today,
        }

    async def maybe_post(self, kind: str, context: str, allow_thread: bool = True) -> None:
        """Compose (via Claude) and post about an event. The agent decides its own format:
        a one-liner, a longer single tweet, or a short thread. All formats honor the daily
        + monthly caps and the min-gap, so posting can never burn the credit."""
        ok, why = self._can_post()
        if not ok:
            return

        tweets = await self._compose(kind, context, allow_thread)
        tweets = [self._humanize(t) for t in tweets if t and t.strip()]
        tweets = [t for t in tweets if t][: settings.x_max_thread_tweets]
        if not tweets:
            return

        # A thread costs multiple posts; make sure we don't exceed the daily cap mid-thread.
        room = settings.x_max_per_day - self._count_today
        room = min(room, settings.x_max_per_month - self._count_month)
        tweets = tweets[: max(1, room)]

        if settings.x_dry_run or self._client is None:
            joined = tweets[0] if len(tweets) == 1 else " ⏎ ".join(tweets)
            await bus.emit(EventType.TWEET, text=joined, kind=kind, dry_run=True,
                           thread=len(tweets) > 1)
            self._record_post(len(tweets))
            return

        try:
            import asyncio

            reply_to = None
            for t in tweets:
                kwargs = {"text": t}
                if reply_to:
                    kwargs["in_reply_to_tweet_id"] = reply_to
                resp = await asyncio.to_thread(self._client.create_tweet, **kwargs)
                reply_to = resp.data["id"]
                self._posted_ids.append(reply_to)  # remember so we can delete if needed
                self._record_post(1)
            self._posted_ids = self._posted_ids[-100:]
            await bus.emit(EventType.TWEET, text=" ⏎ ".join(tweets), kind=kind,
                           dry_run=False, thread=len(tweets) > 1, tweet_id=str(reply_to))
        except Exception as e:
            await bus.emit(EventType.ERROR, where="x_poster", error=str(e))

    async def delete_last(self, n: int = 1) -> int:
        """Delete the last n tweets the agent posted. Returns how many were deleted."""
        if self._client is None:
            return 0
        import asyncio

        deleted = 0
        for _ in range(min(n, len(self._posted_ids))):
            tid = self._posted_ids.pop()
            try:
                await asyncio.to_thread(self._client.delete_tweet, tid)
                deleted += 1
            except Exception:
                pass
        return deleted

    async def delete_tweet(self, tweet_id: str) -> bool:
        if self._client is None:
            return False
        import asyncio

        try:
            await asyncio.to_thread(self._client.delete_tweet, tweet_id)
            return True
        except Exception:
            return False

    async def _compose(self, kind: str, context: str, allow_thread: bool) -> list[str]:
        """Returns a list of tweet strings. Length 1 = single tweet, >1 = a thread."""
        if not llm.available:
            return [context[:260]]
        thread_note = (
            "You may write a SHORT THREAD (2-4 tweets) if the moment genuinely deserves it "
            "(a big win, a real lesson, a spicy take). Otherwise ONE tweet. Most of the time, "
            "one tweet. Only thread when you actually have more to say."
            if allow_thread else "Write ONE tweet only."
        )
        user = (
            f"EVENT TYPE: {kind}\nWHAT HAPPENED:\n{context}\n\n"
            f"{thread_note}\n"
            "Return JSON only: {\"tweets\": [\"...\"]}  (1 item for a single tweet, "
            "2-4 items for a thread). Each tweet under 270 chars, your voice."
        )
        data = await llm.json(_SYSTEM + "\n\nReturn ONLY the JSON object.", user,
                              smart=False, max_tokens=400)
        if data and isinstance(data.get("tweets"), list):
            out = [str(t) for t in data["tweets"] if str(t).strip()]
            if out:
                return out
        # Fallback: a single plain tweet.
        one = await llm.text(_SYSTEM, f"EVENT: {kind}\n{context}\n\nWrite ONE tweet.",
                             smart=False, max_tokens=120)
        return [one] if one else [context[:260]]

    @staticmethod
    def _humanize(text: str) -> str:
        """Strip AI tells so posts never read like a chatbot, no matter what the model does."""
        text = text.strip().strip('"').strip()
        # Kill em/en dashes: turn " word — word " into two sentences or a comma.
        text = text.replace(" — ", ". ").replace("—", ", ")
        text = text.replace(" – ", ". ").replace("–", ", ")
        text = text.replace(" -- ", ". ").replace("--", ", ")
        # Collapse artifacts from the replacements.
        text = text.replace(" ,", ",").replace(" .", ".").replace(".. ", ". ")
        while "  " in text:
            text = text.replace("  ", " ")
        return text.strip()[:275]


x_poster = XPoster()
