from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import os, json, functools, secrets, requests as http_requests
import psycopg2
import psycopg2.extras

app = Flask(__name__, template_folder='../templates', static_folder='../static')
app.secret_key = os.environ.get('SESSION_SECRET', os.environ.get('DASHBOARD_PASSWORD', 'fallback') + '_session')

# ---------------------------------------------------------------------------
# Discord OAuth config
# Set these env vars:
#   DISCORD_CLIENT_ID      — your Discord app's client ID
#   DISCORD_CLIENT_SECRET  — your Discord app's client secret
#   DISCORD_REDIRECT_URI   — e.g. https://yourdomain.com/auth/discord/callback
#   DISCORD_GUILD_ID       — your server's Guild ID (for guild-data endpoint)
# ---------------------------------------------------------------------------
DISCORD_CLIENT_ID     = os.environ.get('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI  = os.environ.get('DISCORD_REDIRECT_URI', '')
DISCORD_GUILD_ID      = os.environ.get('DISCORD_GUILD_ID', '')
DISCORD_BOT_TOKEN     = os.environ.get('DISCORD_TOKEN', '')  # reuse bot token for guild lookups

DISCORD_API = 'https://discord.com/api/v10'

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db():
    conn = psycopg2.connect(os.environ['SUPABASE_URL'], cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def _discord_oauth_url():
    if not DISCORD_CLIENT_ID or not DISCORD_REDIRECT_URI:
        return None
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    params = (
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify"
        f"&state={state}"
    )
    return f"https://discord.com/api/oauth2/authorize{params}"

# ---------------------------------------------------------------------------
# Discord guild-data helper (uses bot token)
# ---------------------------------------------------------------------------
def _discord_bot_headers():
    return {'Authorization': f'Bot {DISCORD_BOT_TOKEN}'}

def _get_guild_data():
    """Fetch members, channels, roles from Discord using the bot token."""
    if not DISCORD_GUILD_ID or not DISCORD_BOT_TOKEN:
        return {'members': [], 'channels': [], 'roles': []}

    headers = _discord_bot_headers()
    guild_id = DISCORD_GUILD_ID

    # Roles
    try:
        r = http_requests.get(f'{DISCORD_API}/guilds/{guild_id}/roles', headers=headers, timeout=5)
        roles = r.json() if r.ok else []
        # Sort by position desc (highest role first)
        roles = sorted([ro for ro in roles if isinstance(ro, dict)], key=lambda x: x.get('position', 0), reverse=True)
    except Exception:
        roles = []

    # Channels
    try:
        r = http_requests.get(f'{DISCORD_API}/guilds/{guild_id}/channels', headers=headers, timeout=5)
        raw_channels = r.json() if r.ok else []
        # Build category map
        cats = {c['id']: c['name'] for c in raw_channels if isinstance(c, dict) and c.get('type') == 4}
        channels = []
        for c in raw_channels:
            if not isinstance(c, dict):
                continue
            if c.get('type') in (0, 2, 15):  # text, voice, forum
                channels.append({
                    'id': c['id'],
                    'name': c['name'],
                    'type': c.get('type', 0),
                    'position': c.get('position', 0),
                    'category': cats.get(str(c.get('parent_id', '')), 'No Category'),
                    'parent_id': c.get('parent_id'),
                })
        # Sort by category then position
        channels.sort(key=lambda x: (x['category'] or 'zzz', x['position']))
    except Exception:
        channels = []

    # Members (up to 1000)
    try:
        r = http_requests.get(
            f'{DISCORD_API}/guilds/{guild_id}/members?limit=1000',
            headers=headers, timeout=10
        )
        raw_members = r.json() if r.ok else []
        members = []
        for m in raw_members:
            if not isinstance(m, dict):
                continue
            u = m.get('user', {})
            if u.get('bot'):
                continue
            members.append({
                'id': u.get('id'),
                'username': u.get('username'),
                'display_name': m.get('nick') or u.get('global_name') or u.get('username'),
                'avatar': u.get('avatar'),
                'roles': m.get('roles', []),
            })
        members.sort(key=lambda x: (x.get('display_name') or '').lower())
    except Exception:
        members = []

    return {'members': members, 'channels': channels, 'roles': roles}

# ---------------------------------------------------------------------------
# Discord user resolution (for logs display)
# ---------------------------------------------------------------------------
_user_cache = {}

def _resolve_discord_user(user_id):
    """Fetch basic info about a Discord user by ID."""
    if user_id in _user_cache:
        return _user_cache[user_id]
    if not DISCORD_BOT_TOKEN:
        return None
    try:
        r = http_requests.get(f'{DISCORD_API}/users/{user_id}', headers=_discord_bot_headers(), timeout=3)
        if r.ok:
            data = r.json()
            _user_cache[user_id] = data
            return data
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == os.environ.get('DASHBOARD_PASSWORD', ''):
            session['logged_in'] = True
            session.pop('discord_user', None)
            return redirect(url_for('index'))
        error = 'Incorrect password.'
    return render_template('login.html', error=error, discord_oauth_url=_discord_oauth_url())

@app.route('/auth/discord')
def auth_discord():
    url = _discord_oauth_url()
    if not url:
        return redirect(url_for('login'))
    return redirect(url)

@app.route('/auth/discord/callback')
def auth_discord_callback():
    code  = request.args.get('code')
    state = request.args.get('state')

    if not code or state != session.pop('oauth_state', None):
        return redirect(url_for('login'))

    # Exchange code for token
    try:
        token_resp = http_requests.post(
            f'{DISCORD_API}/oauth2/token',
            data={
                'client_id':     DISCORD_CLIENT_ID,
                'client_secret': DISCORD_CLIENT_SECRET,
                'grant_type':    'authorization_code',
                'code':          code,
                'redirect_uri':  DISCORD_REDIRECT_URI,
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=10,
        )
        if not token_resp.ok:
            return redirect(url_for('login'))

        access_token = token_resp.json().get('access_token')
        user_resp = http_requests.get(
            f'{DISCORD_API}/users/@me',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=5,
        )
        if not user_resp.ok:
            return redirect(url_for('login'))

        discord_user = user_resp.json()
        session['logged_in'] = True
        session['discord_user'] = {
            'id':       discord_user.get('id'),
            'username': discord_user.get('username'),
            'avatar':   discord_user.get('avatar'),
        }
        return redirect(url_for('index'))
    except Exception:
        return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ---------------------------------------------------------------------------
# Guild data API (used by frontend dropdowns)
# ---------------------------------------------------------------------------
@app.route('/api/guild-data')
@login_required
def api_guild_data():
    data = _get_guild_data()
    return jsonify(data)

# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route('/')
@login_required
def index():
    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT name, house_points AS points, role_id, color FROM houses ORDER BY house_points DESC')
    houses = cur.fetchall()
    cur.execute('SELECT user_id, house_id AS house, contributions_points AS points FROM users ORDER BY contributions_points DESC')
    users = cur.fetchall()
    conn.close()
    return render_template('index.html', houses=houses, users=users)

@app.route('/houses')
@login_required
def houses():
    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT name, house_points AS points, role_id, color, thumbnail_url FROM houses ORDER BY house_points DESC')
    houses = cur.fetchall()
    conn.close()
    return render_template('houses.html', houses=houses)

@app.route('/members')
@login_required
def members():
    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT user_id, house_id AS house, contributions_points AS points FROM users ORDER BY contributions_points DESC')
    users_raw = cur.fetchall()
    cur.execute('SELECT name FROM houses ORDER BY name')
    houses = cur.fetchall()
    conn.close()

    # Enrich with Discord data from guild members
    guild_data = _get_guild_data()
    member_map = {m['id']: m for m in guild_data.get('members', [])}
    role_map   = {r['id']: r for r in guild_data.get('roles', [])}

    users = []
    for u in users_raw:
        uid = str(u['user_id'])
        dm  = member_map.get(uid, {})
        # Build role list with colours
        discord_roles = []
        for rid in dm.get('roles', []):
            ro = role_map.get(rid)
            if ro and ro.get('name') != '@everyone':
                color_int = ro.get('color', 0)
                color_hex = format(color_int, '06x') if color_int else None
                # Convert int to r,g,b for rgba usage
                if color_int:
                    r_val = (color_int >> 16) & 0xFF
                    g_val = (color_int >> 8)  & 0xFF
                    b_val =  color_int         & 0xFF
                    color_rgb = f'{r_val},{g_val},{b_val}'
                else:
                    color_rgb = None
                discord_roles.append({
                    'name':      ro['name'],
                    'color_hex': color_hex,
                    'color_rgb': color_rgb,
                })
        users.append({
            'user_id':       uid,
            'house':         u['house'],
            'points':        u['points'],
            'username':      dm.get('username'),
            'display_name':  dm.get('display_name'),
            'avatar':        dm.get('avatar'),
            'discord_roles': discord_roles,
        })

    return render_template('members.html', users=users, houses=houses)

@app.route('/logs')
@login_required
def logs_page():
    conn = db()
    cur = conn.cursor()
    cur.execute('''
        SELECT l.id, l.user_id, l.amount, l.reason, l.created_at,
               l.action, l.house_id, l.actor_id,
               l.target_username, l.target_avatar,
               l.actor_username, l.actor_avatar
        FROM logs l
        ORDER BY l.created_at DESC
        LIMIT 500
    ''')
    logs = cur.fetchall()
    cur.execute('SELECT name FROM houses ORDER BY name')
    houses = cur.fetchall()
    conn.close()
    return render_template('logs.html', logs=logs, houses=houses)

@app.route('/settings')
@login_required
def settings():
    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT key, value FROM server_config')
    rows = cur.fetchall()
    conn.close()
    cfg = {r['key']: r['value'] for r in rows}
    return render_template('settings.html', cfg=cfg)

# ---------------------------------------------------------------------------
# Houses API
# ---------------------------------------------------------------------------
@app.route('/api/houses', methods=['GET'])
@login_required
def api_houses():
    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT name, house_points AS points, role_id, color, thumbnail_url FROM houses ORDER BY house_points DESC')
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/houses', methods=['POST'])
@login_required
def api_create_house():
    data = request.json
    name  = data.get('name', '').lower().strip()
    role_id       = data.get('role_id') or None
    color         = data.get('color', '5865F2').lstrip('#')
    thumbnail_url = data.get('thumbnail_url', '')
    if not name:
        return jsonify({'error': 'Name required'}), 400
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        '''INSERT INTO houses (name, house_points, role_id, color, thumbnail_url)
           VALUES (%s, 0, %s, %s, %s)
           ON CONFLICT (name) DO UPDATE SET role_id=%s, color=%s, thumbnail_url=%s''',
        (name, role_id, color, thumbnail_url, role_id, color, thumbnail_url)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/houses/<hname>', methods=['PATCH'])
@login_required
def api_update_house(hname):
    data = request.json
    conn = db()
    cur  = conn.cursor()
    if 'color' in data:
        cur.execute('UPDATE houses SET color=%s WHERE name=%s', (data['color'].lstrip('#'), hname.lower()))
    if 'thumbnail_url' in data:
        cur.execute('UPDATE houses SET thumbnail_url=%s WHERE name=%s', (data['thumbnail_url'], hname.lower()))
    if 'role_id' in data:
        cur.execute('UPDATE houses SET role_id=%s WHERE name=%s', (data['role_id'] or None, hname.lower()))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/houses/<hname>', methods=['DELETE'])
@login_required
def api_delete_house(hname):
    conn = db()
    cur  = conn.cursor()
    cur.execute('DELETE FROM houses WHERE name=%s', (hname.lower(),))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/houses/<hname>/points', methods=['POST'])
@login_required
def api_house_points(hname):
    data   = request.json
    action = data.get('action')
    amount = int(data.get('amount', 0))
    reason = data.get('reason', '') or ''
    if action not in ['add', 'remove'] or amount <= 0:
        return jsonify({'error': 'Invalid'}), 400
    modifier   = amount if action == 'add' else -amount
    actor_id   = session.get('discord_user', {}).get('id') if session.get('discord_user') else None
    actor_name = session.get('discord_user', {}).get('username') if session.get('discord_user') else 'Dashboard'
    actor_av   = session.get('discord_user', {}).get('avatar') if session.get('discord_user') else None

    conn = db()
    cur  = conn.cursor()
    cur.execute('UPDATE houses SET house_points = house_points + %s WHERE name=%s', (modifier, hname.lower()))

    # Get all members of the house and update + log each
    cur.execute('SELECT user_id FROM users WHERE house_id=%s', (hname.lower(),))
    members = cur.fetchall()
    for m in members:
        cur.execute('UPDATE users SET contributions_points = contributions_points + %s WHERE user_id=%s', (modifier, m['user_id']))
        _write_log(cur, m['user_id'], None, amount, reason, action, hname.lower(), actor_id, actor_name, actor_av)

    conn.commit()
    cur.execute('SELECT house_points AS points FROM houses WHERE name=%s', (hname.lower(),))
    pts = cur.fetchone()
    conn.close()

    # Post to log channel via pending message
    _queue_log_embed(hname, None, amount, reason, action, actor_name)

    return jsonify({'success': True, 'points': pts['points'] if pts else 0})

@app.route('/api/houses/<hname>/reset', methods=['POST'])
@login_required
def api_reset_house(hname):
    conn = db()
    cur  = conn.cursor()
    cur.execute('UPDATE houses SET house_points=0 WHERE name=%s', (hname.lower(),))
    cur.execute('UPDATE users SET contributions_points=0 WHERE house_id=%s', (hname.lower(),))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/season/reset', methods=['POST'])
@login_required
def api_reset_season():
    conn = db()
    cur  = conn.cursor()
    cur.execute('UPDATE users SET contributions_points=0')
    cur.execute('UPDATE houses SET house_points=0')
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ---------------------------------------------------------------------------
# Members API
# ---------------------------------------------------------------------------
@app.route('/api/members/<user_id>/points', methods=['POST'])
@login_required
def api_member_points(user_id):
    data   = request.json
    action = data.get('action')
    amount = int(data.get('amount', 0))
    reason = data.get('reason', '') or ''
    if action not in ['add', 'remove'] or amount <= 0:
        return jsonify({'error': 'Invalid'}), 400
    modifier   = amount if action == 'add' else -amount
    actor_id   = session.get('discord_user', {}).get('id') if session.get('discord_user') else None
    actor_name = session.get('discord_user', {}).get('username') if session.get('discord_user') else 'Dashboard'
    actor_av   = session.get('discord_user', {}).get('avatar') if session.get('discord_user') else None

    conn = db()
    cur  = conn.cursor()
    cur.execute('SELECT house_id FROM users WHERE user_id=%s', (str(user_id),))
    user = cur.fetchone()
    if not user:
        conn.close()
        return jsonify({'error': 'Not found'}), 404

    house_id = user['house_id']
    cur.execute('UPDATE users SET contributions_points = contributions_points + %s WHERE user_id=%s', (modifier, str(user_id)))
    cur.execute('UPDATE houses SET house_points = house_points + %s WHERE name=%s', (modifier, house_id))

    # Resolve Discord username for log
    target_username = None
    target_avatar   = None
    guild_data = _get_guild_data()
    dm = next((m for m in guild_data.get('members', []) if m['id'] == str(user_id)), None)
    if dm:
        target_username = dm.get('display_name') or dm.get('username')
        target_avatar   = dm.get('avatar')

    _write_log(cur, str(user_id), target_username, amount, reason, action, house_id, actor_id, actor_name, actor_av, target_avatar)
    conn.commit()

    cur.execute('SELECT contributions_points AS points FROM users WHERE user_id=%s', (str(user_id),))
    pts = cur.fetchone()
    conn.close()

    # Post to log channel
    _queue_log_embed(house_id, target_username or user_id, amount, reason, action, actor_name)

    return jsonify({'success': True, 'points': pts['points']})

@app.route('/api/members/assign', methods=['POST'])
@login_required
def api_assign_member():
    data       = request.json
    user_id    = str(data.get('user_id', '')).strip()
    house_name = data.get('house_name', '').lower().strip()
    if not user_id or not house_name:
        return jsonify({'error': 'user_id and house_name required'}), 400
    conn = db()
    cur  = conn.cursor()
    cur.execute('SELECT role_id FROM houses WHERE name=%s', (house_name,))
    house = cur.fetchone()
    if not house:
        conn.close()
        return jsonify({'error': 'House not found'}), 404
    cur.execute('SELECT house_id FROM users WHERE user_id=%s', (user_id,))
    old = cur.fetchone()
    old_role_id = None
    if old:
        cur.execute('SELECT role_id FROM houses WHERE name=%s', (old['house_id'],))
        old_house = cur.fetchone()
        old_role_id = old_house['role_id'] if old_house else None
    cur.execute(
        '''INSERT INTO users (user_id, house_id, contributions_points, role_id)
           VALUES (%s, %s, 0, %s)
           ON CONFLICT (user_id) DO UPDATE SET house_id=%s, role_id=%s''',
        (user_id, house_name, house['role_id'], house_name, house['role_id'])
    )
    cur.execute(
        "INSERT INTO pending_actions (action_type, user_id, house_name, old_role_id) VALUES ('assign', %s, %s, %s)",
        (user_id, house_name, old_role_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ---------------------------------------------------------------------------
# Logs helpers
# ---------------------------------------------------------------------------
def _write_log(cur, user_id, target_username, amount, reason, action, house_id,
               actor_id, actor_name, actor_av=None, target_avatar=None):
    try:
        cur.execute(
            '''INSERT INTO logs (user_id, target_username, target_avatar, amount, reason,
                                 action, house_id, actor_id, actor_username, actor_avatar)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
            (str(user_id), target_username, target_avatar, amount, reason,
             action, house_id, str(actor_id) if actor_id else None,
             actor_name, actor_av)
        )
    except Exception as e:
        print(f'Log write error: {e}')

def _queue_log_embed(house_or_target, member_name, amount, reason, action, actor_name):
    """Queue a rich log embed to the log channel."""
    try:
        conn = db()
        cur  = conn.cursor()
        cur.execute('SELECT value FROM server_config WHERE key=%s', ('log_channel',))
        row = cur.fetchone()
        if not row:
            conn.close()
            return
        channel_id = int(row['value'])
        emoji  = '📈' if action == 'add' else '📉'
        sign   = '+' if action == 'add' else '-'
        color  = '57f287' if action == 'add' else 'ed4245'
        target_display = member_name or house_or_target or 'Unknown'
        desc_lines = [
            f"**Member:** {target_display}",
            f"**House:** {(house_or_target or '—').capitalize()}",
            f"**Points:** {sign}{amount}",
        ]
        if reason:
            desc_lines.append(f"**Reason:** {reason}")
        desc_lines.append(f"**By:** {actor_name or 'Dashboard'}")
        embed_data = {
            'title':       f'{emoji} Points {"Added" if action == "add" else "Removed"}',
            'description': '\n'.join(desc_lines),
            'color':       color,
            'footer_text': 'Ice Dodo Points Log',
            'footer_icon': '',
            'image_url':   '',
            'thumbnail_url': '',
        }
        cur.execute(
            'INSERT INTO pending_messages (channel_id, embed_json, button_label, button_url) VALUES (%s, %s, %s, %s)',
            (channel_id, json.dumps(embed_data), '', '')
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'Queue log embed error: {e}')

# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------
@app.route('/api/settings', methods=['GET'])
@login_required
def api_get_settings():
    conn = db()
    cur  = conn.cursor()
    cur.execute('SELECT key, value FROM server_config')
    rows = cur.fetchall()
    conn.close()
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['POST'])
@login_required
def api_save_settings():
    data    = request.json
    allowed = ['embed_color', 'embed_footer_text', 'embed_footer_icon', 'embed_thumbnail',
               'embed_author_name', 'embed_author_icon', 'prefix', 'xp_enabled',
               'xp_per_msgs', 'xp_amount', 'log_channel']
    conn = db()
    cur  = conn.cursor()
    for k in allowed:
        if k in data:
            cur.execute(
                'INSERT INTO server_config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=%s',
                (k, str(data[k]), str(data[k]))
            )
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ---------------------------------------------------------------------------
# Sticky / Messages API (kept for bot compatibility, no UI page)
# ---------------------------------------------------------------------------
@app.route('/api/sticky', methods=['GET'])
@login_required
def api_get_stickies():
    conn = db()
    cur  = conn.cursor()
    cur.execute('SELECT * FROM sticky_messages ORDER BY id DESC')
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/sticky', methods=['POST'])
@login_required
def api_create_sticky():
    data       = request.json
    channel_id = int(data.get('channel_id', 0))
    if not channel_id:
        return jsonify({'error': 'channel_id required'}), 400
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        '''INSERT INTO sticky_messages
           (channel_id, title, description, color, image_url, thumbnail_url,
            footer_text, footer_icon, button_label, button_url, active)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)''',
        (channel_id, data.get('title',''), data.get('description',''),
         data.get('color','5865F2').lstrip('#'),
         data.get('image_url',''), data.get('thumbnail_url',''),
         data.get('footer_text',''), data.get('footer_icon',''),
         data.get('button_label',''), data.get('button_url',''))
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/sticky/<int:sticky_id>', methods=['DELETE'])
@login_required
def api_delete_sticky(sticky_id):
    conn = db()
    cur  = conn.cursor()
    cur.execute('DELETE FROM sticky_messages WHERE id=%s', (sticky_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/sticky/<int:sticky_id>/toggle', methods=['POST'])
@login_required
def api_toggle_sticky(sticky_id):
    conn = db()
    cur  = conn.cursor()
    cur.execute('UPDATE sticky_messages SET active = NOT active WHERE id=%s', (sticky_id,))
    conn.commit()
    cur.execute('SELECT active FROM sticky_messages WHERE id=%s', (sticky_id,))
    row = cur.fetchone()
    conn.close()
    return jsonify({'success': True, 'active': row['active'] if row else False})

@app.route('/api/send-message', methods=['POST'])
@login_required
def api_send_message():
    data       = request.json
    channel_id = int(data.get('channel_id', 0))
    if not channel_id:
        return jsonify({'error': 'channel_id required'}), 400
    embed_data = {
        'title':         data.get('title', ''),
        'description':   data.get('description', ''),
        'color':         data.get('color', '5865F2').lstrip('#'),
        'image_url':     data.get('image_url', ''),
        'thumbnail_url': data.get('thumbnail_url', ''),
        'footer_text':   data.get('footer_text', ''),
        'footer_icon':   data.get('footer_icon', ''),
        'author_name':   data.get('author_name', ''),
        'author_icon':   data.get('author_icon', ''),
    }
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        'INSERT INTO pending_messages (channel_id, embed_json, button_label, button_url) VALUES (%s, %s, %s, %s)',
        (channel_id, json.dumps(embed_data), data.get('button_label',''), data.get('button_url',''))
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ---------------------------------------------------------------------------
# Message Templates API
# ---------------------------------------------------------------------------
@app.route('/api/templates', methods=['GET'])
@login_required
def api_get_templates():
    conn = db()
    cur  = conn.cursor()
    cur.execute('SELECT * FROM message_templates ORDER BY created_at DESC')
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/templates', methods=['POST'])
@login_required
def api_save_template():
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Template name required'}), 400
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        '''INSERT INTO message_templates
           (name, title, description, color, image_url, thumbnail_url,
            footer_text, footer_icon, button_label, button_url)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
        (name, data.get('title',''), data.get('description',''),
         data.get('color','5865F2').lstrip('#'),
         data.get('image_url',''), data.get('thumbnail_url',''),
         data.get('footer_text',''), data.get('footer_icon',''),
         data.get('button_label',''), data.get('button_url',''))
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/templates/<int:template_id>', methods=['DELETE'])
@login_required
def api_delete_template(template_id):
    conn = db()
    cur  = conn.cursor()
    cur.execute('DELETE FROM message_templates WHERE id=%s', (template_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
