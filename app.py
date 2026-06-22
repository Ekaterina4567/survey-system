import os
import json
import secrets
import hashlib
import qrcode
import io
import base64
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://survey_user:Fd25hZNEWtBryhp1j7b43xVVdgp7hz95@dpg-d8a7p66gvqtc73ck7c30-a.oregon-postgres.render.com/survey_db_ks4f')

if '?' not in DATABASE_URL:
    DATABASE_URL += '?sslmode=require'

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plain_password TEXT,
            role TEXT DEFAULT 'student',
            full_name TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tests (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            unique_code TEXT UNIQUE NOT NULL,
            created_by_user_id INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_editable BOOLEAN DEFAULT TRUE
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS test_questions (
            id SERIAL PRIMARY KEY,
            test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
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
            id SERIAL PRIMARY KEY,
            test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            score INTEGER DEFAULT 0,
            max_score INTEGER DEFAULT 0,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            answers_json TEXT
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS test_answers_detail (
            id SERIAL PRIMARY KEY,
            result_id INTEGER NOT NULL REFERENCES test_results(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL REFERENCES test_questions(id) ON DELETE CASCADE,
            user_answer TEXT,
            is_correct BOOLEAN DEFAULT FALSE,
            points_earned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    admin_hash = hashlib.sha256("admin123".encode()).hexdigest()
    cur.execute('''
        INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
        SELECT 'admin', 'admin@survey.com', %s, 'admin123', 'admin', 'Администратор', TRUE
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'admin')
    ''', (admin_hash,))
    
    teacher_hash = hashlib.sha256("teacher123".encode()).hexdigest()
    cur.execute('''
        INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
        SELECT 'teacher', 'teacher@survey.com', %s, 'teacher123', 'teacher', 'Преподаватель', TRUE
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'teacher')
    ''', (teacher_hash,))
    
    student_hash = hashlib.sha256("student123".encode()).hexdigest()
    cur.execute('''
        INSERT INTO users (username, email, password_hash, plain_password, role, full_name, is_active)
        SELECT 'student', 'student@survey.com', %s, 'student123', 'student', 'Студент', TRUE
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'student')
    ''', (student_hash,))
    
    conn.commit()
    cur.close()
    conn.close()
    print("PostgreSQL database initialized!")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authorization required'}), 401
        return f(*args, **kwargs)
    return decorated_function

def generate_unique_code():
    while True:
        code = secrets.token_urlsafe(6).upper().replace('-', 'X').replace('_', 'Y')
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM tests WHERE unique_code = %s", (code,))
        result = cur.fetchone()
        cur.close()
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

# ===== ROUTES =====

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
        # Сохраняем код теста в сессию для перенаправления после входа
        code = request.args.get('code')
        if code:
            session['redirect_after_login'] = f'/take_test?code={code}'
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

@app.route('/qr_redirect')
def qr_redirect():
    return render_template('qr_redirect.html')

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
        redirect_url = data.get('redirect_url')  # URL для перенаправления после регистрации
        
        if not all([username, email, password]):
            return jsonify({'error': 'Fill in all required fields!'}), 400
        
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({'error': 'User already exists!'}), 400
        
        password_hash = hash_password(password)
        cur.execute('''
            INSERT INTO users (username, email, password_hash, role, full_name, is_active, plain_password)
            VALUES (%s, %s, %s, %s, %s, TRUE, %s) RETURNING id
        ''', (username, email, password_hash, role, full_name, password))
        
        user_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        conn.close()
        
        # Автоматически входим после регистрации
        session['user_id'] = user_id
        session['username'] = username
        session['role'] = role
        session['full_name'] = full_name
        
        return jsonify({
            'success': True, 
            'message': 'Registration successful!',
            'user_id': user_id,
            'redirect_url': redirect_url or '/'
        })
    
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
            WHERE (username = %s OR email = %s) AND password_hash = %s AND is_active = TRUE
        ''', (username, username, password_hash))
        
        user = cur.fetchone()
        cur.close()
        conn.close()
        
        if not user:
            return jsonify({'error': 'Invalid credentials!'}), 401
        
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        session['full_name'] = user['full_name']
        
        # Проверяем, есть ли сохраненный URL для перенаправления
        redirect_url = session.pop('redirect_after_login', None)
        if not redirect_url:
            redirect_url = '/student_dashboard'
        
        return jsonify({
            'success': True, 
            'message': 'Login successful!',
            'role': user['role'],
            'redirect': redirect_url
        })
    
    except Exception as e:
        print(f"Login Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/logout')
def logout():
    session.clear()
    return jsonify({'success': True, 'message': 'Logout successful!'})

@app.route('/check_auth')
def check_auth():
    if 'user_id' in session:
        return jsonify({
            'authenticated': True, 
            'role': session.get('role'), 
            'username': session.get('username'),
            'full_name': session.get('full_name')
        })
    return jsonify({'authenticated': False})

@app.route('/api/create_test', methods=['POST'])
@login_required
def create_test():
    try:
        data = request.json
        title = data.get('title')
        questions = data.get('questions', [])
        
        if not title or not questions:
            return jsonify({'error': 'Title and questions are required!'}), 400
        
        code = generate_unique_code()
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            INSERT INTO tests (title, unique_code, created_by_user_id, created_at, is_editable)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP, TRUE) RETURNING id
        ''', (title, code, session['user_id']))
        
        test_id = cur.fetchone()['id']
        
        for idx, q in enumerate(questions):
            correct_ans = q.get('correct_answer')
            points = q.get('points', 1)
            options = q.get('options', [])
            
            cur.execute('''
                INSERT INTO test_questions (test_id, question_text, question_type, 
                                            options, order_index, correct_answer, points)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (test_id, q.get('text'), q.get('type', 'text'), 
                  json.dumps(options) if options else None, 
                  idx, correct_ans, points))
        
        conn.commit()
        
        # QR code generation
        qr_data = f"{request.host_url}take_test?code={code}"
        qr_img = qrcode.make(qr_data)
        buffered = io.BytesIO()
        qr_img.save(buffered, format="PNG")
        qr_base64 = base64.b64encode(buffered.getvalue()).decode()
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True, 
            'message': 'Test created!', 
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
            return jsonify({'error': 'Enter test code'}), 400
        
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT id, title, unique_code, created_by_user_id
            FROM tests WHERE unique_code = %s
        ''', (code.upper(),))
        
        test = cur.fetchone()
        
        if not test:
            cur.close()
            conn.close()
            return jsonify({'error': 'Test not found'}), 404
        
        cur.execute('''
            SELECT id, question_text, question_type, options, correct_answer, points, order_index
            FROM test_questions WHERE test_id = %s ORDER BY order_index
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
            return jsonify({'error': 'Test ID is required!'}), 400
        
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT id, correct_answer, question_type, points, question_text
            FROM test_questions WHERE test_id = %s
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
                'user_answer': user_answer or '(not specified)',
                'correct_answer': q['correct_answer'] or '(no answer)',
                'points_earned': q['points'] if is_correct else 0,
                'max_points': q['points'],
                'is_correct': is_correct,
                'question_type': q['question_type']
            })
        
        cur.execute('''
            INSERT INTO test_results (test_id, user_id, score, max_score, completed_at, answers_json)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s) RETURNING id
        ''', (test_id, session['user_id'], total_score, max_possible_score, json.dumps(detailed_results, ensure_ascii=False)))
        
        result_id = cur.fetchone()['id']
        
        for detail in detailed_results:
            cur.execute('''
                INSERT INTO test_answers_detail (result_id, question_id, user_answer, is_correct, points_earned)
                VALUES (%s, %s, %s, %s, %s)
            ''', (result_id, detail['question_id'], detail['user_answer'], detail['is_correct'], detail['points_earned']))
        
        conn.commit()
        cur.close()
        conn.close()
        
        percentage = (total_score / max_possible_score * 100) if max_possible_score > 0 else 0
        
        return jsonify({
            'success': True, 
            'message': 'Test completed! Result saved.',
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
            WHERE tr.id = %s AND tr.user_id = %s
        ''', (result_id, session['user_id']))
        
        result = cur.fetchone()
        
        if not result:
            cur.close()
            conn.close()
            return jsonify({'error': 'Result not found'}), 404
        
        cur.execute('''
            SELECT tad.*, tq.question_text, tq.question_type, tq.points, tq.correct_answer
            FROM test_answers_detail tad
            JOIN test_questions tq ON tad.question_id = tq.id
            WHERE tad.result_id = %s
        ''', (result_id,))
        
        details = cur.fetchall()
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'result': dict(result),
            'details': [dict(d) for d in details]
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
            SELECT t.id, t.title, t.unique_code, t.created_at,
                   (SELECT COUNT(*) FROM test_results WHERE test_id = t.id) as attempts_count
            FROM tests t
            WHERE t.created_by_user_id = %s
            ORDER BY t.created_at DESC
        ''', (session['user_id'],))
        
        tests = cur.fetchall()
        cur.close()
        conn.close()
        
        return jsonify([dict(t) for t in tests])
    
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_all_tests', methods=['GET'])
def get_all_tests():
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT t.id, t.title, t.unique_code, t.created_at, u.username as creator_name,
                   (SELECT COUNT(*) FROM test_results WHERE test_id = t.id) as attempts_count
            FROM tests t
            LEFT JOIN users u ON t.created_by_user_id = u.id
            ORDER BY t.created_at DESC
            LIMIT 10
        ''')
        
        tests = cur.fetchall()
        cur.close()
        conn.close()
        
        return jsonify([dict(t) for t in tests])
    
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_test_for_edit/<int:test_id>', methods=['GET'])
@login_required
def get_test_for_edit(test_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT id, title, unique_code FROM tests 
            WHERE id = %s AND created_by_user_id = %s
        ''', (test_id, session['user_id']))
        
        test = cur.fetchone()
        
        if not test:
            cur.close()
            conn.close()
            return jsonify({'error': 'Test not found or no permissions'}), 404
        
        cur.execute('''
            SELECT id, question_text, question_type, options, correct_answer, points, order_index
            FROM test_questions WHERE test_id = %s ORDER BY order_index
        ''', (test_id,))
        
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
            return jsonify({'error': 'Title and questions are required!'}), 400
        
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT id FROM tests WHERE id = %s AND created_by_user_id = %s AND is_editable = TRUE
        ''', (test_id, session['user_id']))
        
        if not cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({'error': 'No permission to edit this test'}), 403
        
        cur.execute("UPDATE tests SET title = %s WHERE id = %s", (title, test_id))
        cur.execute("DELETE FROM test_questions WHERE test_id = %s", (test_id,))
        
        for idx, q in enumerate(questions):
            correct_ans = q.get('correct_answer')
            points = q.get('points', 1)
            options = q.get('options', [])
            
            cur.execute('''
                INSERT INTO test_questions (test_id, question_text, question_type, 
                                            options, order_index, correct_answer, points)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (test_id, q.get('text'), q.get('type', 'text'), 
                  json.dumps(options) if options else None, 
                  idx, correct_ans, points))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Test updated!'})
    
    except Exception as e:
        print(f"Error updating test: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_test_results/<int:test_id>', methods=['GET'])
@login_required
def get_test_results(test_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT created_by_user_id FROM tests WHERE id = %s
        ''', (test_id,))
        test = cur.fetchone()
        
        if not test:
            return jsonify({'error': 'Test not found'}), 404
            
        if test['created_by_user_id'] != session['user_id'] and session.get('role') != 'admin':
            return jsonify({'error': 'No permission to view results'}), 403
        
        cur.execute('''
            SELECT 
                tr.id,
                tr.score,
                tr.max_score,
                tr.completed_at,
                u.full_name,
                u.username,
                u.email,
                ROUND((tr.score * 100.0 / NULLIF(tr.max_score, 0)), 1) as percentage
            FROM test_results tr
            JOIN users u ON tr.user_id = u.id
            WHERE tr.test_id = %s
            ORDER BY tr.completed_at DESC
        ''', (test_id,))
        
        results = cur.fetchall()
        
        cur.execute('''
            SELECT 
                COUNT(*) as total_attempts,
                AVG(score * 100.0 / NULLIF(max_score, 0)) as avg_score,
                MAX(score) as max_score
            FROM test_results
            WHERE test_id = %s
        ''', (test_id,))
        stats = cur.fetchone()
        
        cur.execute('SELECT title FROM tests WHERE id = %s', (test_id,))
        test_info = cur.fetchone()
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'test': {'title': test_info['title']},
            'results': [dict(r) for r in results],
            'stats': {
                'total_attempts': stats['total_attempts'] or 0,
                'avg_score': round(stats['avg_score'] or 0, 1),
                'max_score': stats['max_score'] or 0
            }
        })
        
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
                   t.title, t.unique_code,
                   ROUND((tr.score * 100.0 / NULLIF(tr.max_score, 0)), 1) as percentage
            FROM test_results tr
            JOIN tests t ON tr.test_id = t.id
            WHERE tr.user_id = %s
            ORDER BY tr.completed_at DESC
        ''', (session['user_id'],))
        
        results = cur.fetchall()
        
        avg_percentage = 0
        best_percentage = 0
        if results:
            percentages = [r['percentage'] or 0 for r in results]
            avg_percentage = sum(percentages) / len(percentages)
            best_percentage = max(percentages)
        
        cur.close()
        conn.close()
        
        return jsonify({
            'results': [dict(r) for r in results],
            'stats': {
                'total_tests': len(results),
                'avg_percentage': round(avg_percentage, 1),
                'best_percentage': round(best_percentage, 1)
            }
        })
    
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

# Запуск
if __name__ == '__main__':
    init_db()
    print("=" * 50)
    print("Server: http://localhost:3000")
    print("Database: PostgreSQL")
    print("Admin login: admin / admin123")
    print("Teacher login: teacher / teacher123")
    print("Student login: student / student123")
    print("=" * 50)
    app.run(debug=False, port=3000, host='0.0.0.0')
