from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    """User model for authentication"""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True,
                         nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    # Allow null for OAuth users
    password_hash = db.Column(db.String(255), nullable=True)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    # OAuth fields
    firebase_uid = db.Column(
        db.String(128), unique=True, nullable=True, index=True)
    google_id = db.Column(db.String(100), unique=True,
                          nullable=True, index=True)
    provider = db.Column(db.String(50), default='local')
    avatar_url = db.Column(db.String(255), nullable=True)
    email_verified = db.Column(db.Boolean, default=False)

    podcasts = db.relationship(
        'Podcast', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        """Set password hash"""
        if password:
            self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Check password against hash"""
        if self.password_hash:
            return check_password_hash(self.password_hash, password)
        return False

    def get_full_name(self):
        """Get user's full name"""
        return f"{self.first_name} {self.last_name}"

    def is_oauth_user(self):
        """Check if user signed in via OAuth"""
        return self.provider in ['google', 'firebase']

    def get_avatar_url(self):
        """Get user avatar URL or default"""
        if self.avatar_url:
            return self.avatar_url
        initials = f"{self.first_name[0]}{self.last_name[0]}"
        return f"https://ui-avatars.com/api/?name={initials}&background=667eea&color=fff&size=200"

    @staticmethod
    def create_from_google(google_user_data, firebase_uid=None):
        """Create user from Google OAuth data"""
        user = User(
            email=google_user_data.get('email'),
            first_name=google_user_data.get('given_name', ''),
            last_name=google_user_data.get('family_name', ''),
            username=google_user_data.get('email').split(
                '@')[0], 
            google_id=google_user_data.get('sub'),
            firebase_uid=firebase_uid,
            provider='google',
            avatar_url=google_user_data.get('picture'),
            email_verified=google_user_data.get('email_verified', False)
        )
        return user

    def __repr__(self):
        return f'<User {self.username}>'


class Podcast(db.Model):
    """Podcast model to track user's generated podcasts"""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    audio_filename = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), default='processing')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    file_size = db.Column(db.Integer)  
    duration = db.Column(db.Float)  # Audio duration in seconds

    # Foreign key to User
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    def __repr__(self):
        return f'<Podcast {self.title}>'
