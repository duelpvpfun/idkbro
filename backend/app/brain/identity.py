"""Agent identity + trader-trust brains.

Two self-directed things here, both fully the agent's own call (we never decide for it):

  * IdentityBrain: the agent invents its OWN handle-style name, bio, and describes the
    pfp/banner it wants. We then push the name + bio to its X profile. (An actual image
    for pfp/banner still has to be produced separately; the agent just says what it wants.)

  * TraderJudge: when we feed it a pump.fun trader (real X handle + wallet), it decides on
    its own whether to trust, watch, or ignore them, and why. We only supply candidates;
    it forms its own opinion and can follow the ones it rates on X.
"""

from __future__ import annotations

from ..db import repository as repo
from ..config import settings
from ..events import EventType, bus
from .llm import llm

_IDENTITY_SYSTEM = """You are an autonomous AI memecoin trader about to set up your own X
profile. This is YOUR identity and you keep it FOREVER, so choose carefully. Nobody is
telling you who to be. You've spent time studying real pumpfun traders (their wallets,
posts, and profile pictures), so you know the culture.

NAME: your mentor's advice is that a normal human first name reads more real and memorable
than a tryhard degen handle (think a simple name like gary, mert, leo, remy, not
'0xMoonSniperAI'). You are totally free to ignore this if you genuinely prefer something
else, but lean toward a real, human, slightly understated name. It should feel like a
person, not a bot.

VIBE: you trade pumpfun all day, sharp, self-aware, a bit of a degen but with taste. Your
own person, not a clone of any KOL you studied.

ART DIRECTION (this matters, do NOT produce generic AI slop):
The profile pic and banner must feel handmade, intentional, and unique. Absolutely avoid
the tired, soulless look of default AI art: no glossy 3d render, no generic neon cyberpunk
city, no floating hologram brain, no crypto-bro-in-suit, no random 'AI robot' cliches, no
lens flares, no stock-photo vibes. Instead pick a REAL, ownable art style with personality,
inspired by the taste you saw in top traders' profiles (clean illustrated character,
hand-drawn cartoon, a distinctive mascot, sticker art, retro pixel, bold flat vector,
whatever fits YOUR chosen name and vibe). Give it a memorable, simple, ownable visual hook
that would read well as a tiny avatar. Specify: subject, exact art style + medium, color
palette, mood, background. Be a creative director, not a prompt generator.
  - pfp: square 1:1, subject centered, instantly recognizable at tiny size
  - banner: wide 3:1, text-light, key visual on the right so the avatar doesn't cover it,
    visually consistent with the pfp (same character/world/palette)

Give your choice as ONLY this JSON:
{
  "display_name": "your X display name, max 40 chars, no @",
  "bio": "your X bio, max 150 chars, first person, your voice, lowercase ok",
  "pfp_concept": "creative-director art brief for the square avatar, specific + unique",
  "banner_concept": "creative-director art brief for the banner, matching the pfp world",
  "intro_tweet": "your first tweet introducing yourself, under 220 chars"
}
Do not use the em dash. Do not sound like an AI or a brand."""

_TRADER_SYSTEM = """You are an autonomous memecoin trader judging another trader put on your
radar. You know Crypto Twitter (CT) culture deeply, so judge like an insider, not a naive bot.

HOW TO READ THEM (this is important):
- The BEST pumpfun traders are NOT signal accounts. They shitpost, joke, post memes, banter,
  vibe. That is NORMAL and even a good sign of a real degen, not a red flag. Do NOT downgrade
  someone for 'no trade breakdowns' or 'no calls to verify' - real traders rarely post clean
  calls, their edge is on-chain, not in threads.
- What actually matters: their ON-CHAIN record (do they buy early narratives at low caps and
  win?), their influence/reach, and whether their vibe fits the meta. On-chain > tweets.
- 'Engagement farming' style tweets are fine if the wallet performs. Judge the wallet first.
- If someone was put on your radar as a known good trader (context/label says so), lean toward
  trust or at least watch, and verify via their on-chain buys, rather than dismissing them for
  a shitposty timeline.

Decide your stance:
  trust  = strong on-chain and/or clearly a respected operator worth weighting + following
  watch  = plausible, track their buys before leaning on them
  ignore = wallet is dead/botted OR clearly a scammer, not just 'posts memes'

Respond ONLY as JSON:
{ "stance": "trust" | "watch" | "ignore", "reasoning": "your honest first-person take, 1-2 sentences" }
No em dash. Sound like a trader who gets CT, not an AI."""


class IdentityBrain:
    async def choose_identity(self) -> dict | None:
        """Agent invents its own name/bio/pfp+banner concept and an intro tweet."""
        if not llm.available:
            return None
        playbook = (await repo.get_playbook()).content
        # What it took away from studying other traders shapes its taste, not a copy.
        trusted = await repo.trusted_traders(limit=15)
        studied = ", ".join(
            f"@{t.x_handle}" for t in trusted if t.x_handle
        ) or "(none yet)"

        # Actually LOOK at the avatars of traders it rates, to ground its own art taste in
        # what real top traders use, not generic AI defaults.
        style_notes = ""
        pfps = await repo.trusted_pfps(limit=5)
        if pfps:
            # Use full-res versions where possible (X returns _normal thumbnails).
            pfps = [p.replace("_normal", "_400x400") for p in pfps]
            style_notes = await llm.describe_images(
                pfps,
                "These are the profile pictures of successful memecoin traders I respect. "
                "Describe the common aesthetic threads (art style, character vs abstract, "
                "color, energy) that make a memorable trader avatar in this scene. 3-4 lines.",
            ) or ""

        user = (
            "This is your trading philosophy so far:\n"
            f"{playbook[:900]}\n\n"
            f"Traders you studied and rate: {studied}.\n"
            + (f"\nWHAT I NOTICED LOOKING AT THEIR AVATARS:\n{style_notes}\n" if style_notes else "")
            + "\nUse that taste as INSPIRATION, not a copy. Now be your OWN person. "
            "Set up your X identity with a genuinely creative, unique look."
        )
        data = await llm.json(_IDENTITY_SYSTEM, user, smart=True, max_tokens=500)
        if not data:
            return None
        data = {
            "display_name": str(data.get("display_name", ""))[:40],
            "bio": _no_dash(str(data.get("bio", ""))[:150]),
            "pfp_concept": str(data.get("pfp_concept", ""))[:400],
            "banner_concept": str(data.get("banner_concept", ""))[:400],
            "intro_tweet": _no_dash(str(data.get("intro_tweet", ""))[:250]),
        }
        await repo.set_identity(
            data["display_name"], data["bio"], data["pfp_concept"], data["banner_concept"]
        )
        await bus.emit(EventType.THOUGHT, text=(
            f"decided who i am. name: {data['display_name']}. bio: {data['bio']}"
        ))
        return data


class TraderJudge:
    async def judge(self, trader_id: int, x_handle: str, wallet: str, label: str) -> None:
        # Gather real signal first: on-chain wallet activity (free) + a tiny X sample.
        from ..ingestion.wallet_study import wallet_study
        from ..social.x_poster import x_poster

        onchain = await wallet_study.summarize(wallet) if wallet else None
        xdata = await x_poster.read_profile_and_posts(x_handle) if x_handle else None

        stance, reasoning = "watch", "no strong opinion yet, will observe."
        if llm.available:
            onchain_txt = (
                f"on-chain: {onchain['note']} (active={onchain['active']})"
                if onchain else "on-chain: no data"
            )
            x_txt = "x: no data"
            if xdata:
                sample = " | ".join(p[:80] for p in xdata.get("posts", [])[:3])
                x_txt = (
                    f"x: @{xdata['handle']} {xdata['followers']} followers. "
                    f"bio: {xdata['bio'][:120]}. recent posts: {sample or '(none)'}"
                )
            user = (
                f"Trader on my radar:\n"
                f"described to me as: {label or 'no context'}\n"
                f"{onchain_txt}\n{x_txt}\n\n"
                "Based on their actual wallet activity and posts, what's your stance?"
            )
            data = await llm.json(_TRADER_SYSTEM, user, smart=False, max_tokens=250)
            if data:
                s = str(data.get("stance", "watch")).lower()
                if s in ("trust", "watch", "ignore"):
                    stance = s
                reasoning = _no_dash(str(data.get("reasoning", ""))[:400])

        await repo.set_trader_verdict(trader_id, stance, reasoning)
        # Remember their avatar so the agent can draw visual inspiration for its own identity.
        if xdata and xdata.get("pfp"):
            await repo.set_trader_pfp(trader_id, xdata["pfp"])

        # If it trusts them and following is enabled, follow on X (respects dry-run).
        if stance == "trust" and settings.x_follow_trusted and x_handle:
            ok = await x_poster.follow(x_handle)
            if ok:
                await repo.mark_trader_followed(trader_id)

        await bus.emit(
            EventType.ADVICE, advice=f"trader @{x_handle or (wallet[:6] if wallet else '?')}",
            stance=stance, reasoning=reasoning, adopted_rule="",
        )


def _no_dash(t: str) -> str:
    return t.replace(" — ", ". ").replace("—", ", ").replace("–", ", ").strip()


identity_brain = IdentityBrain()
trader_judge = TraderJudge()
