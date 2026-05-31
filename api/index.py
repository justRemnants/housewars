from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import os, json, functools, secrets, requests as http_requests
import psycopg2, psycopg2.extras
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

app = Flask(__name__, template_folder='../templates', static_folder='../static')

# SESSION_SECRET must be a stable random string set as a Vercel env var.
# If it rotates between deployments the cookie is invalidated and everyone
# gets logged out — that is fine, but it must NOT change within a deploy.
app.secret_key = os.environ.get('SESSION_SECRET', 'CHANGE_ME_IN_ENV')

# SameSite=Lax works for same-domain redirects (housewars.vercel.app → discord → housewars.vercel.app).
# SameSite=None would need Secure=True on every response, which Vercel handles,
# but Lax is simpler and correct here since there is no cross-site iframe use.
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE']   = True
app.config['SESSION_COOKIE_HTTPONLY'] = True

DISCORD_CLIENT_ID     = os.environ.get('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI  = os.environ.get('DISCORD_REDIRECT_URI', '')  # https://housewars.vercel.app/auth/discord/callback
DISCORD_GUILD_ID      = os.environ.get('DISCORD_GUILD_ID', '')
DISCORD_BOT_TOKEN     = os.environ.get('DISCORD_TOKEN', '')

DISCORD_API = 'https://discord.com/api/v10'

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def db():
    conn = psycopg2.connect(os.environ['SUPABASE_URL'], cursor_factory=psycopg2.extras.RealDictCursor)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn

# ---------------------------------------------------------------------------
# OAuth nonce helpers  (stored in DB so Vercel serverless instances share state)
# ---------------------------------------------------------------------------
def _ensure_nonce_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS oauth_nonces (
            key        TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)

def _make_nonce():
    nonce = secrets.token_urlsafe(24)
    try:
        conn = db()
        cur  = conn.cursor()
        _ensure_nonce_table(cur)
        cur.execute("DELETE FROM oauth_nonces WHERE created_at < now() - interval '10 minutes'")
        cur.execute("INSERT INTO oauth_nonces (key) VALUES (%s) ON CONFLICT DO NOTHING", (nonce,))
        conn.close()
    except Exception as e:
        print(f'_make_nonce error: {e}')
        nonce = 'stateless'   # fall back to no CSRF check rather than breaking login
    return nonce

def _consume_nonce(state):
    """Returns True if the nonce is valid (and deletes it). Always returns True for 'stateless'."""
    if not state or state == 'stateless':
        return True
    try:
        conn = db()
        cur  = conn.cursor()
        _ensure_nonce_table(cur)
        cur.execute(
            "DELETE FROM oauth_nonces WHERE key=%s AND created_at > now() - interval '10 minutes' RETURNING key",
            (state,)
        )
        row = cur.fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        print(f'_consume_nonce error: {e}')
        return True   # allow through on DB error rather than locking everyone out

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
    nonce  = _make_nonce()
    params = (
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify%20guilds"
        f"&state={nonce}"
    )
    return f"https://discord.com/api/oauth2/authorize{params}"

# ---------------------------------------------------------------------------
# Discord guild helpers
# ---------------------------------------------------------------------------
def _bot_headers():
    return {'Authorization': f'Bot {DISCORD_BOT_TOKEN}'}

def _get_guild_data():
    if not DISCORD_GUILD_ID or not DISCORD_BOT_TOKEN:
        return {'members': [], 'channels': [], 'roles': []}
    h  = _bot_headers()
    gid = DISCORD_GUILD_ID

    try:
        r     = http_requests.get(f'{DISCORD_API}/guilds/{gid}/roles', headers=h, timeout=5)
        roles = sorted([ro for ro in (r.json() if r.ok else []) if isinstance(ro, dict)],
                       key=lambda x: x.get('position', 0), reverse=True)
    except Exception:
        roles = []

    try:
        r    = http_requests.get(f'{DISCORD_API}/guilds/{gid}/channels', headers=h, timeout=5)
        raw  = r.json() if r.ok else []
        cats = {c['id']: c['name'] for c in raw if isinstance(c, dict) and c.get('type') == 4}
        channels = []
        for c in raw:
            if isinstance(c, dict) and c.get('type') in (0, 2, 5, 15):
                channels.append({'id': c['id'], 'name': c['name'], 'type': c.get('type', 0),
                                  'position': c.get('position', 0),
                                  'category': cats.get(str(c.get('parent_id', '')), 'No Category')})
        channels.sort(key=lambda x: (x['category'] or 'zzz', x['position']))
    except Exception:
        channels = []

    try:
        r       = http_requests.get(f'{DISCORD_API}/guilds/{gid}/members?limit=1000', headers=h, timeout=10)
        members = []
        for m in (r.json() if r.ok else []):
            if not isinstance(m, dict): continue
            u = m.get('user', {})
            if u.get('bot'): continue
            members.append({'id': u.get('id'), 'username': u.get('username'),
                             'display_name': m.get('nick') or u.get('global_name') or u.get('username'),
                             'avatar': u.get('avatar'), 'roles': m.get('roles', [])})
        members.sort(key=lambda x: (x.get('display_name') or '').lower())
    except Exception:
        members = []

    return {'members': members, 'channels': channels, 'roles': roles}

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
            session.modified = True
            return redirect(url_for('index'))
        error = 'Incorrect password.'
    return render_template('login.html', error=error, discord_oauth_url=_discord_oauth_url())

@app.route('/auth/discord')
def auth_discord():
    url = _discord_oauth_url()
    return redirect(url) if url else redirect(url_for('login'))

@app.route('/auth/discord/callback')
def auth_discord_callback():
    code  = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')

    if error or not code:
        print(f'Discord OAuth denied or missing code: error={error}')
        return redirect(url_for('login'))

    if not _consume_nonce(state):
        print(f'Invalid/expired OAuth nonce: {state}')
        return redirect(url_for('login'))

    try:
        token_resp = http_requests.post(
            f'{DISCORD_API}/oauth2/token',
            data={'client_id': DISCORD_CLIENT_ID, 'client_secret': DISCORD_CLIENT_SECRET,
                  'grant_type': 'authorization_code', 'code': code, 'redirect_uri': DISCORD_REDIRECT_URI},
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=10,
        )
        if not token_resp.ok:
            print(f'Token exchange failed: {token_resp.status_code} {token_resp.text}')
            return redirect(url_for('login'))

        access_token = token_resp.json().get('access_token')
        if not access_token:
            print(f'No access_token in response: {token_resp.json()}')
            return redirect(url_for('login'))

        user_resp = http_requests.get(f'{DISCORD_API}/users/@me',
                                      headers={'Authorization': f'Bearer {access_token}'}, timeout=5)
        if not user_resp.ok:
            print(f'User fetch failed: {user_resp.status_code}')
            return redirect(url_for('login'))

        discord_user = user_resp.json()

        # Verify they're in the right guild
        if DISCORD_GUILD_ID:
            gr = http_requests.get(f'{DISCORD_API}/users/@me/guilds',
                                   headers={'Authorization': f'Bearer {access_token}'}, timeout=5)
            if gr.ok:
                ids = [g['id'] for g in gr.json() if isinstance(g, dict)]
                if DISCORD_GUILD_ID not in ids:
                    return render_template('login.html',
                        error='You must be a member of the server to use this dashboard.',
                        discord_oauth_url=_discord_oauth_url())

        session['logged_in']    = True
        session['discord_user'] = {'id': discord_user.get('id'),
                                    'username': discord_user.get('username'),
                                    'avatar': discord_user.get('avatar')}
        session.modified = True
        return redirect(url_for('index'))

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f'OAuth callback exception: {e}')
        return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ---------------------------------------------------------------------------
# Guild data API
# ---------------------------------------------------------------------------
@app.route('/api/guild-data')
@login_required
def api_guild_data():
    return jsonify(_get_guild_data())

# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route('/')
@login_required
def index():
    conn = db(); cur = conn.cursor()
    cur.execute('SELECT name, house_points AS points, role_id, color FROM houses ORDER BY house_points DESC')
    houses = cur.fetchall()
    cur.execute('SELECT user_id, house_id AS house, contributions_points AS points FROM users ORDER BY contributions_points DESC')
    users = cur.fetchall()
    conn.close()
    return render_template('index.html', houses=houses, users=users)

@app.route('/houses')
@login_required
def houses():
    conn = db(); cur = conn.cursor()
    cur.execute('SELECT name, house_points AS points, role_id, color, thumbnail_url FROM houses ORDER BY house_points DESC')
    houses = cur.fetchall(); conn.close()
    return render_template('houses.html', houses=houses)

@app.route('/members')
@login_required
def members():
    conn = db(); cur = conn.cursor()
    cur.execute('SELECT user_id, house_id AS house, contributions_points AS points FROM users ORDER BY contributions_points DESC')
    users_raw = cur.fetchall()
    cur.execute('SELECT name FROM houses ORDER BY name')
    houses = cur.fetchall(); conn.close()

    gd         = _get_guild_data()
    member_map = {m['id']: m for m in gd.get('members', [])}
    role_map   = {r['id']: r for r in gd.get('roles', [])}

    users = []
    for u in users_raw:
        uid = str(u['user_id'])
        dm  = member_map.get(uid, {})
        discord_roles = []
        for rid in dm.get('roles', []):
            ro = role_map.get(rid)
            if ro and ro.get('name') != '@everyone':
                ci = ro.get('color', 0)
                discord_roles.append({'name': ro['name'],
                                       'color_hex': format(ci,'06x') if ci else None,
                                       'color_rgb': f'{(ci>>16)&0xFF},{(ci>>8)&0xFF},{ci&0xFF}' if ci else None})
        users.append({'user_id': uid, 'house': u['house'], 'points': u['points'],
                       'username': dm.get('username'), 'display_name': dm.get('display_name'),
                       'avatar': dm.get('avatar'), 'discord_roles': discord_roles})
    return render_template('members.html', users=users, houses=houses)

@app.route('/logs')
@login_required
def logs_page():
    conn = db(); cur = conn.cursor()
    cur.execute('''SELECT id, user_id, amount, reason, created_at, action, house_id, actor_id,
                          target_username, target_avatar, actor_username, actor_avatar
                   FROM logs ORDER BY created_at DESC LIMIT 500''')
    logs = cur.fetchall()
    cur.execute('SELECT name FROM houses ORDER BY name')
    houses = cur.fetchall(); conn.close()
    return render_template('logs.html', logs=logs, houses=houses)

@app.route('/settings')
@login_required
def settings():
    conn = db(); cur = conn.cursor()
    cur.execute('SELECT key, value FROM server_config')
    rows = cur.fetchall(); conn.close()
    return render_template('settings.html', cfg={r['key']: r['value'] for r in rows})

@app.route('/messages')
@login_required
def messages_page():
    conn = db(); cur = conn.cursor()
    cur.execute('SELECT * FROM sticky_messages ORDER BY id DESC')
    stickies = cur.fetchall()
    cur.execute('SELECT * FROM message_templates ORDER BY created_at DESC')
    templates = cur.fetchall(); conn.close()
    return render_template('messages.html', stickies=stickies, templates=templates)

# ---------------------------------------------------------------------------
# Houses API
# ---------------------------------------------------------------------------
@app.route('/api/houses', methods=['GET'])
@login_required
def api_houses():
    conn = db(); cur = conn.cursor()
    cur.execute('SELECT name, house_points AS points, role_id, color, thumbnail_url FROM houses ORDER BY house_points DESC')
    rows = cur.fetchall(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/houses', methods=['POST'])
@login_required
def api_create_house():
    data = request.json
    name = data.get('name','').lower().strip()
    if not name: return jsonify({'error': 'Name required'}), 400
    rid   = data.get('role_id') or None
    color = data.get('color','5865F2').lstrip('#')
    thumb = data.get('thumbnail_url','')
    conn  = db(); cur = conn.cursor()
    cur.execute('INSERT INTO houses (name,house_points,role_id,color,thumbnail_url) VALUES (%s,0,%s,%s,%s) ON CONFLICT (name) DO UPDATE SET role_id=%s,color=%s,thumbnail_url=%s',
                (name,rid,color,thumb,rid,color,thumb))
    conn.close()
    return jsonify({'success': True})

@app.route('/api/houses/<hname>', methods=['PATCH'])
@login_required
def api_update_house(hname):
    data = request.json; conn = db(); cur = conn.cursor()
    if 'color'         in data: cur.execute('UPDATE houses SET color=%s WHERE name=%s',         (data['color'].lstrip('#'), hname.lower()))
    if 'thumbnail_url' in data: cur.execute('UPDATE houses SET thumbnail_url=%s WHERE name=%s', (data['thumbnail_url'],     hname.lower()))
    if 'role_id'       in data: cur.execute('UPDATE houses SET role_id=%s WHERE name=%s',       (data['role_id'] or None,   hname.lower()))
    conn.close()
    return jsonify({'success': True})

@app.route('/api/houses/<hname>', methods=['DELETE'])
@login_required
def api_delete_house(hname):
    conn = db(); cur = conn.cursor()
    cur.execute('DELETE FROM houses WHERE name=%s', (hname.lower(),)); conn.close()
    return jsonify({'success': True})

@app.route('/api/houses/<hname>/points', methods=['POST'])
@login_required
def api_house_points(hname):
    data   = request.json
    action = data.get('action')
    amount = int(data.get('amount', 0))
    reason = data.get('reason','') or ''
    if action not in ('add','remove') or amount <= 0: return jsonify({'error':'Invalid'}), 400
    mod        = amount if action == 'add' else -amount
    actor_name = (session.get('discord_user') or {}).get('username') or 'Dashboard'
    actor_id   = (session.get('discord_user') or {}).get('id')
    actor_av   = (session.get('discord_user') or {}).get('avatar')
    conn = db(); cur = conn.cursor()
    cur.execute('UPDATE houses SET house_points = house_points + %s WHERE name=%s', (mod, hname.lower()))
    cur.execute('SELECT user_id FROM users WHERE house_id=%s', (hname.lower(),))
    for m in cur.fetchall():
        cur.execute('UPDATE users SET contributions_points = contributions_points + %s WHERE user_id=%s', (mod, m['user_id']))
        _write_log(cur, m['user_id'], None, None, amount, reason, action, hname.lower(), actor_id, actor_name, actor_av)
    cur.execute('SELECT house_points AS points FROM houses WHERE name=%s', (hname.lower(),))
    pts = cur.fetchone(); conn.close()
    _queue_log_embed(hname, None, amount, reason, action, actor_name)
    return jsonify({'success': True, 'points': pts['points'] if pts else 0})

@app.route('/api/houses/<hname>/reset', methods=['POST'])
@login_required
def api_reset_house(hname):
    conn = db(); cur = conn.cursor()
    cur.execute('UPDATE houses SET house_points=0 WHERE name=%s', (hname.lower(),))
    cur.execute('UPDATE users SET contributions_points=0 WHERE house_id=%s', (hname.lower(),))
    conn.close(); return jsonify({'success': True})

@app.route('/api/season/reset', methods=['POST'])
@login_required
def api_reset_season():
    conn = db(); cur = conn.cursor()
    cur.execute('UPDATE users SET contributions_points=0')
    cur.execute('UPDATE houses SET house_points=0')
    conn.close(); return jsonify({'success': True})

# ---------------------------------------------------------------------------
# Members API
# ---------------------------------------------------------------------------
@app.route('/api/members/<user_id>/points', methods=['POST'])
@login_required
def api_member_points(user_id):
    data   = request.json
    action = data.get('action')
    amount = int(data.get('amount', 0))
    reason = data.get('reason','') or ''
    if action not in ('add','remove') or amount <= 0: return jsonify({'error':'Invalid'}), 400
    mod        = amount if action == 'add' else -amount
    actor_name = (session.get('discord_user') or {}).get('username') or 'Dashboard'
    actor_id   = (session.get('discord_user') or {}).get('id')
    actor_av   = (session.get('discord_user') or {}).get('avatar')
    conn = db(); cur = conn.cursor()
    cur.execute('SELECT house_id FROM users WHERE user_id=%s', (str(user_id),))
    user = cur.fetchone()
    if not user: conn.close(); return jsonify({'error':'Not found'}), 404
    house_id = user['house_id']
    cur.execute('UPDATE users SET contributions_points = contributions_points + %s WHERE user_id=%s', (mod, str(user_id)))
    cur.execute('UPDATE houses SET house_points = house_points + %s WHERE name=%s', (mod, house_id))
    gd = _get_guild_data()
    dm = next((m for m in gd.get('members',[]) if m['id']==str(user_id)), None)
    tname = (dm.get('display_name') or dm.get('username')) if dm else None
    tav   = dm.get('avatar') if dm else None
    _write_log(cur, str(user_id), tname, tav, amount, reason, action, house_id, actor_id, actor_name, actor_av)
    cur.execute('SELECT contributions_points AS points FROM users WHERE user_id=%s', (str(user_id),))
    pts = cur.fetchone(); conn.close()
    _queue_log_embed(house_id, tname or user_id, amount, reason, action, actor_name)
    return jsonify({'success': True, 'points': pts['points']})

@app.route('/api/members/assign', methods=['POST'])
@login_required
def api_assign_member():
    data       = request.json
    user_id    = str(data.get('user_id','')).strip()
    house_name = data.get('house_name','').lower().strip()
    if not user_id or not house_name: return jsonify({'error':'user_id and house_name required'}), 400
    conn = db(); cur = conn.cursor()
    cur.execute('SELECT role_id FROM houses WHERE name=%s', (house_name,))
    house = cur.fetchone()
    if not house: conn.close(); return jsonify({'error':'House not found'}), 404
    cur.execute('SELECT house_id FROM users WHERE user_id=%s', (user_id,))
    old = cur.fetchone(); old_role = None
    if old:
        cur.execute('SELECT role_id FROM houses WHERE name=%s', (old['house_id'],))
        oh = cur.fetchone(); old_role = oh['role_id'] if oh else None
    cur.execute('INSERT INTO users (user_id,house_id,contributions_points,role_id) VALUES (%s,%s,0,%s) ON CONFLICT (user_id) DO UPDATE SET house_id=%s,role_id=%s',
                (user_id, house_name, house['role_id'], house_name, house['role_id']))
    cur.execute("INSERT INTO pending_actions (action_type,user_id,house_name,old_role_id) VALUES ('assign',%s,%s,%s)",
                (user_id, house_name, old_role))
    conn.close(); return jsonify({'success': True})

# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------
def _write_log(cur, user_id, target_username, target_avatar, amount, reason,
               action, house_id, actor_id, actor_name, actor_av=None):
    try:
        cur.execute(
            'INSERT INTO logs (user_id,target_username,target_avatar,amount,reason,action,house_id,actor_id,actor_username,actor_avatar) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (str(user_id), target_username, target_avatar, amount, reason,
             action, house_id, str(actor_id) if actor_id else None, actor_name, actor_av))
    except Exception as e:
        print(f'Log write error: {e}')

def _queue_log_embed(house_or_target, member_name, amount, reason, action, actor_name):
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT value FROM server_config WHERE key='log_channel'")
        row = cur.fetchone()
        if not row or not row['value']: conn.close(); return
        sign, emoji = ('+','📈') if action=='add' else ('-','📉')
        color = '57f287' if action=='add' else 'ed4245'
        lines = [f"**Member:** {member_name or house_or_target or 'Unknown'}",
                 f"**House:** {(house_or_target or '—').capitalize()}",
                 f"**Points:** {sign}{amount}"]
        if reason: lines.append(f"**Reason:** {reason}")
        lines.append(f"**By:** {actor_name or 'Dashboard'}")
        embed_data = {'title': f'{emoji} Points {"Added" if action=="add" else "Removed"}',
                      'description': '\n'.join(lines), 'color': color,
                      'footer_text': 'Ice Dodo Points Log', 'footer_icon': '', 'image_url': '', 'thumbnail_url': ''}
        cur.execute('INSERT INTO pending_messages (channel_id,embed_json,button_label,button_url) VALUES (%s,%s,%s,%s)',
                    (int(row['value']), json.dumps(embed_data), '', ''))
        conn.close()
    except Exception as e:
        print(f'Queue log embed error: {e}')

# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------
@app.route('/api/settings', methods=['GET'])
@login_required
def api_get_settings():
    conn = db(); cur = conn.cursor()
    cur.execute('SELECT key, value FROM server_config')
    rows = cur.fetchall(); conn.close()
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['POST'])
@login_required
def api_save_settings():
    allowed = ['embed_color','embed_footer_text','embed_footer_icon','embed_thumbnail',
               'embed_author_name','embed_author_icon','prefix','xp_enabled',
               'xp_per_msgs','xp_amount','log_channel',
               'welcome_channel','welcome_message','auto_assign_house']
    data = request.json; conn = db(); cur = conn.cursor()
    for k in allowed:
        if k in data:
            cur.execute('INSERT INTO server_config (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s',
                        (k, str(data[k]), str(data[k])))
    conn.close(); return jsonify({'success': True})

# ---------------------------------------------------------------------------
# Sticky messages API
# ---------------------------------------------------------------------------
@app.route('/api/sticky', methods=['GET'])
@login_required
def api_get_stickies():
    conn = db(); cur = conn.cursor()
    cur.execute('SELECT * FROM sticky_messages ORDER BY id DESC')
    rows = cur.fetchall(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/sticky', methods=['POST'])
@login_required
def api_create_sticky():
    data = request.json
    try: cid = int(data.get('channel_id',0))
    except: return jsonify({'error':'Invalid channel_id'}), 400
    if not cid: return jsonify({'error':'channel_id required'}), 400
    conn = db(); cur = conn.cursor()
    cur.execute('INSERT INTO sticky_messages (channel_id,title,description,color,image_url,thumbnail_url,footer_text,footer_icon,button_label,button_url,active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)',
                (cid, data.get('title',''), data.get('description',''), data.get('color','5865F2').lstrip('#'),
                 data.get('image_url',''), data.get('thumbnail_url',''), data.get('footer_text',''),
                 data.get('footer_icon',''), data.get('button_label',''), data.get('button_url','')))
    conn.close(); return jsonify({'success': True})

@app.route('/api/sticky/<int:sid>', methods=['PATCH'])
@login_required
def api_update_sticky(sid):
    data   = request.json
    fields = ['title','description','color','image_url','thumbnail_url','footer_text','footer_icon','button_label','button_url']
    conn   = db(); cur = conn.cursor()
    for f in fields:
        if f in data:
            val = data[f].lstrip('#') if f=='color' else data[f]
            cur.execute(f'UPDATE sticky_messages SET {f}=%s WHERE id=%s', (val, sid))
    conn.close(); return jsonify({'success': True})

@app.route('/api/sticky/<int:sid>', methods=['DELETE'])
@login_required
def api_delete_sticky(sid):
    conn = db(); cur = conn.cursor()
    cur.execute('DELETE FROM sticky_messages WHERE id=%s', (sid,)); conn.close()
    return jsonify({'success': True})

@app.route('/api/sticky/<int:sid>/toggle', methods=['POST'])
@login_required
def api_toggle_sticky(sid):
    conn = db(); cur = conn.cursor()
    cur.execute('UPDATE sticky_messages SET active = NOT active WHERE id=%s', (sid,))
    cur.execute('SELECT active FROM sticky_messages WHERE id=%s', (sid,))
    row = cur.fetchone(); conn.close()
    return jsonify({'success': True, 'active': row['active'] if row else False})

# ---------------------------------------------------------------------------
# Send message / Templates API
# ---------------------------------------------------------------------------
@app.route('/api/send-message', methods=['POST'])
@login_required
def api_send_message():
    data = request.json
    try: cid = int(data.get('channel_id',0))
    except: return jsonify({'error':'Invalid channel_id'}), 400
    if not cid: return jsonify({'error':'channel_id required'}), 400
    embed_data = {k: data.get(k,'') for k in ('title','description','image_url','thumbnail_url','footer_text','footer_icon','author_name','author_icon')}
    embed_data['color'] = data.get('color','5865F2').lstrip('#')
    conn = db(); cur = conn.cursor()
    cur.execute('INSERT INTO pending_messages (channel_id,embed_json,button_label,button_url) VALUES (%s,%s,%s,%s)',
                (cid, json.dumps(embed_data), data.get('button_label',''), data.get('button_url','')))
    conn.close(); return jsonify({'success': True})

@app.route('/api/templates', methods=['GET'])
@login_required
def api_get_templates():
    conn = db(); cur = conn.cursor()
    cur.execute('SELECT * FROM message_templates ORDER BY created_at DESC')
    rows = cur.fetchall(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/templates', methods=['POST'])
@login_required
def api_save_template():
    data = request.json; name = data.get('name','').strip()
    if not name: return jsonify({'error':'Template name required'}), 400
    conn = db(); cur = conn.cursor()
    cur.execute('INSERT INTO message_templates (name,title,description,color,image_url,thumbnail_url,footer_text,footer_icon,button_label,button_url) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (name, data.get('title',''), data.get('description',''), data.get('color','5865F2').lstrip('#'),
                 data.get('image_url',''), data.get('thumbnail_url',''), data.get('footer_text',''),
                 data.get('footer_icon',''), data.get('button_label',''), data.get('button_url','')))
    conn.close(); return jsonify({'success': True})

@app.route('/api/templates/<int:tid>', methods=['DELETE'])
@login_required
def api_delete_template(tid):
    conn = db(); cur = conn.cursor()
    cur.execute('DELETE FROM message_templates WHERE id=%s', (tid,)); conn.close()
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
