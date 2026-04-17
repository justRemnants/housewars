from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import sqlite3
import os
import functools

app = Flask(__name__)
app.secret_key = os.environ.get('DASHBOARD_PASSWORD', 'fallback-secret-key') + '_session'

DB_PATH = 'ice_dodo_sweats.db'

def get_db():
    conn = sqlite3.connect(DB_PATH)
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
        password = request.form.get('password', '')
        if password == os.environ.get('DASHBOARD_PASSWORD', ''):
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
    db = get_db()
    houses = db.execute('SELECT name, points, role_id FROM houses ORDER BY points DESC').fetchall()
    users = db.execute('SELECT user_id, house, points FROM users ORDER BY points DESC').fetchall()
    db.close()
    return render_template('index.html', houses=houses, users=users)

@app.route('/houses')
@login_required
def houses():
    db = get_db()
    houses = db.execute('SELECT name, points, role_id FROM houses ORDER BY points DESC').fetchall()
    db.close()
    return render_template('houses.html', houses=houses)

@app.route('/members')
@login_required
def members():
    db = get_db()
    users = db.execute('SELECT user_id, house, points, role_id FROM users ORDER BY points DESC').fetchall()
    houses = db.execute('SELECT name FROM houses ORDER BY name').fetchall()
    db.close()
    return render_template('members.html', users=users, houses=houses)

@app.route('/settings')
@login_required
def settings():
    db = get_db()
    config = db.execute('SELECT key, value FROM server_config').fetchall()
    embed_config = {}
    for row in config:
        embed_config[row['key']] = row['value']
    db.close()
    return render_template('settings.html', config=embed_config)

@app.route('/api/embed-config', methods=['GET'])
@login_required
def get_embed_config():
    db = get_db()
    rows = db.execute('SELECT key, value FROM server_config').fetchall()
    db.close()
    config = {row['key']: row['value'] for row in rows}
    return jsonify(config)

@app.route('/api/embed-config', methods=['POST'])
@login_required
def save_embed_config():
    data = request.json
    allowed_keys = [
        'embed_color', 'embed_footer_text', 'embed_footer_icon',
        'embed_thumbnail', 'embed_author_name', 'embed_author_icon'
    ]
    db = get_db()
    for key in allowed_keys:
        if key in data:
            db.execute(
                'INSERT OR REPLACE INTO server_config (key, value) VALUES (?, ?)',
                (key, str(data[key]))
            )
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/houses', methods=['GET'])
@login_required
def api_houses():
    db = get_db()
    houses = db.execute('SELECT name, points, role_id FROM houses ORDER BY points DESC').fetchall()
    db.close()
    return jsonify([dict(h) for h in houses])

@app.route('/api/houses/<name>/points', methods=['POST'])
@login_required
def api_adjust_points(name):
    data = request.json
    action = data.get('action')
    amount = int(data.get('amount', 0))
    if action not in ['add', 'remove'] or amount <= 0:
        return jsonify({'error': 'Invalid request'}), 400
    modifier = amount if action == 'add' else -amount
    db = get_db()
    db.execute('UPDATE houses SET points = points + ? WHERE name = ?', (modifier, name.lower()))
    db.execute('UPDATE users SET points = points + ? WHERE house = ?', (modifier, name.lower()))
    db.commit()
    new_points = db.execute('SELECT points FROM houses WHERE name = ?', (name.lower(),)).fetchone()
    db.close()
    return jsonify({'success': True, 'points': new_points['points'] if new_points else 0})

@app.route('/api/houses/<name>/reset', methods=['POST'])
@login_required
def api_reset_house(name):
    db = get_db()
    db.execute('UPDATE houses SET points = 0 WHERE name = ?', (name.lower(),))
    db.execute('UPDATE users SET points = 0 WHERE house = ?', (name.lower(),))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/season/reset', methods=['POST'])
@login_required
def api_reset_season():
    db = get_db()
    db.execute('UPDATE users SET points = 0')
    db.execute('UPDATE houses SET points = 0')
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/members', methods=['GET'])
@login_required
def api_members():
    db = get_db()
    users = db.execute('SELECT user_id, house, points FROM users ORDER BY points DESC').fetchall()
    db.close()
    return jsonify([dict(u) for u in users])

@app.route('/api/members/<int:user_id>/points', methods=['POST'])
@login_required
def api_adjust_member_points(user_id):
    data = request.json
    action = data.get('action')
    amount = int(data.get('amount', 0))
    if action not in ['add', 'remove'] or amount <= 0:
        return jsonify({'error': 'Invalid request'}), 400
    modifier = amount if action == 'add' else -amount
    db = get_db()
    user = db.execute('SELECT house FROM users WHERE user_id = ?', (user_id,)).fetchone()
    if not user:
        db.close()
        return jsonify({'error': 'User not found'}), 404
    db.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (modifier, user_id))
    db.execute('UPDATE houses SET points = points + ? WHERE name = ?', (modifier, user['house']))
    db.commit()
    new = db.execute('SELECT points FROM users WHERE user_id = ?', (user_id,)).fetchone()
    db.close()
    return jsonify({'success': True, 'points': new['points']})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
