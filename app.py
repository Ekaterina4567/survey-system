from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import psycopg2
from psycopg2.extras import RealDictCursor
import os
import json
import secrets
import qrcode
import io
import base64
from functools import wraps
import hashlib
import urllib.parse
import traceback
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))

# ===== ФУНКЦИЯ ПОДКЛЮЧЕНИЯ К БАЗЕ ДАННЫХ =====

def get_db_connection():
    """Подключение к PostgreSQL на Render"""
    try:
        database_url = os.environ.get('DATABASE_URL')
        if not database_url:
            logger.error("❌ DATABASE_URL не установлен")
            return None
        
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
        logger.info("✅ Подключение к БД успешно")
        return conn
    except Exception as e:
        logger.error(f"❌ DB Connection Error: {e}")
        return None

# ===== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ =====

def init_database():
    """Создание таблиц и тестовых пользователей"""
    conn = get_db_connection()
    if not conn:
        logger.error("❌ Не удалось подключиться к базе данных")
        return False
    
    try:
        cur = conn.cursor()
        
        # Создаём таблицы
        tables = [
            """CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                plain_password VARCHAR(255),
                role VARCHAR(50) DEFAULT 'student',
                full_name VARCHAR(255),
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS tests (
                id SERIAL PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                unique_code VARCHAR(20) UNIQUE NOT NULL,
                created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_editable BOOLEAN DEFAULT TRUE
            )""",
            """CREATE TABLE IF NOT EXISTS test_questions (
                id SERIAL PRIMARY KEY,
                test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
                question_text TEXT NOT NULL,
                question_type VARCHAR(50) DEFAULT 'choice',
                options TEXT,
                order_index INTEGER DEFAULT 0,
                correct_answer TEXT,
                points INTEGER DEFAULT 1
            )""",
            """CREATE TABLE IF NOT EXISTS test_results (
                id SERIAL PRIMARY KEY,
                test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                score INTEGER DEFAULT 0,
                max_score INTEGER DEFAULT 0,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                answers_json TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS test_answers_detail (
                id SERIAL PRIMARY KEY,
                result_id INTEGER NOT NULL REFERENCES test_results(id) ON DELETE CASCADE,
                question_id INTEGER NOT NULL REFERENCES test_questions(id) ON DELETE CASCADE,
                user_answer TEXT,
                is_correct BOOLEAN DEFAULT FALSE,
                points_earned INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        ]
        
        for sql in tables:
            cur.execute(sql)
        
        # Индексы
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tests_code ON tests(unique_code)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_results_user ON test_results(user_id)")
        
        # Тестовые пользователи
        for username, email, password, role, full_name in [
            ('admin', 'admin@survey.com', 'admin123', 'admin', 'Администратор'),
            ('teacher', 'teacher@survey.com', 'teacher123', 'teacher', 'Преподаватель'),
            ('student', 'student@survey.com', 'student123', 'student', 'Студент')
        ]:
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            cur.execute("""
                INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
                SELECT %s, %s, %s, %s, %s, %s, TRUE
                WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = %s)
            """, (username, email, password_hash, password, role, full_name, username))
        
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ База данных успешно инициализирована!")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}")
        logger.error(traceback.format_exc())
        return False

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            logger.warning("⚠️ Требуется авторизация")
            return jsonify({'error': 'Требуется авторизация'}), 401
        return f(*args, **kwargs)
    return decorated_function

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def generate_unique_code():
    """Генерация уникального кода для теста"""
    for _ in range(10):  # пробуем 10 раз
        code = secrets.token_urlsafe(6).upper().replace('-', 'X').replace('_', 'Y')[:8]
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM tests WHERE unique_code = %s", (code,))
            result = cur.fetchone()
            cur.close()
            conn.close()
            if not result:
                return code
    return secrets.token_urlsafe(8)[:8].upper()

def normalize_text(text):
    if not text:
        return ""
    return text.strip().lower().replace("ё", "е").replace(" ", "")

def check_answer(question_type, user_answer, correct_answer):
    if not user_answer or not correct_answer:
        return False
    if question_type in ['text', 'choice']:
        return normalize_text(str(user_answer)) == normalize_text(str(correct_answer))
    elif question_type == 'checkbox':
        user_set = set(normalize_text(x) for x in str(user_answer).split(',') if x.strip())
        correct_set = set(normalize_text(x) for x in str(correct_answer).split(',') if x.strip())
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
    print("=== REGISTER REQUEST RECEIVED ===")
    try:
        data = request.get_json()
        print(f"Data received: {data}")
        
        if not data:
            print("No JSON data")
            return jsonify({'error': 'Нет данных'}), 400
        
        username = data.get('username')
        email = data.get('email')
        password = data.get('password')
        full_name = data.get('full_name', '')
        role = data.get('role', 'student')
        
        print(f"Username: {username}, Email: {email}, Role: {role}")
        
        if not all([username, email, password]):
            print("Missing fields")
            return jsonify({'error': 'Заполните все поля!'}), 400
        
        conn = get_db_connection()
        if not conn:
            print("Database connection failed")
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor()
        
        # Проверка существования
        cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
        if cur.fetchone():
            print("User already exists")
            cur.close()
            conn.close()
            return jsonify({'error': 'Пользователь уже существует!'}), 400
        
        # Хеширование пароля
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        print(f"Password hash created")
        
        # Вставка
        cur.execute("""
            INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            RETURNING id
        """, (username, email, password_hash, password, role, full_name))
        
        user_id = cur.fetchone()[0]
        conn.commit()
        print(f"User created with id: {user_id}")
        
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Регистрация успешна!', 'user_id': user_id})
    
    except Exception as e:
        print(f"REGISTER ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json(force=True, silent=True) or {}
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
        
        return jsonify({
            'success': True, 
            'message': 'Вход выполнен!',
            'role': user['role'],
            'redirect': '/student_dashboard'
        })
    except Exception as e:
        logger.error(f"Login Error: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/logout')
def logout():
    session.clear()
    return jsonify({'success': True, 'message': 'Выход выполнен!'})

@app.route('/check_auth')
def check_auth():
    if 'user_id' in session:
        return jsonify({
            'authenticated': True, 
            'role': session.get('role'), 
            'username': session.get('username')
        })
    return jsonify({'authenticated': False})

# ===== КЛЮЧЕВОЙ МАРШРУТ: /api/create_test =====

@app.route('/api/create_test', methods=['POST'])
@login_required
def create_test():
    """Создание нового теста с вопросами"""
    conn = None
    cur = None
    
    try:
        logger.info(f"📥 create_test: user_id={session.get('user_id')}, content_type={request.content_type}")
        
        # Получаем JSON
        data = request.get_json(force=True, silent=True)
        logger.info(f"📥 Parsed data keys: {list(data.keys()) if data else 'None'}")
        
        if not data:
            return jsonify({'error': 'Неверный формат данных. Ожидался JSON.'}), 400
        
        title = data.get('title')
        questions = data.get('questions', [])
        
        logger.info(f"📥 title='{title}', questions_count={len(questions) if questions else 0}")
        
        # Валидация
        if not title or not isinstance(title, str) or not title.strip():
            return jsonify({'error': 'Название теста обязательно!'}), 400
        if not isinstance(questions, list) or len(questions) == 0:
            return jsonify({'error': 'Добавьте хотя бы один вопрос!'}), 400
        
        # Генерация кода
        code = generate_unique_code()
        logger.info(f"🔑 Generated code: {code}")
        
        # Подключение к БД
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка подключения к базе данных'}), 500
        
        cur = conn.cursor()
        
        # 🔥 КЛЮЧЕВОЙ МОМЕНТ: проверяем user_id
        user_id = session.get('user_id')
        if not user_id:
            raise ValueError("session['user_id'] не установлен!")
        logger.info(f"🔐 Using user_id={user_id} for test creation")
        
        # Создаём тест
        logger.info(f"🗄️ INSERT: title='{title[:50]}...', code='{code}', user_id={user_id}")
        cur.execute("""
            INSERT INTO tests (title, unique_code, created_by_user_id, created_at, is_editable)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP, TRUE) RETURNING id
        """, (title.strip(), code, int(user_id)))  # 🔥 int() для гарантии типа
        
        result = cur.fetchone()
        logger.info(f"🗄️ INSERT result: {result}")
        
        if not result or result.get('id') is None:
            raise Exception("INSERT не вернул id теста")
        
        test_id = result['id']
        logger.info(f"✅ Test created: id={test_id}")
        
        # Добавляем вопросы
        for idx, q in enumerate(questions):
            if not q or not q.get('text'):
                continue
            cur.execute("""
                INSERT INTO test_questions (test_id, question_text, question_type, 
                                            options, order_index, correct_answer, points)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                test_id,
                q.get('text'),
                q.get('type', 'text'),
                json.dumps(q.get('options', [])) if q.get('options') else None,
                idx,
                q.get('correct_answer'),
                int(q.get('points', 1))
            ))
        
        conn.commit()
        logger.info(f"✅ Added {len(questions)} questions, committed")
        
        # QR-код (не критично)
        qr_base64 = ""
        try:
            qr_data = f"{request.host_url.rstrip('/')}take_test?code={code}"
            qr_img = qrcode.make(qr_data)
            buffered = io.BytesIO()
            qr_img.save(buffered, format="PNG")
            qr_base64 = base64.b64encode(buffered.getvalue()).decode()
            logger.info("✅ QR generated")
        except Exception as qr_err:
            logger.warning(f"⚠️ QR generation warning: {qr_err}")
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'Тест создан!',
            'code': code,
            'qr_code': qr_base64,
            'test_id': test_id
        })
        
    except psycopg2.Error as db_err:
        logger.error(f"❌ PostgreSQL Error: {db_err}")
        logger.error(f"❌ SQL State: {db_err.pgcode if hasattr(db_err, 'pgcode') else 'N/A'}")
        if conn:
            try:
                conn.rollback()
                logger.info("🔄 DB rollback done")
            except:
                pass
        return jsonify({'error': f'Database error: {str(db_err)}'}), 500
        
    except Exception as e:
        # 🔥 ПОЛНОЕ ЛОГИРОВАНИЕ
        error_type = type(e).__name__
        error_msg = str(e)
        error_repr = repr(e)
        error_tb = traceback.format_exc()
        
        logger.error(f"❌ CRITICAL ERROR in create_test:")
        logger.error(f"   Type: {error_type}")
        logger.error(f"   str(e): '{error_msg}'")
        logger.error(f"   repr(e): {error_repr}")
        logger.error(f"   Value: {e} (type: {type(e)})")
        logger.error(f"   Traceback:\n{error_tb}")
        
        if conn:
            try:
                conn.rollback()
            except:
                pass
        
        # 🔥 ВАЖНО: не возвращаем "0", всегда возвращаем понятное сообщение
        safe_msg = error_msg if error_msg and error_msg != "0" else f"{error_type} occurred"
        return jsonify({
            'error': f'{error_type}: {safe_msg}',
            'debug': safe_msg if app.debug else None
        }), 500
        
    finally:
        if cur:
            try:
                cur.close()
            except:
                pass
        if conn:
            try:
                conn.close()
            except:
                pass

# ===== ОСТАЛЬНЫЕ API МАРШРУТЫ (сокращённо) =====

@app.route('/api/get_test_by_code', methods=['POST'])
def get_test_by_code():
    try:
        data = request.get_json(force=True, silent=True) or {}
        code = data.get('code')
        if not code:
            return jsonify({'error': 'Введите код теста'}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, title, unique_code, created_by_user_id FROM tests WHERE unique_code = %s", (code.upper(),))
        test = cur.fetchone()
        
        if not test:
            cur.close()
            conn.close()
            return jsonify({'error': 'Тест не найден'}), 404
        
        cur.execute("""
            SELECT id, question_text, question_type, options, correct_answer, points, order_index
            FROM test_questions WHERE test_id = %s ORDER BY order_index
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
                'id': test['id'], 'title': test['title'],
                'code': test['unique_code'], 'questions': result_questions
            }
        })
    except Exception as e:
        logger.error(f"get_test_by_code error: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/submit_test', methods=['POST'])
@login_required
def submit_test():
    try:
        data = request.get_json(force=True, silent=True) or {}
        test_id = data.get('test_id')
        answers = data.get('answers', {})
        
        if not test_id:
            return jsonify({'error': 'ID теста обязателен!'}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, correct_answer, question_type, points, question_text
            FROM test_questions WHERE test_id = %s
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
                'question_id': q['id'], 'question_text': q['question_text'],
                'user_answer': user_answer or '(не указан)',
                'correct_answer': q['correct_answer'] or '(нет ответа)',
                'points_earned': q['points'] if is_correct else 0,
                'max_points': q['points'], 'is_correct': is_correct,
                'question_type': q['question_type']
            })
        
        cur.execute("""
            INSERT INTO test_results (test_id, user_id, score, max_score, completed_at, answers_json)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s) RETURNING id
        """, (test_id, session['user_id'], total_score, max_possible_score, json.dumps(detailed_results, ensure_ascii=False)))
        result_id = cur.fetchone()['id']
        
        for detail in detailed_results:
            cur.execute("""
                INSERT INTO test_answers_detail (result_id, question_id, user_answer, is_correct, points_earned)
                VALUES (%s, %s, %s, %s, %s)
            """, (result_id, detail['question_id'], detail['user_answer'], detail['is_correct'], detail['points_earned']))
        
        conn.commit()
        cur.close()
        conn.close()
        
        percentage = (total_score / max_possible_score * 100) if max_possible_score > 0 else 0
        return jsonify({
            'success': True, 'message': 'Тест пройден!',
            'score': total_score, 'max_score': max_possible_score,
            'percentage': round(percentage, 1), 'result_id': result_id
        })
    except Exception as e:
        logger.error(f"submit_test error: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/my_results', methods=['GET'])
@login_required
def my_results():
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT tr.id, tr.score, tr.max_score, tr.completed_at,
                   t.title, t.unique_code,
                   ROUND((tr.score::numeric / NULLIF(tr.max_score, 0)::numeric) * 100, 1) as percentage
            FROM test_results tr
            JOIN tests t ON tr.test_id = t.id
            WHERE tr.user_id = %s
            ORDER BY tr.completed_at DESC
        """, (session['user_id'],))
        
        results = [dict(row) for row in cur.fetchall()]
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        logger.error(f"my_results error: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_my_tests', methods=['GET'])
@login_required
def get_my_tests():
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка БД'}), 500
        
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT t.id, t.title, t.unique_code, t.created_at,
                   (SELECT COUNT(*) FROM test_results WHERE test_id = t.id) as attempts_count
            FROM tests t
            WHERE t.created_by_user_id = %s
            ORDER BY t.created_at DESC
        """, (session['user_id'],))
        
        tests = [dict(row) for row in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify(tests)
    except Exception as e:
        logger.error(f"get_my_tests error: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

# ===== ЗАПУСК =====

if __name__ == '__main__':
    logger.info("🚀 Starting SurveySystem...")
    
    print("Инициализация базы данных...")
    init_database()
    
    print("=" * 50)
    print("🚀 Server: http://localhost:3000")
    print("👨‍💼 Admin: admin / admin123")
    print("👨‍🏫 Teacher: teacher / teacher123") 
    print("👨‍🎓 Student: student / student123")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 3000))
    app.run(debug=False, port=port, host='0.0.0.0')
