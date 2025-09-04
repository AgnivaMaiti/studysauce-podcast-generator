from flask import Flask, request, jsonify, send_file, render_template, redirect, url_for, flash, session
from flask_cors import CORS
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_sqlalchemy import SQLAlchemy
import os
import re
import time
import random
import io
import tempfile
import json
from datetime import datetime
import fitz
from pydub import AudioSegment
from dotenv import load_dotenv
import google.generativeai as genai
from tenacity import retry, stop_after_attempt, wait_exponential
from elevenlabs.client import ElevenLabs
from elevenlabs import play, stream, save
from google.api_core.exceptions import BadRequest
import threading
from werkzeug.utils import secure_filename
from models import db, User, Podcast
from forms import LoginForm, RegistrationForm, ChangePasswordForm
from firebase_config import firebase_config

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
CORS(app)

# Configuration
app.config['SECRET_KEY'] = os.getenv(
    'SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL', 'sqlite:///studysauce.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize extensions
db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'output'
ALLOWED_EXTENSIONS = {'pdf'}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

if not GEMINI_API_KEY or not ELEVENLABS_API_KEY:
    raise ValueError("API keys not found. Please check your .env file")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")
eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

NUM_HOSTS = 2
NUM_GUESTS = 1
NAME_POOL = [
    "Alex", "Taylor", "Jordan", "Casey", "Morgan",
    "Riley", "Dakota", "Harper", "Quinn", "Reese"
]
MIN_SPLIT_SIZE = 1000
AVERAGE_WPM = 150.0

processing_status = {}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def call_gemini(prompt: str) -> str:
    response = model.generate_content(prompt)
    text = response.text or ""
    return text.strip()


def split_on_sentence_boundary(text: str) -> tuple[str, str]:
    mid = len(text) // 2
    idx = text.rfind(". ", 0, mid)
    if idx == -1:
        return text[:mid].strip(), text[mid:].strip()
    else:
        return text[: idx + 1].strip(), text[idx + 1:].strip()


def recursive_summarize(text: str) -> str:
    prompt = (
        "Summarize the following academic content in a concise, "
        "concept-focused manner. Preserve key ideas:\n\n"
        f"{text}"
    )
    try:
        return call_gemini(prompt)
    except BadRequest as e:
        if len(text) < MIN_SPLIT_SIZE:
            truncated = text[: min(len(text), MIN_SPLIT_SIZE)]
            try:
                return call_gemini(
                    "Summarize this small snippet concisely:\n\n" + truncated
                )
            except Exception:
                return truncated[:500] + "..."
        left, right = split_on_sentence_boundary(text)
        left_sum = recursive_summarize(left)
        right_sum = recursive_summarize(right)
        return f"{left_sum} {right_sum}"


def process_pdf_to_summary(pdf_path: str, task_id: str) -> str:
    processing_status[task_id]['status'] = 'Processing PDF...'
    processing_status[task_id]['progress'] = 10

    with fitz.open(pdf_path) as doc:
        all_summaries = []
        total_pages = len(doc)

        for page_num, page in enumerate(doc, start=1):
            raw_text = page.get_text("text").strip()
            page_text = re.sub(r"\s+", " ", raw_text)

            if len(page_text) < 50:
                continue

            processing_status[task_id]['status'] = f'Summarizing page {page_num}/{total_pages}...'
            processing_status[task_id]['progress'] = 10 + \
                (page_num / total_pages) * 30

            try:
                page_summary = recursive_summarize(page_text)
                all_summaries.append(page_summary)
                time.sleep(1)
            except Exception as e:
                fallback = page_text[:500] + "..."
                all_summaries.append(fallback)

        if not all_summaries:
            raise RuntimeError("No valid pages found in PDF.")

        concatenated = " ".join(all_summaries)
        final_summary = concatenated[:2000]
        return final_summary


def pick_random_names(num_total, pool, selected_names=None):
    if selected_names and len(selected_names) > 0:
        available_names = [name for name in pool if name not in selected_names]
        if len(selected_names) >= num_total:
            return selected_names[:num_total]
        else:
            remaining_needed = num_total - len(selected_names)
            additional_names = random.sample(available_names, k=min(
                remaining_needed, len(available_names)))
            return selected_names + additional_names
    else:
        return random.sample(pool, k=num_total)


def build_voice_settings(speakers):
    settings = {}
    try:
        voices_response = eleven_client.voices.get_all()

        if hasattr(voices_response, 'voices'):
            all_voices = voices_response.voices
        else:
            all_voices = voices_response

        filtered = []
        for voice in all_voices:
            if hasattr(voice, 'voice_id') and hasattr(voice, 'name'):
                if voice.voice_id and voice.name:
                    filtered.append(voice)
            elif isinstance(voice, dict):
                if voice.get('voice_id') and voice.get('name'):
                    filtered.append(voice)

        if not filtered:
            fallback_voices = [
                {"voice_id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},
                {"voice_id": "AZnzlk1XvdvUeBnXmlld", "name": "Domi"},
                {"voice_id": "EXAVITQu4vr4xnSDxMaL", "name": "Bella"},
                {"voice_id": "ErXwobaYiN019PkySvjV", "name": "Antoni"}
            ]
            filtered = fallback_voices

        for name in speakers:
            choice = random.choice(filtered)
            if isinstance(choice, dict):
                settings[name.lower()] = {
                    "voice_id": choice['voice_id'],
                    "voice_name": choice['name']
                }
            else:
                settings[name.lower()] = {
                    "voice_id": choice.voice_id,
                    "voice_name": choice.name
                }

        return settings

    except Exception as e:
        fallback_voices = [
            {"voice_id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},
            {"voice_id": "AZnzlk1XvdvUeBnXmlld", "name": "Domi"},
            {"voice_id": "EXAVITQu4vr4xnSDxMaL", "name": "Bella"},
            {"voice_id": "ErXwobaYiN019PkySvjV", "name": "Antoni"},
            {"voice_id": "MF3mGyEYCl7XYWbV9V6O", "name": "Elli"}
        ]

        settings = {}
        for i, name in enumerate(speakers):
            voice = fallback_voices[i % len(fallback_voices)]
            settings[name.lower()] = {
                "voice_id": voice["voice_id"],
                "voice_name": voice["name"]
            }

        return settings


def generate_podcast_script(summary: str, speakers: list[str], task_id: str) -> str:
    processing_status[task_id]['status'] = 'Generating podcast script...'
    processing_status[task_id]['progress'] = 45

    num_hosts = processing_status[task_id]['num_hosts']
    num_guests = processing_status[task_id]['num_guests']
    podcast_length = processing_status[task_id]['podcast_length']

    hosts = speakers[:num_hosts]
    guests = speakers[num_hosts:num_hosts +
                      num_guests] if num_guests > 0 else []

    intro_speakers = ", ".join(hosts)
    if guests:
        intro_speakers += " with guest" + \
            ("s" if len(guests) > 1 else "") + " " + ", ".join(guests)

    if podcast_length <= 5:
        length_instruction = "Keep the conversation concise and focused, suitable for a 3-5 minute podcast."
    elif podcast_length <= 10:
        length_instruction = f"Create a {podcast_length}-minute conversation with good depth but not too lengthy."
    else:
        length_instruction = f"Create a comprehensive {podcast_length}-minute discussion with detailed exploration of topics."

    prompt = f"""
Generate a conversational podcast script titled 'StudySauce' with {intro_speakers}.
They will discuss the following research summary in a natural, engaging way.

Research Summary:
{summary}

Guidelines:
- Start with a casual intro by one of the hosts (e.g. "Hey everyone, welcome to StudySauce...")
- Alternate dialogue lines between speakers (e.g. "Alex:" / "Taylor:" / "Morgan:")
- Keep it free-flowing—no visible headers like "Introduction" or "Conclusion."
- Include insightful commentary, light humor, and deeper reflections.
- {length_instruction}
- Target approximately {podcast_length * 150} words total (average 150 words per minute).
"""
    resp = model.generate_content(prompt)
    script_text = resp.text.strip()
    return script_text


def clean_line(line: str) -> str:
    return re.sub(r"\*+", "", line).strip()


def is_dialogue_line(line: str, speakers_lower: set[str]) -> bool:
    lower = line.lower().lstrip()
    return any(lower.startswith(f"{speaker}:") for speaker in speakers_lower)


def get_voice_settings_for_line(line: str, voice_settings: dict) -> dict:
    lower = line.lower()
    for name, cfg in voice_settings.items():
        if lower.startswith(f"{name}:"):
            return cfg
    return {"voice_id": None, "voice_name": None}


def strip_speaker_label(line: str) -> str:
    return re.sub(r"^[A-Za-z]+:\s*", "", line)


def text_to_audio_elevenlabs(text: str, voice_id: str) -> AudioSegment:
    try:
        audio_generator = eleven_client.generate(
            text=text,
            voice=voice_id,
            model="eleven_flash_v2_5"
        )

        audio_data = b"".join(audio_generator)
        return AudioSegment.from_file(io.BytesIO(audio_data), format="mp3")

    except Exception as e:
        try:
            stream = eleven_client.text_to_speech.convert(
                text=text,
                voice_id=voice_id,
                model_id="eleven_flash_v2_5",
                output_format="mp3_44100_128",
                voice_settings={
                    "stability": 0.3,
                    "similarity_boost": 0.6,
                    "style": 0.1,
                    "use_speaker_boost": False
                }
            )
            data = b"".join(stream)
            return AudioSegment.from_file(io.BytesIO(data), format="mp3")
        except Exception as e2:
            print(f"Audio generation failed: {e2}")
            raise e2


def process_podcast_creation(pdf_path: str, task_id: str):
    try:
        num_hosts = processing_status[task_id]['num_hosts']
        num_guests = processing_status[task_id]['num_guests']
        selected_hosts = processing_status[task_id]['selected_hosts']

        summary = process_pdf_to_summary(pdf_path, task_id)

        total_speakers = num_hosts + num_guests
        speakers = pick_random_names(total_speakers, NAME_POOL, selected_hosts)
        voice_settings = build_voice_settings(speakers)

        script = generate_podcast_script(summary, speakers, task_id)

        processing_status[task_id]['status'] = 'Converting to audio...'
        processing_status[task_id]['progress'] = 60

        final_audio = AudioSegment.silent(duration=1000)
        speakers_lower = set(name.lower() for name in speakers)

        script_lines = [clean_line(line)
                        for line in script.split("\n") if clean_line(line)]
        total_lines = len(
            [line for line in script_lines if is_dialogue_line(line, speakers_lower)])
        processed_lines = 0

        for line in script_lines:
            if not line:
                continue

            if is_dialogue_line(line, speakers_lower):
                cfg = get_voice_settings_for_line(line, voice_settings)
                voice_id = cfg["voice_id"]
                content = strip_speaker_label(line)

                if not voice_id:
                    continue

                try:
                    audio_seg = text_to_audio_elevenlabs(
                        text=content, voice_id=voice_id)
                    final_audio += audio_seg + \
                        AudioSegment.silent(duration=500)
                    processed_lines += 1

                    progress = 60 + (processed_lines / total_lines) * 35
                    processing_status[task_id]['progress'] = min(progress, 95)
                    processing_status[task_id][
                        'status'] = f'Processing audio ({processed_lines}/{total_lines})...'

                except Exception as e:
                    print(f"Error processing line: {e}")
                    continue

        processing_status[task_id]['status'] = 'Finalizing podcast...'
        processing_status[task_id]['progress'] = 95

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"StudySauce_Podcast_{timestamp}.mp3"
        output_path = os.path.join(
            app.config['OUTPUT_FOLDER'], output_filename)

        final_audio.export(output_path, format="mp3")

        # Update database record
        if 'podcast_id' in processing_status[task_id]:
            try:
                with app.app_context():
                    podcast = Podcast.query.get(
                        processing_status[task_id]['podcast_id'])
                    if podcast:
                        podcast.audio_filename = output_filename
                        podcast.status = 'completed'
                        podcast.completed_at = datetime.utcnow()
                        # Calculate duration in seconds
                        podcast.duration = len(final_audio) / 1000.0
                        db.session.commit()
            except Exception as e:
                print(f"Database update error: {e}")

        processing_status[task_id]['status'] = 'Complete!'
        processing_status[task_id]['progress'] = 100
        processing_status[task_id]['download_url'] = f'/download/{output_filename}'
        processing_status[task_id]['filename'] = output_filename

    except Exception as e:
        # Update database record with error status
        if 'podcast_id' in processing_status[task_id]:
            try:
                with app.app_context():
                    podcast = Podcast.query.get(
                        processing_status[task_id]['podcast_id'])
                    if podcast:
                        podcast.status = 'failed'
                        db.session.commit()
            except Exception as db_error:
                print(f"Database error update failed: {db_error}")

        processing_status[task_id]['status'] = f'Error: {str(e)}'
        processing_status[task_id]['progress'] = 0
        processing_status[task_id]['error'] = True


# Authentication Routes


@app.route('/')
def index():
    """Main page - redirects based on authentication status"""
    if current_user.is_authenticated:
        return render_template('dashboard.html', user=current_user)
    return render_template('landing.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    form = LoginForm()
    if form.validate_on_submit():
        # Check if username is actually an email
        user = None
        if '@' in form.username.data:
            user = User.query.filter_by(email=form.username.data).first()
        else:
            user = User.query.filter_by(username=form.username.data).first()

        if user and user.check_password(form.password.data):
            user.last_login = datetime.utcnow()
            db.session.commit()
            login_user(user, remember=form.remember_me.data)
            flash('Login successful!', 'success')

            # Redirect to next page or dashboard
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('index'))
        else:
            flash('Invalid username/email or password', 'error')

    return render_template('auth/login.html', form=form)


@app.route('/register', methods=['GET', 'POST'])
def register():
    """Registration page"""
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(
            username=form.username.data,
            email=form.email.data,
            first_name=form.first_name.data,
            last_name=form.last_name.data
        )
        user.set_password(form.password.data)

        try:
            db.session.add(user)
            db.session.commit()
            flash('Registration successful! You can now log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            flash('Registration failed. Please try again.', 'error')

    return render_template('auth/register.html', form=form)


@app.route('/logout')
@login_required
def logout():
    """Logout user"""
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

# Firebase Google Authentication Routes


@app.route('/auth/google')
def google_auth():
    """Initiate Google OAuth via Firebase"""
    return render_template('auth/google_signin.html')


@app.route('/auth/google/callback', methods=['POST'])
def google_auth_callback():
    """Handle Google OAuth callback from Firebase"""
    try:
        data = request.get_json()
        id_token = data.get('idToken')

        if not id_token:
            return jsonify({'error': 'No ID token provided'}), 400

        # Verify the Firebase ID token
        decoded_token = firebase_config.verify_id_token(id_token)

        if not decoded_token:
            return jsonify({'error': 'Invalid token'}), 401

        firebase_uid = decoded_token['uid']
        email = decoded_token.get('email')

        # Get additional user info from Firebase
        firebase_user = firebase_config.get_user_by_uid(firebase_uid)

        if not firebase_user:
            return jsonify({'error': 'User not found in Firebase'}), 404

        # Check if user already exists in our database
        user = User.query.filter_by(firebase_uid=firebase_uid).first()

        if not user:
            user = User.query.filter_by(email=email).first()

            if user:
                # Link existing account to Firebase
                user.firebase_uid = firebase_uid
                user.provider = 'google'
                user.email_verified = decoded_token.get(
                    'email_verified', False)
                if firebase_user.photo_url:
                    user.avatar_url = firebase_user.photo_url
            else:
                # Create new user from Firebase data
                google_user_data = {
                    'email': email,
                    'given_name': firebase_user.display_name.split(' ')[0] if firebase_user.display_name else 'User',
                    'family_name': ' '.join(firebase_user.display_name.split(' ')[1:]) if firebase_user.display_name and len(firebase_user.display_name.split(' ')) > 1 else 'Name',
                    'picture': firebase_user.photo_url,
                    'email_verified': decoded_token.get('email_verified', False),
                    'sub': firebase_uid
                }

                user = User.create_from_google(google_user_data, firebase_uid)

                # Handle duplicate username
                base_username = user.username
                counter = 1
                while User.query.filter_by(username=user.username).first():
                    user.username = f"{base_username}{counter}"
                    counter += 1

        # Update last login
        user.last_login = datetime.utcnow()

        try:
            db.session.add(user)
            db.session.commit()

            # Log in the user
            login_user(user, remember=True)

            return jsonify({
                'success': True,
                'redirect_url': url_for('index'),
                'user': {
                    'name': user.get_full_name(),
                    'email': user.email,
                    'avatar': user.get_avatar_url()
                }
            })

        except Exception as e:
            db.session.rollback()
            print(f"Database error: {e}")
            return jsonify({'error': 'Database error occurred'}), 500

    except Exception as e:
        print(f"Google auth error: {e}")
        return jsonify({'error': 'Authentication failed'}), 500


@app.route('/auth/firebase-config')
def firebase_config_endpoint():
    """Provide Firebase configuration to frontend"""
    config = {
        'apiKey': os.getenv('FIREBASE_API_KEY'),
        'authDomain': os.getenv('FIREBASE_AUTH_DOMAIN'),
        'projectId': os.getenv('FIREBASE_PROJECT_ID'),
        'storageBucket': os.getenv('FIREBASE_STORAGE_BUCKET'),
        'messagingSenderId': os.getenv('FIREBASE_MESSAGING_SENDER_ID'),
        'appId': os.getenv('FIREBASE_APP_ID'),
        'measurementId': os.getenv('FIREBASE_MEASUREMENT_ID')
    }
    return jsonify(config)


@app.route('/profile')
@login_required
def profile():
    """User profile page"""
    podcasts = Podcast.query.filter_by(user_id=current_user.id).order_by(
        Podcast.created_at.desc()).all()
    return render_template('profile.html', user=current_user, podcasts=podcasts)


@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Change password page"""
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if current_user.check_password(form.current_password.data):
            current_user.set_password(form.new_password.data)
            db.session.commit()
            flash('Password changed successfully!', 'success')
            return redirect(url_for('profile'))
        else:
            flash('Current password is incorrect.', 'error')

    return render_template('auth/change_password.html', form=form)

# Protected API Routes


@app.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    """Protected upload endpoint"""
    return upload_file()

# Update the existing upload route to require authentication


@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    """Upload and process PDF file"""
    if 'pdf' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['pdf']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        # Get file size
        file_size = os.path.getsize(filepath)

        num_hosts = int(request.form.get('num_hosts', 2))
        num_guests = int(request.form.get('num_guests', 1))
        podcast_length = int(request.form.get(
            'podcast_length', 10))  # in minutes
        selected_hosts = request.form.getlist('selected_hosts[]')

        if num_hosts < 1 or num_hosts > 4:
            return jsonify({'error': 'Number of hosts must be between 1 and 4'}), 400
        if num_guests < 0 or num_guests > 3:
            return jsonify({'error': 'Number of guests must be between 0 and 3'}), 400
        if podcast_length < 3 or podcast_length > 15:
            return jsonify({'error': 'Podcast length must be between 3 and 15 minutes'}), 400

        task_id = f"task_{timestamp}_{random.randint(1000, 9999)}"

        # Create podcast record in database
        podcast = Podcast(
            title=file.filename,
            original_filename=filename,
            status='processing',
            file_size=file_size,
            user_id=current_user.id
        )

        try:
            db.session.add(podcast)
            db.session.commit()

            processing_status[task_id] = {
                'status': 'Starting...',
                'progress': 0,
                'error': False,
                'num_hosts': num_hosts,
                'num_guests': num_guests,
                'podcast_length': podcast_length,
                'selected_hosts': selected_hosts,
                'podcast_id': podcast.id
            }

            thread = threading.Thread(
                target=process_podcast_creation, args=(filepath, task_id))
            thread.daemon = True
            thread.start()

            return jsonify({'task_id': task_id})

        except Exception as e:
            db.session.rollback()
            return jsonify({'error': 'Failed to create podcast record'}), 500

    return jsonify({'error': 'Invalid file type. Please upload a PDF.'}), 400


@app.route('/status/<task_id>')
@login_required
def get_status(task_id):
    if task_id in processing_status:
        return jsonify(processing_status[task_id])
    else:
        return jsonify({'error': 'Task not found'}), 404


@app.route('/download/<identifier>')
@login_required
def download_file(identifier):
    try:
        if identifier in processing_status and 'filename' in processing_status[identifier]:
            filename = processing_status[identifier]['filename']
        else:
            filename = identifier

        filepath = os.path.join(app.config['OUTPUT_FOLDER'], filename)

        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404

        return send_file(
            filepath,
            as_attachment=True,
            download_name=filename,
            mimetype='audio/mpeg'
        )
    except Exception as e:
        print(f"Download error: {e}")
        return jsonify({'error': f'Download failed: {str(e)}'}), 500


if __name__ == '__main__':
    # Create database tables
    with app.app_context():
        db.create_all()
        print("📊 Database tables created successfully!")

    print("🎧 StudySauce Backend Starting...")
    print("📋 Make sure to set your API keys as environment variables:")
    print("   export GEMINI_API_KEY='your_gemini_key'")
    print("   export ELEVENLABS_API_KEY='your_elevenlabs_key'")
    print("🌐 Server will be available at: http://localhost:5000")
    print("🔐 Authentication system enabled!")
    app.run(debug=True, host='0.0.0.0', port=5000)
