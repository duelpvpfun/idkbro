"""One-time helper: authorize the @crypCE0 app to post AS THE BOT ACCOUNT.

Why this exists: the developer app (and your API credit) lives on your main account, but
you want the tweets to appear on your new bot account. The portal's "Access Token & Secret"
button only makes a token for the app owner (your main account). To get a token for the BOT
account instead, the bot has to authorize the app via a normal login flow. This script runs
that flow using a PIN, so no public callback server is needed.

HOW TO RUN:
  1. In your browser, log OUT of your main X, and log IN to the BOT account.
     (Or use a private/incognito window logged into the bot.)
  2. From backend/:  source .venv/bin/activate && python -m tools.x_authorize_bot
  3. Open the URL it prints, click Authorize app, copy the 7-digit PIN it shows.
  4. Paste the PIN back here.
  5. It prints X_ACCESS_TOKEN and X_ACCESS_SECRET for the BOT account. Give those to me
     (or paste into .env). Consumer keys + bearer stay as they are.

Requires X_API_KEY and X_API_SECRET already set in .env (the app's consumer keys).
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv(".env")


def main() -> None:
    ck = os.getenv("X_API_KEY", "").strip()
    cs = os.getenv("X_API_SECRET", "").strip()
    if not ck or not cs:
        print("ERROR: set X_API_KEY and X_API_SECRET in .env first (the app consumer keys).")
        sys.exit(1)

    import tweepy

    # "oob" = PIN based, no callback server needed.
    handler = tweepy.OAuth1UserHandler(ck, cs, callback="oob")
    try:
        url = handler.get_authorization_url()
    except Exception as e:  # usually means the app lacks OAuth1 / write setup
        print("Could not start auth. Check the app has OAuth 1.0a enabled with Read+Write.")
        print("Details:", repr(e))
        sys.exit(1)

    print("\n1) Make sure your browser is logged into the BOT account.")
    print("2) Open this URL and click 'Authorize app':\n")
    print("   " + url + "\n")
    pin = input("3) Paste the PIN shown after you authorize: ").strip()

    try:
        access_token, access_secret = handler.get_access_token(verifier=pin)
    except Exception as e:
        print("Failed to exchange PIN. Did you paste it correctly?")
        print("Details:", repr(e))
        sys.exit(1)

    # Confirm which account this token belongs to.
    client = tweepy.Client(
        consumer_key=ck, consumer_secret=cs,
        access_token=access_token, access_token_secret=access_secret,
    )
    try:
        me = client.get_me()
        who = f"@{me.data.username}"
    except Exception:
        who = "(could not verify handle, but token was issued)"

    print("\n===== SUCCESS =====")
    print(f"Authorized as: {who}")
    print("Put these in .env (they post AS the bot, billed to your main app's credit):\n")
    print(f"X_ACCESS_TOKEN={access_token}")
    print(f"X_ACCESS_SECRET={access_secret}")
    print("\nDouble-check the handle above is your BOT account, not your main one.")


if __name__ == "__main__":
    main()
