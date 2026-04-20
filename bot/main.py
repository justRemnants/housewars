import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import signal
import time
import atexit
import json
from typing import Optional, Literal
import psycopg2
import psycopg2.extras
from psycopg2 import pool

# --- Supabase / PostgreSQL with connection pooling ---
# Connection pool keeps connections alive instead of opening/closing constantly
connection_pool = None

def init_pool():
    global connection_pool
    connection_pool = psycopg2.pool.SimpleConnectionPool(
        1,  # Min connections
        10,  # Max connections
        os.environ['SUPABASE_URL'],
        cursor_factory=psycopg2.extras.RealDictCursor
    )
    print("✅ Database connection pool initialized")

def get_db():
    return connection_pool.getconn()

def return_db(conn):
    connection_pool.putconn(conn)

# --- Single-instance lock (kills any stale process before connecting) ---
_PID_FILE = '/tmp/ice_dodo_bot.pid'

def _acquire_instance_lock():
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as _f:
                _old_pid = int(_f.read().strip())
            os.kill(_old_pid, 0)
            print(f"Stopping old bot process {_old_pid}...")
            os.kill(_old_pid, signal.SIGTERM)
            time.sleep(3)
        except (ProcessLookupError, ValueError, OSError):
            pass
    with open(_PID_FILE, 'w') as _f:
        _f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(_PID_FILE) and os.unlink(_PID_FILE))

_acquire_instance_lock()

# --- Dynamic prefix ---
async def get_prefix(bot, message):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT value FROM server_config WHERE key = %s', ('prefix',))
    res = cur.fetchone()
    return_db(conn)
    return res['value'] if res else '!'

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=get_prefix, intents=intents)

# Deduplication guard
_handled_messages: set = set()

# --- Embed helpers ---
def get_cfg():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT key, value FROM server_config')
    rows = cur.fetchall()
    return_db(conn)
    return {r['key']: r['value'] for r in rows}

def build_embed(title, desc, color=None, house=None):
    cfg = get_cfg()
    house_thumb = None

    if house:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT color, thumbnail_url FROM houses WHERE name = %s', (house.lower(),))
        h = cur.fetchone()
        return_db(conn)
        if h:
            if h['color'] and color is None:
                try:
                    color = int(h['color'].lstrip('#'), 16)
                except ValueError:
                    pass
            house_thumb = h['thumbnail_url'] if h['thumbnail_url'] else None

    if color is None:
        raw = cfg.get('embed_color')
        color = int(raw) if raw and str(raw).isdigit() else 0x5865F2

    e = discord.Embed(title=title, description=desc, color=color)

    footer_text = cfg.get('embed_footer_text', 'Ice Dodo | No Cap')
    footer_icon = cfg.get('embed_footer_icon', '')
    e.set_footer(text=footer_text, icon_url=footer_icon) if footer_icon else e.set_footer(text=footer_text)

    thumb = house_thumb or cfg.get('embed_thumbnail', '')
    if thumb:
        e.set_thumbnail(url=thumb)

    author_name = cfg.get('embed_author_name', '')
    author_icon = cfg.get('embed_author_icon', '')
    if author_name and author_icon:
        e.set_author(name=author_name, icon_url=author_icon)
    elif author_name:
        e.set_author(name=author_name)

    return e

def embed(title, desc, color=None):
    return build_embed(title, desc, color=color)

async def log_action(ctx_or_guild, title, desc, guild=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT value FROM server_config WHERE key = %s', ('log_channel',))
    res = cur.fetchone()
    return_db(conn)
    if res:
        g = guild or (ctx_or_guild.guild if hasattr(ctx_or_guild, 'guild') else None)
        channel = bot.get_channel(int(res['value']))
        if channel:
            await channel.send(embed=embed(title, desc))

# --- Background tasks ---
@tasks.loop(seconds=2)
async def process_pending():
    try:
        conn = get_db()
        cur = conn.cursor()

        # Send pending messages
        cur.execute("SELECT * FROM pending_messages WHERE sent = FALSE LIMIT 3")
        messages = cur.fetchall()
        for msg in messages:
            channel = bot.get_channel(msg['channel_id'])
            if not channel:
                try:
                    channel = await bot.fetch_channel(msg['channel_id'])
                except Exception as fetch_err:
                    print(f'Cannot find channel {msg["channel_id"]}: {fetch_err}')
                    channel = None
            if not channel:
                cur.execute("UPDATE pending_messages SET sent = TRUE WHERE id=%s", (msg['id'],))
                print(f'Pending message {msg["id"]} dropped — channel {msg["channel_id"]} not accessible')
            if channel:
                try:
                    ed = json.loads(msg['embed_json'])
                    color_val = ed.get('color', '5865F2')
                    try:
                        color_int = int(color_val) if str(color_val).isdigit() else int(str(color_val).lstrip('#'), 16)
                    except:
                        color_int = 0x5865F2
                    e = discord.Embed(title=ed.get('title', ''), description=ed.get('description', ''), color=color_int)
                    if ed.get('image_url'):
                        e.set_image(url=ed['image_url'])
                    if ed.get('thumbnail_url'):
                        e.set_thumbnail(url=ed['thumbnail_url'])
                    ft = ed.get('footer_text', '')
                    fi = ed.get('footer_icon', '')
                    if ft:
                        e.set_footer(text=ft, icon_url=fi) if fi else e.set_footer(text=ft)
                    if ed.get('author_name'):
                        if ed.get('author_icon'):
                            e.set_author(name=ed['author_name'], icon_url=ed['author_icon'])
                        else:
                            e.set_author(name=ed['author_name'])
                    view = None
                    if msg['button_label'] and msg['button_url']:
                        view = discord.ui.View()
                        view.add_item(discord.ui.Button(label=msg['button_label'], url=msg['button_url'], style=discord.ButtonStyle.link))
                    await channel.send(embed=e, view=view)
                    cur.execute("UPDATE pending_messages SET sent = TRUE WHERE id=%s", (msg['id'],))
                except Exception as ex:
                    print(f'Pending message error: {ex}')
                    cur.execute("UPDATE pending_messages SET sent = TRUE WHERE id=%s", (msg['id'],))

        # Process pending role assignments from dashboard
        cur.execute("SELECT value FROM server_config WHERE key=%s", ('guild_id',))
        cfg_row = cur.fetchone()
        if cfg_row:
            guild = bot.get_guild(int(cfg_row['value']))
            if guild:
                cur.execute("SELECT * FROM pending_actions WHERE action_type=%s AND done = FALSE LIMIT 5", ('assign',))
                actions = cur.fetchall()
                for action in actions:
                    member = guild.get_member(int(action['user_id']))
                    if member:
                        try:
                            if action['old_role_id']:
                                old_role = guild.get_role(int(action['old_role_id']))
                                if old_role:
                                    await member.remove_roles(old_role)
                            cur.execute("SELECT role_id FROM houses WHERE name=%s", (action['house_name'],))
                            house = cur.fetchone()
                            if house and house['role_id']:
                                new_role = guild.get_role(int(house['role_id']))
                                if new_role:
                                    await member.add_roles(new_role)
                        except Exception as ex:
                            print(f'Role assign error: {ex}')
                    cur.execute("UPDATE pending_actions SET done = TRUE WHERE id=%s", (action['id'],))

        conn.commit()
        return_db(conn)
    except Exception as ex:
        print(f'process_pending error: {ex}')

# --- Events ---
@bot.event
async def on_ready():
    print('Bot is awake. Time to grind some Ice Dodo.')
    conn = get_db()
    cur = conn.cursor()
    for guild in bot.guilds:
        cur.execute('INSERT INTO server_config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=%s', ('guild_id', str(guild.id), str(guild.id)))
    conn.commit()
    return_db(conn)
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

    # Drop duplicate
    if message.id in _handled_messages:
        return
    _handled_messages.add(message.id)
    if len(_handled_messages) > 1000:
        _handled_messages.clear()

    if not message.author.bot:
        # XP per messages
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT value FROM server_config WHERE key=%s', ('xp_enabled',))
        xp_on = cur.fetchone()
        if xp_on and xp_on['value'] == '1':
            cur.execute('SELECT house_id FROM users WHERE user_id=%s', (str(message.author.id),))
            user_h = cur.fetchone()
            if user_h:
                cur.execute('SELECT value FROM server_config WHERE key=%s', ('xp_per_msgs',))
                per = cur.fetchone()
                per = int(per['value']) if per else 10
                cur.execute('SELECT value FROM server_config WHERE key=%s', ('xp_amount',))
                amt = cur.fetchone()
                amt = int(amt['value']) if amt else 1
                cur.execute('UPDATE users SET contributions_points = contributions_points + %s WHERE user_id=%s', (amt, str(message.author.id)))
                cur.execute('UPDATE houses SET house_points = house_points + %s WHERE name=%s', (amt, user_h['house_id']))
                conn.commit()

        # Sticky messages
        cur.execute('SELECT id, title, description, color, image_url, thumbnail_url, footer_text, footer_icon, button_label, button_url FROM sticky_messages WHERE channel_id=%s AND active=TRUE', (message.channel.id,))
        sticky = cur.fetchone()
        return_db(conn)
        if sticky:
            try:
                color_int = int(sticky['color'].lstrip('#'), 16)
            except Exception:
                color_int = 0x5865F2
            se = discord.Embed(title=sticky['title'] or '', description=sticky['description'] or '', color=color_int)
            if sticky['image_url']:
                se.set_image(url=sticky['image_url'])
            if sticky['thumbnail_url']:
                se.set_thumbnail(url=sticky['thumbnail_url'])
            ft, fi = sticky['footer_text'], sticky['footer_icon']
            if ft:
                se.set_footer(text=ft, icon_url=fi) if fi else se.set_footer(text=ft)
            view = None
            if sticky['button_label'] and sticky['button_url']:
                view = discord.ui.View(timeout=None)
                view.add_item(discord.ui.Button(label=sticky['button_label'], url=sticky['button_url'], style=discord.ButtonStyle.link))
            try:
                await message.channel.send(embed=se, view=view)
            except Exception as sticky_err:
                print(f'Sticky send error in channel {message.channel.id}: {sticky_err}')

    if message.content:
        await bot.process_commands(message)

# --- Commands ---
@bot.hybrid_command(name="setprefix", description="Change the bot's command prefix")
@commands.has_permissions(administrator=True)
@app_commands.describe(new_prefix="The new prefix, e.g. ? or $")
async def setprefix(ctx, new_prefix: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT INTO server_config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=%s', ('prefix', new_prefix, new_prefix))
    conn.commit()
    return_db(conn)
    await ctx.send(embed=embed("✅ Prefix Updated", f"New prefix is `{new_prefix}`. Use `{new_prefix}help` for commands.", color=0x57F287))

@bot.hybrid_command(name="setlog", description="Set the log channel")
@commands.has_permissions(administrator=True)
@app_commands.describe(channel="The channel for bot logs")
async def setlog(ctx, channel: discord.TextChannel):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT INTO server_config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=%s', ('log_channel', str(channel.id), str(channel.id)))
    conn.commit()
    return_db(conn)
    await ctx.send(embed=embed("📋 Log Channel Set", f"Logs will go to {channel.mention}.", color=0x57F287))

@bot.hybrid_command(name="sethouse", description="Link a house to a Discord role")
@commands.has_permissions(administrator=True)
@app_commands.describe(house_name="Name of the house", role="The Discord role to link")
async def sethouse(ctx, house_name: str, role: discord.Role):
    house_name = house_name.lower()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO houses (name, house_points, role_id, color, thumbnail_url)
           VALUES (%s, 0, %s, %s, %s)
           ON CONFLICT (name) DO UPDATE SET role_id=%s, color=%s, thumbnail_url=%s''',
        (house_name, role.id, '5865F2', '', role.id, '5865F2', '')
    )
    conn.commit()
    return_db(conn)
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
    conn = get_db()
    cur = conn.cursor()

    if role:
        cur.execute('SELECT name FROM houses WHERE role_id=%s', (role.id,))
        res = cur.fetchone()
        if not res:
            return_db(conn)
            return await ctx.send(embed=embed("❌ No House Found", f"{role.mention} isn't linked to any house. Use `sethouse` first.", color=0xED4245))
        resolved_house = res['name']
        resolved_role = role
    elif house_name:
        resolved_house = house_name.lower()
        cur.execute('SELECT role_id FROM houses WHERE name=%s', (resolved_house,))
        res = cur.fetchone()
        if not res:
            return_db(conn)
            return await ctx.send(embed=embed("❌ House Not Found", f"**{house_name}** doesn't exist. Use `sethouse` first.", color=0xED4245))
        resolved_role = ctx.guild.get_role(int(res['role_id'])) if res['role_id'] else None

    cur.execute('SELECT house_id, contributions_points, role_id FROM users WHERE user_id=%s', (str(member.id),))
    old = cur.fetchone()

    if old and old['house_id'] == resolved_house:
        return_db(conn)
        return await ctx.send(embed=embed("⚠️ Already in House", f"{member.mention} is already in **{resolved_house.capitalize()}**.", color=0xFEE75C))

    if old and old['role_id']:
        old_role = ctx.guild.get_role(int(old['role_id']))
        if old_role:
            try:
                await member.remove_roles(old_role)
            except discord.Forbidden:
                pass

    cur.execute('INSERT INTO users (user_id, house_id, contributions_points, role_id) VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET house_id=%s, role_id=%s',
                (str(member.id), resolved_house, old['contributions_points'] if old else 0, resolved_role.id if resolved_role else None, resolved_house, resolved_role.id if resolved_role else None))
    conn.commit()
    return_db(conn)

    if resolved_role:
        try:
            await member.add_roles(resolved_role)
        except discord.Forbidden:
            await ctx.send(embed=embed("⚠️ Role Error", "Couldn't assign the role — bot role must be above the house role.", color=0xFEE75C))

    action = "moved to" if old and old['house_id'] else "placed in"
    await ctx.send(embed=build_embed("✅ Player Assigned", f"{member.mention} has been {action} **{resolved_house.capitalize()}**.", house=resolved_house, color=0x57F287))
    await log_action(ctx, "🏠 Assignment", f"{ctx.author.mention} {action} {member.mention} → **{resolved_house.capitalize()}**.")

@bot.hybrid_command(name="housepoints", description="Add or remove points from a member")
@commands.has_permissions(administrator=True)
@app_commands.describe(action="Add or remove points", member="The member", amount="Number of points")
async def housepoints(ctx, action: Literal['add', 'remove'], member: discord.Member, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT house_id FROM users WHERE user_id=%s', (str(member.id),))
    result = cur.fetchone()
    if not result:
        return_db(conn)
        return await ctx.send(embed=embed("❌ No House", f"{member.mention} isn't in a house yet.", color=0xED4245))
    house_name = result['house_id']
    modifier = amount if action == 'add' else -amount
    cur.execute('UPDATE users SET contributions_points = contributions_points + %s WHERE user_id=%s', (modifier, str(member.id)))
    cur.execute('UPDATE houses SET house_points = house_points + %s WHERE name=%s', (modifier, house_name))
    conn.commit()
    return_db(conn)
    if action == 'add':
        await ctx.send(embed=build_embed("📈 Points Added", f"{member.mention} earned **+{amount}** points for **{house_name.capitalize()}**.", house=house_name, color=0x57F287))
    else:
        await ctx.send(embed=build_embed("📉 Points Removed", f"{member.mention} lost **{amount}** points from **{house_name.capitalize()}**.", house=house_name, color=0xED4245))
    await log_action(ctx, "💰 Points Changed", f"{ctx.author.mention} {'added' if action == 'add' else 'removed'} {amount}pts {'to' if action == 'add' else 'from'} {member.mention} ({house_name.capitalize()}).")

@bot.hybrid_command(name="stats", description="Check your stats or another member's")
@app_commands.describe(member="The member to check (leave blank for yourself)")
async def stats(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT house_id, contributions_points FROM users WHERE user_id=%s', (str(member.id),))
    res = cur.fetchone()
    return_db(conn)
    if not res:
        return await ctx.send(embed=embed("❌ No Stats", f"{member.mention} isn't in any house yet.", color=0xED4245))
    e = build_embed(f"📊 {member.display_name}'s Stats", f"**House:** {res['house_id'].capitalize()}\n**Points:** {res['contributions_points']:,}", house=res['house_id'])
    e.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=e)

@bot.hybrid_command(name="houseboard", description="Show the house leaderboard")
async def houseboard(ctx):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT name, house_points FROM houses ORDER BY house_points DESC')
    res = cur.fetchall()
    return_db(conn)
    if not res:
        return await ctx.send(embed=embed("🏆 House Leaderboard", "No houses yet.", color=0xFEE75C))
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else f'**{i+1}.**'} **{r['name'].capitalize()}** — {r['house_points']:,} pts" for i, r in enumerate(res)]
    await ctx.send(embed=embed("🏆 House Leaderboard", "\n".join(lines), color=0xFEE75C))

@bot.hybrid_command(name="leaderboard", description="Show member leaderboard (all or by house)")
@app_commands.describe(house_name="Optional: filter by house name")
async def leaderboard(ctx, house_name: Optional[str] = None):
    conn = get_db()
    cur = conn.cursor()
    
    if house_name:
        # Leaderboard for specific house
        house_name = house_name.lower()
        cur.execute('SELECT user_id, contributions_points, house_id FROM users WHERE house_id=%s ORDER BY contributions_points DESC LIMIT 10', (house_name,))
        res = cur.fetchall()
        return_db(conn)
        if not res:
            return await ctx.send(embed=embed("❌ No Members", f"**{house_name.capitalize()}** has no members yet.", color=0xED4245))
        
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(res):
            member = ctx.guild.get_member(int(r['user_id']))
            name = member.display_name if member else f"User {r['user_id']}"
            rank = medals[i] if i < 3 else f"**{i+1}.**"
            lines.append(f"{rank} {name} — **{r['contributions_points']:,}** pts")
        
        e = build_embed(f"🏆 {house_name.capitalize()} Leaderboard", "\n".join(lines), house=house_name, color=0xFEE75C)
        await ctx.send(embed=e)
    else:
        # Overall leaderboard (all members)
        cur.execute('SELECT user_id, contributions_points, house_id FROM users ORDER BY contributions_points DESC LIMIT 15')
        res = cur.fetchall()
        return_db(conn)
        if not res:
            return await ctx.send(embed=embed("🏆 Member Leaderboard", "No members yet.", color=0xFEE75C))
        
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(res):
            member = ctx.guild.get_member(int(r['user_id']))
            name = member.display_name if member else f"User {r['user_id']}"
            rank = medals[i] if i < 3 else f"**{i+1}.**"
            house_tag = f"({r['house_id'].capitalize()})" if r['house_id'] else ""
            lines.append(f"{rank} {name} {house_tag} — **{r['contributions_points']:,}** pts")
        
        await ctx.send(embed=embed("🏆 Member Leaderboard", "\n".join(lines), color=0xFEE75C))

@bot.hybrid_command(name="mvp", description="Find the top scorer in a house")
@app_commands.describe(house_name="The house to check")
async def mvp(ctx, house_name: str):
    house_name = house_name.lower()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id, contributions_points FROM users WHERE house_id=%s ORDER BY contributions_points DESC LIMIT 1', (house_name,))
    res = cur.fetchone()
    return_db(conn)
    if not res:
        return await ctx.send(embed=embed("❌ No Results", f"**{house_name.capitalize()}** doesn't exist or has no members.", color=0xED4245))
    member = ctx.guild.get_member(int(res['user_id']))
    mention = member.mention if member else f"User {res['user_id']}"
    e = build_embed(f"⭐ {house_name.capitalize()} MVP", f"{mention} is carrying with **{res['contributions_points']:,}** points.", house=house_name, color=0xFEE75C)
    if member:
        e.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=e)

@bot.hybrid_command(name="resetseason", description="Reset all points for a new season")
@commands.has_permissions(administrator=True)
async def resetseason(ctx):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE users SET contributions_points = 0')
    cur.execute('UPDATE houses SET house_points = 0')
    conn.commit()
    return_db(conn)
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

if __name__ == '__main__':
    init_pool()
    bot.run(os.environ['DISCORD_TOKEN'], reconnect=False)
