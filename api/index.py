from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import os, json, functools
import psycopg2
import psycopg2.extras

app = Flask(__name__, template_folder='../templates', static_folder='../static')
app.secret_key = os.environ.get('SESSION_SECRET', os.environ.get('DASHBOARD_PASSWORD', 'fallback') + '_session')

# ---------------------------------------------------------------------------
# Database — Supabase / PostgreSQL
# Uses SUPABASE_URL env var (already set in your Vercel project).
# Make sure it's the full connection URI, e.g.:
#   postgresql://postgres:[PASSWORD]@db.[REF].supabase.co:5432/postgres
# (Supabase → Project Settings → Database → Connection string → URI)
# ---------------------------------------------------------------------------
def db():
    conn = psycopg2.connect(os.environ['SUPABASE_URL'], cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

# ---------------------------------------------------------------------------
# Column name mapping (matches YOUR existing Supabase tables):
#   houses : name, role_id, house_points
#   users  : user_id, house_id, contributions_points
# ---------------------------------------------------------------------------

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == os.environ.get('DASHBOARD_PASSWORD', ''):
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = 'Incorrect password.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

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
    users = cur.fetchall()
    cur.execute('SELECT name FROM houses ORDER BY name')
    houses = cur.fetchall()
    conn.close()
    return render_template('members.html', users=users, houses=houses)

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

@app.route('/messages')
@login_required
def messages():
    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM sticky_messages ORDER BY id DESC')
    stickies = cur.fetchall()
    conn.close()
    return render_template('messages.html', stickies=stickies)

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
    name = data.get('name', '').lower().strip()
    role_id = data.get('role_id') or None
    color = data.get('color', '5865F2').lstrip('#')
    thumbnail_url = data.get('thumbnail_url', '')
    if not name:
        return jsonify({'error': 'Name required'}), 400
    conn = db()
    cur = conn.cursor()
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
    cur = conn.cursor()
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
    cur = conn.cursor()
    cur.execute('DELETE FROM houses WHERE name=%s', (hname.lower(),))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/houses/<hname>/points', methods=['POST'])
@login_required
def api_house_points(hname):
    data = request.json
    action = data.get('action')
    amount = int(data.get('amount', 0))
    if action not in ['add', 'remove'] or amount <= 0:
        return jsonify({'error': 'Invalid'}), 400
    modifier = amount if action == 'add' else -amount
    conn = db()
    cur = conn.cursor()
    cur.execute('UPDATE houses SET house_points = house_points + %s WHERE name=%s', (modifier, hname.lower()))
    cur.execute('UPDATE users SET contributions_points = contributions_points + %s WHERE house_id=%s', (modifier, hname.lower()))
    conn.commit()
    cur.execute('SELECT house_points AS points FROM houses WHERE name=%s', (hname.lower(),))
    pts = cur.fetchone()
    conn.close()
    return jsonify({'success': True, 'points': pts['points'] if pts else 0})

@app.route('/api/houses/<hname>/reset', methods=['POST'])
@login_required
def api_reset_house(hname):
    conn = db()
    cur = conn.cursor()
    cur.execute('UPDATE houses SET house_points=0 WHERE name=%s', (hname.lower(),))
    cur.execute('UPDATE users SET contributions_points=0 WHERE house_id=%s', (hname.lower(),))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/season/reset', methods=['POST'])
@login_required
def api_reset_season():
    conn = db()
    cur = conn.cursor()
    cur.execute('UPDATE users SET contributions_points=0')
    cur.execute('UPDATE houses SET house_points=0')
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ---------------------------------------------------------------------------
# Members API
# ---------------------------------------------------------------------------
@app.route('/api/members/<int:user_id>/points', methods=['POST'])
@login_required
def api_member_points(user_id):
    data = request.json
    action = data.get('action')
    amount = int(data.get('amount', 0))
    if action not in ['add', 'remove'] or amount <= 0:
        return jsonify({'error': 'Invalid'}), 400
    modifier = amount if action == 'add' else -amount
    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT house_id FROM users WHERE user_id=%s', (str(user_id),))
    user = cur.fetchone()
    if not user:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    cur.execute('UPDATE users SET contributions_points = contributions_points + %s WHERE user_id=%s', (modifier, str(user_id)))
    cur.execute('UPDATE houses SET house_points = house_points + %s WHERE name=%s', (modifier, user['house_id']))
    conn.commit()
    cur.execute('SELECT contributions_points AS points FROM users WHERE user_id=%s', (str(user_id),))
    pts = cur.fetchone()
    conn.close()
    return jsonify({'success': True, 'points': pts['points']})

@app.route('/api/members/assign', methods=['POST'])
@login_required
def api_assign_member():
    data = request.json
    user_id = str(data.get('user_id', '')).strip()
    house_name = data.get('house_name', '').lower().strip()
    if not user_id or not house_name:
        return jsonify({'error': 'user_id and house_name required'}), 400
    conn = db()
    cur = conn.cursor()
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
# Settings API
# ---------------------------------------------------------------------------
@app.route('/api/settings', methods=['GET'])
@login_required
def api_get_settings():
    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT key, value FROM server_config')
    rows = cur.fetchall()
    conn.close()
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['POST'])
@login_required
def api_save_settings():
    data = request.json
    allowed = ['embed_color', 'embed_footer_text', 'embed_footer_icon', 'embed_thumbnail',
               'embed_author_name', 'embed_author_icon', 'prefix', 'xp_enabled', 'xp_per_msgs', 'xp_amount']
    conn = db()
    cur = conn.cursor()
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
# Sticky / Messages API
# ---------------------------------------------------------------------------
@app.route('/api/sticky', methods=['GET'])
@login_required
def api_get_stickies():
    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM sticky_messages ORDER BY id DESC')
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/sticky', methods=['POST'])
@login_required
def api_create_sticky():
    data = request.json
    channel_id = int(data.get('channel_id', 0))
    if not channel_id:
        return jsonify({'error': 'channel_id required'}), 400
    conn = db()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO sticky_messages
           (channel_id, title, description, color, image_url, thumbnail_url,
            footer_text, footer_icon, button_label, button_url, active)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)''',
        (channel_id, data.get('title', ''), data.get('description', ''),
         data.get('color', '5865F2').lstrip('#'),
         data.get('image_url', ''), data.get('thumbnail_url', ''),
         data.get('footer_text', ''), data.get('footer_icon', ''),
         data.get('button_label', ''), data.get('button_url', ''))
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/sticky/<int:sticky_id>', methods=['DELETE'])
@login_required
def api_delete_sticky(sticky_id):
    conn = db()
    cur = conn.cursor()
    cur.execute('DELETE FROM sticky_messages WHERE id=%s', (sticky_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/sticky/<int:sticky_id>/toggle', methods=['POST'])
@login_required
def api_toggle_sticky(sticky_id):
    conn = db()
    cur = conn.cursor()
    cur.execute('UPDATE sticky_messages SET active = NOT active WHERE id=%s', (sticky_id,))
    conn.commit()
    cur.execute('SELECT active FROM sticky_messages WHERE id=%s', (sticky_id,))
    row = cur.fetchone()
    conn.close()
    return jsonify({'success': True, 'active': row['active'] if row else False})

@app.route('/api/send-message', methods=['POST'])
@login_required
def api_send_message():
    data = request.json
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
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO pending_messages (channel_id, embed_json, button_label, button_url) VALUES (%s, %s, %s, %s)',
        (channel_id, json.dumps(embed_data), data.get('button_label', ''), data.get('button_url', ''))
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
    cur = conn.cursor()
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
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO message_templates
           (name, title, description, color, image_url, thumbnail_url,
            footer_text, footer_icon, button_label, button_url)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
        (name, data.get('title', ''), data.get('description', ''),
         data.get('color', '5865F2').lstrip('#'),
         data.get('image_url', ''), data.get('thumbnail_url', ''),
         data.get('footer_text', ''), data.get('footer_icon', ''),
         data.get('button_label', ''), data.get('button_url', ''))
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/templates/<int:template_id>', methods=['DELETE'])
@login_required
def api_delete_template(template_id):
    conn = db()
    cur = conn.cursor()
    cur.execute('DELETE FROM message_templates WHERE id=%s', (template_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
