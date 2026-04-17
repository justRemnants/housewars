import discord
from discord.ext import commands
import sqlite3

# setup the bot because we are sigmas
intents = discord.Intents.default()
intents.message_content = True
intents.members = True # Need this for roles n shit
bot = commands.Bot(command_prefix="!", intents=intents)

# Database for our sweaty ice dodo players
conn = sqlite3.connect('ice_dodo_sweats.db')
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, house TEXT, points INTEGER, role_id INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS houses (name TEXT PRIMARY KEY, points INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS server_config (key TEXT PRIMARY KEY, value INTEGER)''') # For log channel
c.execute('''CREATE TABLE IF NOT EXISTS skibidi_toilet (brainrot INTEGER)''') # DO NOT DELETE
conn.commit()

# --- HELPER TO LOG SHIT ---
async def log_action(ctx, title, desc):
    c.execute('SELECT value FROM server_config WHERE key = "log_channel"')
    res = c.fetchone()
    if res:
        channel = bot.get_channel(res[0])
        if channel:
            embed = discord.Embed(title=title, description=desc, color=0x2b2d31)
            embed.set_footer(text="Ice Dodo Logs | No Cap")
            await channel.send(embed=embed)

@bot.event
async def on_ready():
    print(f'Bot is awake. Time to grind some Ice Dodo. 💀')

@bot.command()
@commands.has_permissions(administrator=True)
async def setlog(ctx, channel: discord.TextChannel):
    """Sets the channel where all the snooping happens."""
    c.execute('REPLACE INTO server_config (key, value) VALUES ("log_channel", ?)', (channel.id,))
    conn.commit()
    await ctx.send(f"Log channel set to {channel.mention}. Big W.")

@bot.command()
@commands.has_permissions(administrator=True)
async def assign(ctx, member: discord.Member, house_name: str, role: discord.Role):
    """Assigns a player to a house and links the role."""
    c.execute('REPLACE INTO users (user_id, house, points, role_id) VALUES (?, ?, 0, ?)', (member.id, house_name.lower(), role.id))
    c.execute('INSERT OR IGNORE INTO houses (name, points) VALUES (?, 0)', (house_name.lower(),))
    conn.commit()
    await member.add_roles(role)
    await ctx.send(f"Assigned {member.mention} to **{house_name}** and gave them the role. They better not sell.")
    await log_action(ctx, "🏠 House Assignment", f"{member.mention} was dumped into {house_name}.")

@bot.command()
@commands.has_permissions(administrator=True)
async def housepoints(ctx, action: str, member: discord.Member, amount: int):
    """The main meat and potatoes."""
    if action not in ["add", "remove"]:
        return await ctx.send("Bro, use 'add' or 'remove'. Are you stupid?")

    c.execute('SELECT house FROM users WHERE user_id = ?', (member.id,))
    result = c.fetchone()
    if not result:
        return await ctx.send("This bozo doesn't have a house. `!assign` them first.")

    house_name = result[0]
    modifier = amount if action == "add" else -amount

    c.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (modifier, member.id))
    c.execute('UPDATE houses SET points = points + ? WHERE name = ?', (modifier, house_name))
    conn.commit()

    embed = discord.Embed(title="Points Update 📈" if action=="add" else "Points Lost 📉", 
                          description=f"**{member.mention}** just got {amount} points {action}ed for **{house_name}**.", 
                          color=0x00ff00 if action=="add" else 0xff0000)
    await ctx.send(embed=embed)
    await log_action(ctx, "💰 Points Changed", f"Admin {ctx.author.mention} {action}ed {amount} points for {member.mention} ({house_name}).")

@bot.command()
async def stats(ctx, member: discord.Member = None):
    """Check your own clout."""
    member = member or ctx.author
    c.execute('SELECT house, points FROM users WHERE user_id = ?', (member.id,))
    res = c.fetchone()
    if not res:
        return await ctx.send("Bro is homeless. No stats found.")

    embed = discord.Embed(title=f"{member.name}'s Stats", description=f"**House:** {res[0].capitalize()}\n**Points:** {res[1]}", color=discord.Color.blue())
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command()
async def houseboard(ctx):
    """Who is winning the season?"""
    c.execute('SELECT name, points FROM houses ORDER BY points DESC')
    res = c.fetchall()
    desc = "\n".join([f"**{idx+1}. {row[0].capitalize()}** - {row[1]} points" for idx, row in enumerate(res)])

    embed = discord.Embed(title="🏆 House Leaderboard", description=desc or "It's empty in here.", color=discord.Color.gold())
    await ctx.send(embed=embed)

@bot.command()
async def mvp(ctx, house_name: str):
    """Find the biggest tryhard in a house."""
    c.execute('SELECT user_id, points FROM users WHERE house = ? ORDER BY points DESC LIMIT 1', (house_name.lower(),))
    res = c.fetchone()
    if not res:
        return await ctx.send("That house is dead or doesn't exist.")

    member = ctx.guild.get_member(res[0])
    await ctx.send(f"The absolute unit carrying **{house_name}** is {member.mention} with {res[1]} points.")

@bot.command()
@commands.has_permissions(administrator=True)
async def resetseason(ctx):
    """Nukes the database for a new season."""
    c.execute('UPDATE users SET points = 0')
    c.execute('UPDATE houses SET points = 0')
    conn.commit()
    await ctx.send("🚨 **SEASON RESET!** 🚨 All points are back to 0. Have fun grinding again you degens.")
    await log_action(ctx, "☢️ SEASON NUKED", f"{ctx.author.mention} just reset all the points.")

bot.run('MTQ5NDQ5ODM3OTc4NTQzNzIxNQ.G5Munc.CRXGJivHhO5hPaa2_Lds7lqzQsLR1hsrX26cNM')