# Minecraft SMP Whitelist Bot

A Discord bot that lets players link their Minecraft account from Discord and gets them whitelisted and ranked automatically. It is built for small SMP servers, including servers on free hosts that sleep when nobody is online.

The bot talks to your server through a [DiscordSRV](https://github.com/DiscordSRV/DiscordSRV) console channel: it posts whitelist and [LuckPerms](https://luckperms.net/) commands there and reads the console output back. If the server is asleep, requests are queued and applied when it wakes up.

## Features

- **Self-service linking** for Java and Bedrock players (`/link`, `/unlink`, `/mylink`).
- **Rank assignment**: members get the `default` group; members with your donor role get `founder`.
- **Offline queue.** If the server is off, actions are saved in SQLite and applied automatically once the server is up. The queue survives bot restarts.
- **Confirmed delivery.** A command only counts as delivered if the server's console reacts to it, so nothing is silently lost on a sleeping or crashed server.
- **Completion messages.** People who queued a request are DMed when it is applied (with a generic channel ping if their DMs are closed).
- **Staff tools**: `/forceunlink`, `/lookup`, a private audit log and a live `/status` view of the queue.
- **Optional extras**: a "Linked" Discord role, one account per Discord user, and detection of server state from the console relay bot's online status or the Pterodactyl panel API.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/link <name> <edition>` | Everyone | Links your Minecraft account and whitelists it |
| `/unlink <name> <edition>` | Everyone | Removes your own account from the whitelist |
| `/mylink` | Everyone | Shows your linked account and whether it is active or queued |
| `/status` | Everyone | Shows whether the server is online, player count, and queue size |
| `/forceunlink <name> <edition>` | Staff | Removes any player and kicks them |
| `/lookup [user] [name]` | Staff | Finds who owns a Minecraft name, or what a Discord user has linked |

All replies are private (ephemeral).

## How it works

1. A player runs `/link`. The bot records the claim in SQLite, then posts the whitelist and rank commands in the console channel.
2. If the server answers (the console relay posts new output), the action is marked done. If not, it stays queued.
3. A background loop checks the queue every few seconds. When the server shows signs of life, the queued commands are sent. The first one is confirmed by a console reaction; the rest follow straight away.
4. Server state is judged from, in order of strength: the Pterodactyl panel (optional), the console relay bot's Discord presence (optional), the console log, and an active `list` probe.

## Requirements

- Python 3.10 or newer
- A Discord bot token, with the **Message Content** intent enabled
- A Minecraft server running DiscordSRV and LuckPerms (plus Floodgate/Geyser if you support Bedrock)

Full details, including Discord permissions and every setting, are in [REQUIREMENTS.md](REQUIREMENTS.md).

## Quick start

```bash
# 1. Get the code
git clone https://github.com/YOUR-USERNAME/YOUR-REPO.git
cd YOUR-REPO

# 2. (Recommended) create a virtual environment
python -m venv .venv
# Windows:     .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

# 3. Install dependencies
python -m pip install -r requirements.txt
```

**4. Add your token.** Create a file named `.env` next to `bot.py`:

```
DISCORD_TOKEN=your-bot-token-here
```

**5. Fill in the IDs.** All IDs at the top of `bot.py` are `None` in this repository. At minimum set `CONSOLE_CHANNEL_ID`, `CONSOLE_STREAMER_ID` and `STAFF_ROLE_IDS` (a set, e.g. `{123456789}`). See [Configuration](#configuration).

**6. Invite the bot** with the `bot` and `applications.commands` scopes, and give it the channel permissions listed in [REQUIREMENTS.md](REQUIREMENTS.md).

**7. Run it**

```bash
python bot.py
```

Slash commands can take a minute to appear the first time.

## Minecraft server setup

Create the two groups the bot assigns. `founder` should inherit `default`, because the bot replaces a player's groups when it sets a rank:

```
lp creategroup founder
lp group founder parent add default
```

Point DiscordSRV's console channel at the channel you set as `CONSOLE_CHANNEL_ID`.

> **Behaviour to be aware of:** `/link` runs `lp user <name> parent set default|founder`, which replaces any groups the player already had. `/unlink` and `/forceunlink` run `lp user <name> clear`, which removes all of that player's LuckPerms data. If you manage extra ranks (staff or higher donor tiers) by hand, relinking will reset them.

## Configuration

Secrets go in `.env` (or real environment variables):

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Bot token |
| `PTERO_PANEL_URL`, `PTERO_SERVER_ID`, `PTERO_API_KEY` | No | Enables the Pterodactyl status check (all three or none) |

Settings live as constants near the top of `bot.py`:

| Constant | Required | Description |
|---|---|---|
| `CONSOLE_CHANNEL_ID` | Yes | Channel the DiscordSRV console is mirrored to |
| `CONSOLE_STREAMER_ID` | Strongly recommended | The account that posts console output. If `None`, anyone's messages in that channel are trusted |
| `STAFF_ROLE_IDS` | For staff commands | Set of role IDs allowed to use staff commands |
| `DONOR_ROLE_ID` | Optional | Role that receives the `founder` rank |
| `LINKED_ROLE_ID` | Optional | Role given on link, removed on unlink |
| `AUDIT_CHANNEL_ID` | Optional | Private staff channel for link/unlink logs |
| `MAX_LINKS_PER_USER` | Optional | Accounts per Discord user (default `1`) |
| `USE_STREAMER_PRESENCE` | Optional | Use the console relay bot's online status (needs the Presence and Server Members intents) |
| `DEBUG_STATE` | Optional | Prints how server status was determined |

## Security

- **Lock down the console channel.** Anything typed there runs as a server command, and the console can show player IP addresses. Only the bot and trusted admins should have access.
- **Keep secrets out of git.** `.env` and `linked_players.db` are in `.gitignore`. The database links Discord IDs to Minecraft names, which is personal data.
- **Set `CONSOLE_STREAMER_ID`.** It stops other users in the console channel from faking server status.
- If your token is ever exposed, reset it immediately in the Discord Developer Portal.

## Troubleshooting

| Problem | Try this |
|---|---|
| `DISCORD_TOKEN environment variable is not set` | Check the file is named exactly `.env`, sits next to `bot.py`, and `python-dotenv` is installed for the Python you are running |
| `pip` is not recognised | Use `python -m pip install -r requirements.txt` (or `py -m pip ...` on Windows) |
| Slash commands do not appear | Wait a minute after startup, and confirm the invite included `applications.commands` |
| Requests stay queued although the server is up | Set `DEBUG_STATE = True` and read the `[STATE]` lines; check `CONSOLE_STREAMER_ID` matches the account that posts console output |
| Linked role is not applied | Give the bot Manage Roles and place its role above the Linked role |
| Bot refuses to start with an intents error | Enable the privileged intents in the Developer Portal, or set `USE_STREAMER_PRESENCE = False` |

## Debugging

With `DEBUG_STATE = True`, the terminal shows one line per signal consulted (`panel`, `presence`, `log`, `probe`, `ack`) and a final `DECISION`. Set it to `False`, or comment the line out, to silence it.

## Project layout

```
bot.py             The bot
requirements.txt   Python dependencies
REQUIREMENTS.md    Full setup requirements
RELEASING.md       How to cut a release
LICENSE            MIT license
.gitignore         Keeps secrets and the database out of git
```

## License

Released under the [MIT License](LICENSE).
