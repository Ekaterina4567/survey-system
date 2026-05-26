from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
import sqlite3
import os
import json
import secrets
import qrcode
import io
import base64
from datetime import datetime
from functools import wraps
import hashlib
import threading
import time

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))

socketio = SocketIO(app, cors_allowed_origins="*")

active_games = {}
DATABASE = 'survey.db'

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plain_password TEXT,
            role TEXT DEFAULT 'student',
            full_name TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            unique_code TEXT UNIQUE NOT NULL,
            created_by_user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_editable INTEGER DEFAULT 1,
            game_mode INTEGER DEFAULT 0,
            time_per_question INTEGER DEFAULT 10
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS test_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            question_type TEXT DEFAULT 'choice',
            options TEXT,
            order_index INTEGER DEFAULT 0,
            correct_answer TEXT,
            points INTEGER DEFAULT 1
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS test_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            score INTEGER DEFAULT 0,
            max_score INTEGER DEFAULT 0,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            answers_json TEXT,
            game_session_id TEXT
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS test_answers_detail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            user_answer TEXT,
            is_correct INTEGER DEFAULT 0,
            points_earned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Тестовые пользователи
    admin_hash = hashlib.sha256("admin123".encode()).hexdigest()
    cur.execute("SELECT id FROM users WHERE username = 'admin'")
    if not cur.fetchone():
        cur.execute("INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                   ('admin', 'admin@survey.com', admin_hash, 'admin123', 'admin', 'Администратор'))
    
    teacher_hash = hashlib.sha256("teacher123".encode()).hexdigest()
    cur.execute("SELECT id FROM users WHERE username = 'teacher'")
    if not cur.fetchone():
        cur.execute("INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                   ('teacher', 'teacher@survey.com', teacher_hash, 'teacher123', 'teacher', 'Преподаватель'))
    
    student_hash = hashlib.sha256("student123".encode()).hexdigest()
    cur.execute("SELECT id FROM users WHERE username = 'student'")
    if not cur.fetchone():
        cur.execute("INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                   ('student', 'student@survey.com', student_hash, 'student123', 'student', 'Студент'))
    
    conn.commit()
    conn.close()
    print("✅ Database ready")

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Требуется авторизация'}), 401
        return f(*args, **kwargs)
    return decorated

def generate_unique_code():
    while True:
        code = secrets.token_urlsafe(6).upper().replace('-', 'X').replace('_', 'Y')
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM tests WHERE unique_code = ?", (code,))
        result = cur.fetchone()
        conn.close()
        if not result:
            return code

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def normalize_text(text):
    if not text:
        return ""
    return text.strip().lower().replace("ё", "е").replace(" ", "")

def check_answer(question_type, user_answer, correct_answer):
    if not user_answer or not correct_answer:
        return False
    if question_type == 'text':
        return normalize_text(str(user_answer)) == normalize_text(str(correct_answer))
    elif question_type == 'choice':
        return normalize_text(str(user_answer)) == normalize_text(str(correct_answer))
    elif question_type == 'checkbox':
        user_set = set(normalize_text(x) for x in str(user_answer).split(','))
        correct_set = set(normalize_text(x) for x in str(correct_answer).split(','))
        return user_set == correct_set
    return False

# ==================== СТРАНИЦЫ ====================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login_page')
def login_page():
    return render_template('login.html')

@app.route('/register_page')
def register_page():
    return render_template('register.html')

@app.route('/student_dashboard')
def student_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('student_dashboard.html', user=session)

@app.route('/create_test_page')
def create_test_page():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('create_test.html', user=session)

@app.route('/game_host/<code>')
def game_host(code):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('game_host.html', code=code, user=session)

@app.route('/game_player/<code>')
def game_player(code):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('game_player.html', code=code, user=session)

@app.route('/take_test')
def take_test():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('take_test.html', user=session)

@app.route('/result_detail/<int:result_id>')
def result_detail(result_id):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('result_detail.html', result_id=result_id, user=session)

# ==================== API ====================

@app.route('/register', methods=['POST'])
def register():
    try:
        data = request.json
        username = data.get('username')
        email = data.get('email')
        password = data.get('password')
        full_name = data.get('full_name')
        role = data.get('role', 'student')
        
        if not all([username, email, password]):
            return jsonify({'error': 'Заполните все поля'}), 400
        
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
        if cur.fetchone():
            conn.close()
            return jsonify({'error': 'Пользователь уже существует'}), 400
        
        password_hash = hash_password(password)
        cur.execute('''INSERT INTO users (username, email, password_hash, role, full_name, is_active, plain_password)
                       VALUES (?, ?, ?, ?, ?, 1, ?)''', 
                   (username, email, password_hash, role, full_name, password))
        user_id = cur.lastrowid
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Регистрация успешна', 'user_id': user_id})
    except Exception as e:
        print(f"Register error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.json
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            return jsonify({'error': 'Введите логин и пароль'}), 400
        
        conn = get_db()
        cur = conn.cursor()
        password_hash = hash_password(password)
        
        cur.execute("SELECT * FROM users WHERE (username = ? OR email = ?) AND password_hash = ? AND is_active = 1",
                   (username, username, password_hash))
        user = cur.fetchone()
        conn.close()
        
        if not user:
            return jsonify({'error': 'Неверный логин или пароль'}), 401
        
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        session['full_name'] = user['full_name']
        
        return jsonify({
            'success': True,
            'message': 'Вход выполнен',
            'role': user['role'],
            'redirect': '/student_dashboard'
        })
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/logout')
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/check_auth')
def check_auth():
    if 'user_id' in session:
        return jsonify({'authenticated': True, 'role': session.get('role'), 'username': session.get('username')})
    return jsonify({'authenticated': False})

@app.route('/api/create_test', methods=['POST'])
@login_required
def create_test():
    try:
        data = request.json
        title = data.get('title')
        questions = data.get('questions', [])
        game_mode = data.get('game_mode', 0)
        time_per_question = data.get('time_per_question', 10)
        
        if not title or not questions:
            return jsonify({'error': 'Название и вопросы обязательны'}), 400
        
        code = generate_unique_code()
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''INSERT INTO tests (title, unique_code, created_by_user_id, game_mode, time_per_question)
                       VALUES (?, ?, ?, ?, ?)''',
                   (title, code, session['user_id'], game_mode, time_per_question))
        test_id = cur.lastrowid
        
        for idx, q in enumerate(questions):
            cur.execute('''INSERT INTO test_questions (test_id, question_text, question_type, options, order_index, correct_answer, points)
                           VALUES (?, ?, ?, ?, ?, ?, ?)''',
                       (test_id, q.get('text'), q.get('type', 'text'),
                        json.dumps(q.get('options', [])), idx, q.get('correct_answer'), q.get('points', 1)))
        
        conn.commit()
        
        qr_data = f"{request.host_url}take_test?code={code}"
        qr_img = qrcode.make(qr_data)
        buffered = io.BytesIO()
        qr_img.save(buffered, format="PNG")
        qr_base64 = base64.b64encode(buffered.getvalue()).decode()
        
        game_qr_data = f"{request.host_url}game_player/{code}"
        game_qr_img = qrcode.make(game_qr_data)
        game_buffered = io.BytesIO()
        game_qr_img.save(game_buffered, format="PNG")
        game_qr_base64 = base64.b64encode(game_buffered.getvalue()).decode()
        
        conn.close()
        
        return jsonify({
            'success': True,
            'code': code,
            'qr_code': qr_base64,
            'game_qr_code': game_qr_base64,
            'test_id': test_id
        })
    except Exception as e:
        print(f"Create test error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_test_by_code', methods=['POST'])
def get_test_by_code():
    try:
        data = request.json
        code = data.get('code')
        if not code:
            return jsonify({'error': 'Введите код'}), 400
        
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, title, unique_code, created_by_user_id, game_mode, time_per_question FROM tests WHERE unique_code = ?", (code.upper(),))
        test = cur.fetchone()
        
        if not test:
            conn.close()
            return jsonify({'error': 'Тест не найден'}), 404
        
        cur.execute("SELECT id, question_text, question_type, options, correct_answer, points, order_index FROM test_questions WHERE test_id = ? ORDER BY order_index", (test['id'],))
        questions = []
        for row in cur.fetchall():
            q = dict(row)
            if q['options']:
                try:
                    q['options'] = json.loads(q['options'])
                except:
                    q['options'] = []
            else:
                q['options'] = []
            questions.append(q)
        
        conn.close()
        
        return jsonify({
            'success': True,
            'test': {
                'id': test['id'],
                'title': test['title'],
                'code': test['unique_code'],
                'game_mode': test['game_mode'],
                'time_per_question': test['time_per_question'],
                'questions': questions
            }
        })
    except Exception as e:
        print(f"Get test error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/submit_test', methods=['POST'])
@login_required
def submit_test():
    try:
        data = request.json
        test_id = data.get('test_id')
        answers = data.get('answers', {})
        
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, correct_answer, question_type, points FROM test_questions WHERE test_id = ?", (test_id,))
        questions = cur.fetchall()
        
        total_score = 0
        max_score = 0
        details = []
        
        for q in questions:
            max_score += q['points']
            user_answer = answers.get(str(q['id']))
            is_correct = check_answer(q['question_type'], user_answer, q['correct_answer'])
            if is_correct:
                total_score += q['points']
            details.append({
                'question_id': q['id'],
                'user_answer': user_answer or '',
                'is_correct': is_correct,
                'points_earned': q['points'] if is_correct else 0
            })
        
        cur.execute('''INSERT INTO test_results (test_id, user_id, score, max_score, answers_json)
                       VALUES (?, ?, ?, ?, ?)''',
                   (test_id, session['user_id'], total_score, max_score, json.dumps(details)))
        result_id = cur.lastrowid
        
        for d in details:
            cur.execute('''INSERT INTO test_answers_detail (result_id, question_id, user_answer, is_correct, points_earned)
                           VALUES (?, ?, ?, ?, ?)''',
                       (result_id, d['question_id'], d['user_answer'], 1 if d['is_correct'] else 0, d['points_earned']))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'score': total_score,
            'max_score': max_score,
            'percentage': round(total_score / max_score * 100, 1) if max_score > 0 else 0,
            'result_id': result_id
        })
    except Exception as e:
        print(f"Submit error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/my_results', methods=['GET'])
@login_required
def my_results():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''SELECT tr.id, tr.score, tr.max_score, tr.completed_at, t.title, t.unique_code
                       FROM test_results tr JOIN tests t ON tr.test_id = t.id
                       WHERE tr.user_id = ? ORDER BY tr.completed_at DESC''', (session['user_id'],))
        results = [dict(row) for row in cur.fetchall()]
        conn.close()
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_my_tests', methods=['GET'])
@login_required
def get_my_tests():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, title, unique_code, created_at, game_mode FROM tests WHERE created_by_user_id = ? ORDER BY created_at DESC", (session['user_id'],))
        tests = [dict(row) for row in cur.fetchall()]
        conn.close()
        return jsonify(tests)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== WEBSOCKET ====================

@socketio.on('join_game_host')
def handle_join_host(data):
    code = data.get('game_code')
    if code:
        join_room(code)
        if code not in active_games:
            active_games[code] = {'players': {}, 'current': -1, 'active': False, 'questions': [], 'time': 10}
        active_games[code]['host_sid'] = request.sid
        emit('host_connected', room=request.sid)

@socketio.on('join_game_player')
def handle_join_player(data):
    code = data.get('game_code')
    uid = data.get('user_id')
    name = data.get('username')
    if code in active_games and uid:
        join_room(code)
        if uid not in active_games[code]['players']:
            active_games[code]['players'][uid] = {'name': name, 'answers': {}, 'score': 0}
        active_games[code]['players'][uid]['sid'] = request.sid
        players_list = [{'name': p['name']} for p in active_games[code]['players'].values()]
        emit('players_update', {'players': players_list}, room=code)

@socketio.on('start_game')
def handle_start(data):
    code = data.get('game_code')
    if code in active_games:
        game = active_games[code]
        game['questions'] = data.get('questions', [])
        game['time'] = data.get('time', 10)
        game['active'] = True
        game['current'] = -1
        emit('game_started', {'total': len(game['questions'])}, room=code)
        def start_first():
            socketio.emit('next_question', room=code)
        threading.Timer(2, start_first).start()

@socketio.on('next_question')
def handle_next():
    for code, game in active_games.items():
        if request.sid == game.get('host_sid'):
            game['current'] += 1
            if game['current'] < len(game['questions']):
                q = game['questions'][game['current']]
                emit('question_start', {
                    'index': game['current'],
                    'question': q,
                    'time_left': game['time']
                }, room=code)
                def end_q():
                    socketio.emit('time_up', room=code)
                    threading.Timer(2, lambda: show_results(code)).start()
                threading.Timer(game['time'], end_q).start()
            else:
                end_game(code)
            break

@socketio.on('submit_answer')
def handle_answer(data):
    code = data.get('game_code')
    uid = data.get('user_id')
    qidx = data.get('question_index')
    answer = data.get('answer')
    
    if code in active_games and uid in active_games[code]['players']:
        game = active_games[code]
        if qidx < len(game['questions']):
            q = game['questions'][qidx]
            is_correct = check_answer(q.get('type'), answer, q.get('correct_answer'))
            points = q.get('points', 1) if is_correct else 0
            game['players'][uid]['answers'][qidx] = {'correct': is_correct, 'points': points}
            game['players'][uid]['score'] += points
            emit('answer_result', {'correct': is_correct, 'points': points, 'total': game['players'][uid]['score']}, room=request.sid)

def show_results(code):
    if code in active_games:
        game = active_games[code]
        current = game['current']
        total = len(game['players'])
        correct = sum(1 for p in game['players'].values() if p['answers'].get(current, {}).get('correct', False))
        emit('question_results', {'total': total, 'correct': correct, 'percent': round(correct/total*100, 1) if total > 0 else 0}, room=code)
        threading.Timer(3, lambda: socketio.emit('next_question', room=code)).start()

def end_game(code):
    if code in active_games:
        game = active_games[code]
        results = [{'name': p['name'], 'score': p['score']} for p in game['players'].values()]
        results.sort(key=lambda x: x['score'], reverse=True)
        emit('game_ended', {'results': results}, room=code)
        del active_games[code]

# ==================== ЗАПУСК ====================

if __name__ == '__main__':
    init_db()
    port = int(os.getenv('PORT', 3000))
    print(f"🚀 Server on http://localhost:{port}")
    print("👨‍💼 admin/admin123 | 👨‍🏫 teacher/teacher123 | 👨‍🎓 student/student123")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
