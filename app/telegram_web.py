"""Limited, unauthenticated reader for explicitly approved public Telegram web channels.

This reads only HTML that Telegram already exposes at t.me/s/<channel>.  It does
not use credentials, bypass access restrictions, crawl archives or collect media.
Only add a channel when you have permission to process its content.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse


_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}\Z")
_POST_ID = re.compile(r"([A-Za-z0-9_]{5,32})/(\d{1,20})\Z")


def normalize_public_channel_url(value: str) -> str:
    """Accept only t.me public channel pages, never fetch arbitrary user URLs."""
    value = value.strip()
    if value.startswith("@"):
        username = value[1:]
    else:
        url = urlparse(value)
        if (url.scheme != "https" or url.hostname not in {"t.me", "www.t.me"}
                or url.port is not None or url.username or url.password
                or url.query or url.fragment):
            raise ValueError("Укажите публичную ссылку вида https://t.me/s/channelname")
        parts = [part for part in url.path.strip("/").split("/") if part]
        if len(parts) == 1:
            username = parts[0]
        elif len(parts) == 2 and parts[0] == "s":
            username = parts[1]
        else:
            raise ValueError("Ссылка должна вести на публичную страницу канала /s/channelname")
    if not _USERNAME.fullmatch(username):
        raise ValueError("Неверное публичное имя Telegram-канала")
    return f"https://t.me/s/{username.lower()}"


class TelegramWebParser(HTMLParser):
    def __init__(self, channel: str):
        super().__init__(convert_charrefs=True)
        self.channel = channel.lower()
        self.depth = 0
        self.post_depth = None
        self.text_depth = None
        self.post = None
        self.results = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.depth += 1
            classes = attrs.get("class", "").split()
            post_id = attrs.get("data-post", "")
            matched = _POST_ID.fullmatch(post_id)
            if (self.post is None and "tgme_widget_message" in classes
                    and matched and matched.group(1).lower() == self.channel):
                self.post = {"id": matched.group(2), "parts": [], "published": None}
                self.post_depth = self.depth
            if self.post is not None and "tgme_widget_message_text" in classes:
                self.text_depth = self.depth
        if self.post is not None:
            if tag == "time" and attrs.get("datetime"):
                try:
                    stamp = datetime.fromisoformat(attrs["datetime"].replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    self.post["published"] = stamp.astimezone(timezone.utc).replace(tzinfo=None)
                except ValueError:
                    pass
            if self.text_depth is not None and tag == "br":
                self.post["parts"].append("\n")

    def handle_data(self, data):
        if self.post is not None and self.text_depth is not None:
            self.post["parts"].append(data)

    def handle_endtag(self, tag):
        if tag != "div":
            return
        if self.text_depth is not None and self.depth == self.text_depth:
            self.text_depth = None
        if self.post_depth is not None and self.depth == self.post_depth:
            post = self.post
            text = re.sub(r"[ \t\r\f\v]+", " ", "".join(post["parts"]))
            text = re.sub(r"\n\s*\n+", "\n", text).strip()
            if len(text) >= 20 and post["published"] is not None:
                self.results.append({
                    "external": post["id"],
                    "title": text.splitlines()[0][:110],
                    "body": text[:5500],
                    "link": f"https://t.me/{self.channel}/{post['id']}",
                    "published": post["published"],
                })
            self.post = None
            self.post_depth = None
            self.text_depth = None
        self.depth = max(0, self.depth - 1)


def parse_public_channel(html: bytes, channel: str) -> list[dict]:
    """Extract original post texts, stable IDs, dates and links from a public page."""
    parser = TelegramWebParser(channel)
    parser.feed(html.decode("utf-8", errors="replace"))
    parser.close()
    # Telegram may show the oldest posts first; prefer newest on initial import.
    return sorted(parser.results, key=lambda row: row["published"], reverse=True)[:30]
