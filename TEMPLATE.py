"""Discord -> Minecraft whitelist linker bot.

Players link their Minecraft account with /link. The bot relays whitelist and
LuckPerms commands through a Discord console channel. If the server is down,
actions are queued in SQLite and flushed once the server shows signs of life.

Commands: /link /unlink /mylink /status  (players)
          /forceunlink /lookup           (staff)

Required environment variable:
    DISCORD_TOKEN   Bot token (never hard-code this)
"""
import asyncio
import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

try:  # Load DISCORD_TOKEN etc. from a .env file next to this script
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    print(
        "WARNING: python-dotenv is not installed for this Python, so .env was NOT loaded.\n"
        f"Install it with:  \"{sys.executable}\" -m pip install python-dotenv",
        file=sys.stderr,
    )

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("DISCORD_TOKEN")

# Pterodactyl panel (optional): lets the bot ask the host if the server is running.
# Leave unset if the panel is behind a Cloudflare challenge.
PTERO_PANEL_URL = os.getenv("PTERO_PANEL_URL")
PTERO_SERVER_ID = os.getenv("PTERO_SERVER_ID")
PTERO_API_KEY = os.getenv("PTERO_API_KEY")
PTERO_CONFIGURED = bool(PTERO_PANEL_URL and PTERO_SERVER_ID and PTERO_API_KEY)
PTERO_TIMEOUT = 10.0

CONSOLE_CHANNEL_ID = None      # Channel the server console is mirrored to
CONSOLE_STREAMER_ID = None     # User/bot that posts console output (None = trust anyone)
DONOR_ROLE_ID = None           # "Donator/Founder" role
STAFF_ROLE_IDS = None

LINKED_ROLE_ID = None         # Role given on /link, removed on /unlink (None = disabled)
AUDIT_CHANNEL_ID = None       # Private staff channel for link/unlink logs (None = disabled)
MAX_LINKS_PER_USER = 1        # Minecraft accounts one Discord user may have linked at once

# Optional: read the console relay bot's online/offline status straight from Discord.
# Needs BOTH "Presence Intent" and "Server Members Intent" switched on for THIS bot in the
# Developer Portal (Bot tab -> Privileged Gateway Intents). Unrelated to DiscordSRV's own
# EnablePresenceInformation setting, which should stay false.
USE_STREAMER_PRESENCE = True

DB_FILE = "linked_players.db"
BEDROCK_PREFIX = "."          # Floodgate prefix
MIN_NAME_LEN = 3
MAX_NAME_LEN = 16

HISTORY_LIMIT = 50            # Console messages scanned when checking server state
SYNC_INTERVAL = 15.0          # Seconds between pending-queue checks
SEND_DELAY = 0.2              # Seconds between console messages

# Liveness is inferred from console activity (the host's port changes, so no ping).
LIVENESS_WINDOW = 600.0       # Seconds of console silence before the server counts as quiet
ACK_TIMEOUT = 20.0            # Seconds to wait for the console to react after we send commands
ACK_POLL = 2.0                # How often to check for that reaction
RETRY_COOLDOWN = 60.0         # Pause between flush attempts after an unconfirmed delivery
PROBE_INTERVAL = 600.0        # Min seconds between automatic "list" probes while actions are queued
STATUS_CACHE_SECONDS = 45.0   # /status reuses a recent probe result instead of spamming the console

# Lifecycle patterns, anchored to the console's log format so player chat can't fake them
# (a chat line looks like "] <Player> text", so text must come right after the logger's "]").
START_RE = re.compile(r"\bserver\]\s+done \(")                      # Server] Done (19.6s)! For help, type "help"
STOP_RES = (
    re.compile(r"\bserver\]\s+stopping (?:the )?server"),             # Server] Stopping the server / Stopping server
    re.compile(r"\]\s+disabling \S+ v\d"),                          # [Plugin] Disabling Plugin v1.2.3 (shutdown only)
)
# Boot-only lines: seeing one after the last "Done" means the server is mid-start, not ready yet.
BOOT_RES = (
    re.compile(r"\bdiscordsrv\]\s+enabling \S+ hook"),               # First lines the relay posts each boot
    re.compile(r"\bserver\]\s+running delayed init tasks"),           # Printed just before "Done"
    re.compile(r"\]\s+enabling \S+ v\d"),                            # [Plugin] Enabling Plugin v1.2.3
)
PLAYER_COUNT_RE = re.compile(r"There are (\d+)(?: of a max of |/)(\d+) players? online", re.IGNORECASE)

# When a queued request finishes, DM the requester. If their DMs are closed, post a short
# generic ping (no account names) in the channel where they ran the command.
NOTIFY_CHANNEL_FALLBACK = True
NOTIFY_HINT = "You'll be messaged when it's done (DM, or a ping here if your DMs are closed)."

ACTIVE = "active"
PENDING_LINK = "pending_link_"
PENDING_UNLINK = "pending_unlink"
# =======================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("whitelister")

# ----------------- DEBUG (optional) -----------------
# Prints how each server-state decision was made (panel / presence / log / probe / ack).
# Set to False, or comment out the DEBUG_STATE line, to silence it. Safe to leave the calls in.
DEBUG_STATE = True


def debug_state(source: str, result, detail: str = "") -> None:
    if globals().get("DEBUG_STATE"):
        print(f"[STATE] {source:<8} -> {str(result):<5} | {detail}", flush=True)


# ----------------- DATABASE -----------------
@contextmanager
def db():
    """Open a connection that commits on success, rolls back on error, and always closes."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS links ("
            "discord_id INTEGER, "
            "minecraft_name TEXT UNIQUE, "
            "sync_status TEXT DEFAULT 'active')"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_links_discord_id ON links (discord_id)")
        # Migrate older databases
        existing = {row[1] for row in conn.execute("PRAGMA table_info(links)")}
        for column, ctype in (
            ("notify_id", "INTEGER"),          # Who to notify when a queued action completes
            ("notify_channel_id", "INTEGER"),
            ("notify_kind", "TEXT"),
            ("display_name", "TEXT"),          # Original-casing name (minecraft_name is lowercased)
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE links ADD COLUMN {column} {ctype}")


def get_link(key: str):
    with db() as conn:
        return conn.execute("SELECT * FROM links WHERE minecraft_name = ?", (key,)).fetchone()


def get_links_for_user(user_id: int) -> list:
    with db() as conn:
        return conn.execute("SELECT * FROM links WHERE discord_id = ?", (user_id,)).fetchall()


def find_links(keys: list[str]) -> list:
    with db() as conn:
        marks = ",".join("?" for _ in keys)
        return conn.execute(f"SELECT * FROM links WHERE minecraft_name IN ({marks})", keys).fetchall()


def shown_name(row) -> str:
    return row["display_name"] or row["minecraft_name"]


def claim_link(key: str, display: str, user_id: int, status: str, channel_id: int | None) -> tuple[str, str | None]:
    """Atomically create/update a link.

    Returns ("ok", None), ("taken", None) if another user owns the name, or
    ("limit", existing_name) if the user is already at MAX_LINKS_PER_USER other accounts.
    """
    with db() as conn:
        others = conn.execute(
            "SELECT * FROM links WHERE discord_id = ? AND sync_status != ? AND minecraft_name != ?",
            (user_id, PENDING_UNLINK, key),
        ).fetchall()
        if len(others) >= MAX_LINKS_PER_USER:
            return "limit", shown_name(others[0])

        cur = conn.execute(
            "INSERT INTO links (discord_id, minecraft_name, display_name, sync_status, "
            "notify_id, notify_channel_id, notify_kind) VALUES (?, ?, ?, ?, ?, ?, 'link') "
            "ON CONFLICT(minecraft_name) DO UPDATE SET "
            "display_name = excluded.display_name, sync_status = excluded.sync_status, "
            "notify_id = excluded.notify_id, notify_channel_id = excluded.notify_channel_id, "
            "notify_kind = excluded.notify_kind "
            "WHERE links.discord_id = excluded.discord_id",
            (user_id, key, display, status, user_id, channel_id),
        )
        return ("ok", None) if cur.rowcount > 0 else ("taken", None)


def mark_pending_unlink(key: str, notify_id: int, channel_id: int | None, kind: str) -> None:
    """Flag a name for removal, creating a placeholder row if we've never seen it."""
    with db() as conn:
        conn.execute(
            "INSERT INTO links (discord_id, minecraft_name, sync_status, notify_id, notify_channel_id, notify_kind) "
            "VALUES (NULL, ?, ?, ?, ?, ?) "
            "ON CONFLICT(minecraft_name) DO UPDATE SET "
            "sync_status = excluded.sync_status, notify_id = excluded.notify_id, "
            "notify_channel_id = excluded.notify_channel_id, notify_kind = excluded.notify_kind",
            (key, PENDING_UNLINK, notify_id, channel_id, kind),
        )


def set_status(key: str, status: str) -> None:
    with db() as conn:
        conn.execute("UPDATE links SET sync_status = ? WHERE minecraft_name = ?", (status, key))
        if status == ACTIVE:  # Nothing left to notify about
            conn.execute(
                "UPDATE links SET notify_id = NULL, notify_channel_id = NULL, notify_kind = NULL "
                "WHERE minecraft_name = ?",
                (key,),
            )


def delete_link(key: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM links WHERE minecraft_name = ?", (key,))


def fetch_pending() -> list:
    with db() as conn:
        return conn.execute("SELECT * FROM links WHERE sync_status != ?", (ACTIVE,)).fetchall()


# ----------------- BOT SETUP -----------------
intents = discord.Intents.default()
intents.message_content = True  # Needed to read console log text
if USE_STREAMER_PRESENCE:
    intents.presences = True
    intents.members = True


class WhitelistBot(commands.Bot):
    sync_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        # Runs once per process (unlike on_ready, which fires on every reconnect).
        init_db()
        await self.tree.sync()
        self.sync_task = asyncio.create_task(auto_sync_pending_queue())

    async def close(self) -> None:
        if self.sync_task:
            self.sync_task.cancel()
        await super().close()


bot = WhitelistBot(command_prefix="!", intents=intents)

EDITION_CHOICES = [
    app_commands.Choice(name="☕ Java Edition", value="java"),
    app_commands.Choice(name="📱 Bedrock Edition", value="bedrock"),
]


# ----------------- HELPERS -----------------
def validate_and_condition_name(input_name: str, edition: str) -> str:
    """Validate a username and return it in its canonical form for the given edition."""
    cleaned = input_name.strip()
    offset = 0
    if cleaned.startswith(BEDROCK_PREFIX):
        cleaned = cleaned[len(BEDROCK_PREFIX):]
        offset = len(BEDROCK_PREFIX)

    if not MIN_NAME_LEN <= len(cleaned) <= MAX_NAME_LEN:
        raise ValueError(f"Username must be between {MIN_NAME_LEN} and {MAX_NAME_LEN} characters.")

    allow_space = edition == "bedrock"
    for index, char in enumerate(cleaned):
        ok = char.isascii() and (char.isalnum() or char == "_" or (allow_space and char == " "))
        if not ok:
            if char == " ":
                shown = "space"
            elif char.isprintable() and char.strip():
                shown = f"'{char}'"
            else:
                shown = "invisible character"
            raise ValueError(f"Contains an illegal character ({shown}) at position {index + offset + 1}.")

    if cleaned != cleaned.strip():
        raise ValueError("Username cannot start or end with a space.")
    if "  " in cleaned:
        raise ValueError("Username cannot contain consecutive spaces.")

    return BEDROCK_PREFIX + cleaned if allow_space else cleaned


def quote_target(name: str) -> str:
    """Bedrock (prefixed) names are quoted in console commands."""
    return f'"{name}"' if name.startswith(BEDROCK_PREFIX) else name


def edition_label(name: str) -> str:
    return "Bedrock" if name.startswith(BEDROCK_PREFIX) else "Java"


def describe_status(status: str) -> str:
    if status == ACTIVE:
        return "✅ Active"
    if status.startswith(PENDING_LINK):
        return f"⏳ Link queued ({status[len(PENDING_LINK):]} rank)"
    if status == PENDING_UNLINK:
        return "⏳ Removal queued"
    return status


def whitelist_command(target: str) -> str:
    """Bedrock (Floodgate) players use /fwhitelist; Java players use the vanilla /whitelist."""
    return "fwhitelist" if target.strip('"').startswith(BEDROCK_PREFIX) else "whitelist"


def link_commands(target: str, rank_group: str) -> list[str]:
    return [f"{whitelist_command(target)} add {target}", f"lp user {target} parent set {rank_group}"]


def unlink_commands(target: str, raw_name: str, kick_reason: str) -> list[str]:
    return [
        f"{whitelist_command(target)} remove {target}",
        f"lp user {target} clear",
        f"kick {raw_name} {kick_reason}",
    ]


def is_staff(member: discord.Member) -> bool:
    return any(role.id in STAFF_ROLE_IDS for role in member.roles)


# ----------------- CONSOLE I/O -----------------
async def get_console_channel():
    return bot.get_channel(CONSOLE_CHANNEL_ID) or await bot.fetch_channel(CONSOLE_CHANNEL_ID)


console_lock = asyncio.Lock()  # Keeps command batches and probes from interleaving


def classify_console_line(line: str) -> str | None:
    """Return "start", "stop", "boot" (mid-startup), or None for a single console log line."""
    line = line.lower()
    if START_RE.search(line):
        return "start"
    if any(pattern.search(line) for pattern in STOP_RES):
        return "stop"
    if any(pattern.search(line) for pattern in BOOT_RES):
        return "boot"
    return None


def message_time(message: discord.Message):
    """The console relay edits messages to append lines, so the edit time is the real activity time."""
    return message.edited_at or message.created_at


def is_console_line(message: discord.Message) -> bool:
    """True for messages that really come from the server's console relay."""
    if message.author.id == bot.user.id:
        return False  # Our own commands
    return not CONSOLE_STREAMER_ID or message.author.id == CONSOLE_STREAMER_ID


async def collect_console_lines(channel, after: discord.Message) -> list:
    """Console messages newer than `after`, or edited after it was sent.

    The relay batches log lines into messages and edits them to append more, so a reaction to
    our command may arrive as an edit of an older message rather than as a new message.
    """
    found = []
    async for message in channel.history(limit=20):
        if not is_console_line(message):
            continue
        edited_after = message.edited_at is not None and message.edited_at > after.created_at
        if message.id > after.id or edited_after:
            found.append(message)
    return found


async def wait_for_console_activity(channel, after: discord.Message) -> list:
    """Wait for console lines posted after `after` (proof the server is alive). Empty = none."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ACK_TIMEOUT
    while loop.time() < deadline:
        await asyncio.sleep(ACK_POLL)
        lines = await collect_console_lines(channel, after)
        if lines:
            return lines
    return []


panel_ok: bool | None = None  # Did the last panel API call work? (None = not tried yet)


async def forward_to_console(console_commands: list[str], confirm: bool | None = None) -> bool:
    """Send commands to the console channel.

    With confirmation on, only returns True if the console reacts afterwards (the relay echoes
    server output back into the channel). A sleeping or crashed server never reacts, so the
    action stays queued instead of being silently lost. By default confirmation is skipped when
    the panel API is working, since the panel already told us the server is running.
    """
    if confirm is None:
        confirm = panel_ok is not True
    try:
        channel = await get_console_channel()
        async with console_lock:
            last_sent = None
            for cmd in console_commands:
                last_sent = await channel.send(cmd)
                await asyncio.sleep(SEND_DELAY)
            if not confirm or last_sent is None:
                return True
            if await wait_for_console_activity(channel, last_sent):
                debug_state("ack", True, "console reacted after our command")
                return True
            log.warning("No console response after sending commands; keeping them queued.")
            debug_state("ack", False, "no console reaction within ACK_TIMEOUT")
            return False
    except Exception:
        log.exception("Failed to forward console commands")
        return False


# ----------------- SERVER STATE -----------------
async def get_panel_state() -> bool | None:
    """Ask the Pterodactyl panel for the server's power state (True/False, None if unreachable)."""
    global panel_ok
    url = f"{PTERO_PANEL_URL.rstrip('/')}/api/client/servers/{PTERO_SERVER_ID}/resources"
    headers = {
        "Authorization": f"Bearer {PTERO_API_KEY}",
        "Accept": "application/vnd.pterodactyl.v1+json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=PTERO_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.json(content_type=None)
        state = data["attributes"]["current_state"]
    except Exception as exc:
        if panel_ok is not False:  # Log once per outage, not every poll
            log.warning("Panel API unavailable (%s); falling back to console activity.", exc)
        panel_ok = False
        return None

    if panel_ok is not True:
        log.info("Panel API working. Server state: %s", state)
    panel_ok = True
    return state == "running"


def get_streamer_presence() -> bool | None:
    """Is the console relay bot connected to Discord?

    True -> online/idle/dnd, False -> offline, None -> unknown (feature off, or member not cached)
    """
    if not USE_STREAMER_PRESENCE or not CONSOLE_STREAMER_ID:
        return None
    guild = getattr(bot.get_channel(CONSOLE_CHANNEL_ID), "guild", None)
    member = guild.get_member(CONSOLE_STREAMER_ID) if guild else None
    if member is None:
        return None
    return member.status != discord.Status.offline


async def _state_from_console_log() -> bool | None:
    try:
        channel = await get_console_channel()
        newest = None
        saw_start = False
        async for message in channel.history(limit=HISTORY_LIMIT):
            if not is_console_line(message):
                continue
            stamp = message_time(message)
            if newest is None or stamp > newest:
                newest = stamp
            # Messages hold many lines: walk them newest-first to find the latest lifecycle line.
            for line in reversed(message.content.splitlines()):
                kind = classify_console_line(line)
                if kind == "stop":
                    debug_state("log", False, f"STOP line: {line.strip()[:70]!r}")
                    return False
                if kind == "boot":
                    debug_state("log", False, f"BOOT line (not ready yet): {line.strip()[:70]!r}")
                    return False
                if kind == "start":
                    saw_start = True
                    debug_state("log", "start", f"START line: {line.strip()[:70]!r}")
                    break
            if saw_start:
                break  # Newest lifecycle marker is a start
        if newest is None:
            debug_state("log", None, "no console lines found in recent history")
            return None
        age = (discord.utils.utcnow() - newest).total_seconds()
        result = True if age <= LIVENESS_WINDOW else None
        debug_state(
            "log", result,
            f"last console activity {age:.0f}s ago (window {LIVENESS_WINDOW:.0f}s), "
            f"start line {'seen' if saw_start else 'not seen'}",
        )
        return result
    except Exception:
        log.exception("Could not read console channel")
        debug_state("log", False, "could not read console channel")
        return False


async def get_server_state() -> bool | None:
    """Best guess at server state, without sending anything.

    Sources, strongest first: the Pterodactyl panel (if configured and reachable), then the
    console relay bot's Discord presence (if enabled), then the console log:
      False -> panel says not running / relay bot offline / a stop was logged
      True  -> panel says running / console active recently / relay bot online but console quiet
      None  -> no recent console output and no presence info (asleep, or just idle)
    """
    if PTERO_CONFIGURED:
        panel_state = await get_panel_state()
        if panel_state is not None:
            debug_state("panel", panel_state, "Pterodactyl API answered")
            return panel_state
        debug_state("panel", None, "API unreachable, falling back")

    presence = get_streamer_presence()
    if USE_STREAMER_PRESENCE:
        debug_state("presence", presence, {True: "relay bot is online", False: "relay bot is OFFLINE",
                                           None: "unknown (relay bot not visible/cached)"}[presence])
    if presence is False:
        debug_state("DECISION", False, "relay bot offline -> server asleep/stopped")
        return False  # Relay bot is offline, so the server is asleep or stopped

    state = await _state_from_console_log()
    if state is None and presence is True:
        debug_state("DECISION", True, "relay bot online + console quiet -> running but idle")
        return True  # Relay bot connected but console quiet: running and idle, not asleep
    debug_state("DECISION", state, "from console log")
    return state


async def get_last_console_activity():
    """Timestamp of the newest console activity (new line or edit), or None."""
    latest = None
    try:
        channel = await get_console_channel()
        async for message in channel.history(limit=HISTORY_LIMIT):
            if is_console_line(message):
                stamp = message_time(message)
                if latest is None or stamp > latest:
                    latest = stamp
    except Exception:
        log.exception("Could not read console channel")
    return latest


last_probe_at = float("-inf")
probe_cache: tuple[float, bool, str | None] | None = None


async def probe_server() -> tuple[bool, str | None]:
    """Actively check the server: send a harmless `list` and see whether the console reacts.

    Returns (alive, "online/max" players if the reply could be parsed).
    """
    global last_probe_at, probe_cache
    last_probe_at = time.monotonic()
    alive, players = False, None
    try:
        channel = await get_console_channel()
        async with console_lock:
            sent = await channel.send("list")
            replies = await wait_for_console_activity(channel, sent)
            if replies:
                alive = True
                await asyncio.sleep(ACK_POLL)  # Let the actual `list` output arrive too
                replies = await collect_console_lines(channel, sent) or replies
                matches = PLAYER_COUNT_RE.findall("\n".join(m.content for m in replies))
                if matches:
                    players = f"{matches[-1][0]}/{matches[-1][1]}"  # Latest `list` reply wins
    except Exception:
        log.exception("Server probe failed")
    probe_cache = (time.monotonic(), alive, players)
    debug_state("probe", alive, f"sent 'list' to console; players={players or 'n/a'}")
    return alive, players


async def probe_cached() -> tuple[bool, str | None]:
    """Like probe_server, but reuses a very recent result so /status can't spam the console."""
    if probe_cache and time.monotonic() - probe_cache[0] < STATUS_CACHE_SECONDS:
        debug_state("probe", probe_cache[1], "cached result (under STATUS_CACHE_SECONDS old)")
        return probe_cache[1], probe_cache[2]
    return await probe_server()


async def server_may_be_online() -> bool:
    """User commands always try delivery unless the log says the server stopped.

    Delivery is confirmed by console activity, so a dead server just means "stay queued".
    """
    return (await get_server_state()) is not False


# ----------------- ROLE / AUDIT / NOTIFY -----------------
async def set_linked_role(guild: discord.Guild | None, user_id: int, grant: bool) -> str | None:
    """Give or remove the Linked role. Returns a short warning on failure, else None."""
    if not LINKED_ROLE_ID or guild is None:
        return None
    role = guild.get_role(LINKED_ROLE_ID)
    if role is None:
        log.warning("LINKED_ROLE_ID %s not found in guild", LINKED_ROLE_ID)
        return "The Linked role is misconfigured; please tell staff."
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if grant:
            await member.add_roles(role, reason="Minecraft account linked")
        else:
            await member.remove_roles(role, reason="Minecraft account unlinked")
    except discord.NotFound:
        return None  # They left the server; nothing to change
    except discord.Forbidden:
        log.warning("Missing permission to manage the Linked role (check role order / Manage Roles).")
        return "I couldn't update your Linked role; please tell staff."
    except discord.HTTPException:
        log.exception("Failed to update Linked role for %s", user_id)
        return "I couldn't update your Linked role; please tell staff."
    return None


async def audit(text: str) -> None:
    """Post a line to the private staff audit channel (no pings)."""
    if not AUDIT_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(AUDIT_CHANNEL_ID) or await bot.fetch_channel(AUDIT_CHANNEL_ID)
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.exception("Could not write to audit channel")


def build_notice(kind: str, target: str, rank_group: str | None) -> str:
    if kind == "link":
        rank = "Founder rank" if rank_group == "founder" else "Standard rank"
        return f"✅ The server is back online and your account `{target}` is now linked ({rank})."
    if kind == "unlink":
        return f"🧹 The server is back online and `{target}` has been removed from the whitelist."
    return f"🚨 The server is back online and your force-unlink of `{target}` has been completed."


async def notify_user(user_id: int | None, channel_id: int | None, kind: str | None,
                      target: str, rank_group: str | None = None) -> None:
    """Tell the requester a queued action finished: DM first, generic channel ping as fallback."""
    if not user_id or not kind:
        return

    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(build_notice(kind, target, rank_group))
        return
    except (discord.Forbidden, discord.HTTPException):
        log.info("Could not DM user %s; trying channel fallback.", user_id)
    except Exception:
        log.exception("Unexpected error DMing user %s", user_id)

    if NOTIFY_CHANNEL_FALLBACK and channel_id:
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            await channel.send(
                f"<@{user_id}> your queued Minecraft request has been processed. "
                "(Allow DMs from server members to get the details privately.)",
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)]),
            )
        except Exception:
            log.exception("Channel fallback notification failed for user %s", user_id)


# ==================== /LINK ====================
@bot.tree.command(name="link", description="Securely link your account and manage whitelist states.")
@app_commands.guild_only()
@app_commands.describe(minecraft_name="Your exact in-game name", edition="Choose Java or Bedrock Edition")
@app_commands.choices(edition=EDITION_CHOICES)
async def link(interaction: discord.Interaction, minecraft_name: str, edition: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    try:
        name = validate_and_condition_name(minecraft_name, edition.value)
    except ValueError as err:
        await interaction.followup.send(f"❌ **Registration Denied:** {err}", ephemeral=True)
        return

    key = name.lower()
    target = quote_target(name)
    is_donor = any(role.id == DONOR_ROLE_ID for role in interaction.user.roles)
    rank_group = "founder" if is_donor else "default"
    rank_msg = "Founder rank kit privileges!" if is_donor else "Standard Survival profile parameters."

    # Claim first (atomic); mark pending so a crash mid-push is retried by the sync loop.
    result, existing = claim_link(
        key, name, interaction.user.id, f"{PENDING_LINK}{rank_group}", interaction.channel_id
    )
    if result == "limit":
        await interaction.followup.send(
            f"⛔ **Link Limit:** You already have `{existing}` linked "
            f"(max {MAX_LINKS_PER_USER} per Discord user). Use `/unlink` first.",
            ephemeral=True,
        )
        return
    if result == "taken":
        await interaction.followup.send(f"⛔ **Linking Conflict:** Profile `{name}` is already claimed.", ephemeral=True)
        return

    role_warning = await set_linked_role(interaction.guild, interaction.user.id, True)

    instant = await server_may_be_online() and await forward_to_console(link_commands(target, rank_group))
    if instant:
        set_status(key, ACTIVE)
        prefix = "✅ **Account Link Complete!** Ranks pushed instantly."
    else:
        prefix = (
            "🛠️ **Queue Engaged:** The Minecraft server is currently offline/under maintenance. "
            "Your account information has been saved securely and will be pushed automatically "
            f"once the server is back up! {NOTIFY_HINT}"
        )

    await audit(
        f"🔗 **Link** — {interaction.user.mention} (`{interaction.user.id}`) → `{name}` "
        f"[{edition_label(name)}, {rank_group}] — {'applied instantly' if instant else 'queued'}"
    )

    reply = f"{prefix}\nProfile `{target}` mapped to {rank_msg}"
    if role_warning:
        reply += f"\n⚠️ {role_warning}"
    await interaction.followup.send(reply, ephemeral=True)


# ==================== /UNLINK ====================
@bot.tree.command(name="unlink", description="Remove your own Minecraft account from the server whitelist.")
@app_commands.guild_only()
@app_commands.describe(minecraft_name="Your exact in-game name", edition="Choose Java or Bedrock Edition")
@app_commands.choices(edition=EDITION_CHOICES)
async def unlink(interaction: discord.Interaction, minecraft_name: str, edition: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    try:
        name = validate_and_condition_name(minecraft_name, edition.value)
    except ValueError as err:
        await interaction.followup.send(f"❌ **Unlink Denied:** {err}", ephemeral=True)
        return

    key = name.lower()
    row = get_link(key)
    if not row or row["discord_id"] != interaction.user.id:
        await interaction.followup.send(
            "❌ **Action Denied:** You do not own a database link record for this name.", ephemeral=True
        )
        return

    target = quote_target(name)
    mark_pending_unlink(key, interaction.user.id, interaction.channel_id, "unlink")  # Crash-safe

    # Drop the Linked role right away (the user's intent), unless they still have another account.
    remaining = [r for r in get_links_for_user(interaction.user.id) if r["sync_status"] != PENDING_UNLINK]
    role_warning = None if remaining else await set_linked_role(interaction.guild, interaction.user.id, False)

    instant = await server_may_be_online() and await forward_to_console(unlink_commands(target, name, "Unlinked."))
    if instant:
        delete_link(key)
        msg = f"🧹 **Account Unlinked!** You are no longer whitelisted. `{target}`."
    else:
        msg = (
            f"🛠️ **Queue Engaged:** Server is currently down. Profile `{target}` is marked for "
            f"removal and will be purged once the server starts! {NOTIFY_HINT}"
        )
    if role_warning:
        msg += f"\n⚠️ {role_warning}"

    await audit(
        f"🧹 **Unlink** — {interaction.user.mention} (`{interaction.user.id}`) → `{name}` "
        f"— {'applied instantly' if instant else 'queued'}"
    )
    await interaction.followup.send(msg, ephemeral=True)


# ==================== /FORCEUNLINK (STAFF ONLY) ====================
@bot.tree.command(name="forceunlink", description="[STAFF ONLY] Forcefully strip any player from the whitelist registry.")
@app_commands.guild_only()
@app_commands.describe(minecraft_name="The target exact in-game name", edition="Choose Java or Bedrock Edition")
@app_commands.choices(edition=EDITION_CHOICES)
async def forceunlink(interaction: discord.Interaction, minecraft_name: str, edition: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    if not is_staff(interaction.user):
        await interaction.followup.send("⛔ **Access Denied:** Missing moderation permissions.", ephemeral=True)
        return

    try:
        name = validate_and_condition_name(minecraft_name, edition.value)
    except ValueError as err:
        await interaction.followup.send(f"❌ **Invalid Parameters:** {err}", ephemeral=True)
        return

    key = name.lower()
    target = quote_target(name)
    row = get_link(key)
    owner_id = row["discord_id"] if row else None
    mark_pending_unlink(key, interaction.user.id, interaction.channel_id, "force")

    # Strip the owner's Linked role unless they still have another account.
    if owner_id:
        remaining = [r for r in get_links_for_user(owner_id) if r["sync_status"] != PENDING_UNLINK]
        if not remaining:
            await set_linked_role(interaction.guild, owner_id, False)

    kick_reason = "Your account has been unlinked by an Admin. &rContact an Admin for further information."
    instant = await server_may_be_online() and await forward_to_console(unlink_commands(target, name, kick_reason))
    if instant:
        delete_link(key)
        msg = f"🚨 **Moderation Action Logged!** Target `{target}` cleared dynamically."
    else:
        msg = f"🚨 **Staff Override Queued:** Server is offline. Target `{target}` marked for removal on reboot. {NOTIFY_HINT}"

    owner_text = f"<@{owner_id}> (`{owner_id}`)" if owner_id else "unknown owner"
    await audit(
        f"🚨 **Force-unlink** by {interaction.user.mention} (`{interaction.user.id}`) → `{name}` "
        f"(owner: {owner_text}) — {'applied instantly' if instant else 'queued'}"
    )
    log.info("forceunlink by %s (%s) on %s", interaction.user, interaction.user.id, name)
    await interaction.followup.send(msg, ephemeral=True)


# ==================== /MYLINK ====================
@bot.tree.command(name="mylink", description="Show the Minecraft account linked to you.")
@app_commands.guild_only()
async def mylink(interaction: discord.Interaction):
    rows = get_links_for_user(interaction.user.id)
    if not rows:
        await interaction.response.send_message(
            "You don't have a linked account yet. Use `/link` to add one.", ephemeral=True
        )
        return

    lines = [
        f"• `{shown_name(r)}` ({edition_label(r['minecraft_name'])}) — {describe_status(r['sync_status'])}"
        for r in rows
    ]
    await interaction.response.send_message("**Your linked account:**\n" + "\n".join(lines), ephemeral=True)


# ==================== /LOOKUP (STAFF ONLY) ====================
@bot.tree.command(name="lookup", description="[STAFF ONLY] Find who owns a Minecraft name, or what a Discord user has linked.")
@app_commands.guild_only()
@app_commands.describe(user="Discord user to look up", minecraft_name="Minecraft name to look up (Java or Bedrock)")
async def lookup(interaction: discord.Interaction, user: Optional[discord.Member] = None,
                 minecraft_name: Optional[str] = None):
    if not is_staff(interaction.user):
        await interaction.response.send_message("⛔ **Access Denied:** Missing moderation permissions.", ephemeral=True)
        return
    if user is None and not minecraft_name:
        await interaction.response.send_message("Provide a Discord user or a Minecraft name.", ephemeral=True)
        return

    rows = []
    if user is not None:
        rows += get_links_for_user(user.id)
    if minecraft_name:
        n = minecraft_name.strip().lower()
        candidates = {n, n[len(BEDROCK_PREFIX):]} if n.startswith(BEDROCK_PREFIX) else {n, BEDROCK_PREFIX + n}
        rows += find_links(sorted(candidates))

    seen, lines = set(), []
    for r in rows:
        if r["minecraft_name"] in seen:
            continue
        seen.add(r["minecraft_name"])
        owner = f"<@{r['discord_id']}> (`{r['discord_id']}`)" if r["discord_id"] else "no owner on record"
        lines.append(
            f"• `{shown_name(r)}` ({edition_label(r['minecraft_name'])}) — {owner} — {describe_status(r['sync_status'])}"
        )

    await interaction.response.send_message(
        "\n".join(lines) if lines else "No records found.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ==================== /STATUS ====================
@bot.tree.command(name="status", description="Check whether the Minecraft server is online.")
@app_commands.guild_only()
async def status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    presence = get_streamer_presence()
    if presence is False:
        alive, players = False, None  # Relay bot offline: no need to wait on a probe
    else:
        alive, players = await probe_cached()
    last_activity = await get_last_console_activity()
    pending = fetch_pending()

    embed = discord.Embed(
        title="Server Status",
        colour=discord.Colour.green() if alive else discord.Colour.red(),
    )
    embed.add_field(name="Server", value="🟢 Online" if alive else "🔴 No response (asleep or offline)", inline=False)
    if players:
        embed.add_field(name="Players", value=players, inline=True)
    embed.add_field(
        name="Last console activity",
        value=f"<t:{int(last_activity.timestamp())}:R>" if last_activity else "None recently",
        inline=True,
    )
    if presence is not None:
        embed.add_field(name="Console relay bot", value="🟢 Online" if presence else "🔴 Offline", inline=True)
    embed.add_field(name="Queued actions", value=str(len(pending)), inline=True)
    if not alive:
        embed.set_footer(text="Queued link/unlink requests are applied automatically once the server is back.")

    if pending and is_staff(interaction.user):
        queue_lines = [f"• `{shown_name(r)}` — {describe_status(r['sync_status'])}" for r in pending[:10]]
        if len(pending) > 10:
            queue_lines.append(f"…and {len(pending) - 10} more")
        embed.add_field(name="Queue (staff)", value="\n".join(queue_lines), inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


# ==================== BACKGROUND SYNC ====================
next_flush_at = 0.0  # monotonic time before which we won't retry a failed flush


async def flush_pending() -> None:
    """Push queued link/unlink actions once the server is believed to be up.

    Only the first action is checked for a console response; once it gets one, the server is
    proven alive and the rest are sent straight away without waiting for individual responses.
    """
    global next_flush_at

    pending = fetch_pending()
    if not pending:
        return  # Nothing to do, so don't bother reading the channel
    now = time.monotonic()
    if now < next_flush_at:
        return

    state = await get_server_state()
    proven = False
    if state is False:
        return  # A stop was logged (or the console channel is unreachable)
    if state is None:
        # Console has been quiet: the server may be asleep, or just idle (e.g. the bot was
        # restarted while it runs). Probe now and then instead of waiting forever.
        if now - last_probe_at < PROBE_INTERVAL:
            return
        alive, _ = await probe_server()
        if not alive:
            return
        proven = True

    log.info("Server looks up. Flushing %d pending action(s)...", len(pending))
    for row in pending:
        key, status_text = row["minecraft_name"], row["sync_status"]
        shown = shown_name(row)
        target = quote_target(shown)
        rank_group = None

        if status_text.startswith(PENDING_LINK):
            rank_group = status_text[len(PENDING_LINK):]
            ok = await forward_to_console(link_commands(target, rank_group), confirm=False if proven else None)
            if ok:
                set_status(key, ACTIVE)
            action = "link"
        elif status_text == PENDING_UNLINK:
            reason = "Sync execution update: Account Unlinked."
            ok = await forward_to_console(unlink_commands(target, shown, reason), confirm=False if proven else None)
            if ok:
                delete_link(key)
            action = "force-unlink" if row["notify_kind"] == "force" else "unlink"
        else:
            continue

        if not ok:
            next_flush_at = time.monotonic() + RETRY_COOLDOWN
            return  # Server isn't really listening; retry later

        proven = True
        owner = f"<@{row['discord_id']}> " if row["discord_id"] else ""
        await audit(f"⏱️ **Queued {action} applied** — {owner}`{shown}`")
        await notify_user(row["notify_id"], row["notify_channel_id"], row["notify_kind"], target, rank_group)


async def auto_sync_pending_queue() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await flush_pending()
        except Exception:
            log.exception("Error in auto-sync cycle")
        await asyncio.sleep(SYNC_INTERVAL)


@bot.event
async def on_ready():
    log.info("Whitelister online as %s", bot.user)


if __name__ == "__main__":
    if not BOT_TOKEN:
        log.critical("DISCORD_TOKEN environment variable is not set.")
        sys.exit(1)
    try:
        bot.run(BOT_TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        log.critical(
            "USE_STREAMER_PRESENCE is on, but the Presence Intent and Server Members Intent are not "
            "enabled for this bot. Enable them in the Developer Portal (Bot tab -> Privileged Gateway "
            "Intents), or set USE_STREAMER_PRESENCE = False."
        )
        sys.exit(1)