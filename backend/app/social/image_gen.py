"""Optional pfp / banner image generation via OpenAI images API.

The agent writes its own art brief; if an OpenAI API key is present, this turns that brief
into actual images at X-friendly sizes and returns local file paths so they can be uploaded
to the profile. If there's no key, it returns None and the agent just hands you the brief to
generate yourself.

Note: ChatGPT Plus is the web app only; it does NOT include API access. The images API is
separate pay-per-use billing (a few cents per image).
"""

from __future__ import annotations

import base64
import os
import time

import httpx

from ..config import settings

_OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "identity")


class ImageGen:
    @property
    def available(self) -> bool:
        return bool(settings.openai_api_key)

    async def generate(self, prompt: str, size: str) -> str | None:
        """Generate one image, return its saved file path. size like '1024x1024'."""
        if not self.available:
            return None
        os.makedirs(_OUT_DIR, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post(
                    "https://api.openai.com/v1/images/generations",
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    json={
                        "model": settings.openai_image_model,
                        "prompt": prompt,
                        "size": size,
                        "n": 1,
                    },
                )
                r.raise_for_status()
                data = r.json()["data"][0]
        except (httpx.HTTPError, KeyError, ValueError):
            return None

        stamp = str(int(time.time()))
        dest = os.path.join(_OUT_DIR, f"img-{size}-{stamp}.png")
        try:
            if data.get("b64_json"):
                with open(dest, "wb") as f:
                    f.write(base64.b64decode(data["b64_json"]))
            elif data.get("url"):
                async with httpx.AsyncClient(timeout=60) as c:
                    img = await c.get(data["url"])
                    img.raise_for_status()
                    with open(dest, "wb") as f:
                        f.write(img.content)
            else:
                return None
        except (OSError, httpx.HTTPError):
            return None
        return dest

    async def make_pfp(self, brief: str) -> str | None:
        # Square avatar. gpt-image-1 supports 1024x1024.
        prompt = (
            f"{brief}\n\nStyle: clean, bold, readable at very small sizes, centered subject, "
            "suitable as a Twitter/X profile avatar. Square composition."
        )
        return await self.generate(prompt, "1024x1024")

    async def make_banner(self, brief: str) -> str | None:
        # Wide banner. gpt-image-1 supports 1536x1024 (closest to X's 3:1).
        prompt = (
            f"{brief}\n\nStyle: wide banner for a Twitter/X profile header, text-light, key "
            "visual weighted to the right so it isn't hidden behind the avatar on the left."
        )
        return await self.generate(prompt, "1536x1024")


image_gen = ImageGen()
