"""
Twitter / X posting for Alfred — post tweets and threads to the Velthar Lab account.

Free X API tier (post-only). OAuth 1.0a user context. Reading/monitoring timelines
is NOT available on the free tier, so this tool only posts.

Config (config/api_keys.json):
  x_api_key, x_api_secret, x_access_token, x_access_secret
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "config" / "api_keys.json"
_TWEET_URL  = "https://api.twitter.com/2/tweets"
_MAX_LEN    = 280


def _cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _auth():
    """Build an OAuth1 session from the four X keys, or None if not configured."""
    c = _cfg()
    k, ks = c.get("x_api_key", ""), c.get("x_api_secret", "")
    t, ts = c.get("x_access_token", ""), c.get("x_access_secret", "")
    if not all((k, ks, t, ts)):
        return None
    try:
        from requests_oauthlib import OAuth1
        return OAuth1(k, ks, t, ts)
    except Exception as e:
        print(f"[X] OAuth init failed: {e}")
        return None


def _post_one(text: str, auth, reply_to: str | None = None) -> tuple[bool, str]:
    """Post a single tweet. Returns (ok, tweet_id_or_error)."""
    import requests
    body: dict = {"text": text[:_MAX_LEN]}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}
    try:
        r = requests.post(_TWEET_URL, json=body, auth=auth, timeout=20)
        if r.status_code in (200, 201):
            return True, r.json().get("data", {}).get("id", "")
        return False, f"{r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, str(e)


def _split_thread(text: str) -> list[str]:
    """Split long text into <=280-char tweets on sentence/word boundaries."""
    text = text.strip()
    if len(text) <= _MAX_LEN:
        return [text]
    parts, cur = [], ""
    for word in text.split():
        if len(cur) + len(word) + 1 > _MAX_LEN - 6:   # room for " (n/m)"
            parts.append(cur.strip())
            cur = word
        else:
            cur = (cur + " " + word).strip()
    if cur:
        parts.append(cur.strip())
    n = len(parts)
    return [f"{p} ({i+1}/{n})" for i, p in enumerate(parts)] if n > 1 else parts


def twitter_tool(parameters: dict, response=None, player=None,
                 session_memory=None, **_) -> str:
    """
    Post to X / Twitter.  parameters:
      text:   the tweet content (required)
      thread: bool — if true, long text is auto-split into a numbered thread
    """
    params = parameters or {}
    text   = (params.get("text") or params.get("content") or params.get("status") or "").strip()
    thread = bool(params.get("thread", False))

    if not text:
        return "Sir, what would you like me to post?"

    auth = _auth()
    if auth is None:
        return ("X is not connected yet, sir. Add your four X API keys "
                "(x_api_key, x_api_secret, x_access_token, x_access_secret) to the config.")

    if player:
        try: player.write_log(f"[X] Posting: {text[:60]}…")
        except Exception: pass

    def _plain(err: str) -> str:
        """Translate X API errors into butler English with the actual remedy."""
        if "402" in err or "credits" in err:
            return ("X says my posting credits are depleted, sir — their API now runs on "
                    "prepaid credits. Top up or wait for the monthly reset at "
                    "console dot x dot com, and I'll post the moment they return.")
        if "403" in err:
            return ("X refused the post, sir — the access token may be read-only. "
                    "Regenerate it with Read and Write at developer dot x dot com.")
        if "429" in err:
            return "X is rate-limiting us, sir — give it a little while and I'll try again."
        if "duplicate" in err.lower():
            return "X rejected it as a duplicate of a recent post, sir."
        return f"I couldn't post that, sir: {err}"

    tweets = _split_thread(text) if thread else [text[:_MAX_LEN]]
    first_id, reply_to, posted = None, None, 0
    for tw in tweets:
        ok, res = _post_one(tw, auth, reply_to)
        if not ok:
            if posted:
                return f"Posted {posted} of {len(tweets)}, sir, then: {_plain(res)}"
            return _plain(res)
        posted += 1
        reply_to = res
        first_id = first_id or res

    if len(tweets) > 1:
        return f"Posted a {posted}-tweet thread to Velthar Lab, sir."
    return "Posted to Velthar Lab, sir."
