# app.py
from flask import Flask, render_template, request, redirect, url_for, session, flash
from openai import OpenAI
from dotenv import load_dotenv
import os, datetime
from sqlalchemy import create_engine, Column, Integer, String, Date, Boolean, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from passlib.hash import bcrypt

# Load env
load_dotenv()

OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://openrouter.ai/api/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FLASK_SECRET = os.getenv("FLASK_SECRET", "change_this_secret")

# Init OpenAI/OpenRouter client
client = OpenAI(base_url=OPENAI_API_BASE, api_key=OPENAI_API_KEY)

# Flask app
app = Flask(__name__)
app.secret_key = FLASK_SECRET

# Database (SQLite with SQLAlchemy)
DATABASE_URL = "sqlite:///hutextnizer.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
Base = declarative_base()
DBSession = sessionmaker(bind=engine)
db = DBSession()

# Models
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, nullable=True)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    is_paid = Column(Boolean, default=False)

class Usage(Base):
    __tablename__ = "usage"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=True)   # null for anonymous if needed (we track anon in session)
    date = Column(Date, default=datetime.date.today)
    words = Column(Integer, default=0)

Base.metadata.create_all(engine)

# Constants
DAILY_FREE_WORDS = 10000

# Helpers
def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db.query(User).filter(User.id == uid).first()

def count_today_words_for_user(user: User):
    if not user:
        return 0
    today = datetime.date.today()
    usage = db.query(Usage).filter(Usage.user_id == user.id, Usage.date == today).first()
    return usage.words if usage else 0

def add_words_for_user(user: User, words: int):
    today = datetime.date.today()
    usage = db.query(Usage).filter(Usage.user_id == (user.id if user else None), Usage.date == today).first()
    if not usage:
        usage = Usage(user_id=(user.id if user else None), date=today, words=0)
        db.add(usage)
    usage.words += words
    db.commit()

def words_in_text(text: str):
    return len(text.strip().split())

# Routes
@app.route('/')
def home():
    user = get_current_user()
    # provide remaining free words info
    remaining = None
    if user:
        if user.is_paid:
            remaining = None
        else:
            used = count_today_words_for_user(user)
            remaining = max(0, DAILY_FREE_WORDS - used)
    else:
        # anonymous: track in session
        session.setdefault("anon_words_today", 0)
        remaining = max(0, DAILY_FREE_WORDS - session["anon_words_today"])
    return render_template('home.html', remaining=remaining)

@app.route('/pricing')
def pricing():
    return render_template('pricing.html')

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get("username")
        email = request.form.get("email").lower().strip()
        password = request.form.get("password")
        if not email or not password:
            flash("Email and password are required.", "error")
            return redirect(url_for('signup'))
        # check exists
        if db.query(User).filter(User.email == email).first():
            flash("Email already registered. Please login.", "error")
            return redirect(url_for('login'))
        hash_pw = bcrypt.hash(password)
        user = User(username=username, email=email, password_hash=hash_pw, is_paid=False)
        db.add(user); db.commit()
        # log them in
        session['user_id'] = user.id
        flash("Account created — you are logged in.", "success")
        return redirect(url_for('home'))
    return render_template('signup.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "")
        user = db.query(User).filter(User.email == email).first()
        if not user or not bcrypt.verify(password, user.password_hash):
            flash("Invalid credentials.", "error")
            return redirect(url_for('login'))
        session['user_id'] = user.id
        flash("Logged in successfully.", "success")
        return redirect(url_for('home'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    flash("Logged out.", "info")
    return redirect(url_for('home'))

@app.route('/convert', methods=['POST'])
def convert():
    text = request.form.get('user_text') or request.form.get('text') or ""
    if not text.strip():
        flash("Please paste text to convert.", "error")
        return redirect(url_for('home'))

    user = get_current_user()

    # If anonymous and already used once, force signup/login before another use
    anon_uses = session.get("anon_uses", 0)
    if (not user) and anon_uses >= 1:
        flash("Please Signup / Login to use the converter again.", "info")
        return redirect(url_for('signup'))

    # Count words
    wc = words_in_text(text)

    # Check daily limit
    if user:
        if not user.is_paid:
            used = count_today_words_for_user(user)
            if used + wc > DAILY_FREE_WORDS:
                flash("Daily free limit exceeded. Please subscribe for more.", "error")
                return redirect(url_for('pricing'))
    else:
        # anonymous limit via session
        session.setdefault("anon_words_today", 0)
        if session["anon_words_today"] + wc > DAILY_FREE_WORDS:
            flash("Daily free limit exceeded. Please signup for more.", "error")
            return redirect(url_for('signup'))

    # Call OpenRouter/OpenAI
    try:
        # build prompt
        system = "You are a helpful AI that rewrites text to sound more natural, fluent, and human-written. Preserve meaning; do not add facts."
        user_prompt = f"Please rewrite the following text to sound natural and human-written without changing meaning:\n\n{text}"

        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system", "content": system},
                {"role":"user", "content": user_prompt}
            ],
            max_tokens=800
        )

        result_text = resp.choices[0].message.content

    except Exception as e:
        # show error message on result page
        result_text = f"[Error calling AI service: {e}]"
        # do NOT count words if API failed
        return render_template('result.html', original_text=text, humanized_text=result_text)

    # Save usage
    if user:
        add_words_for_user(user, wc)
    else:
        # increment anonymous usage
        session['anon_uses'] = anon_uses + 1
        session['anon_words_today'] = session.get('anon_words_today', 0) + wc

    return render_template('result.html', original_text=text, humanized_text=result_text)

# Admin / debug route (optional) - view simple usage (remove in production)
@app.route('/_debug_usage')
def debug_usage():
    user = get_current_user()
    today = datetime.date.today()
    if user:
        used = count_today_words_for_user(user)
        return f"User {user.email} used {used} words today. Paid: {user.is_paid}"
    return f"Anonymous uses: {session.get('anon_uses',0)}, anon words today: {session.get('anon_words_today',0)}"

if __name__ == '__main__':
    app.run(debug=True)
