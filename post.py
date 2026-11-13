#!/usr/bin/env python3
"""Post a message to a Discord channel.

Usage:
    ./post.py                               # posts config "message" to default_channel
    ./post.py "hello world"                 # posts to default_channel
    ./post.py -c notes "hello world"        # posts to a named channel from config
    ./post.py -c 123456789012345678 "hi"    # posts to a raw channel/thread id
    ./post.py -c https://discord.com/channels/<server>/<channel>/threads/<thread> "hi"
    echo "from stdin" | ./post.py -c notes  # reads message from stdin
    ./post.py --list                        # show configured channels

If min_minutes/max_minutes are set in config, the script keeps running and reposts
at a random interval in that range. Use --once to post a single time and exit.
"""

import argparse
import json
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

API_BASE = "https://discord.com/api/v10"
MAX_LEN = 2000  # Discord's per-message character limit
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"

# Python 3.13+ enables VERIFY_X509_STRICT by default, which rejects some corporate
# proxy root CAs (e.g. "Basic Constraints of CA cert not marked critical").
# Relax only that flag; certificate chain and hostname are still fully verified.
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Config not found at {path}. Copy config.example.json to config.json and fill it in.")
    # Explicit UTF-8 so emoji work on Windows; "-sig" tolerates the BOM Notepad may add.
    with path.open(encoding="utf-8-sig") as f:
        cfg = json.load(f)
    token = cfg.get("token", "")
    if not token or token.startswith("PASTE") or "TOKEN HERE" in token.upper():
        sys.exit(f"Set 'token' in {path}.")
    return cfg


def resolve_channel(cfg: dict, name_or_id: str | None) -> str:
    channels = cfg.get("channels", {})
    target = name_or_id or cfg.get("default_channel")
    if not target:
        sys.exit("No channel given and no 'default_channel' in config.")
    if target in channels:
        target = str(channels[target])
    channel_id = to_channel_id(target)
    if channel_id:
        return channel_id
    sys.exit(f"Unknown channel '{target}'. Known: {', '.join(channels) or '(none)'}")


def to_channel_id(value: str) -> str | None:
    """Accept a raw id or a Discord URL; return the channel/thread id.

    https://discord.com/channels/<server>/<channel>                  -> <channel>
    https://discord.com/channels/<server>/<channel>/threads/<thread> -> <thread>
    """
    value = value.strip()
    if value.isdigit():
        return value
    if "discord.com/channels/" in value:
        last = value.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if last.isdigit():
            return last
    return None


def chunk(text: str, size: int = MAX_LEN) -> list[str]:
    """Split text into <=size pieces, preferring newline boundaries."""
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


def send(cfg: dict, channel_id: str, content: str) -> dict:
    token = cfg["token"]
    if cfg.get("token_type", "user") == "bot":
        token = f"Bot {token}"

    req = urllib.request.Request(
        f"{API_BASE}/channels/{channel_id}/messages",
        data=json.dumps({"content": content}).encode(),
        method="POST",
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
            # Discord/Cloudflare rejects the default urllib User-Agent.
            "User-Agent": cfg.get("user_agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"),
        },
    )

    for _ in range(5):
        try:
            with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429:
                retry = json.loads(body).get("retry_after", 1)
                print(f"Rate limited, retrying in {retry}s...", file=sys.stderr)
                time.sleep(float(retry))
                continue
            if e.code == 401:
                sys.exit("401 Unauthorized: token is invalid or expired. Grab a fresh one.")
            if e.code == 403:
                sys.exit(f"403 Forbidden: no permission to post in channel {channel_id}.")
            sys.exit(f"HTTP {e.code}: {body}")
    sys.exit("Gave up after repeated rate limiting.")


def main() -> None:
    p = argparse.ArgumentParser(description="Post a message to Discord.")
    p.add_argument("message", nargs="?", help="Message text (reads stdin if omitted)")
    p.add_argument("-c", "--channel", help="Channel name from config, or a raw channel id")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config.json")
    p.add_argument("--list", action="store_true", help="List configured channels and exit")
    p.add_argument("--dry-run", action="store_true", help="Print what would be sent without sending")
    p.add_argument("--once", action="store_true", help="Post once and exit, ignoring min/max_minutes")
    args = p.parse_args()

    cfg = load_config(args.config)

    if args.list:
        default = cfg.get("default_channel")
        for name, cid in cfg.get("channels", {}).items():
            print(f"{name:20} {cid}{'  (default)' if name == default else ''}")
        return

    # Priority: CLI argument > piped stdin > "message" in config.
    message = args.message
    if message is None and not sys.stdin.isatty():
        message = sys.stdin.read().strip() or None
    if message is None:
        message = cfg.get("message")
    if isinstance(message, list):  # allow multi-line messages as a list of lines
        message = "\n".join(message)
    message = (message or "").strip()
    if not message:
        p.error("No message: pass one as an argument, pipe via stdin, or set 'message' in config.")

    channel_id = resolve_channel(cfg, args.channel)
    interval = None if args.once else get_interval(cfg)

    if interval is None:
        post_message(cfg, channel_id, message, args.dry_run)
        return

    lo, hi = interval
    log(f"Recurring mode: posting to {channel_id} every {lo:g}-{hi:g} min. Ctrl+C to stop.")
    try:
        while True:
            try:
                post_message(cfg, channel_id, message, args.dry_run)
            except (urllib.error.URLError, OSError) as e:
                # Network blip (wifi drop, laptop sleep, etc.): skip this round, keep looping.
                log(f"Network error, will try again next round: {e}")
            # Pick a whole number of seconds anywhere in [min, max], e.g. 30-60 -> 42m 32s.
            wait = random.randint(round(lo * 60), round(hi * 60))
            next_at = datetime.now() + timedelta(seconds=wait)
            log(f"Next post in {wait // 60}m {wait % 60:02d}s (at {next_at:%H:%M:%S})")
            time.sleep(wait)
    except KeyboardInterrupt:
        log("Stopped.")


def get_interval(cfg: dict) -> tuple[float, float] | None:
    """Return (min, max) minutes from config, or None if recurring mode isn't configured."""
    lo, hi = cfg.get("min_minutes"), cfg.get("max_minutes")
    if lo is None and hi is None:
        return None
    lo = float(lo if lo is not None else hi)
    hi = float(hi if hi is not None else lo)
    if lo <= 0 or hi < lo:
        sys.exit(f"Invalid interval: need 0 < min_minutes <= max_minutes (got {lo}, {hi}).")
    return lo, hi


def post_message(cfg: dict, channel_id: str, message: str, dry_run: bool) -> None:
    parts = chunk(message)
    for i, part in enumerate(parts):
        if dry_run:
            log(f"[dry-run] -> {channel_id} ({len(part)} chars):\n{part}")
            continue
        msg = send(cfg, channel_id, part)
        log(f"Posted message {msg.get('id')} to channel {channel_id}")
        if i < len(parts) - 1:
            time.sleep(0.5)


def log(text: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}", flush=True)

if __name__ == "__main__":
    main()
