# Requirements

Everything needed to run the Discord ↔ Minecraft whitelist bot (`bot.py`). All Discord IDs in the source are set to `None`; fill them in before running (see [Configuration](#configuration)).

## Runtime

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10 or newer | Uses `X \| None` type syntax |
| SQLite | 3.24 or newer | Needs upsert (`ON CONFLICT DO UPDATE`); bundled with current Python builds |
| Network | Outbound HTTPS | Discord API and gateway (plus your Pterodactyl panel, if enabled) |
| Uptime | Bot process running | Queued actions are only applied while the bot is online; the queue itself survives restarts |

## Python packages

| Package | Required | Purpose |
|---|---|---|
| `discord.py` (2.0+) | Yes | Discord client and slash commands |
| `aiohttp` | Yes | Installed with discord.py; also used for the optional panel check |
| `python-dotenv` | Recommended | Loads `DISCORD_TOKEN` and other secrets from a `.env` file. The bot prints a warning if it is missing |

```bash
python -m pip install "discord.py>=2.0" aiohttp python-dotenv
```

All other imports (`sqlite3`, `asyncio`, `re`, `logging`, etc.) are from the standard library.

## Discord setup

**Application and token**
- Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
- Provide the token as the environment variable `DISCORD_TOKEN` (never hard-code it).

**Invite scopes:** `bot` and `applications.commands`

**Privileged Gateway Intents** (Developer Portal → Bot tab)

| Intent | Required | Why |
|---|---|---|
| Message Content | Yes | Reads the server console log text |
| Presence | Only if `USE_STREAMER_PRESENCE = True` | Sees whether the console relay bot is online |
| Server Members | Only if `USE_STREAMER_PRESENCE = True` | Lets the bot look up the relay bot as a member |

If `USE_STREAMER_PRESENCE` is on but these intents are not enabled, the bot exits at startup with an explanatory error.

**Channel permissions**

| Channel | Bot needs |
|---|---|
| Console channel | View Channel, Read Message History, Send Messages |
| Audit channel (optional) | View Channel, Send Messages |
| Channels where players run commands | Nothing extra (replies are ephemeral) |

**Server-wide**
- **Manage Roles**, with the bot's role placed *above* the Linked role in the role list (only if `LINKED_ROLE_ID` is set).
- Players can receive completion messages by DM; if their DMs are closed, the bot posts a short ping in the channel where they ran the command.

## Minecraft server requirements

| Component | Required | Purpose |
|---|---|---|
| Java server (Spigot/Paper or compatible) | Yes | Runs the whitelist and kick commands |
| [DiscordSRV](https://github.com/DiscordSRV/DiscordSRV) | Yes | Mirrors the console to a Discord channel and runs messages sent there as console commands |
| [LuckPerms](https://luckperms.net/) | Yes | Rank assignment (`lp user … parent set`, `lp user … clear`). Create the groups **`default`** and **`founder`** |
| Vanilla whitelist enabled | Yes | Java players are added with `whitelist add` |
| [Floodgate](https://geysermc.org/wiki/floodgate/) + Geyser | For Bedrock players | Bedrock players are added with `fwhitelist add` |

**Console log format.** Server start, stop and boot detection matches lines in this style (see `START_RE`, `STOP_RES` and `BOOT_RES` in `bot.py`):

```
[Sun 21:02:43 INFO  Server] Done (19.615s)! For help, type "help"
[Sun 17:35:27 INFO  Server] Stopping server
```

If your logger names or line layout differ, adjust those patterns. Set `DEBUG_STATE = True` to print how each status decision was made.

**Security note:** anything sent in a DiscordSRV console channel runs as a server command, and console output can include player IP addresses. Restrict the console channel to the bot and trusted admins only.

## Configuration

**Environment variables** (set in the shell or in a `.env` file next to `bot.py`)

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Bot token |
| `PTERO_PANEL_URL` | No | Panel address, e.g. `https://panel.example.com` |
| `PTERO_SERVER_ID` | No | Short server ID from the panel URL |
| `PTERO_API_KEY` | No | Pterodactyl *client* API key |

The three `PTERO_` variables are all-or-nothing. They do not work if the panel sits behind a Cloudflare browser challenge; the bot then falls back to console-based detection automatically.

**Constants in `bot.py`** (all IDs are `None` in the repository and must be set)

| Constant | Required | Description |
|---|---|---|
| `CONSOLE_CHANNEL_ID` | Yes | Channel the DiscordSRV console is mirrored to |
| `CONSOLE_STREAMER_ID` | Strongly recommended | User ID of the account that posts console output. If `None`, anyone's messages in the console channel are trusted, which lets others fake server status |
| `STAFF_ROLE_IDS` | Yes, for staff commands | A **set** of role IDs allowed to use `/forceunlink` and `/lookup`, e.g. `{123, 456}` |
| `DONOR_ROLE_ID` | Optional | Role that receives the `founder` rank on link |
| `LINKED_ROLE_ID` | Optional | Role granted on `/link` and removed on `/unlink` |
| `AUDIT_CHANNEL_ID` | Optional | Private staff channel for link/unlink logs |
| `MAX_LINKS_PER_USER` | Optional | Accounts per Discord user (default `1`) |
| `BEDROCK_PREFIX` | Optional | Floodgate username prefix (default `.`) |
| `USE_STREAMER_PRESENCE` | Optional | Detect server state from the relay bot's online status (default `False`) |
| `DEBUG_STATE` | Optional | Print how server status was determined |

Timing values such as `ACK_TIMEOUT`, `LIVENESS_WINDOW`, `SYNC_INTERVAL` and `PROBE_INTERVAL` can be left at their defaults.

## Files not to commit

Add these to `.gitignore`:

```
.env
linked_players.db
__pycache__/
```

- `.env` contains your bot token.
- `linked_players.db` links Discord user IDs to Minecraft names, which is personal data.
- If a token is ever committed, reset it in the Developer Portal right away.
