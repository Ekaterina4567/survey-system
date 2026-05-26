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

# Хранилище активных игр
active_games = {}

# Путь к базе данных
DATABASE = 'survey.db'

def get_db():
    """Получение соединения с SQLite"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Инициализация базы данных"""
    conn = get_db()
    cur = conn.cursor()
    
    # Таблица пользователей
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
    
    # Таблица тестов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            unique_code TEXT UNIQUE NOT NULL,
            created_by_user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_editable INTEGER DEFAULT 1,
            game_mode INTEGER DEFAULT 0,
            time_per_question INTEGER DEFAULT 10,
            FOREIGN KEY (created_by_user_id) REFERENCES users (id)
        )
    ''')
    
    # Таблица вопросов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS test_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            question_type TEXT DEFAULT 'choice',
            options TEXT,
            order_index INTEGER DEFAULT 0,
            correct_answer TEXT,
            points INTEGER DEFAULT 1,
            FOREIGN KEY (test_id) REFERENCES tests (id) ON DELETE CASCADE
        )
    ''')
    
    # Таблица результатов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS test_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            score INTEGER DEFAULT 0,
            max_score INTEGER DEFAULT 0,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            answers_json TEXT,
            game_session_id TEXT,
            FOREIGN KEY (test_id) REFERENCES tests (id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
    ''')
    
    # Таблица детальных ответов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS test_answers_detail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            user_answer TEXT,
            is_correct INTEGER DEFAULT 0,
            points_earned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (result_id) REFERENCES test_results (id) ON DELETE CASCADE,
            FOREIGN KEY (question_id) REFERENCES test_questions (id) ON DELETE CASCADE
        )
    ''')
    
    # Индексы
    cur.execute('CREATE INDEX IF NOT EXISTS idx_tests_code ON tests(unique_code)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_results_user ON test_results(user_id)')
    
    # Создаём тестовых пользователей
    admin_hash = hashlib.sha256("admin123".encode()).hexdigest()
    cur.execute('''
        INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
        SELECT 'admin', 'admin@survey.com', ?, 'admin123', 'admin', 'Администратор', 1
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'admin')
    ''', (admin_hash,))
    
    teacher_hash = hashlib.sha256("teacher123".encode()).hexdigest()
    cur.execute('''
        INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
        SELECT 'teacher', 'teacher@survey.com', ?, 'teacher123', 'teacher', 'Преподаватель', 1
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'teacher')
    ''', (teacher_hash,))
    
    student_hash = hashlib.sha256("student123".encode()).hexdigest()
    cur.execute('''
        INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
        SELECT 'student', 'student@survey.com', ?, 'student123', 'student', 'Студент', 1
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'student')
    ''', (student_hash,))
    
    conn.commit()
    conn.close()
    print("✅ SQLite база данных инициализирована!")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Требуется авторизация'}), 401
        return f(*args, **kwargs)
    return decorated_function

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
        user_norm = normalize_text(str(user_answer))
        correct_norm = normalize_text(str(correct_answer))
        return user_norm == correct_norm
    elif question_type == 'choice':
        user_norm = normalize_text(str(user_answer))
        correct_norm = normalize_text(str(correct_answer))
        return user_norm == correct_norm
    elif question_type == 'checkbox':
        user_set = set(normalize_text(x) for x in str(user_answer).split(','))
        correct_set = set(normalize_text(x) for x in str(correct_answer).split(','))
        return user_set == correct_set
    
    return False

# ===== МАРШРУТЫ =====

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

# ===== API =====

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
            return jsonify({'error': 'Заполните все обязательные поля!'}), 400
        
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
        if cur.fetchone():
            conn.close()
            return jsonify({'error': 'Пользователь уже существует!'}), 400
        
        password_hash = hash_password(password)
        cur.execute('''
            INSERT INTO users (username, email, password_hash, role, full_name, is_active, plain_password)
            VALUES (?, ?, ?, ?, ?, 1, ?) RETURNING id
        ''', (username, email, password_hash, role, full_name, password))
        
        user_id = cur.lastrowid
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Регистрация успешна!', 'user_id': user_id})
    
    except Exception as e:
        print(f"Register Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.json
        username = data.get('username')
        password = data.get('password')
        
        conn = get_db()
        cur = conn.cursor()
        
        password_hash = hash_password(password)
        cur.execute('''
            SELECT * FROM users 
            WHERE (username = ? OR email = ?) AND password_hash = ? AND is_active = 1
        ''', (username, username, password_hash))
        
        user = cur.fetchone()
        conn.close()
        
        if not user:
            return jsonify({'error': 'Неверные учетные данные!'}), 401
        
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        session['full_name'] = user['full_name']
        
        return jsonify({
            'success': True, 
            'message': 'Вход выполнен!',
            'role': user['role'],
            'redirect': '/student_dashboard'
        })
    
    except Exception as e:
        print(f"Login Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/logout')
def logout():
    session.clear()
    return jsonify({'success': True, 'message': 'Выход выполнен!'})

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
            return jsonify({'error': 'Название и вопросы обязательны!'}), 400
        
        code = generate_unique_code()
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            INSERT INTO tests (title, unique_code, created_by_user_id, created_at, is_editable, game_mode, time_per_question)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1, ?, ?) RETURNING id
        ''', (title, code, session['user_id'], game_mode, time_per_question))
        
        test_id = cur.lastrowid
        
        for idx, q in enumerate(questions):
            correct_ans = q.get('correct_answer')
            points = q.get('points', 1)
            options = q.get('options', [])
            
            cur.execute('''
                INSERT INTO test_questions (test_id, question_text, question_type, 
                                            options, order_index, correct_answer, points)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (test_id, q.get('text'), q.get('type', 'text'), 
                  json.dumps(options) if options else None, 
                  idx, correct_ans, points))
        
        conn.commit()
        
        # Генерация QR кода
        qr_data = f"{request.host_url}take_test?code={code}"
        qr_img = qrcode.make(qr_data)
        buffered = io.BytesIO()
        qr_img.save(buffered, format="PNG")
        qr_base64 = base64.b64encode(buffered.getvalue()).decode()
        
        # Также создаем QR для игрового режима
        game_qr_data = f"{request.host_url}game_player/{code}"
        game_qr_img = qrcode.make(game_qr_data)
        game_buffered = io.BytesIO()
        game_qr_img.save(game_buffered, format="PNG")
        game_qr_base64 = base64.b64encode(game_buffered.getvalue()).decode()
        
        conn.close()
        
        return jsonify({
            'success': True, 
            'message': 'Тест создан!', 
            'code': code,
            'qr_code': qr_base64,
            'game_qr_code': game_qr_base64,
            'test_id': test_id
        })
    
    except Exception as e:
        print(f"Error creating test: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_test_by_code', methods=['POST'])
def get_test_by_code():
    try:
        data = request.json
        code = data.get('code')
        
        if not code:
            return jsonify({'error': 'Введите код теста'}), 400
        
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT id, title, unique_code, created_by_user_id, game_mode, time_per_question
            FROM tests WHERE unique_code = ?
        ''', (code.upper(),))
        
        test = cur.fetchone()
        
        if not test:
            conn.close()
            return jsonify({'error': 'Тест не найден'}), 404
        
        cur.execute('''
            SELECT id, question_text, question_type, options, correct_answer, points, order_index
            FROM test_questions WHERE test_id = ? ORDER BY order_index
        ''', (test['id'],))
        
        questions = cur.fetchall()
        
        result_questions = []
        for q in questions:
            q_dict = dict(q)
            if q_dict['options']:
                try:
                    q_dict['options'] = json.loads(q_dict['options'])
                except:
                    q_dict['options'] = []
            else:
                q_dict['options'] = []
            result_questions.append(q_dict)
        
        conn.close()
        
        return jsonify({
            'success': True,
            'test': {
                'id': test['id'],
                'title': test['title'],
                'code': test['unique_code'],
                'created_by_user_id': test['created_by_user_id'],
                'game_mode': test['game_mode'],
                'time_per_question': test['time_per_question'],
                'questions': result_questions
            }
        })
    
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/submit_test', methods=['POST'])
@login_required
def submit_test():
    try:
        data = request.json
        test_id = data.get('test_id')
        answers = data.get('answers', {})
        game_session_id = data.get('game_session_id')
        
        if not test_id:
            return jsonify({'error': 'ID теста обязателен!'}), 400
        
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT id, correct_answer, question_type, points, question_text
            FROM test_questions WHERE test_id = ?
        ''', (test_id,))
        
        questions = cur.fetchall()
        
        total_score = 0
        max_possible_score = 0
        detailed_results = []
        
        for q in questions:
            max_possible_score += q['points']
            user_answer = answers.get(str(q['id']))
            
            is_correct = check_answer(q['question_type'], user_answer, q['correct_answer'])
            
            if is_correct:
                total_score += q['points']
            
            detailed_results.append({
                'question_id': q['id'],
                'question_text': q['question_text'],
                'user_answer': user_answer or '(не указан)',
                'correct_answer': q['correct_answer'] or '(нет ответа)',
                'points_earned': q['points'] if is_correct else 0,
                'max_points': q['points'],
                'is_correct': is_correct,
                'question_type': q['question_type']
            })
        
        cur.execute('''
            INSERT INTO test_results (test_id, user_id, score, max_score, completed_at, answers_json, game_session_id)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?) RETURNING id
        ''', (test_id, session['user_id'], total_score, max_possible_score, json.dumps(detailed_results, ensure_ascii=False), game_session_id))
        
        result_id = cur.lastrowid
        
        for detail in detailed_results:
            cur.execute('''
                INSERT INTO test_answers_detail (result_id, question_id, user_answer, is_correct, points_earned)
                VALUES (?, ?, ?, ?, ?)
            ''', (result_id, detail['question_id'], detail['user_answer'], 1 if detail['is_correct'] else 0, detail['points_earned']))
        
        conn.commit()
        conn.close()
        
        percentage = (total_score / max_possible_score * 100) if max_possible_score > 0 else 0
        
        return jsonify({
            'success': True, 
            'message': 'Тест пройден! Результат сохранен.',
            'score': total_score,
            'max_score': max_possible_score,
            'percentage': round(percentage, 1),
            'result_id': result_id,
            'detailed_results': detailed_results
        })
    
    except Exception as e:
        print(f"Error submitting test: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_my_results_detail/<int:result_id>', methods=['GET'])
@login_required
def get_my_results_detail(result_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT tr.id, tr.score, tr.max_score, tr.completed_at, t.title, tr.answers_json
            FROM test_results tr
            JOIN tests t ON tr.test_id = t.id
            WHERE tr.id = ? AND tr.user_id = ?
        ''', (result_id, session['user_id']))
        
        result = cur.fetchone()
        
        if not result:
            conn.close()
            return jsonify({'error': 'Результат не найден'}), 404
        
        cur.execute('''
            SELECT tad.*, tq.question_text, tq.question_type, tq.points, tq.correct_answer
            FROM test_answers_detail tad
            JOIN test_questions tq ON tad.question_id = tq.id
            WHERE tad.result_id = ?
        ''', (result_id,))
        
        details = [dict(row) for row in cur.fetchall()]
        
        conn.close()
        
        return jsonify({
            'success': True,
            'result': dict(result),
            'details': details
        })
    
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_my_tests', methods=['GET'])
@login_required
def get_my_tests():
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT t.id, t.title, t.unique_code, t.created_at, t.game_mode,
                   (SELECT COUNT(*) FROM test_results WHERE test_id = t.id) as attempts_count
            FROM tests t
            WHERE t.created_by_user_id = ?
            ORDER BY t.created_at DESC
        ''', (session['user_id'],))
        
        tests = [dict(row) for row in cur.fetchall()]
        conn.close()
        
        return jsonify(tests)
    
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/my_results', methods=['GET'])
@login_required
def my_results():
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT tr.id, tr.score, tr.max_score, tr.completed_at,
                   t.title, t.unique_code, t.game_mode,
                   ROUND((tr.score * 100.0 / NULLIF(tr.max_score, 0)), 1) as percentage,
                   tr.game_session_id
            FROM test_results tr
            JOIN tests t ON tr.test_id = t.id
            WHERE tr.user_id = ?
            ORDER BY tr.completed_at DESC
        ''', (session['user_id'],))
        
        results = [dict(row) for row in cur.fetchall()]
        
        avg_percentage = 0
        best_percentage = 0
        if results:
            percentages = [r['percentage'] or 0 for r in results]
            avg_percentage = sum(percentages) / len(percentages)
            best_percentage = max(percentages)
        
        conn.close()
        
        return jsonify({
            'results': results,
            'stats': {
                'total_tests': len(results),
                'avg_percentage': round(avg_percentage, 1),
                'best_percentage': round(best_percentage, 1)
            }
        })
    
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

# ===== SOCKET.IO События для игры =====

@socketio.on('join_game_host')
def handle_join_game_host(data):
    """Ведущий подключается к игре"""
    game_code = data.get('game_code')
    if game_code:
        join_room(game_code)
        
        # Инициализируем игру
        if game_code not in active_games:
            active_games[game_code] = {
                'players': {},
                'current_question': -1,
                'is_active': False,
                'question_start_time': None,
                'question_results': {},
                'time_per_question': data.get('time_per_question', 10),
                'total_questions': data.get('total_questions', 0)
            }
        
        active_games[game_code]['host_sid'] = request.sid
        emit('host_connected', {'message': 'Подключен как ведущий'}, room=request.sid)

@socketio.on('join_game_player')
def handle_join_game_player(data):
    """Игрок подключается к игре"""
    game_code = data.get('game_code')
    user_id = data.get('user_id')
    username = data.get('username')
    
    if game_code and game_code in active_games:
        join_room(game_code)
        
        # Добавляем игрока
        if user_id not in active_games[game_code]['players']:
            active_games[game_code]['players'][user_id] = {
                'username': username,
                'user_id': user_id,
                'answers': {},
                'score': 0,
                'connected': True
            }
        
        active_games[game_code]['players'][user_id]['sid'] = request.sid
        
        # Обновляем всех игроков
        players_list = [
            {'username': p['username'], 'user_id': p['user_id']} 
            for p in active_games[game_code]['players'].values()
        ]
        
        emit('players_update', {'players': players_list}, room=game_code)
        emit('player_joined', {'username': username, 'count': len(players_list)}, room=game_code)
        
        # Если игра уже идет, отправляем текущий вопрос
        game = active_games[game_code]
        if game['is_active'] and game['current_question'] >= 0:
            emit('question_start', {
                'question_index': game['current_question'],
                'question': data.get('current_question'),
                'time_left': game['time_per_question']
            }, room=request.sid)

@socketio.on('start_game')
def handle_start_game(data):
    """Ведущий начинает игру"""
    game_code = data.get('game_code')
    questions = data.get('questions', [])
    time_per_question = data.get('time_per_question', 10)
    
    if game_code in active_games:
        game = active_games[game_code]
        game['questions'] = questions
        game['time_per_question'] = time_per_question
        game['total_questions'] = len(questions)
        game['is_active'] = True
        game['current_question'] = -1
        
        emit('game_started', {'total_questions': len(questions)}, room=game_code)
        
        # Запускаем первый вопрос через 3 секунды
        def start_first_question():
            socketio.emit('next_question', room=game_code)
        
        threading.Timer(3, start_first_question).start()

@socketio.on('next_question')
def handle_next_question(data=None):
    """Переход к следующему вопросу"""
    game_code = data.get('game_code') if data else None
    
    # Ищем game_code из комнаты
    if not game_code:
        for room, game in active_games.items():
            if request.sid == game.get('host_sid'):
                game_code = room
                break
    
    if game_code and game_code in active_games:
        game = active_games[game_code]
        game['current_question'] += 1
        
        if game['current_question'] < game['total_questions']:
            question = game['questions'][game['current_question']]
            game['question_start_time'] = time.time()
            game['question_results'] = {}
            
            # Отправляем вопрос всем игрокам
            emit('question_start', {
                'question_index': game['current_question'],
                'question': {
                    'text': question.get('text'),
                    'type': question.get('type'),
                    'options': question.get('options', []),
                    'points': question.get('points', 1)
                },
                'time_left': game['time_per_question']
            }, room=game_code)
            
            # Запускаем таймер на ответы
            def end_question():
                if game_code in active_games:
                    socketio.emit('time_up', room=game_code)
                    # Ждем 2 секунды и переходим к результатам
                    threading.Timer(2, lambda: show_question_results(game_code)).start()
            
            threading.Timer(game['time_per_question'], end_question).start()
        else:
            # Игра закончена
            end_game(game_code)

@socketio.on('submit_answer')
def handle_submit_answer(data):
    """Игрок отправляет ответ"""
    game_code = data.get('game_code')
    user_id = data.get('user_id')
    question_index = data.get('question_index')
    answer = data.get('answer')
    
    if game_code in active_games:
        game = active_games[game_code]
        
        # Проверяем, не истекло ли время
        if game['question_start_time']:
            time_elapsed = time.time() - game['question_start_time']
            if time_elapsed > game['time_per_question']:
                emit('answer_timeout', {'message': 'Время вышло!'}, room=request.sid)
                return
        
        # Проверяем правильность ответа
        if question_index < len(game['questions']):
            question = game['questions'][question_index]
            correct_answer = question.get('correct_answer', '')
            is_correct = check_answer(question.get('type'), answer, correct_answer)
            points_earned = question.get('points', 1) if is_correct else 0
            
            # Сохраняем ответ
            if user_id in game['players']:
                game['players'][user_id]['answers'][question_index] = {
                    'answer': answer,
                    'is_correct': is_correct,
                    'points': points_earned
                }
                game['players'][user_id]['score'] += points_earned
            
            # Сохраняем результат вопроса
            if question_index not in game['question_results']:
                game['question_results'][question_index] = {
                    'total': 0,
                    'correct': 0,
                    'players_answers': {}
                }
            
            game['question_results'][question_index]['total'] += 1
            if is_correct:
                game['question_results'][question_index]['correct'] += 1
            
            game['question_results'][question_index]['players_answers'][user_id] = {
                'is_correct': is_correct,
                'answer': answer
            }
            
            emit('answer_received', {'success': True}, room=request.sid)

def show_question_results(game_code):
    """Показывает результаты вопроса всем игрокам и ведущему"""
    if game_code in active_games:
        game = active_games[game_code]
        current_q = game['current_question']
        
        if current_q in game['question_results']:
            results = game['question_results'][current_q]
            
            # Отправляем статистику всем
            emit('question_results', {
                'total_players': results['total'],
                'correct_count': results['correct'],
                'percentage': round(results['correct'] / results['total'] * 100, 1) if results['total'] > 0 else 0
            }, room=game_code)
            
            # Отправляем каждому игроку его результат
            for user_id, player in game['players'].items():
                if current_q in player['answers']:
                    answer_data = player['answers'][current_q]
                    emit('player_question_result', {
                        'is_correct': answer_data['is_correct'],
                        'points_earned': answer_data['points'],
                        'total_score': player['score']
                    }, room=player.get('sid'))
            
            # Через 3 секунды переходим к следующему вопросу
            def next():
                if game_code in active_games and game['is_active']:
                    socketio.emit('next_question', room=game_code)
            
            threading.Timer(3, next).start()

def end_game(game_code):
    """Завершает игру и показывает финальные результаты"""
    if game_code in active_games:
        game = active_games[game_code]
        game['is_active'] = False
        
        # Собираем финальную статистику
        final_results = []
        for user_id, player in game['players'].items():
            final_results.append({
                'username': player['username'],
                'score': player['score'],
                'total_possible': sum(q.get('points', 1) for q in game['questions']),
                'correct_answers': sum(1 for a in player['answers'].values() if a['is_correct'])
            })
        
        # Сортируем по очкам
        final_results.sort(key=lambda x: x['score'], reverse=True)
        
        emit('game_ended', {
            'results': final_results,
            'top_player': final_results[0] if final_results else None
        }, room=game_code)
        
        # Удаляем игру через минуту
        def cleanup():
            if game_code in active_games:
                del active_games[game_code]
        
        threading.Timer(60, cleanup).start()

@socketio.on('leave_game')
def handle_leave_game(data):
    game_code = data.get('game_code')
    user_id = data.get('user_id')
    
    if game_code in active_games and user_id in active_games[game_code]['players']:
        del active_games[game_code]['players'][user_id]
        emit('player_left', {'user_id': user_id}, room=game_code)

# Запуск
if __name__ == '__main__':
    init_db()
    
    print("=" * 50)
    print("🚀 Server: http://localhost:3000")
    print("📁 Database: SQLite (survey.db)")
    print("👨‍💼 Admin login: admin / admin123")
    print("👨‍🏫 Teacher login: teacher / teacher123")
    print("👨‍🎓 Student login: student / student123")
    print("=" * 50)
    socketio.run(app, debug=True, port=3000, host='0.0.0.0', allow_unsafe_werkzeug=True)
