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

# Определяем тип базы данных
DATABASE_URL = os.getenv('DATABASE_URL', '')
USE_POSTGRES = DATABASE_URL.startswith('postgres')

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    
    def get_db():
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    
    def init_db():
        conn = get_db()
        cur = conn.cursor()
        
        # Таблица пользователей
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
        
        # Создаём индексы
        cur.execute('CREATE INDEX IF NOT EXISTS idx_tests_code ON tests(unique_code)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_results_user ON test_results(user_id)')
        
        # Создаём тестовых пользователей
        admin_hash = hashlib.sha256("admin123".encode()).hexdigest()
        cur.execute('''
            INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
            SELECT 'admin', 'admin@survey.com', %s, 'admin123', 'admin', 'Администратор', 1
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'admin')
        ''', (admin_hash,))
        
        teacher_hash = hashlib.sha256("teacher123".encode()).hexdigest()
        cur.execute('''
            INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
            SELECT 'teacher', 'teacher@survey.com', %s, 'teacher123', 'teacher', 'Преподаватель', 1
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'teacher')
        ''', (teacher_hash,))
        
        student_hash = hashlib.sha256("student123".encode()).hexdigest()
        cur.execute('''
            INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
            SELECT 'student', 'student@survey.com', %s, 'student123', 'student', 'Студент', 1
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'student')
        ''', (student_hash,))
        
        conn.commit()
        conn.close()
        print("✅ PostgreSQL база данных инициализирована!")
else:
    DATABASE = 'survey.db'
    
    def get_db():
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_db():
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
        if USE_POSTGRES:
            cur.execute("SELECT id FROM tests WHERE unique_code = %s", (code,))
        else:
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

# ===== API МАРШРУТЫ =====

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
        
        if USE_POSTGRES:
            cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
        else:
            cur.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
        
        if cur.fetchone():
            conn.close()
            return jsonify({'error': 'Пользователь уже существует!'}), 400
        
        password_hash = hash_password(password)
        if USE_POSTGRES:
            cur.execute('''
                INSERT INTO users (username, email, password_hash, role, full_name, is_active, plain_password)
                VALUES (%s, %s, %s, %s, %s, 1, %s) RETURNING id
            ''', (username, email, password_hash, role, full_name, password))
            user_id = cur.fetchone()[0]
        else:
            cur.execute('''
                INSERT INTO users (username, email, password_hash, role, full_name, is_active, plain_password)
                VALUES (?, ?, ?, ?, ?, 1, ?)
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
        if USE_POSTGRES:
            cur.execute('''
                SELECT * FROM users 
                WHERE (username = %s OR email = %s) AND password_hash = %s AND is_active = 1
            ''', (username, username, password_hash))
        else:
            cur.execute('''
                SELECT * FROM users 
                WHERE (username = ? OR email = ?) AND password_hash = ? AND is_active = 1
            ''', (username, username, password_hash))
        
        user = cur.fetchone()
        conn.close()
        
        if not user:
            return jsonify({'error': 'Неверные учетные данные!'}), 401
        
        session['user_id'] = user[0] if USE_POSTGRES else user['id']
        session['username'] = user[1] if USE_POSTGRES else user['username']
        session['role'] = user[5] if USE_POSTGRES else user['role']
        session['full_name'] = user[6] if USE_POSTGRES else user['full_name']
        
        return jsonify({
            'success': True, 
            'message': 'Вход выполнен!',
            'role': session['role'],
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
        
        if USE_POSTGRES:
            cur.execute('''
                INSERT INTO tests (title, unique_code, created_by_user_id, created_at, is_editable, game_mode, time_per_question)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP, 1, %s, %s) RETURNING id
            ''', (title, code, session['user_id'], game_mode, time_per_question))
            test_id = cur.fetchone()[0]
        else:
            cur.execute('''
                INSERT INTO tests (title, unique_code, created_by_user_id, created_at, is_editable, game_mode, time_per_question)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1, ?, ?)
            ''', (title, code, session['user_id'], game_mode, time_per_question))
            test_id = cur.lastrowid
        
        for idx, q in enumerate(questions):
            correct_ans = q.get('correct_answer')
            points = q.get('points', 1)
            options = q.get('options', [])
            
            if USE_POSTGRES:
                cur.execute('''
                    INSERT INTO test_questions (test_id, question_text, question_type, 
                                                options, order_index, correct_answer, points)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''', (test_id, q.get('text'), q.get('type', 'text'), 
                      json.dumps(options) if options else None, 
                      idx, correct_ans, points))
            else:
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
        
        # QR для игрового режима
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
        
        if USE_POSTGRES:
            cur.execute('''
                SELECT id, title, unique_code, created_by_user_id, game_mode, time_per_question
                FROM tests WHERE unique_code = %s
            ''', (code.upper(),))
        else:
            cur.execute('''
                SELECT id, title, unique_code, created_by_user_id, game_mode, time_per_question
                FROM tests WHERE unique_code = ?
            ''', (code.upper(),))
        
        test_row = cur.fetchone()
        
        if not test_row:
            conn.close()
            return jsonify({'error': 'Тест не найден'}), 404
        
        if USE_POSTGRES:
            test = {
                'id': test_row[0],
                'title': test_row[1],
                'unique_code': test_row[2],
                'created_by_user_id': test_row[3],
                'game_mode': test_row[4],
                'time_per_question': test_row[5]
            }
        else:
            test = dict(test_row)
        
        if USE_POSTGRES:
            cur.execute('''
                SELECT id, question_text, question_type, options, correct_answer, points, order_index
                FROM test_questions WHERE test_id = %s ORDER BY order_index
            ''', (test['id'],))
        else:
            cur.execute('''
                SELECT id, question_text, question_type, options, correct_answer, points, order_index
                FROM test_questions WHERE test_id = ? ORDER BY order_index
            ''', (test['id'],))
        
        questions_rows = cur.fetchall()
        
        result_questions = []
        for row in questions_rows:
            if USE_POSTGRES:
                q_dict = {
                    'id': row[0],
                    'question_text': row[1],
                    'question_type': row[2],
                    'options': row[3],
                    'correct_answer': row[4],
                    'points': row[5],
                    'order_index': row[6]
                }
            else:
                q_dict = dict(row)
            
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
        
        if USE_POSTGRES:
            cur.execute('''
                SELECT id, correct_answer, question_type, points, question_text
                FROM test_questions WHERE test_id = %s
            ''', (test_id,))
        else:
            cur.execute('''
                SELECT id, correct_answer, question_type, points, question_text
                FROM test_questions WHERE test_id = ?
            ''', (test_id,))
        
        questions_rows = cur.fetchall()
        
        questions = []
        for row in questions_rows:
            if USE_POSTGRES:
                questions.append({
                    'id': row[0],
                    'correct_answer': row[1],
                    'question_type': row[2],
                    'points': row[3],
                    'question_text': row[4]
                })
            else:
                questions.append(dict(row))
        
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
        
        if USE_POSTGRES:
            cur.execute('''
                INSERT INTO test_results (test_id, user_id, score, max_score, completed_at, answers_json, game_session_id)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s) RETURNING id
            ''', (test_id, session['user_id'], total_score, max_possible_score, json.dumps(detailed_results, ensure_ascii=False), game_session_id))
            result_id = cur.fetchone()[0]
        else:
            cur.execute('''
                INSERT INTO test_results (test_id, user_id, score, max_score, completed_at, answers_json, game_session_id)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
            ''', (test_id, session['user_id'], total_score, max_possible_score, json.dumps(detailed_results, ensure_ascii=False), game_session_id))
            result_id = cur.lastrowid
        
        for detail in detailed_results:
            if USE_POSTGRES:
                cur.execute('''
                    INSERT INTO test_answers_detail (result_id, question_id, user_answer, is_correct, points_earned)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (result_id, detail['question_id'], detail['user_answer'], 1 if detail['is_correct'] else 0, detail['points_earned']))
            else:
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
        
        if USE_POSTGRES:
            cur.execute('''
                SELECT tr.id, tr.score, tr.max_score, tr.completed_at, t.title, tr.answers_json
                FROM test_results tr
                JOIN tests t ON tr.test_id = t.id
                WHERE tr.id = %s AND tr.user_id = %s
            ''', (result_id, session['user_id']))
        else:
            cur.execute('''
                SELECT tr.id, tr.score, tr.max_score, tr.completed_at, t.title, tr.answers_json
                FROM test_results tr
                JOIN tests t ON tr.test_id = t.id
                WHERE tr.id = ? AND tr.user_id = ?
            ''', (result_id, session['user_id']))
        
        result_row = cur.fetchone()
        
        if not result_row:
            conn.close()
            return jsonify({'error': 'Результат не найден'}), 404
        
        if USE_POSTGRES:
            result = {
                'id': result_row[0],
                'score': result_row[1],
                'max_score': result_row[2],
                'completed_at': result_row[3],
                'title': result_row[4],
                'answers_json': result_row[5]
            }
        else:
            result = dict(result_row)
        
        if USE_POSTGRES:
            cur.execute('''
                SELECT tad.*, tq.question_text, tq.question_type, tq.points, tq.correct_answer
                FROM test_answers_detail tad
                JOIN test_questions tq ON tad.question_id = tq.id
                WHERE tad.result_id = %s
            ''', (result_id,))
        else:
            cur.execute('''
                SELECT tad.*, tq.question_text, tq.question_type, tq.points, tq.correct_answer
                FROM test_answers_detail tad
                JOIN test_questions tq ON tad.question_id = tq.id
                WHERE tad.result_id = ?
            ''', (result_id,))
        
        details_rows = cur.fetchall()
        details = []
        for row in details_rows:
            if USE_POSTGRES:
                details.append(dict(zip(['id', 'result_id', 'question_id', 'user_answer', 'is_correct', 'points_earned', 'created_at', 'question_text', 'question_type', 'points', 'correct_answer'], row)))
            else:
                details.append(dict(row))
        
        conn.close()
        
        return jsonify({
            'success': True,
            'result': result,
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
        
        if USE_POSTGRES:
            cur.execute('''
                SELECT t.id, t.title, t.unique_code, t.created_at, t.game_mode,
                       (SELECT COUNT(*) FROM test_results WHERE test_id = t.id) as attempts_count
                FROM tests t
                WHERE t.created_by_user_id = %s
                ORDER BY t.created_at DESC
            ''', (session['user_id'],))
        else:
            cur.execute('''
                SELECT t.id, t.title, t.unique_code, t.created_at, t.game_mode,
                       (SELECT COUNT(*) FROM test_results WHERE test_id = t.id) as attempts_count
                FROM tests t
                WHERE t.created_by_user_id = ?
                ORDER BY t.created_at DESC
            ''', (session['user_id'],))
        
        rows = cur.fetchall()
        tests = []
        for row in rows:
            if USE_POSTGRES:
                tests.append({
                    'id': row[0],
                    'title': row[1],
                    'unique_code': row[2],
                    'created_at': row[3],
                    'game_mode': row[4],
                    'attempts_count': row[5]
                })
            else:
                tests.append(dict(row))
        
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
        
        if USE_POSTGRES:
            cur.execute('''
                SELECT tr.id, tr.score, tr.max_score, tr.completed_at,
                       t.title, t.unique_code, t.game_mode,
                       ROUND((tr.score * 100.0 / NULLIF(tr.max_score, 0)), 1) as percentage,
                       tr.game_session_id
                FROM test_results tr
                JOIN tests t ON tr.test_id = t.id
                WHERE tr.user_id = %s
                ORDER BY tr.completed_at DESC
            ''', (session['user_id'],))
        else:
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
        
        rows = cur.fetchall()
        results = []
        for row in rows:
            if USE_POSTGRES:
                results.append({
                    'id': row[0],
                    'score': row[1],
                    'max_score': row[2],
                    'completed_at': row[3],
                    'title': row[4],
                    'unique_code': row[5],
                    'game_mode': row[6],
                    'percentage': row[7],
                    'game_session_id': row[8]
                })
            else:
                results.append(dict(row))
        
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

# ===== SOCKET.IO СОБЫТИЯ (оставляем без изменений из предыдущей версии) =====
# ... (весь код socketio событий остается таким же как в предыдущей версии)

# Запуск
if __name__ == '__main__':
    init_db()
    
    port = int(os.getenv('PORT', 3000))
    print("=" * 50)
    print("🚀 Server starting...")
    print("=" * 50)
    socketio.run(app, debug=False, port=port, host='0.0.0.0')
