"""Slack Web API, read only: the messages in one channel and the names of the people who posted them.

Lab Network never posts to Slack. It needs a bot token (xoxb-...) with groups:history for a private channel (or
channels:history for a public one) and users:read for names, and the bot must be a member of the channel.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .http import Client

API = "https://slack.com/api/"


class SlackError(Exception):
    pass


def call(client: Client, token: str, method: str, **params: Any) -> Dict[str, Any]:
    try:
        data = client.get(API + method, params, use_cache=False, headers={"Authorization": f"Bearer {token}"})
    except ValueError:                        # an unusable token in the header; the message would quote it
        raise SlackError(f"Slack {method} failed: the token could not be sent (check that it is a single xoxb- token)")
    if not isinstance(data, dict) or not data.get("ok"):
        err = data.get("error", "unknown error") if isinstance(data, dict) else "unexpected response"
        raise SlackError(f"Slack {method} failed: {err}")
    return data


def auth_test(client: Client, token: str) -> Dict[str, Any]:
    """Who the token belongs to; `url` is the workspace address, used for links to messages."""
    return call(client, token, "auth.test")


def history(client: Client, token: str, channel: str, oldest: str = "") -> List[Dict[str, Any]]:
    """Every top-level message posted after `oldest` (a message timestamp), oldest first."""
    out: List[Dict[str, Any]] = []
    cursor = ""
    while True:
        params: Dict[str, Any] = {"channel": channel, "limit": 200}
        if oldest:
            params["oldest"] = oldest
        if cursor:
            params["cursor"] = cursor
        data = call(client, token, "conversations.history", **params)
        out.extend(data.get("messages") or [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor or not data.get("has_more"):
            break
    return sorted(out, key=lambda m: float(m.get("ts") or 0))


def members(client: Client, token: str, channel: str) -> List[str]:
    out: List[str] = []
    cursor = ""
    while True:
        params: Dict[str, Any] = {"channel": channel, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = call(client, token, "conversations.members", **params)
        out.extend(data.get("members") or [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            return out


def user(client: Client, token: str, user_id: str) -> Dict[str, Any]:
    """A person's Slack record: `name` is their real name when they gave one, `bot` whether it is an app."""
    u = call(client, token, "users.info", user=user_id).get("user") or {}
    prof = u.get("profile") or {}
    return {"id": user_id, "name": prof.get("real_name") or u.get("real_name") or prof.get("display_name") or u.get("name") or "",
            "bot": bool(u.get("is_bot") or u.get("is_app_user")), "deleted": bool(u.get("deleted"))}


def find_channel(client: Client, token: str, name: str) -> Dict[str, Any]:
    """A channel the bot belongs to, by name (with or without #) or by ID."""
    want = name.strip().lstrip("#").lower()
    cursor = ""
    types = "private_channel,public_channel"
    while True:
        params: Dict[str, Any] = {"types": types, "limit": 200, "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        try:
            data = call(client, token, "users.conversations", **params)
        except SlackError as exc:
            if "missing_scope" in str(exc) and types != "private_channel":
                types, cursor = "private_channel", ""      # a token that may read only private channels
                continue
            raise
        for c in data.get("channels") or []:
            if c.get("id", "").lower() == want or (c.get("name") or "").lower() == want:
                return {"id": c["id"], "name": c.get("name", ""), "private": bool(c.get("is_private"))}
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            raise SlackError(f"The bot is not a member of a channel called {name}. Invite it to the channel first "
                             "(in the channel, type /invite and the bot's name).")


def permalink(team_url: str, channel: str, ts: str) -> str:
    if not team_url:
        return ""
    return f"{team_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"
