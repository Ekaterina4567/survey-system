from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import psycopg2
from psycopg2.extras import RealDictCursor
import os
import json
import secrets
import qrcode
import io
import base64
from datetime import datetime
from functools import wraps
import hashlib
import urllib.parse

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))

# ===== ФУНКЦИЯ ПОДКЛЮЧЕНИЯ К БАЗЕ ДАННЫХ =====

def get_db_connection():
    """Подключение к PostgreSQL на Render"""
    try:
        database_url = os.environ.get('DATABASE_URL')
        if not database_url:
            print("❌ DATABASE_URL не установлен")
            return None
        
        # Парсим URL
        urllib.parse.uses_netloc.append('postgres')
        url = urllib.parse.urlparse(database_url)
        
        conn = psycopg2.connect(
            host=url.hostname,
            port=url.port,
            database=url.path[1:],
            user=url.username,
            password=url.password
        )
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
    
    # Таблица пользователей
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
    
    # Таблица тестов
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
    
    # Таблица вопросов
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
    
    # Таблица результатов
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
    
    # Таблица детальных ответов
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
    
    # Индексы
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tests_code ON tests(unique_code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_results_user ON test_results(user_id)")
    
    # Создаём тестового администратора (пароль: admin123)
    admin_hash = hashlib.sha256("admin123".encode()).hexdigest()
    cur.execute("""
        INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
        SELECT 'admin', 'admin@survey.com', %s, 'admin123', 'admin', 'Администратор', TRUE
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'admin')
    """, (admin_hash,))
    
    # Создаём тестового преподавателя (пароль: teacher123)
    teacher_hash = hashlib.sha256("teacher123".encode()).hexdigest()
    cur.execute("""
        INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
        SELECT 'teacher', 'teacher@survey.com', %s, 'teacher123', 'teacher', 'Преподаватель', TRUE
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'teacher')
    """, (teacher_hash,))
    
    # Создаём тестового студента (пароль: student123)
    student_hash = hashlib.sha256("student123".encode()).hexdigest()
    cur.execute("""
        INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
        SELECT 'student', 'student@survey.com', %s, 'student123', 'student', 'Студент', TRUE
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'student')
    """, (student_hash,))
    
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована!")

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
            cur.execute("SELECT id FROM tests WHERE unique_code = %s", (code,))
            result = cur.fetchone()
            cur.close()
            conn.close()
            if not result:
                return code
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
        return normalize_text(str(user_answer)) == normalize_text(str(correct_answer))
    elif question_type == 'choice':
        return normalize_text(str(user_answer)) == normalize_text(str(correct_answer))
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
        cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({'error': 'Пользователь уже существует!'}), 400
        
        password_hash = hash_password(password)
        cur.execute("""
            INSERT INTO users (username, email, password_hash, role, full_name, is_active, plain_password)
            VALUES (%s, %s, %s, %s, %s, TRUE, %s) RETURNING id
        """, (username, email, password_hash, role, full_name, password))
        
        user_id = cur.fetchone()[0]
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
        
        cur = conn.cursor(cursor_factory=RealDictCursor)
        password_hash = hash_password(password)
        cur.execute("""
            SELECT * FROM users 
            WHERE (username = %s OR email = %s) AND password_hash = %s AND is_active = TRUE
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
        
        return jsonify({'success': True, 'message': 'Вход выполнен!', 'role': user['role'], 'redirect': '/student_dashboard'})
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
        cur.execute("""
            INSERT INTO tests (title, unique_code, created_by_user_id, created_at, is_editable)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP, TRUE) RETURNING id
        """, (title, code, session['user_id']))
        
        test_id = cur.fetchone()[0]
        
        for idx, q in enumerate(questions):
            cur.execute("""
                INSERT INTO test_questions (test_id, question_text, question_type, options, order_index, correct_answer, points)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (test_id, q.get('text'), q.get('type', 'text'), json.dumps(q.get('options', [])), idx, q.get('correct_answer'), q.get('points', 1)))
        
        conn.commit()
        
        qr_data = f"{request.host_url}take_test?code={code}"
        qr_img = qrcode.make(qr_data)
        buffered = io.BytesIO()
        qr_img.save(buffered, format="PNG")
        qr_base64 = base64.b64encode(buffered.getvalue()).decode()
        
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Тест создан!', 'code': code, 'qr_code': qr_base64, 'test_id': test_id})
    except Exception as e:
        print(f"Error creating test: {e}")
        return jsonify({'error': str(e)}), 500

# Добавьте остальные API маршруты аналогично (submit_test, get_test_by_code и т.д.)

if __name__ == '__main__':
    init_database()
    print("=" * 50)
    print("🚀 Server starting...")
    print("👨‍💼 Admin: admin / admin123")
    print("=" * 50)
    port = int(os.environ.get('PORT', 3000))
    app.run(debug=False, port=port, host='0.0.0.0')
