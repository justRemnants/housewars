import discord
from discord.ext import commands, tasks
from discord import app_commands
import os, signal, time, atexit, json, random
from typing import Optional, Literal
import psycopg2, psycopg2.extras
from psycopg2 import pool
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------
connection_pool = None

def init_pool():
    global connection_pool
    connection_pool = psycopg2.pool.SimpleConnectionPool(
        2, 15,
        os.environ['SUPABASE_URL'],
        cursor_factory=psycopg2.extras.RealDictCursor,
        options='-c statement_timeout=5000'
    )
    print("✅ DB pool ready (2-15 connections)")

def get_db():
    conn = connection_pool.getconn()
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn

def return_db(conn):
    connection_pool.putconn(conn)

# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------
_PID_FILE = '/tmp/ice_dodo_bot.pid'

def _acquire_lock():
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as f:
                old = int(f.read().strip())
            os.kill(old, 0)
            print(f"Stopping old process {old}…")
            os.kill(old, signal.SIGTERM)
            time.sleep(3)
        except (ProcessLookupError, ValueError, OSError):
            pass
    with open(_PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(_PID_FILE) and os.unlink(_PID_FILE))

_acquire_lock()

# ---------------------------------------------------------------------------
# Dynamic prefix
# ---------------------------------------------------------------------------
_prefix_cache      = '!'
_prefix_cache_time = 0

async def get_prefix(bot, message):
    global _prefix_cache, _prefix_cache_time
    now = time.time()
    if now - _prefix_cache_time > 300:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM server_config WHERE key='prefix'")
            res = cur.fetchone()
            _prefix_cache      = res['value'] if res else '!'
            _prefix_cache_time = now
        finally:
            return_db(conn)
    return _prefix_cache

intents = discord.Intents.default()
intents.message_content = True
intents.members = True        # required for on_member_join and guild member list
bot = commands.Bot(command_prefix=get_prefix, intents=intents)

_handled_messages: set = set()
_config_cache      = {}
_config_cache_time = 0
_house_cache       = {}
_house_cache_time  = 0

# ---------------------------------------------------------------------------
# Config / house cache
# ---------------------------------------------------------------------------
def get_cfg():
    global _config_cache, _config_cache_time
    now = time.time()
    if now - _config_cache_time > 120:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute('SELECT key, value FROM server_config')
            _config_cache      = {r['key']: r['value'] for r in cur.fetchall()}
            _config_cache_time = now
        finally:
            return_db(conn)
    return _config_cache

def get_house_data(house_name):
    global _house_cache, _house_cache_time
    now = time.time()
    if now - _house_cache_time > 60:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute('SELECT name, color, thumbnail_url FROM houses')
            _house_cache      = {r['name']: r for r in cur.fetchall()}
            _house_cache_time = now
        finally:
            return_db(conn)
    return _house_cache.get(house_name.lower())

# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------
def build_embed(title, desc, color=None, house=None):
    cfg        = get_cfg()
    house_thumb = None
    if house:
        h = get_house_data(house)
        if h:
            if h['color'] and color is None:
                try: color = int(h['color'].lstrip('#'), 16)
                except ValueError: pass
            house_thumb = h['thumbnail_url'] or None
    if color is None:
        raw   = cfg.get('embed_color')
        color = int(raw) if raw and str(raw).isdigit() else 0x5865F2
    e = discord.Embed(title=title, description=desc, color=color)
    ft, fi = cfg.get('embed_footer_text','Ice Dodo | No Cap'), cfg.get('embed_footer_icon','')
    e.set_footer(text=ft, icon_url=fi) if fi else e.set_footer(text=ft)
    thumb = house_thumb or cfg.get('embed_thumbnail','')
    if thumb: e.set_thumbnail(url=thumb)
    an, ai = cfg.get('embed_author_name',''), cfg.get('embed_author_icon','')
    if an and ai: e.set_author(name=an, icon_url=ai)
    elif an:      e.set_author(name=an)
    return e

def embed(title, desc, color=None):
    return build_embed(title, desc, color=color)

# ---------------------------------------------------------------------------
# Channel helper  (cache → fetch, with clear error logging)
# ---------------------------------------------------------------------------
async def get_channel(channel_id: int):
    ch = bot.get_channel(channel_id)
    if ch:
        return ch
    try:
        return await bot.fetch_channel(channel_id)
    except discord.NotFound:
        print(f'get_channel: {channel_id} not found')
    except discord.Forbidden:
        print(f'get_channel: {channel_id} — bot lacks permission')
    except Exception as e:
        print(f'get_channel({channel_id}) error: {e}')
    return None

# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------
def write_log(cur, user_id, target_username, target_avatar, amount, reason,
              action, house_id, actor_id, actor_name, actor_avatar=None):
    try:
        cur.execute(
            'INSERT INTO logs (user_id,target_username,target_avatar,amount,reason,action,house_id,actor_id,actor_username,actor_avatar) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (str(user_id), target_username, target_avatar, amount, reason,
             action, house_id, str(actor_id) if actor_id else None, actor_name, actor_avatar))
    except Exception as e:
        print(f'write_log error: {e}')

async def post_log(house_id, member_display, amount, reason, action, actor_name):
    cfg = get_cfg()
    ch  = cfg.get('log_channel')
    if not ch:
        return
    channel = await get_channel(int(ch))
    if not channel:
        return
    sign, emoji = ('+','📈') if action=='add' else ('-','📉')
    color = 0x57F287 if action=='add' else 0xED4245
    lines = [f"**Member:** {member_display or '—'}",
             f"**House:** {(house_id or '—').capitalize()}",
             f"**Points:** `{sign}{amount}`"]
    if reason: lines.append(f"**Reason:** {reason}")
    lines.append(f"**By:** {actor_name or '—'}")
    e = discord.Embed(title=f'{emoji} Points {"Added" if action=="add" else "Removed"}',
                      description='\n'.join(lines), color=color)
    e.set_footer(text='Ice Dodo Points Log')
    try:
        await channel.send(embed=e)
    except Exception as ex:
        print(f'post_log send error: {ex}')

async def log_action(title, desc):
    cfg = get_cfg()
    ch  = cfg.get('log_channel')
    if not ch:
        return
    channel = await get_channel(int(ch))
    if channel:
        try: await channel.send(embed=embed(title, desc))
        except Exception as e: print(f'log_action error: {e}')

# ---------------------------------------------------------------------------
# Background task — pending messages + role assignments
# ---------------------------------------------------------------------------
@tasks.loop(seconds=2)
async def process_pending():
    try:
        conn = get_db()
        try:
            cur = conn.cursor()

            # Pending messages
            cur.execute("SELECT * FROM pending_messages WHERE sent=FALSE LIMIT 3")
            for msg in cur.fetchall():
                channel = await get_channel(int(msg['channel_id']))
                if not channel:
                    cur.execute("UPDATE pending_messages SET sent=TRUE WHERE id=%s", (msg['id'],))
                    continue
                try:
                    ed  = json.loads(msg['embed_json'])
                    cv  = ed.get('color','5865F2')
                    try: ci = int(cv) if str(cv).isdigit() else int(str(cv).lstrip('#'),16)
                    except: ci = 0x5865F2
                    e = discord.Embed(title=ed.get('title',''), description=ed.get('description',''), color=ci)
                    if ed.get('image_url'):     e.set_image(url=ed['image_url'])
                    if ed.get('thumbnail_url'): e.set_thumbnail(url=ed['thumbnail_url'])
                    ft, fi = ed.get('footer_text',''), ed.get('footer_icon','')
                    if ft: e.set_footer(text=ft, icon_url=fi) if fi else e.set_footer(text=ft)
                    if ed.get('author_name'):
                        e.set_author(name=ed['author_name'], icon_url=ed['author_icon']) if ed.get('author_icon') else e.set_author(name=ed['author_name'])
                    view = None
                    if msg['button_label'] and msg['button_url']:
                        view = discord.ui.View()
                        view.add_item(discord.ui.Button(label=msg['button_label'], url=msg['button_url'], style=discord.ButtonStyle.link))
                    await channel.send(embed=e, view=view)
                except Exception as ex:
                    print(f'Pending message error (id={msg["id"]}): {ex}')
                finally:
                    cur.execute("UPDATE pending_messages SET sent=TRUE WHERE id=%s", (msg['id'],))

            # Pending role assignments
            cur.execute("SELECT value FROM server_config WHERE key='guild_id'")
            row = cur.fetchone()
            if row and row['value']:
                guild = bot.get_guild(int(row['value']))
                if guild:
                    cur.execute("SELECT * FROM pending_actions WHERE action_type='assign' AND done=FALSE LIMIT 5")
                    for action in cur.fetchall():
                        member = guild.get_member(int(action['user_id']))
                        if member:
                            try:
                                if action['old_role_id']:
                                    old_role = guild.get_role(int(action['old_role_id']))
                                    if old_role: await member.remove_roles(old_role)
                                cur.execute("SELECT role_id FROM houses WHERE name=%s", (action['house_name'],))
                                house = cur.fetchone()
                                if house and house['role_id']:
                                    new_role = guild.get_role(int(house['role_id']))
                                    if new_role: await member.add_roles(new_role)
                            except Exception as ex:
                                print(f'Role assign error (user={action["user_id"]}): {ex}')
                        cur.execute("UPDATE pending_actions SET done=TRUE WHERE id=%s", (action['id'],))
        finally:
            return_db(conn)
    except Exception as ex:
        print(f'process_pending error: {ex}')

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f'✅ Online as {bot.user}')
    conn = get_db()
    try:
        cur = conn.cursor()
        for guild in bot.guilds:
            cur.execute('INSERT INTO server_config (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s',
                        ('guild_id', str(guild.id), str(guild.id)))
    finally:
        return_db(conn)
    process_pending.start()
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} slash commands')
    except Exception as e:
        print(f'Slash sync failed: {e}')

@bot.event
async def on_member_join(member: discord.Member):
    cfg            = get_cfg()
    auto_house_cfg = cfg.get('auto_assign_house','').strip().lower()
    resolved_house = None

    if auto_house_cfg:
        conn = get_db()
        try:
            cur = conn.cursor()
            # 'random' → pick any house at random, otherwise assign to the named house
            if auto_house_cfg == 'random':
                cur.execute('SELECT name, role_id FROM houses ORDER BY RANDOM() LIMIT 1')
            else:
                cur.execute('SELECT name, role_id FROM houses WHERE name=%s', (auto_house_cfg,))
            house = cur.fetchone()
            if house:
                resolved_house = house['name']
                cur.execute(
                    'INSERT INTO users (user_id,house_id,contributions_points,role_id) VALUES (%s,%s,0,%s) ON CONFLICT (user_id) DO NOTHING',
                    (str(member.id), resolved_house, house['role_id']))
                if house['role_id']:
                    role = member.guild.get_role(int(house['role_id']))
                    if role:
                        try: await member.add_roles(role)
                        except discord.Forbidden:
                            print(f'on_member_join: cannot assign role {role.id} — check bot hierarchy')
        except Exception as e:
            print(f'on_member_join auto-assign error: {e}')
        finally:
            return_db(conn)

    # Welcome message
    welcome_ch  = cfg.get('welcome_channel','').strip()
    welcome_msg = cfg.get('welcome_message','').strip()
    if welcome_ch and welcome_msg:
        channel = await get_channel(int(welcome_ch))
        if channel:
            try:
                text = welcome_msg.replace('{user}', member.mention)
                text = text.replace('{house}', resolved_house.capitalize() if resolved_house else 'a house')
                e = build_embed(f'👋 Welcome, {member.display_name}!', text, house=resolved_house)
                e.set_thumbnail(url=member.display_avatar.url)
                await channel.send(embed=e)
            except Exception as ex:
                print(f'on_member_join welcome error: {ex}')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.id in _handled_messages:
        return
    _handled_messages.add(message.id)
    if len(_handled_messages) > 1000:
        _handled_messages.clear()

    if not message.author.bot:
        cfg = get_cfg()

        # XP per message
        if cfg.get('xp_enabled') == '1':
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute('SELECT house_id FROM users WHERE user_id=%s', (str(message.author.id),))
                uh = cur.fetchone()
                if uh:
                    amt = int(cfg.get('xp_amount','1'))
                    cur.execute('UPDATE users SET contributions_points = contributions_points + %s WHERE user_id=%s', (amt, str(message.author.id)))
                    cur.execute('UPDATE houses SET house_points = house_points + %s WHERE name=%s', (amt, uh['house_id']))
            finally:
                return_db(conn)

        # Sticky messages
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                'SELECT title,description,color,image_url,thumbnail_url,footer_text,footer_icon,button_label,button_url FROM sticky_messages WHERE channel_id=%s AND active=TRUE',
                (message.channel.id,))
            sticky = cur.fetchone()
            if sticky:
                try:
                    ci = int(sticky['color'].lstrip('#'), 16)
                except Exception:
                    ci = 0x5865F2
                se = discord.Embed(title=sticky['title'] or '', description=sticky['description'] or '', color=ci)
                if sticky['image_url']:     se.set_image(url=sticky['image_url'])
                if sticky['thumbnail_url']: se.set_thumbnail(url=sticky['thumbnail_url'])
                ft, fi = sticky['footer_text'], sticky['footer_icon']
                if ft: se.set_footer(text=ft, icon_url=fi) if fi else se.set_footer(text=ft)
                view = None
                if sticky['button_label'] and sticky['button_url']:
                    view = discord.ui.View(timeout=None)
                    view.add_item(discord.ui.Button(label=sticky['button_label'], url=sticky['button_url'], style=discord.ButtonStyle.link))
                try: await message.channel.send(embed=se, view=view)
                except Exception as ex: print(f'Sticky send error: {ex}')
        finally:
            return_db(conn)

    if message.content:
        await bot.process_commands(message)

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@bot.hybrid_command(name="setprefix", description="Change the bot's command prefix")
@commands.has_permissions(administrator=True)
@app_commands.describe(new_prefix="New prefix, e.g. ? or $")
async def setprefix(ctx, new_prefix: str):
    global _prefix_cache, _prefix_cache_time
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('INSERT INTO server_config (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s', ('prefix',new_prefix,new_prefix))
        _prefix_cache = new_prefix; _prefix_cache_time = time.time()
        await ctx.send(embed=embed("✅ Prefix Updated", f"New prefix: `{new_prefix}`", color=0x57F287))
    finally:
        return_db(conn)

@bot.hybrid_command(name="dbtest", description="Test database connection speed")
async def dbtest(ctx):
    t0=time.time(); conn=get_db(); t1=time.time()
    conn.cursor().execute('SELECT 1'); t2=time.time(); return_db(conn); t3=time.time()
    await ctx.send(embed=embed("🔍 DB Test",
        f"**Connect:** {(t1-t0)*1000:.0f}ms\n**Query:** {(t2-t1)*1000:.0f}ms\n**Total:** {(t3-t0)*1000:.0f}ms",
        color=0x57F287))

@bot.hybrid_command(name="setlog", description="Set the log channel")
@commands.has_permissions(administrator=True)
@app_commands.describe(channel="Channel for bot logs")
async def setlog(ctx, channel: discord.TextChannel):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('INSERT INTO server_config (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s', ('log_channel',str(channel.id),str(channel.id)))
        await ctx.send(embed=embed("📋 Log Channel Set", f"Logs → {channel.mention}", color=0x57F287))
    finally:
        return_db(conn)

@bot.hybrid_command(name="setwelcome", description="Set welcome channel and message")
@commands.has_permissions(administrator=True)
@app_commands.describe(channel="Channel for welcome messages", message="Text — use {user} for mention, {house} for house name")
async def setwelcome(ctx, channel: discord.TextChannel, *, message: str):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('INSERT INTO server_config (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s', ('welcome_channel',str(channel.id),str(channel.id)))
        cur.execute('INSERT INTO server_config (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s', ('welcome_message',message,message))
        await ctx.send(embed=embed("👋 Welcome Set",
            f"**Channel:** {channel.mention}\n**Message:** {message}\n\nUse `{{user}}` for mention, `{{house}}` for house name.",
            color=0x57F287))
    finally:
        return_db(conn)

@bot.hybrid_command(name="setautoassign", description="Auto-assign new members to a house on join")
@commands.has_permissions(administrator=True)
@app_commands.describe(house_name="House name, 'random' to randomise, or leave blank to disable")
async def setautoassign(ctx, house_name: Optional[str] = None):
    conn = get_db()
    try:
        cur = conn.cursor()
        val = house_name.lower().strip() if house_name else ''
        cur.execute('INSERT INTO server_config (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s', ('auto_assign_house',val,val))
        if val == 'random':
            await ctx.send(embed=embed("🎲 Auto-Assign: Random", "New members will be placed in a random house on join.", color=0x57F287))
        elif val:
            await ctx.send(embed=embed("🏠 Auto-Assign Set", f"New members will be placed in **{val.capitalize()}**.", color=0x57F287))
        else:
            await ctx.send(embed=embed("🏠 Auto-Assign Disabled", "New members won't be auto-assigned to a house.", color=0xFEE75C))
    finally:
        return_db(conn)

@bot.hybrid_command(name="sethouse", description="Link a house to a Discord role")
@commands.has_permissions(administrator=True)
@app_commands.describe(house_name="Name of the house", role="Discord role to link")
async def sethouse(ctx, house_name: str, role: discord.Role):
    global _house_cache_time
    hn   = house_name.lower()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('INSERT INTO houses (name,house_points,role_id,color,thumbnail_url) VALUES (%s,0,%s,%s,%s) ON CONFLICT (name) DO UPDATE SET role_id=%s,color=%s,thumbnail_url=%s',
                    (hn, str(role.id), '5865F2', '', str(role.id), '5865F2', ''))
        _house_cache_time = 0
        await ctx.send(embed=build_embed("🏠 House Linked", f"**{hn.capitalize()}** linked to {role.mention}.", house=hn, color=0x57F287))
        await log_action("🏠 House Created", f"{ctx.author.mention} linked **{hn.capitalize()}** to {role.mention}.")
    finally:
        return_db(conn)

@bot.hybrid_command(name="assign", description="Assign a member to a house")
@commands.has_permissions(administrator=True)
@app_commands.describe(member="Member to assign", house_name="House name", role="House role (alternative to name)")
async def assign(ctx, member: discord.Member, house_name: Optional[str] = None, role: Optional[discord.Role] = None):
    try:
        import re
        if house_name and not role:
            m = re.match(r'^<@&(\d+)>$', house_name.strip())
            if m: role = ctx.guild.get_role(int(m.group(1))); house_name = None

        if not house_name and not role:
            return await ctx.send(embed=embed("❌ Missing Info",
                "Provide a house name or role.\nExamples:\n`!assign @user Phoenix`\n`!assign @user @PhoenixRole`",
                color=0xED4245))

        conn = get_db()
        try:
            cur = conn.cursor()
            resolved_house = None
            resolved_role  = None

            if role:
                cur.execute('SELECT name, role_id FROM houses WHERE role_id=%s', (str(role.id),))
                res = cur.fetchone()
                if not res:
                    return await ctx.send(embed=embed("❌ No House Found",
                        f"{role.mention} isn't linked to any house. Use `/sethouse` first.", color=0xED4245))
                resolved_house = res['name']
                resolved_role  = role
            else:
                resolved_house = house_name.lower()
                cur.execute('SELECT name, role_id FROM houses WHERE name=%s', (resolved_house,))
                res = cur.fetchone()
                if not res:
                    return await ctx.send(embed=embed("❌ House Not Found",
                        f"**{house_name}** doesn't exist. Use `/sethouse` first.", color=0xED4245))
                if res['role_id']:
                    resolved_role = ctx.guild.get_role(int(res['role_id']))

            cur.execute('SELECT house_id, contributions_points, role_id FROM users WHERE user_id=%s', (str(member.id),))
            old = cur.fetchone()

            if old and old['house_id'] == resolved_house:
                return await ctx.send(embed=embed("⚠️ Already in House",
                    f"{member.mention} is already in **{resolved_house.capitalize()}**.", color=0xFEE75C))

            if old and old['role_id']:
                old_role = ctx.guild.get_role(int(old['role_id']))
                if old_role:
                    try: await member.remove_roles(old_role)
                    except discord.Forbidden: pass

            cur.execute('INSERT INTO users (user_id,house_id,contributions_points,role_id) VALUES (%s,%s,%s,%s) ON CONFLICT (user_id) DO UPDATE SET house_id=%s,role_id=%s',
                        (str(member.id), resolved_house,
                         old['contributions_points'] if old else 0,
                         str(resolved_role.id) if resolved_role else None,
                         resolved_house, str(resolved_role.id) if resolved_role else None))

            if resolved_role:
                try: await member.add_roles(resolved_role)
                except discord.Forbidden:
                    await ctx.send(embed=embed("⚠️ Role Error",
                        "Couldn't assign role — bot role must be above the house role.", color=0xFEE75C))

            word = "moved to" if old and old['house_id'] else "placed in"
            await ctx.send(embed=build_embed("✅ Assigned",
                f"{member.mention} {word} **{resolved_house.capitalize()}**.",
                house=resolved_house, color=0x57F287))
            await log_action("🏠 Assignment",
                f"{ctx.author.mention} {word} {member.mention} → **{resolved_house.capitalize()}**")
        finally:
            return_db(conn)
    except Exception as e:
        import traceback; traceback.print_exc()
        await ctx.send(embed=embed("❌ Error", str(e), color=0xED4245))

@bot.hybrid_command(name="housepoints", description="Add or remove points from a member")
@commands.has_permissions(administrator=True)
@app_commands.describe(action="add or remove", member="Member", amount="Points", reason="Reason (optional)")
async def housepoints(ctx, action: Literal['add','remove'], member: discord.Member, amount: int, *, reason: Optional[str] = None):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT house_id FROM users WHERE user_id=%s', (str(member.id),))
        res = cur.fetchone()
        if not res:
            return await ctx.send(embed=embed("❌ No House", f"{member.mention} isn't in a house.", color=0xED4245))
        house = res['house_id']
        mod   = amount if action=='add' else -amount
        cur.execute('UPDATE users SET contributions_points = contributions_points + %s WHERE user_id=%s', (mod, str(member.id)))
        cur.execute('UPDATE houses SET house_points = house_points + %s WHERE name=%s', (mod, house))
        write_log(cur, str(member.id), member.display_name,
                  member.avatar.key if member.avatar else None,
                  amount, reason or '', action, house,
                  str(ctx.author.id), ctx.author.display_name,
                  ctx.author.avatar.key if ctx.author.avatar else None)
        desc = (f"{member.mention} earned **+{amount}** pts for **{house.capitalize()}**."
                if action=='add' else
                f"{member.mention} lost **{amount}** pts from **{house.capitalize()}**.")
        if reason: desc += f"\n**Reason:** {reason}"
        await ctx.send(embed=build_embed(
            '📈 Points Added' if action=='add' else '📉 Points Removed',
            desc, house=house, color=0x57F287 if action=='add' else 0xED4245))
        await post_log(house, member.display_name, amount, reason or '', action, ctx.author.display_name)
    finally:
        return_db(conn)

@bot.hybrid_command(name="stats", description="Check a member's stats")
@app_commands.describe(member="Member to check (leave blank for yourself)")
async def stats(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    conn   = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT house_id, contributions_points FROM users WHERE user_id=%s', (str(member.id),))
        res = cur.fetchone()
        if not res:
            return await ctx.send(embed=embed("❌ No Stats", f"{member.mention} isn't in a house.", color=0xED4245))
        e = build_embed(f"📊 {member.display_name}'s Stats",
                        f"**House:** {res['house_id'].capitalize()}\n**Points:** {res['contributions_points']:,}",
                        house=res['house_id'])
        e.set_thumbnail(url=member.display_avatar.url)
        await ctx.send(embed=e)
    finally:
        return_db(conn)

@bot.hybrid_command(name="houseboard", description="House leaderboard")
async def houseboard(ctx):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT name, house_points FROM houses ORDER BY house_points DESC')
        res = cur.fetchall()
        if not res:
            return await ctx.send(embed=embed("🏆 House Leaderboard", "No houses yet.", color=0xFEE75C))
        medals = ["🥇","🥈","🥉"]
        lines  = [f"{medals[i] if i<3 else f'**{i+1}.**'} **{r['name'].capitalize()}** — {r['house_points']:,} pts"
                  for i, r in enumerate(res)]
        await ctx.send(embed=embed("🏆 House Leaderboard", "\n".join(lines), color=0xFEE75C))
    finally:
        return_db(conn)

@bot.hybrid_command(name="leaderboard", description="Member leaderboard")
@app_commands.describe(house_name="Filter by house (optional)")
async def leaderboard(ctx, house_name: Optional[str] = None):
    conn = get_db()
    try:
        cur = conn.cursor()
        if house_name:
            hn = house_name.lower()
            cur.execute('SELECT user_id, contributions_points FROM users WHERE house_id=%s ORDER BY contributions_points DESC LIMIT 10', (hn,))
            res = cur.fetchall()
            if not res:
                return await ctx.send(embed=embed("❌ No Members", f"**{hn.capitalize()}** has no members.", color=0xED4245))
            medals = ["🥇","🥈","🥉"]
            lines  = []
            for i, r in enumerate(res):
                m    = ctx.guild.get_member(int(r['user_id']))
                name = m.display_name if m else f"User {r['user_id']}"
                lines.append(f"{medals[i] if i<3 else f'**{i+1}.**'} {name} — **{r['contributions_points']:,}** pts")
            await ctx.send(embed=build_embed(f"🏆 {hn.capitalize()} Leaderboard", "\n".join(lines), house=hn, color=0xFEE75C))
        else:
            cur.execute('SELECT user_id, contributions_points, house_id FROM users ORDER BY contributions_points DESC LIMIT 15')
            res = cur.fetchall()
            if not res:
                return await ctx.send(embed=embed("🏆 Leaderboard", "No members yet.", color=0xFEE75C))
            medals = ["🥇","🥈","🥉"]
            lines  = []
            for i, r in enumerate(res):
                m    = ctx.guild.get_member(int(r['user_id']))
                name = m.display_name if m else f"User {r['user_id']}"
                tag  = f"({r['house_id'].capitalize()})" if r['house_id'] else ""
                lines.append(f"{medals[i] if i<3 else f'**{i+1}.**'} {name} {tag} — **{r['contributions_points']:,}** pts")
            await ctx.send(embed=embed("🏆 Member Leaderboard", "\n".join(lines), color=0xFEE75C))
    finally:
        return_db(conn)

@bot.hybrid_command(name="mvp", description="Top scorer in a house")
@app_commands.describe(house_name="House to check")
async def mvp(ctx, house_name: str):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT user_id, contributions_points FROM users WHERE house_id=%s ORDER BY contributions_points DESC LIMIT 1', (house_name.lower(),))
        res = cur.fetchone()
        if not res:
            return await ctx.send(embed=embed("❌ No Results", f"**{house_name.capitalize()}** has no members.", color=0xED4245))
        m = ctx.guild.get_member(int(res['user_id']))
        e = build_embed(f"⭐ {house_name.capitalize()} MVP",
                        f"{m.mention if m else f'User {res[\"user_id\"]}'} — **{res['contributions_points']:,}** pts",
                        house=house_name.lower(), color=0xFEE75C)
        if m: e.set_thumbnail(url=m.display_avatar.url)
        await ctx.send(embed=e)
    finally:
        return_db(conn)

@bot.hybrid_command(name="resetseason", description="Reset all points for a new season")
@commands.has_permissions(administrator=True)
async def resetseason(ctx):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('UPDATE users SET contributions_points=0')
        cur.execute('UPDATE houses SET house_points=0')
        await ctx.send(embed=embed("🚨 Season Reset", "All points wiped. The grind starts fresh.", color=0xED4245))
        await log_action("☢️ Season Reset", f"{ctx.author.mention} reset all points.")
    finally:
        return_db(conn)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=embed("🔒 No Permission", "You need **Administrator**.", color=0xED4245))
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
