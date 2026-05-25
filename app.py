from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv
import json
import secrets
import qrcode
import io
import base64
from datetime import datetime
from functools import wraps
import hashlib
import sqlite3
import urllib.parse

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))

# ===== ФУНКЦИЯ ПОДКЛЮЧЕНИЯ К БАЗЕ ДАННЫХ =====

def get_db_connection():
    """Подключение к PostgreSQL на Render или SQLite локально"""
    try:
        # Проверяем, есть ли DATABASE_URL (на Render)
        database_url = os.environ.get('DATABASE_URL')
        
        if database_url:
            # На Render - используем PostgreSQL
            # Парсим URL для совместимости с psycopg2
            urllib.parse.uses_netloc.append('postgres')
            url = urllib.parse.urlparse(database_url)
            
            conn = psycopg2.connect(
                host=url.hostname,
                port=url.port,
                database=url.path[1:],
                user=url.username,
                password=url.password,
                cursor_factory=RealDictCursor
            )
            return conn
        else:
            # Локально - используем SQLite
            conn = sqlite3.connect('survey.db')
            conn.row_factory = sqlite3.Row
            return conn
    except Exception as e:
        print(f"DB Error: {e}")
        return None

# ===== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ =====

def init_database():
    """Создание таблиц, если их нет"""
    conn = get_db_connection()
    if not conn:
        print("❌ Не удалось подключиться к базе данных")
        return
    
    cur = conn.cursor()
    
    # Определяем тип базы данных
    is_postgres = 'postgres' in str(type(conn))
    
    if is_postgres:
        # PostgreSQL синтаксис
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                plain_password VARCHAR(255),
                role VARCHAR(50) DEFAULT 'student',
                full_name VARCHAR(255),
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tests (
                id SERIAL PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                unique_code VARCHAR(20) UNIQUE NOT NULL,
                created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_editable BOOLEAN DEFAULT TRUE
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS test_questions (
                id SERIAL PRIMARY KEY,
                test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
                question_text TEXT NOT NULL,
                question_type VARCHAR(50) DEFAULT 'choice',
                options TEXT,
                order_index INTEGER DEFAULT 0,
                correct_answer TEXT,
                points INTEGER DEFAULT 1
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS test_results (
                id SERIAL PRIMARY KEY,
                test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                score INTEGER DEFAULT 0,
                max_score INTEGER DEFAULT 0,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                answers_json TEXT
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS test_answers_detail (
                id SERIAL PRIMARY KEY,
                result_id INTEGER NOT NULL REFERENCES test_results(id) ON DELETE CASCADE,
                question_id INTEGER NOT NULL REFERENCES test_questions(id) ON DELETE CASCADE,
                user_answer TEXT,
                is_correct BOOLEAN DEFAULT FALSE,
                points_earned INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        # SQLite синтаксис
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
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
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
                points INTEGER DEFAULT 1,
                FOREIGN KEY (test_id) REFERENCES tests (id) ON DELETE CASCADE
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
                FOREIGN KEY (test_id) REFERENCES tests (id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (result_id) REFERENCES test_results (id) ON DELETE CASCADE,
                FOREIGN KEY (question_id) REFERENCES test_questions (id) ON DELETE CASCADE
            )
        ''')
    
    # Индексы (общий синтаксис)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tests_code ON tests(unique_code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_results_user ON test_results(user_id)")
    
    # Создаём тестового администратора (пароль: admin123)
    admin_hash = hashlib.sha256("admin123".encode()).hexdigest()
    if is_postgres:
        cur.execute("""
            INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
            SELECT 'admin', 'admin@survey.com', %s, 'admin123', 'admin', 'Администратор', TRUE
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'admin')
        """, (admin_hash,))
    else:
        cur.execute("""
            INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
            SELECT 'admin', 'admin@survey.com', ?, 'admin123', 'admin', 'Администратор', 1
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'admin')
        """, (admin_hash,))
    
    # Создаём тестового преподавателя (пароль: teacher123)
    teacher_hash = hashlib.sha256("teacher123".encode()).hexdigest()
    if is_postgres:
        cur.execute("""
            INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
            SELECT 'teacher', 'teacher@survey.com', %s, 'teacher123', 'teacher', 'Преподаватель', TRUE
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'teacher')
        """, (teacher_hash,))
    else:
        cur.execute("""
            INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
            SELECT 'teacher', 'teacher@survey.com', ?, 'teacher123', 'teacher', 'Преподаватель', 1
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'teacher')
        """, (teacher_hash,))
    
    # Создаём тестового студента (пароль: student123)
    student_hash = hashlib.sha256("student123".encode()).hexdigest()
    if is_postgres:
        cur.execute("""
            INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
            SELECT 'student', 'student@survey.com', %s, 'student123', 'student', 'Студент', TRUE
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'student')
        """, (student_hash,))
    else:
        cur.execute("""
            INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
            SELECT 'student', 'student@survey.com', ?, 'student123', 'student', 'Студент', 1
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'student')
        """, (student_hash,))
    
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована!")

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====

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
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            try:
                if 'postgres' in str(type(conn)):
                    cur.execute("SELECT id FROM tests WHERE unique_code = %s", (code,))
                else:
                    cur.execute("SELECT id FROM tests WHERE unique_code = ?", (code,))
                result = cur.fetchone()
                cur.close()
                conn.close()
                if not result:
                    return code
            except:
                cur.close()
                conn.close()
                return code[:8]
        else:
            return code[:8]

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

# ===== МАРШРУТЫ СТРАНИЦ =====

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

@app.route('/edit_test_page/<int:test_id>')
def edit_test_page(test_id):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('edit_test.html', test_id=test_id, user=session)

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

@app.route('/admin')
def admin():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    return redirect(url_for('create_test_page'))

@app.route('/teacher_dashboard')
def teacher_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    return redirect(url_for('student_dashboard'))

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
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка подключения к БД'}), 500
        
        cur = conn.cursor()
        is_postgres = 'postgres' in str(type(conn))
        
        # Проверка существования пользователя
        if is_postgres:
            cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
        else:
            cur.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
        
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({'error': 'Пользователь уже существует!'}), 400
        
        password_hash = hash_password(password)
        
        if is_postgres:
            cur.execute("""
                INSERT INTO users (username, email, password_hash, role, full_name, is_active, plain_password)
                VALUES (%s, %s, %s, %s, %s, TRUE, %s) RETURNING id
            """, (username, email, password_hash, role, full_name, password))
            user_id = cur.fetchone()[0]
        else:
            cur.execute("""
                INSERT INTO users (username, email, password_hash, role, full_name, is_active, plain_password)
                VALUES (?, ?, ?, ?, ?, 1, ?) RETURNING id
            """, (username, email, password_hash, role, full_name, password))
            user_id = cur.lastrowid
        
        conn.commit()
        cur.close()
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
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка подключения к БД'}), 500
        
        cur = conn.cursor()
        is_postgres = 'postgres' in str(type(conn))
        
        password_hash = hash_password(password)
        
        if is_postgres:
            cur.execute("""
                SELECT * FROM users 
                WHERE (username = %s OR email = %s) AND password_hash = %s AND is_active = TRUE
            """, (username, username, password_hash))
        else:
            cur.execute("""
                SELECT * FROM users 
                WHERE (username = ? OR email = ?) AND password_hash = ? AND is_active = 1
            """, (username, username, password_hash))
        
        user = cur.fetchone()
        cur.close()
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
        
        if not title or not questions:
            return jsonify({'error': 'Название и вопросы обязательны!'}), 400
        
        code = generate_unique_code()
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor()
        is_postgres = 'postgres' in str(type(conn))
        
        if is_postgres:
            cur.execute("""
                INSERT INTO tests (title, unique_code, created_by_user_id, created_at, is_editable)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP, TRUE) RETURNING id
            """, (title, code, session['user_id']))
            test_id = cur.fetchone()[0]
        else:
            cur.execute("""
                INSERT INTO tests (title, unique_code, created_by_user_id, created_at, is_editable)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1) RETURNING id
            """, (title, code, session['user_id']))
            test_id = cur.lastrowid
        
        for idx, q in enumerate(questions):
            correct_ans = q.get('correct_answer')
            points = q.get('points', 1)
            options = q.get('options', [])
            
            if is_postgres:
                cur.execute("""
                    INSERT INTO test_questions (test_id, question_text, question_type, 
                                                options, order_index, correct_answer, points)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (test_id, q.get('text'), q.get('type', 'text'), 
                      json.dumps(options) if options else None, 
                      idx, correct_ans, points))
            else:
                cur.execute("""
                    INSERT INTO test_questions (test_id, question_text, question_type, 
                                                options, order_index, correct_answer, points)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (test_id, q.get('text'), q.get('type', 'text'), 
                      json.dumps(options) if options else None, 
                      idx, correct_ans, points))
        
        conn.commit()
        
        # Генерация QR кода
        qr_data = f"{request.host_url}take_test?code={code}"
        qr_img = qrcode.make(qr_data)
        buffered = io.BytesIO()
        qr_img.save(buffered, format="PNG")
        qr_base64 = base64.b64encode(buffered.getvalue()).decode()
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True, 
            'message': 'Тест создан!', 
            'code': code,
            'qr_code': qr_base64,
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
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка подключения к БД'}), 500
        
        cur = conn.cursor()
        is_postgres = 'postgres' in str(type(conn))
        
        if is_postgres:
            cur.execute("""
                SELECT id, title, unique_code, created_by_user_id
                FROM tests WHERE unique_code = %s
            """, (code.upper(),))
        else:
            cur.execute("""
                SELECT id, title, unique_code, created_by_user_id
                FROM tests WHERE unique_code = ?
            """, (code.upper(),))
        
        test = cur.fetchone()
        
        if not test:
            cur.close()
            conn.close()
            return jsonify({'error': 'Тест не найден'}), 404
        
        if is_postgres:
            cur.execute("""
                SELECT id, question_text, question_type, options, correct_answer, points, order_index
                FROM test_questions WHERE test_id = %s ORDER BY order_index
            """, (test['id'],))
        else:
            cur.execute("""
                SELECT id, question_text, question_type, options, correct_answer, points, order_index
                FROM test_questions WHERE test_id = ? ORDER BY order_index
            """, (test['id'],))
        
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
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'test': {
                'id': test['id'],
                'title': test['title'],
                'code': test['unique_code'],
                'created_by_user_id': test['created_by_user_id'],
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
        
        if not test_id:
            return jsonify({'error': 'ID теста обязателен!'}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor()
        is_postgres = 'postgres' in str(type(conn))
        
        # Получаем вопросы теста
        if is_postgres:
            cur.execute("""
                SELECT id, correct_answer, question_type, points, question_text
                FROM test_questions WHERE test_id = %s
            """, (test_id,))
        else:
            cur.execute("""
                SELECT id, correct_answer, question_type, points, question_text
                FROM test_questions WHERE test_id = ?
            """, (test_id,))
        
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
        
        # Сохраняем результат
        if is_postgres:
            cur.execute("""
                INSERT INTO test_results (test_id, user_id, score, max_score, completed_at, answers_json)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s) RETURNING id
            """, (test_id, session['user_id'], total_score, max_possible_score, json.dumps(detailed_results, ensure_ascii=False)))
            result_id = cur.fetchone()[0]
        else:
            cur.execute("""
                INSERT INTO test_results (test_id, user_id, score, max_score, completed_at, answers_json)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?) RETURNING id
            """, (test_id, session['user_id'], total_score, max_possible_score, json.dumps(detailed_results, ensure_ascii=False)))
            result_id = cur.lastrowid
        
        # Сохраняем детальные ответы
        for detail in detailed_results:
            if is_postgres:
                cur.execute("""
                    INSERT INTO test_answers_detail (result_id, question_id, user_answer, is_correct, points_earned)
                    VALUES (%s, %s, %s, %s, %s)
                """, (result_id, detail['question_id'], detail['user_answer'], detail['is_correct'], detail['points_earned']))
            else:
                cur.execute("""
                    INSERT INTO test_answers_detail (result_id, question_id, user_answer, is_correct, points_earned)
                    VALUES (?, ?, ?, ?, ?)
                """, (result_id, detail['question_id'], detail['user_answer'], 1 if detail['is_correct'] else 0, detail['points_earned']))
        
        conn.commit()
        cur.close()
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
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor()
        is_postgres = 'postgres' in str(type(conn))
        
        if is_postgres:
            cur.execute("""
                SELECT tr.id, tr.score, tr.max_score, tr.completed_at, t.title, tr.answers_json
                FROM test_results tr
                JOIN tests t ON tr.test_id = t.id
                WHERE tr.id = %s AND tr.user_id = %s
            """, (result_id, session['user_id']))
        else:
            cur.execute("""
                SELECT tr.id, tr.score, tr.max_score, tr.completed_at, t.title, tr.answers_json
                FROM test_results tr
                JOIN tests t ON tr.test_id = t.id
                WHERE tr.id = ? AND tr.user_id = ?
            """, (result_id, session['user_id']))
        
        result = cur.fetchone()
        
        if not result:
            cur.close()
            conn.close()
            return jsonify({'error': 'Результат не найден'}), 404
        
        if is_postgres:
            cur.execute("""
                SELECT tad.*, tq.question_text, tq.question_type, tq.points, tq.correct_answer
                FROM test_answers_detail tad
                JOIN test_questions tq ON tad.question_id = tq.id
                WHERE tad.result_id = %s
            """, (result_id,))
        else:
            cur.execute("""
                SELECT tad.*, tq.question_text, tq.question_type, tq.points, tq.correct_answer
                FROM test_answers_detail tad
                JOIN test_questions tq ON tad.question_id = tq.id
                WHERE tad.result_id = ?
            """, (result_id,))
        
        details = [dict(row) for row in cur.fetchall()]
        
        cur.close()
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
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor()
        is_postgres = 'postgres' in str(type(conn))
        
        if is_postgres:
            cur.execute("""
                SELECT t.id, t.title, t.unique_code, t.created_at,
                       (SELECT COUNT(*) FROM test_results WHERE test_id = t.id) as attempts_count
                FROM tests t
                WHERE t.created_by_user_id = %s
                ORDER BY t.created_at DESC
            """, (session['user_id'],))
        else:
            cur.execute("""
                SELECT t.id, t.title, t.unique_code, t.created_at,
                       (SELECT COUNT(*) FROM test_results WHERE test_id = t.id) as attempts_count
                FROM tests t
                WHERE t.created_by_user_id = ?
                ORDER BY t.created_at DESC
            """, (session['user_id'],))
        
        tests = [dict(row) for row in cur.fetchall()]
        cur.close()
        conn.close()
        
        return jsonify(tests)
    
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_all_tests', methods=['GET'])
@login_required
def get_all_tests():
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor()
        is_postgres = 'postgres' in str(type(conn))
        
        if is_postgres:
            cur.execute("""
                SELECT t.id, t.title, t.unique_code, t.created_at, u.username as creator_name,
                       (SELECT COUNT(*) FROM test_results WHERE test_id = t.id) as attempts_count
                FROM tests t
                LEFT JOIN users u ON t.created_by_user_id = u.id
                ORDER BY t.created_at DESC
            """)
        else:
            cur.execute("""
                SELECT t.id, t.title, t.unique_code, t.created_at, u.username as creator_name,
                       (SELECT COUNT(*) FROM test_results WHERE test_id = t.id) as attempts_count
                FROM tests t
                LEFT JOIN users u ON t.created_by_user_id = u.id
                ORDER BY t.created_at DESC
            """)
        
        tests = [dict(row) for row in cur.fetchall()]
        cur.close()
        conn.close()
        
        return jsonify(tests)
    
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_test_for_edit/<int:test_id>', methods=['GET'])
@login_required
def get_test_for_edit(test_id):
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor()
        is_postgres = 'postgres' in str(type(conn))
        
        if is_postgres:
            cur.execute("""
                SELECT id, title, unique_code FROM tests 
                WHERE id = %s AND created_by_user_id = %s
            """, (test_id, session['user_id']))
        else:
            cur.execute("""
                SELECT id, title, unique_code FROM tests 
                WHERE id = ? AND created_by_user_id = ?
            """, (test_id, session['user_id']))
        
        test = cur.fetchone()
        
        if not test:
            cur.close()
            conn.close()
            return jsonify({'error': 'Тест не найден или у вас нет прав'}), 404
        
        if is_postgres:
            cur.execute("""
                SELECT id, question_text, question_type, options, correct_answer, points, order_index
                FROM test_questions WHERE test_id = %s ORDER BY order_index
            """, (test_id,))
        else:
            cur.execute("""
                SELECT id, question_text, question_type, options, correct_answer, points, order_index
                FROM test_questions WHERE test_id = ? ORDER BY order_index
            """, (test_id,))
        
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
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'test': dict(test),
            'questions': questions
        })
    
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/update_test/<int:test_id>', methods=['PUT'])
@login_required
def update_test(test_id):
    try:
        data = request.json
        title = data.get('title')
        questions = data.get('questions', [])
        
        if not title or not questions:
            return jsonify({'error': 'Название и вопросы обязательны!'}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor()
        is_postgres = 'postgres' in str(type(conn))
        
        if is_postgres:
            cur.execute("""
                SELECT id FROM tests WHERE id = %s AND created_by_user_id = %s AND is_editable = TRUE
            """, (test_id, session['user_id']))
        else:
            cur.execute("""
                SELECT id FROM tests WHERE id = ? AND created_by_user_id = ? AND is_editable = 1
            """, (test_id, session['user_id']))
        
        if not cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({'error': 'У вас нет прав на редактирование этого теста'}), 403
        
        # Обновляем название
        if is_postgres:
            cur.execute("UPDATE tests SET title = %s WHERE id = %s", (title, test_id))
            cur.execute("DELETE FROM test_questions WHERE test_id = %s", (test_id,))
        else:
            cur.execute("UPDATE tests SET title = ? WHERE id = ?", (title, test_id))
            cur.execute("DELETE FROM test_questions WHERE test_id = ?", (test_id,))
        
        # Добавляем новые вопросы
        for idx, q in enumerate(questions):
            correct_ans = q.get('correct_answer')
            points = q.get('points', 1)
            options = q.get('options', [])
            
            if is_postgres:
                cur.execute("""
                    INSERT INTO test_questions (test_id, question_text, question_type, 
                                                options, order_index, correct_answer, points)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (test_id, q.get('text'), q.get('type', 'text'), 
                      json.dumps(options) if options else None, 
                      idx, correct_ans, points))
            else:
                cur.execute("""
                    INSERT INTO test_questions (test_id, question_text, question_type, 
                                                options, order_index, correct_answer, points)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (test_id, q.get('text'), q.get('type', 'text'), 
                      json.dumps(options) if options else None, 
                      idx, correct_ans, points))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Тест обновлен!'})
    
    except Exception as e:
        print(f"Error updating test: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/my_results', methods=['GET'])
@login_required
def my_results():
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor()
        is_postgres = 'postgres' in str(type(conn))
        
        if is_postgres:
            cur.execute("""
                SELECT tr.id, tr.score, tr.max_score, tr.completed_at,
                       t.title, t.unique_code,
                       ROUND((tr.score::numeric / NULLIF(tr.max_score, 0)::numeric) * 100, 1) as percentage
                FROM test_results tr
                JOIN tests t ON tr.test_id = t.id
                WHERE tr.user_id = %s
                ORDER BY tr.completed_at DESC
            """, (session['user_id'],))
        else:
            cur.execute("""
                SELECT tr.id, tr.score, tr.max_score, tr.completed_at,
                       t.title, t.unique_code,
                       ROUND((tr.score * 100.0 / NULLIF(tr.max_score, 0)), 1) as percentage
                FROM test_results tr
                JOIN tests t ON tr.test_id = t.id
                WHERE tr.user_id = ?
                ORDER BY tr.completed_at DESC
            """, (session['user_id'],))
        
        results = [dict(row) for row in cur.fetchall()]
        
        avg_percentage = 0
        best_percentage = 0
        if results:
            percentages = [r['percentage'] or 0 for r in results]
            avg_percentage = sum(percentages) / len(percentages)
            best_percentage = max(percentages)
        
        cur.close()
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

# ===== ЗАПУСК =====

if __name__ == '__main__':
    # Инициализируем базу данных
    init_database()
    
    print("=" * 50)
    print("🚀 Server: http://localhost:3000")
    print("📁 База данных: PostgreSQL (Render) или SQLite (локально)")
    print("👨‍💼 Admin: admin / admin123")
    print("👨‍🏫 Teacher: teacher / teacher123")
    print("👨‍🎓 Student: student / student123")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 3000))
    app.run(debug=False, port=port, host='0.0.0.0')
