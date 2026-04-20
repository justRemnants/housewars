from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import sqlite3, os, json, functools

app = Flask(__name__)
app.secret_key = os.environ.get('DASHBOARD_PASSWORD', 'fallback') + '_session'
DB = 'ice_dodo_sweats.db'

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

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

@app.route('/')
@login_required
def index():
    d = db()
    houses = d.execute('SELECT name, points, role_id, color FROM houses ORDER BY points DESC').fetchall()
    users = d.execute('SELECT user_id, house, points FROM users ORDER BY points DESC').fetchall()
    d.close()
    return render_template('index.html', houses=houses, users=users)

@app.route('/houses')
@login_required
def houses():
    d = db()
    houses = d.execute('SELECT name, points, role_id, color, thumbnail_url FROM houses ORDER BY points DESC').fetchall()
    d.close()
    return render_template('houses.html', houses=houses)

@app.route('/members')
@login_required
def members():
    d = db()
    users = d.execute('SELECT user_id, house, points FROM users ORDER BY points DESC').fetchall()
    houses = d.execute('SELECT name FROM houses ORDER BY name').fetchall()
    d.close()
    return render_template('members.html', users=users, houses=houses)

@app.route('/settings')
@login_required
def settings():
    d = db()
    rows = d.execute('SELECT key, value FROM server_config').fetchall()
    d.close()
    cfg = {r['key']: r['value'] for r in rows}
    return render_template('settings.html', cfg=cfg)

@app.route('/messages')
@login_required
def messages():
    d = db()
    stickies = d.execute('SELECT * FROM sticky_messages ORDER BY id DESC').fetchall()
    d.close()
    return render_template('messages.html', stickies=stickies)

# --- Houses API ---
@app.route('/api/houses', methods=['GET'])
@login_required
def api_houses():
    d = db()
    rows = d.execute('SELECT name, points, role_id, color, thumbnail_url FROM houses ORDER BY points DESC').fetchall()
    d.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/houses', methods=['POST'])
@login_required
def api_create_house():
    data = request.json
    name = data.get('name', '').lower().strip()
    role_id = data.get('role_id')
    color = data.get('color', '5865F2').lstrip('#')
    thumbnail_url = data.get('thumbnail_url', '')
    if not name:
        return jsonify({'error': 'Name required'}), 400
    d = db()
    d.execute('INSERT OR IGNORE INTO houses (name, points, role_id, color, thumbnail_url) VALUES (?,0,?,?,?)',
              (name, role_id or None, color, thumbnail_url))
    d.execute('UPDATE houses SET role_id=?, color=?, thumbnail_url=? WHERE name=?',
              (role_id or None, color, thumbnail_url, name))
    d.commit()
    d.close()
    return jsonify({'success': True})

@app.route('/api/houses/<name>', methods=['PATCH'])
@login_required
def api_update_house(name):
    data = request.json
    d = db()
    if 'color' in data:
        d.execute('UPDATE houses SET color=? WHERE name=?', (data['color'].lstrip('#'), name.lower()))
    if 'thumbnail_url' in data:
        d.execute('UPDATE houses SET thumbnail_url=? WHERE name=?', (data['thumbnail_url'], name.lower()))
    if 'role_id' in data:
        d.execute('UPDATE houses SET role_id=? WHERE name=?', (data['role_id'] or None, name.lower()))
    d.commit()
    d.close()
    return jsonify({'success': True})

@app.route('/api/houses/<name>', methods=['DELETE'])
@login_required
def api_delete_house(name):
    d = db()
    d.execute('DELETE FROM houses WHERE name=?', (name.lower(),))
    d.commit()
    d.close()
    return jsonify({'success': True})

@app.route('/api/houses/<name>/points', methods=['POST'])
@login_required
def api_house_points(name):
    data = request.json
    action, amount = data.get('action'), int(data.get('amount', 0))
    if action not in ['add', 'remove'] or amount <= 0:
        return jsonify({'error': 'Invalid'}), 400
    modifier = amount if action == 'add' else -amount
    d = db()
    d.execute('UPDATE houses SET points = points + ? WHERE name=?', (modifier, name.lower()))
    d.execute('UPDATE users SET points = points + ? WHERE house=?', (modifier, name.lower()))
    d.commit()
    pts = d.execute('SELECT points FROM houses WHERE name=?', (name.lower(),)).fetchone()
    d.close()
    return jsonify({'success': True, 'points': pts['points'] if pts else 0})

@app.route('/api/houses/<name>/reset', methods=['POST'])
@login_required
def api_reset_house(name):
    d = db()
    d.execute('UPDATE houses SET points=0 WHERE name=?', (name.lower(),))
    d.execute('UPDATE users SET points=0 WHERE house=?', (name.lower(),))
    d.commit(); d.close()
    return jsonify({'success': True})

@app.route('/api/season/reset', methods=['POST'])
@login_required
def api_reset_season():
    d = db()
    d.execute('UPDATE users SET points=0')
    d.execute('UPDATE houses SET points=0')
    d.commit(); d.close()
    return jsonify({'success': True})

# --- Members API ---
@app.route('/api/members/<int:user_id>/points', methods=['POST'])
@login_required
def api_member_points(user_id):
    data = request.json
    action, amount = data.get('action'), int(data.get('amount', 0))
    if action not in ['add', 'remove'] or amount <= 0:
        return jsonify({'error': 'Invalid'}), 400
    modifier = amount if action == 'add' else -amount
    d = db()
    user = d.execute('SELECT house FROM users WHERE user_id=?', (user_id,)).fetchone()
    if not user: d.close(); return jsonify({'error': 'Not found'}), 404
    d.execute('UPDATE users SET points = points + ? WHERE user_id=?', (modifier, user_id))
    d.execute('UPDATE houses SET points = points + ? WHERE name=?', (modifier, user['house']))
    d.commit()
    pts = d.execute('SELECT points FROM users WHERE user_id=?', (user_id,)).fetchone()
    d.close()
    return jsonify({'success': True, 'points': pts['points']})

@app.route('/api/members/assign', methods=['POST'])
@login_required
def api_assign_member():
    data = request.json
    user_id = int(data.get('user_id', 0))
    house_name = data.get('house_name', '').lower().strip()
    if not user_id or not house_name:
        return jsonify({'error': 'user_id and house_name required'}), 400
    d = db()
    house = d.execute('SELECT role_id FROM houses WHERE name=?', (house_name,)).fetchone()
    if not house: d.close(); return jsonify({'error': 'House not found'}), 404
    old = d.execute('SELECT house, role_id FROM users WHERE user_id=?', (user_id,)).fetchone()
    old_role_id = old['role_id'] if old else None
    d.execute('REPLACE INTO users (user_id, house, points, role_id) VALUES (?,?,COALESCE((SELECT points FROM users WHERE user_id=?),0),?)',
              (user_id, house_name, user_id, house['role_id']))
    d.execute('INSERT OR IGNORE INTO houses (name, points) VALUES (?,0)', (house_name,))
    d.execute('INSERT INTO pending_actions (action_type, user_id, house_name, old_role_id) VALUES ("assign",?,?,?)',
              (user_id, house_name, old_role_id))
    d.commit(); d.close()
    return jsonify({'success': True})

# --- Settings API ---
@app.route('/api/settings', methods=['GET'])
@login_required
def api_get_settings():
    d = db()
    rows = d.execute('SELECT key, value FROM server_config').fetchall()
    d.close()
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['POST'])
@login_required
def api_save_settings():
    data = request.json
    allowed = ['embed_color','embed_footer_text','embed_footer_icon','embed_thumbnail',
                'embed_author_name','embed_author_icon','prefix','xp_enabled','xp_per_msgs','xp_amount']
    d = db()
    for k in allowed:
        if k in data:
            d.execute('REPLACE INTO server_config (key, value) VALUES (?,?)', (k, str(data[k])))
    d.commit(); d.close()
    return jsonify({'success': True})

# --- Sticky / Messages API ---
@app.route('/api/sticky', methods=['GET'])
@login_required
def api_get_stickies():
    d = db()
    rows = d.execute('SELECT * FROM sticky_messages ORDER BY id DESC').fetchall()
    d.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/sticky', methods=['POST'])
@login_required
def api_create_sticky():
    data = request.json
    channel_id = int(data.get('channel_id', 0))
    if not channel_id: return jsonify({'error': 'channel_id required'}), 400
    d = db()
    d.execute('''INSERT INTO sticky_messages (channel_id,title,description,color,image_url,thumbnail_url,
                footer_text,footer_icon,button_label,button_url,active)
                VALUES (?,?,?,?,?,?,?,?,?,?,1)''',
              (channel_id, data.get('title',''), data.get('description',''),
               data.get('color','5865F2').lstrip('#'),
               data.get('image_url',''), data.get('thumbnail_url',''),
               data.get('footer_text',''), data.get('footer_icon',''),
               data.get('button_label',''), data.get('button_url','')))
    d.commit(); d.close()
    return jsonify({'success': True})

@app.route('/api/sticky/<int:sticky_id>', methods=['DELETE'])
@login_required
def api_delete_sticky(sticky_id):
    d = db()
    d.execute('DELETE FROM sticky_messages WHERE id=?', (sticky_id,))
    d.commit(); d.close()
    return jsonify({'success': True})

@app.route('/api/sticky/<int:sticky_id>/toggle', methods=['POST'])
@login_required
def api_toggle_sticky(sticky_id):
    d = db()
    d.execute('UPDATE sticky_messages SET active = 1 - active WHERE id=?', (sticky_id,))
    d.commit()
    row = d.execute('SELECT active FROM sticky_messages WHERE id=?', (sticky_id,)).fetchone()
    d.close()
    return jsonify({'success': True, 'active': row['active'] if row else 0})

@app.route('/api/send-message', methods=['POST'])
@login_required
def api_send_message():
    data = request.json
    channel_id = int(data.get('channel_id', 0))
    if not channel_id: return jsonify({'error': 'channel_id required'}), 400
    embed_data = {
        'title': data.get('title',''), 'description': data.get('description',''),
        'color': data.get('color','5865F2').lstrip('#'),
        'image_url': data.get('image_url',''), 'thumbnail_url': data.get('thumbnail_url',''),
        'footer_text': data.get('footer_text',''), 'footer_icon': data.get('footer_icon',''),
        'author_name': data.get('author_name',''), 'author_icon': data.get('author_icon',''),
    }
    d = db()
    d.execute('INSERT INTO pending_messages (channel_id, embed_json, button_label, button_url) VALUES (?,?,?,?)',
              (channel_id, json.dumps(embed_data), data.get('button_label',''), data.get('button_url','')))
    d.commit(); d.close()
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
