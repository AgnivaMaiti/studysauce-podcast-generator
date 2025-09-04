import os
import json
import firebase_admin
from firebase_admin import credentials, auth
import pyrebase
from dotenv import load_dotenv

load_dotenv()

class FirebaseConfig:
    """Firebase configuration and authentication service"""

    def __init__(self):
        self.firebase_config = {
            "apiKey": os.getenv('FIREBASE_API_KEY'),
            "authDomain": os.getenv('FIREBASE_AUTH_DOMAIN'),
            "projectId": os.getenv('FIREBASE_PROJECT_ID'),
            "storageBucket": os.getenv('FIREBASE_STORAGE_BUCKET'),
            "messagingSenderId": os.getenv('FIREBASE_MESSAGING_SENDER_ID'),
            "appId": os.getenv('FIREBASE_APP_ID'),
            "measurementId": os.getenv('FIREBASE_MEASUREMENT_ID'),
            "databaseURL": ""  # Not needed for auth only
        }

        self.firebase = None
        self.admin_app = None
        self.initialize_firebase()

    def initialize_firebase(self):
        """Initialize Firebase Admin SDK and Pyrebase"""
        try:
            admin_sdk_path = os.getenv('FIREBASE_ADMIN_SDK_PATH')
            if admin_sdk_path and os.path.exists(admin_sdk_path):
                cred = credentials.Certificate(admin_sdk_path)
                if not firebase_admin._apps:
                    self.admin_app = firebase_admin.initialize_app(cred)
                else:
                    self.admin_app = firebase_admin.get_app()

            self.firebase = pyrebase.initialize_app(self.firebase_config)

            print("✅ Firebase initialized successfully!")

        except Exception as e:
            print(f"❌ Firebase initialization error: {e}")
            self.firebase = None
            self.admin_app = None

    def get_auth(self):
        """Get Firebase Auth instance"""
        if self.firebase:
            return self.firebase.auth()
        return None

    def verify_id_token(self, id_token):
        """Verify Firebase ID token using Admin SDK"""
        try:
            if self.admin_app:
                decoded_token = auth.verify_id_token(id_token)
                return decoded_token
            return None
        except Exception as e:
            print(f"Token verification error: {e}")
            return None

    def get_user_by_uid(self, uid):
        """Get user information by UID"""
        try:
            if self.admin_app:
                user_record = auth.get_user(uid)
                return user_record
            return None
        except Exception as e:
            print(f"Error getting user: {e}")
            return None


firebase_config = FirebaseConfig()
