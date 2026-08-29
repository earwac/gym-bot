# Gym Challenge Discord Bot

Auto-logs a gym day when someone posts an image in the tracked channel.
Weeks run Monday–Sunday. Goal is 3 gym days/week; each day short of 3 costs $5.

## What it does
- Watches one channel per server (you pick it with `/setchannel`).
- Any image posted there logs **one** gym day for the poster, for today.
- Multiple photos the same day count once (reacts 🔁 on the extras).
- Reacts ✅ when you hit 3, 💪 otherwise, and posts a short running tally.

## Slash commands
| Command | What it shows |
|---|---|
| `/status [member]` | This week's days + owed for you or a member |
| `/week` | Everyone's counts this week |
| `/owed` | Total owed per person across all weeks |
| `/leaderboard` | Most gym days this week |
| `/undo` | Remove today's logged day for yourself (fix mistakes) |
| `/setchannel` | (Admin) Track the current channel |

---

## Setup (about 10 minutes)

### 1. Create the bot application
1. Go to https://discord.com/developers/applications → **New Application**, name it.
2. Left sidebar → **Bot** → **Add Bot**.
3. Under **Privileged Gateway Intents**, turn ON **Message Content Intent** (the bot reads attachments) AND **Server Members Intent** (the bot lists everyone in the channel so no-shows appear in reports). Both are required.
4. Click **Reset Token**, copy the token, keep it secret. This is your `DISCORD_TOKEN`.

### 2. Invite the bot to your server
1. Left sidebar → **OAuth2** → **URL Generator**.
2. Scopes: check **bot** and **applications.commands**.
3. Bot Permissions: **Read Messages/View Channels**, **Send Messages**, **Add Reactions**, **Read Message History**.
4. Copy the generated URL, open it, pick your server, authorize.

### 3. Run it

**Locally (to test):**
```bash
cd gym-bot
python -m pip install -r requirements.txt
export DISCORD_TOKEN="your-token-here"      # Windows: set DISCORD_TOKEN=your-token-here
python bot.py
```
You should see `Logged in as ... Synced N commands.`

**In Discord:** go to your gym channel and run `/setchannel`. Post a photo — the bot logs it.

---

## Hosting it 24/7

The bot only tracks while `bot.py` is running. Options:

**Railway (easiest free-ish):**
1. Push this folder to a GitHub repo.
2. railway.app → New Project → Deploy from GitHub repo.
3. Add a variable `DISCORD_TOKEN` with your token.
4. Set the start command to `python bot.py`.
5. Add a **Volume** mounted at the project directory so `gym.db` persists across restarts (otherwise data resets on redeploy).

**A Raspberry Pi / old laptop / any always-on machine:**
Run the same commands as "Locally" above, ideally under a process manager so it restarts:
```bash
# using pm2 (needs Node) — or use systemd / screen / tmux
pm2 start bot.py --name gym-bot --interpreter python3
```

**Fly.io** also works; any host that runs a long-lived Python process is fine. Serverless platforms (Vercel, Lambda) are **not** suitable — this needs a persistent connection.

---

## Notes & limits
- **Verification is human.** The bot confirms an image was posted and by whom; it does not judge whether it's really a gym. The photo is your proof, same as posting it manually — logging is just automatic now.
- **One channel per server.** Run `/setchannel` again in a different channel to move it.
- **Time zone** follows the machine running the bot (uses its local date). If your group spans zones, host somewhere in your preferred zone, or tell me and I'll pin it to a fixed zone.
- **Data** is in `gym.db` (SQLite) next to `bot.py`. Back it up to keep history.
- Want a `/report` that exports everything to a CSV/Excel, or weekly auto-summaries every Sunday night? Ask and I'll add them.
