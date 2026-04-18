import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import os
import threading
import json
from typing import Union, Optional, Literal

# --- Dynamic prefix ---
async def get_prefix(bot, message):
    c.execute('SELECT value FROM server_config WHERE key = "prefix"')
    res = c.fetchone()
    return res[0] if res else '!'

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=get_prefix, intents=intents)

# Deduplication guard — prevents two lingering processes from both handling the same message
_handled_messages: set = set()

conn = sqlite3.connect('ice_dodo_sweats.db', check_same_thread=False)
conn.execute('PRAGMA journal_mode=WAL')
c = conn.cursor()

# --- Base tables ---
c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, house TEXT, points INTEGER, role_id INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS houses (name TEXT PRIMARY KEY, points INTEGER, role_id INTEGER, color TEXT, thumbnail_url TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS server_config (key TEXT PRIMARY KEY, value TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS skibidi_toilet (brainrot INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS sticky_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    title TEXT, description TEXT,
    color TEXT DEFAULT '5865F2',
    image_url TEXT, thumbnail_url TEXT,
    footer_text TEXT, footer_icon TEXT,
    button_label TEXT, button_url TEXT,
    last_message_id INTEGER, active INTEGER DEFAULT 1
)''')
c.execute('''CREATE TABLE IF NOT EXISTS message_tracking (user_id INTEGER PRIMARY KEY, message_count INTEGER DEFAULT 0)''')
c.execute('''CREATE TABLE IF NOT EXISTS pending_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    embed_json TEXT NOT NULL,
    button_label TEXT, button_url TEXT,
    status TEXT DEFAULT 'pending'
)''')
c.execute('''CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    user_id INTEGER, house_name TEXT, old_role_id INTEGER,
    status TEXT DEFAULT 'pending'
)''')
conn.commit()

# --- Migrations ---
for migration in [
    'ALTER TABLE houses ADD COLUMN color TEXT',
    'ALTER TABLE houses ADD COLUMN thumbnail_url TEXT',
]:
    try: c.execute(migration); conn.commit()
    except Exception: pass


# --- Embed helpers ---
def get_cfg():
    c.execute('SELECT key, value FROM server_config')
    return {r[0]: r[1] for r in c.fetchall()}


def build_embed(title, desc, color=None, house=None):
    cfg = get_cfg()
    house_thumb = None

    if house:
        c.execute('SELECT color, thumbnail_url FROM houses WHERE name = ?', (house.lower(),))
        h = c.fetchone()
        if h:
            if h[0] and color is None:
                try: color = int(h[0].lstrip('#'), 16)
                except ValueError: pass
            house_thumb = h[1] if h[1] else None

    if color is None:
        raw = cfg.get('embed_color')
        color = int(raw) if raw and str(raw).isdigit() else 0x5865F2

    e = discord.Embed(title=title, description=desc, color=color)

    footer_text = cfg.get('embed_footer_text', 'Ice Dodo | No Cap')
    footer_icon = cfg.get('embed_footer_icon', '')
    e.set_footer(text=footer_text, icon_url=footer_icon) if footer_icon else e.set_footer(text=footer_text)

    thumb = house_thumb or cfg.get('embed_thumbnail', '')
    if thumb: e.set_thumbnail(url=thumb)

    author_name = cfg.get('embed_author_name', '')
    author_icon = cfg.get('embed_author_icon', '')
    if author_name and author_icon: e.set_author(name=author_name, icon_url=author_icon)
    elif author_name: e.set_author(name=author_name)

    return e


def embed(title, desc, color=None):
    return build_embed(title, desc, color=color)


async def log_action(ctx_or_guild, title, desc, guild=None):
    c.execute('SELECT value FROM server_config WHERE key = "log_channel"')
    res = c.fetchone()
    if res:
        g = guild or (ctx_or_guild.guild if hasattr(ctx_or_guild, 'guild') else None)
        channel = bot.get_channel(int(res[0]))
        if channel:
            await channel.send(embed=embed(title, desc))


# --- Background tasks ---
@tasks.loop(seconds=2)
async def process_pending():
    try:
        db = sqlite3.connect('ice_dodo_sweats.db')
        db.row_factory = sqlite3.Row

        # Send pending messages
        for msg in db.execute("SELECT * FROM pending_messages WHERE status='pending' LIMIT 3").fetchall():
            channel = bot.get_channel(msg['channel_id'])
            if not channel:
                try:
                    cfg_row = db.execute("SELECT value FROM server_config WHERE key='guild_id'").fetchone()
                    guild = bot.get_guild(int(cfg_row['value'])) if cfg_row else None
                    if guild:
                        channel = guild.get_channel(msg['channel_id']) or await bot.fetch_channel(msg['channel_id'])
                except Exception:
                    channel = None
            if channel:
                try:
                    ed = json.loads(msg['embed_json'])
                    color_val = ed.get('color', '5865F2')
                    try: color_int = int(color_val) if str(color_val).isdigit() else int(str(color_val).lstrip('#'), 16)
                    except: color_int = 0x5865F2
                    e = discord.Embed(title=ed.get('title',''), description=ed.get('description',''), color=color_int)
                    if ed.get('image_url'): e.set_image(url=ed['image_url'])
                    if ed.get('thumbnail_url'): e.set_thumbnail(url=ed['thumbnail_url'])
                    ft = ed.get('footer_text','')
                    fi = ed.get('footer_icon','')
                    if ft: e.set_footer(text=ft, icon_url=fi) if fi else e.set_footer(text=ft)
                    if ed.get('author_name'):
                        if ed.get('author_icon'): e.set_author(name=ed['author_name'], icon_url=ed['author_icon'])
                        else: e.set_author(name=ed['author_name'])
                    view = None
                    if msg['button_label'] and msg['button_url']:
                        view = discord.ui.View()
                        view.add_item(discord.ui.Button(label=msg['button_label'], url=msg['button_url'], style=discord.ButtonStyle.link))
                    await channel.send(embed=e, view=view)
                    db.execute("UPDATE pending_messages SET status='sent' WHERE id=?", (msg['id'],))
                except Exception as ex:
                    print(f'Pending message error: {ex}')
                    db.execute("UPDATE pending_messages SET status='error' WHERE id=?", (msg['id'],))

        # Process pending role assignments from dashboard
        cfg_row = db.execute("SELECT value FROM server_config WHERE key='guild_id'").fetchone()
        if cfg_row:
            guild = bot.get_guild(int(cfg_row['value']))
            if guild:
                for action in db.execute("SELECT * FROM pending_actions WHERE action_type='assign' AND status='pending' LIMIT 5").fetchall():
                    member = guild.get_member(action['user_id'])
                    if member:
                        try:
                            if action['old_role_id']:
                                old_role = guild.get_role(action['old_role_id'])
                                if old_role: await member.remove_roles(old_role)
                            house = db.execute("SELECT role_id FROM houses WHERE name=?", (action['house_name'],)).fetchone()
                            if house and house['role_id']:
                                new_role = guild.get_role(house['role_id'])
                                if new_role: await member.add_roles(new_role)
                        except Exception as ex:
                            print(f'Role assign error: {ex}')
                    db.execute("UPDATE pending_actions SET status='done' WHERE id=?", (action['id'],))

        db.commit()
        db.close()
    except Exception as ex:
        print(f'process_pending error: {ex}')


# --- Events ---
@bot.event
async def on_ready():
    print('Bot is awake. Time to grind some Ice Dodo.')
    for guild in bot.guilds:
        c.execute('REPLACE INTO server_config (key, value) VALUES ("guild_id", ?)', (str(guild.id),))
    conn.commit()
    process_pending.start()
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} slash commands')
    except Exception as e:
        print(f'Slash sync failed: {e}')


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Drop duplicate — happens when old + new process briefly overlap on restart
    if message.id in _handled_messages:
        return
    _handled_messages.add(message.id)
    if len(_handled_messages) > 1000:
        _handled_messages.clear()

    if not message.author.bot:
        # XP per messages
        c.execute('SELECT value FROM server_config WHERE key="xp_enabled"')
        xp_on = c.fetchone()
        if xp_on and xp_on[0] == '1':
            c.execute('SELECT house FROM users WHERE user_id=?', (message.author.id,))
            user_h = c.fetchone()
            if user_h:
                c.execute('SELECT value FROM server_config WHERE key="xp_per_msgs"')
                per = c.fetchone(); per = int(per[0]) if per else 10
                c.execute('SELECT value FROM server_config WHERE key="xp_amount"')
                amt = c.fetchone(); amt = int(amt[0]) if amt else 1
                c.execute('INSERT OR IGNORE INTO message_tracking (user_id, message_count) VALUES (?,0)', (message.author.id,))
                c.execute('UPDATE message_tracking SET message_count = message_count + 1 WHERE user_id=?', (message.author.id,))
                c.execute('SELECT message_count FROM message_tracking WHERE user_id=?', (message.author.id,))
                count = c.fetchone()[0]
                if count % per == 0:
                    c.execute('UPDATE users SET points = points + ? WHERE user_id=?', (amt, message.author.id))
                    c.execute('UPDATE houses SET points = points + ? WHERE name=?', (amt, user_h[0]))
                    conn.commit()

        # Sticky messages
        c.execute('SELECT id,title,description,color,image_url,thumbnail_url,footer_text,footer_icon,button_label,button_url,last_message_id FROM sticky_messages WHERE channel_id=? AND active=1', (message.channel.id,))
        sticky = c.fetchone()
        if sticky:
            # Delete previous sticky post (only once)
            if sticky[10]:
                try:
                    old = await message.channel.fetch_message(sticky[10])
                    await old.delete()
                except Exception:
                    pass
            try:
                color_int = int(sticky[3].lstrip('#'), 16)
            except Exception:
                color_int = 0x5865F2
            se = discord.Embed(title=sticky[1] or '', description=sticky[2] or '', color=color_int)
            if sticky[4]: se.set_image(url=sticky[4])
            if sticky[5]: se.set_thumbnail(url=sticky[5])
            ft, fi = sticky[6], sticky[7]
            if ft:
                se.set_footer(text=ft, icon_url=fi) if fi else se.set_footer(text=ft)
            view = None
            if sticky[8] and sticky[9]:
                view = discord.ui.View(timeout=None)
                view.add_item(discord.ui.Button(label=sticky[8], url=sticky[9], style=discord.ButtonStyle.link))
            sent = await message.channel.send(embed=se, view=view)
            c.execute('UPDATE sticky_messages SET last_message_id=? WHERE id=?', (sent.id, sticky[0]))
            conn.commit()

    if message.content:
        await bot.process_commands(message)


# --- Commands ---

@bot.hybrid_command(name="setprefix", description="Change the bot's command prefix")
@commands.has_permissions(administrator=True)
@app_commands.describe(new_prefix="The new prefix, e.g. ? or $")
async def setprefix(ctx, new_prefix: str):
    c.execute('REPLACE INTO server_config (key, value) VALUES ("prefix", ?)', (new_prefix,))
    conn.commit()
    await ctx.send(embed=embed("✅ Prefix Updated", f"New prefix is `{new_prefix}`. Use `{new_prefix}help` for commands.", color=0x57F287))


@bot.hybrid_command(name="setlog", description="Set the log channel")
@commands.has_permissions(administrator=True)
@app_commands.describe(channel="The channel for bot logs")
async def setlog(ctx, channel: discord.TextChannel):
    c.execute('REPLACE INTO server_config (key, value) VALUES ("log_channel", ?)', (str(channel.id),))
    conn.commit()
    await ctx.send(embed=embed("📋 Log Channel Set", f"Logs will go to {channel.mention}.", color=0x57F287))


@bot.hybrid_command(name="sethouse", description="Link a house to a Discord role")
@commands.has_permissions(administrator=True)
@app_commands.describe(house_name="Name of the house", role="The Discord role to link")
async def sethouse(ctx, house_name: str, role: discord.Role):
    house_name = house_name.lower()
    c.execute('INSERT OR IGNORE INTO houses (name, points, role_id) VALUES (?, 0, ?)', (house_name, role.id))
    c.execute('UPDATE houses SET role_id=? WHERE name=?', (role.id, house_name))
    conn.commit()
    await ctx.send(embed=build_embed("🏠 House Linked", f"**{house_name.capitalize()}** linked to {role.mention}.", house=house_name, color=0x57F287))
    await log_action(ctx, "🏠 House Created", f"{ctx.author.mention} linked **{house_name.capitalize()}** to {role.mention}.")


@bot.hybrid_command(name="assign", description="Assign a member to a house")
@commands.has_permissions(administrator=True)
@app_commands.describe(member="The member to assign", house_name="House name to assign to", role="House role to assign via")
async def assign(ctx, member: discord.Member, house_name: Optional[str] = None, role: Optional[discord.Role] = None):
    if not house_name and not role:
        return await ctx.send(embed=embed("❌ Missing Info", "Provide a house name or a role.\nExample: `/assign @user Phoenix`", color=0xED4245))

    resolved_house = None
    resolved_role = None

    if role:
        c.execute('SELECT name FROM houses WHERE role_id=?', (role.id,))
        res = c.fetchone()
        if not res:
            return await ctx.send(embed=embed("❌ No House Found", f"{role.mention} isn't linked to any house. Use `sethouse` first.", color=0xED4245))
        resolved_house = res[0]
        resolved_role = role
    elif house_name:
        resolved_house = house_name.lower()
        c.execute('SELECT role_id FROM houses WHERE name=?', (resolved_house,))
        res = c.fetchone()
        if not res:
            return await ctx.send(embed=embed("❌ House Not Found", f"**{house_name}** doesn't exist. Use `sethouse` first.", color=0xED4245))
        resolved_role = ctx.guild.get_role(int(res[0])) if res[0] else None

    c.execute('SELECT house, role_id, points FROM users WHERE user_id=?', (member.id,))
    old = c.fetchone()

    if old and old[0] == resolved_house:
        return await ctx.send(embed=embed("⚠️ Already in House", f"{member.mention} is already in **{resolved_house.capitalize()}**.", color=0xFEE75C))

    if old and old[1]:
        old_role = ctx.guild.get_role(int(old[1]))
        if old_role:
            try: await member.remove_roles(old_role)
            except discord.Forbidden: pass

    c.execute('REPLACE INTO users (user_id, house, points, role_id) VALUES (?,?,?,?)',
              (member.id, resolved_house, old[2] if old else 0, resolved_role.id if resolved_role else None))
    conn.commit()

    if resolved_role:
        try: await member.add_roles(resolved_role)
        except discord.Forbidden:
            await ctx.send(embed=embed("⚠️ Role Error", "Couldn't assign the role — bot role must be above the house role.", color=0xFEE75C))

    action = "moved to" if old and old[0] else "placed in"
    await ctx.send(embed=build_embed("✅ Player Assigned", f"{member.mention} has been {action} **{resolved_house.capitalize()}**.", house=resolved_house, color=0x57F287))
    await log_action(ctx, "🏠 Assignment", f"{ctx.author.mention} {action} {member.mention} → **{resolved_house.capitalize()}**.")


@bot.hybrid_command(name="housepoints", description="Add or remove points from a member")
@commands.has_permissions(administrator=True)
@app_commands.describe(action="Add or remove points", member="The member", amount="Number of points")
async def housepoints(ctx, action: Literal['add', 'remove'], member: discord.Member, amount: int):
    c.execute('SELECT house FROM users WHERE user_id=?', (member.id,))
    result = c.fetchone()
    if not result:
        return await ctx.send(embed=embed("❌ No House", f"{member.mention} isn't in a house yet.", color=0xED4245))
    house_name = result[0]
    modifier = amount if action == 'add' else -amount
    c.execute('UPDATE users SET points = points + ? WHERE user_id=?', (modifier, member.id))
    c.execute('UPDATE houses SET points = points + ? WHERE name=?', (modifier, house_name))
    conn.commit()
    if action == 'add':
        await ctx.send(embed=build_embed("📈 Points Added", f"{member.mention} earned **+{amount}** points for **{house_name.capitalize()}**.", house=house_name, color=0x57F287))
    else:
        await ctx.send(embed=build_embed("📉 Points Removed", f"{member.mention} lost **{amount}** points from **{house_name.capitalize()}**.", house=house_name, color=0xED4245))
    await log_action(ctx, "💰 Points Changed", f"{ctx.author.mention} {'added' if action=='add' else 'removed'} {amount}pts {'to' if action=='add' else 'from'} {member.mention} ({house_name.capitalize()}).")


@bot.hybrid_command(name="stats", description="Check your stats or another member's")
@app_commands.describe(member="The member to check (leave blank for yourself)")
async def stats(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    c.execute('SELECT house, points FROM users WHERE user_id=?', (member.id,))
    res = c.fetchone()
    if not res:
        return await ctx.send(embed=embed("❌ No Stats", f"{member.mention} isn't in any house yet.", color=0xED4245))
    e = build_embed(f"📊 {member.display_name}'s Stats", f"**House:** {res[0].capitalize()}\n**Points:** {res[1]:,}", house=res[0])
    e.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=e)


@bot.hybrid_command(name="houseboard", description="Show the house leaderboard")
async def houseboard(ctx):
    c.execute('SELECT name, points FROM houses ORDER BY points DESC')
    res = c.fetchall()
    if not res:
        return await ctx.send(embed=embed("🏆 House Leaderboard", "No houses yet.", color=0xFEE75C))
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else f'**{i+1}.**'} **{r[0].capitalize()}** — {r[1]:,} pts" for i, r in enumerate(res)]
    await ctx.send(embed=embed("🏆 House Leaderboard", "\n".join(lines), color=0xFEE75C))


@bot.hybrid_command(name="mvp", description="Find the top scorer in a house")
@app_commands.describe(house_name="The house to check")
async def mvp(ctx, house_name: str):
    house_name = house_name.lower()
    c.execute('SELECT user_id, points FROM users WHERE house=? ORDER BY points DESC LIMIT 1', (house_name,))
    res = c.fetchone()
    if not res:
        return await ctx.send(embed=embed("❌ No Results", f"**{house_name.capitalize()}** doesn't exist or has no members.", color=0xED4245))
    member = ctx.guild.get_member(res[0])
    mention = member.mention if member else f"User {res[0]}"
    e = build_embed(f"⭐ {house_name.capitalize()} MVP", f"{mention} is carrying with **{res[1]:,}** points.", house=house_name, color=0xFEE75C)
    if member: e.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=e)


@bot.hybrid_command(name="resetseason", description="Reset all points for a new season")
@commands.has_permissions(administrator=True)
async def resetseason(ctx):
    c.execute('UPDATE users SET points = 0')
    c.execute('UPDATE houses SET points = 0')
    conn.commit()
    await ctx.send(embed=embed("🚨 Season Reset", "All points wiped. The grind starts fresh.", color=0xED4245))
    await log_action(ctx, "☢️ Season Reset", f"{ctx.author.mention} reset all points.")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=embed("🔒 No Permission", "You need **Administrator** to use that.", color=0xED4245))
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send(embed=embed("❌ Member Not Found", "Couldn't find that member.", color=0xED4245))
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send(embed=embed("❌ Role Not Found", "Couldn't find that role.", color=0xED4245))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=embed("❌ Bad Argument", f"`{error}`", color=0xED4245))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=embed("❌ Missing Argument", f"Missing: `{error.param.name}`", color=0xED4245))
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        raise error


def run_dashboard():
    from dashboard import app
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)


if __name__ == '__main__':
    threading.Thread(target=run_dashboard, daemon=True).start()
    print("Dashboard running on port 5000")
    bot.run(os.environ['DISCORD_TOKEN'], reconnect=False)
