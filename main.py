import discord
from discord.ext import commands
import sqlite3
import os
from typing import Union

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

conn = sqlite3.connect('ice_dodo_sweats.db')
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, house TEXT, points INTEGER, role_id INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS houses (name TEXT PRIMARY KEY, points INTEGER, role_id INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS server_config (key TEXT PRIMARY KEY, value INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS skibidi_toilet (brainrot INTEGER)''')
conn.commit()

try:
    c.execute('ALTER TABLE houses ADD COLUMN role_id INTEGER')
    conn.commit()
except Exception:
    pass


def embed(title, desc, color=0x5865F2):
    e = discord.Embed(title=title, description=desc, color=color)
    e.set_footer(text="Ice Dodo | No Cap")
    return e


async def log_action(ctx, title, desc):
    c.execute('SELECT value FROM server_config WHERE key = "log_channel"')
    res = c.fetchone()
    if res:
        channel = bot.get_channel(res[0])
        if channel:
            await channel.send(embed=embed(title, desc, color=0x2b2d31))


@bot.event
async def on_ready():
    print(f'Bot is awake. Time to grind some Ice Dodo. 💀')


@bot.command()
@commands.has_permissions(administrator=True)
async def setlog(ctx, channel: discord.TextChannel):
    """Sets the channel where all the logging happens."""
    c.execute('REPLACE INTO server_config (key, value) VALUES ("log_channel", ?)', (channel.id,))
    conn.commit()
    await ctx.send(embed=embed("📋 Log Channel Set", f"All actions will be logged to {channel.mention}.", color=0x57F287))


@bot.command()
@commands.has_permissions(administrator=True)
async def sethouse(ctx, house_name: str, role: discord.Role):
    """Links a house name to a Discord role."""
    house_name = house_name.lower()
    c.execute('INSERT OR IGNORE INTO houses (name, points, role_id) VALUES (?, 0, ?)', (house_name, role.id))
    c.execute('UPDATE houses SET role_id = ? WHERE name = ?', (role.id, house_name))
    conn.commit()
    await ctx.send(embed=embed("🏠 House Linked", f"**{house_name.capitalize()}** is now linked to {role.mention}.\nUse `!assign @user {house_name.capitalize()}` or `!assign @user {role.mention}` to add members.", color=0x57F287))
    await log_action(ctx, "🏠 House Created", f"{ctx.author.mention} linked **{house_name.capitalize()}** to {role.mention}.")


@bot.command()
@commands.has_permissions(administrator=True)
async def assign(ctx, member: discord.Member, target: Union[discord.Role, str]):
    """Assigns a player to a house using a house name or role mention."""
    house_name = None
    role = None

    if isinstance(target, discord.Role):
        role = target
        c.execute('SELECT name FROM houses WHERE role_id = ?', (role.id,))
        res = c.fetchone()
        if not res:
            return await ctx.send(embed=embed("❌ No House Found", f"{role.mention} isn't linked to any house. Use `!sethouse` first.", color=0xED4245))
        house_name = res[0]
    else:
        house_name = target.lower()
        c.execute('SELECT role_id FROM houses WHERE name = ?', (house_name,))
        res = c.fetchone()
        if not res:
            return await ctx.send(embed=embed("❌ House Not Found", f"**{target}** doesn't exist. Use `!sethouse` to create it first.", color=0xED4245))
        role_id = res[0]
        role = ctx.guild.get_role(role_id) if role_id else None

    c.execute('SELECT house, role_id, points FROM users WHERE user_id = ?', (member.id,))
    old = c.fetchone()

    if old and old[0] == house_name:
        return await ctx.send(embed=embed("⚠️ Already in House", f"{member.mention} is already in **{house_name.capitalize()}**.", color=0xFEE75C))

    if old and old[1]:
        old_role = ctx.guild.get_role(old[1])
        if old_role:
            try:
                await member.remove_roles(old_role)
            except discord.Forbidden:
                pass

    current_points = old[2] if old else 0
    c.execute('REPLACE INTO users (user_id, house, points, role_id) VALUES (?, ?, ?, ?)',
              (member.id, house_name, current_points, role.id if role else None))
    conn.commit()

    if role:
        try:
            await member.add_roles(role)
        except discord.Forbidden:
            await ctx.send(embed=embed("⚠️ Missing Permissions", "Couldn't assign the role — make sure the bot's role is above the house role.", color=0xFEE75C))

    action = "moved to" if old and old[0] else "placed in"
    await ctx.send(embed=embed("✅ Player Assigned", f"{member.mention} has been {action} **{house_name.capitalize()}**.", color=0x57F287))
    await log_action(ctx, "🏠 House Assignment", f"{ctx.author.mention} {action} {member.mention} → **{house_name.capitalize()}**.")


@bot.command()
@commands.has_permissions(administrator=True)
async def housepoints(ctx, action: str, member: discord.Member, amount: int):
    """Add or remove points from a member."""
    if action not in ["add", "remove"]:
        return await ctx.send(embed=embed("❌ Invalid Action", "Use `add` or `remove`.\nExample: `!housepoints add @user 10`", color=0xED4245))

    c.execute('SELECT house FROM users WHERE user_id = ?', (member.id,))
    result = c.fetchone()
    if not result:
        return await ctx.send(embed=embed("❌ No House", f"{member.mention} isn't in a house yet. Use `!assign` first.", color=0xED4245))

    house_name = result[0]
    modifier = amount if action == "add" else -amount

    c.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (modifier, member.id))
    c.execute('UPDATE houses SET points = points + ? WHERE name = ?', (modifier, house_name))
    conn.commit()

    if action == "add":
        e = embed("📈 Points Added", f"{member.mention} earned **+{amount}** points for **{house_name.capitalize()}**.", color=0x57F287)
    else:
        e = embed("📉 Points Removed", f"{member.mention} lost **{amount}** points from **{house_name.capitalize()}**.", color=0xED4245)

    await ctx.send(embed=e)
    await log_action(ctx, "💰 Points Changed", f"{ctx.author.mention} {'added' if action == 'add' else 'removed'} {amount} points {'to' if action == 'add' else 'from'} {member.mention} ({house_name.capitalize()}).")


@bot.command()
async def stats(ctx, member: discord.Member = None):
    """Check your own or another member's stats."""
    member = member or ctx.author
    c.execute('SELECT house, points FROM users WHERE user_id = ?', (member.id,))
    res = c.fetchone()
    if not res:
        return await ctx.send(embed=embed("❌ No Stats", f"{member.mention} isn't in any house yet.", color=0xED4245))

    e = embed(f"📊 {member.display_name}'s Stats",
              f"**House:** {res[0].capitalize()}\n**Points:** {res[1]:,}",
              color=0x5865F2)
    e.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=e)


@bot.command()
async def houseboard(ctx):
    """Shows the house leaderboard."""
    c.execute('SELECT name, points FROM houses ORDER BY points DESC')
    res = c.fetchall()
    if not res:
        return await ctx.send(embed=embed("🏆 House Leaderboard", "No houses have been set up yet.", color=0xFEE75C))

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, row in enumerate(res):
        prefix = medals[idx] if idx < 3 else f"**{idx+1}.**"
        lines.append(f"{prefix} **{row[0].capitalize()}** — {row[1]:,} points")

    await ctx.send(embed=embed("🏆 House Leaderboard", "\n".join(lines), color=0xFEE75C))


@bot.command()
async def mvp(ctx, house_name: str):
    """Find the top scorer in a house."""
    house_name = house_name.lower()
    c.execute('SELECT user_id, points FROM users WHERE house = ? ORDER BY points DESC LIMIT 1', (house_name,))
    res = c.fetchone()
    if not res:
        return await ctx.send(embed=embed("❌ No Results", f"**{house_name.capitalize()}** doesn't exist or has no members.", color=0xED4245))

    member = ctx.guild.get_member(res[0])
    name = member.display_name if member else f"Unknown User ({res[0]})"
    mention = member.mention if member else name

    e = embed(f"⭐ {house_name.capitalize()} MVP",
              f"{mention} is carrying **{house_name.capitalize()}** with **{res[1]:,}** points.",
              color=0xFEE75C)
    if member:
        e.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=e)


@bot.command()
@commands.has_permissions(administrator=True)
async def resetseason(ctx):
    """Resets all points for a new season."""
    c.execute('UPDATE users SET points = 0')
    c.execute('UPDATE houses SET points = 0')
    conn.commit()
    await ctx.send(embed=embed("🚨 Season Reset", "All points have been wiped. The grind starts fresh. Good luck.", color=0xED4245))
    await log_action(ctx, "☢️ Season Reset", f"{ctx.author.mention} reset all points for a new season.")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=embed("🔒 No Permission", "You need **Administrator** permissions to use that command.", color=0xED4245))
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send(embed=embed("❌ Member Not Found", "Couldn't find that member. Try mentioning them directly.", color=0xED4245))
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send(embed=embed("❌ Role Not Found", "Couldn't find that role. Try mentioning it directly.", color=0xED4245))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=embed("❌ Bad Argument", f"Something doesn't look right. Double-check your command.\n`{error}`", color=0xED4245))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=embed("❌ Missing Argument", f"You're missing a required argument: `{error.param.name}`", color=0xED4245))


bot.run(os.environ['DISCORD_TOKEN'])
