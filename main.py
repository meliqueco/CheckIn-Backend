from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from typing import Optional
import sqlite3
import hashlib
import hmac
import secrets
import jwt
import datetime
import os

app = FastAPI(title="NFC Check-in System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Config ---
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 12
NFC_HMAC_SECRET = os.getenv("NFC_HMAC_SECRET", secrets.token_hex(32))

# --- DB Setup ---
DB_PATH = "checkin.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT,
            is_admin INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS nfc_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT UNIQUE NOT NULL,
            location_name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tag_uid TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('in', 'out')),
            timestamp TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            ip_address TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    pw_hash = hash_password("admin123")
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, full_name, is_admin) VALUES (?, ?, ?, 1)",
            ("admin", pw_hash, "Administrador")
        )
        conn.commit()
    except:
        pass
    conn.close()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(password), hashed)

def create_jwt(user_id: int, username: str, is_admin: bool) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "is_admin": is_admin,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=JWT_EXPIRE_HOURS)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)

def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")

def sign_tag_uid(uid: str) -> str:
    return hmac.new(NFC_HMAC_SECRET.encode(), uid.encode(), hashlib.sha256).hexdigest()[:16]

def verify_tag_signature(uid: str, sig: str) -> bool:
    expected = sign_tag_uid(uid)
    return hmac.compare_digest(expected, sig)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

def get_current_user(token: str = Depends(oauth2_scheme)):
    return decode_jwt(token)

def require_admin(user=Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Se requiere rol admin")
    return user

# --- Models ---
class RegisterUser(BaseModel):
    username: str
    password: str
    full_name: str

class CheckinRequest(BaseModel):
    tag_uid: str
    tag_sig: str
    event_type: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None

class CreateTag(BaseModel):
    uid: str
    location_name: str

# --- Endpoints ---

@app.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends()):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE username = ?", (form.username,)
    ).fetchone()
    conn.close()
    if not user or not verify_password(form.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    token = create_jwt(user["id"], user["username"], bool(user["is_admin"]))
    return {"access_token": token, "token_type": "bearer", "full_name": user["full_name"]}

@app.post("/auth/register")
def register(data: RegisterUser, admin=Depends(require_admin)):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, full_name) VALUES (?, ?, ?)",
            (data.username, hash_password(data.password), data.full_name)
        )
        conn.commit()
        return {"message": "Usuario creado exitosamente"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="El usuario ya existe")
    finally:
        conn.close()

@app.post("/checkin")
def checkin(data: CheckinRequest, request_info: dict = Depends(get_current_user)):
    if not verify_tag_signature(data.tag_uid, data.tag_sig):
        raise HTTPException(status_code=403, detail="Tag NFC no válido o manipulado")

    conn = get_db()
    tag = conn.execute("SELECT * FROM nfc_tags WHERE uid = ?", (data.tag_uid,)).fetchone()
    if not tag:
        conn.close()
        raise HTTPException(status_code=404, detail="Tag no registrado en el sistema")

    user_id = int(request_info["sub"])
    recent = conn.execute("""
        SELECT * FROM records
        WHERE user_id = ? AND event_type = ?
        AND timestamp > datetime('now', '-5 minutes')
    """, (user_id, data.event_type)).fetchone()
    if recent:
        conn.close()
        raise HTTPException(status_code=429, detail="Ya registraste este evento recientemente")

    now = datetime.datetime.utcnow().isoformat()
    conn.execute("""
        INSERT INTO records (user_id, tag_uid, event_type, timestamp, latitude, longitude)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, data.tag_uid, data.event_type, now, data.latitude, data.longitude))
    conn.commit()
    conn.close()

    return {
        "message": f"Check-{'in' if data.event_type == 'in' else 'out'} registrado correctamente",
        "timestamp": now,
        "location": tag["location_name"]
    }

@app.get("/records/me")
def my_records(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("""
        SELECT r.*, u.full_name FROM records r
        JOIN users u ON r.user_id = u.id
        WHERE r.user_id = ?
        ORDER BY r.timestamp DESC LIMIT 50
    """, (int(user["sub"]),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/records/all")
def all_records(admin=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("""
        SELECT r.*, u.full_name, u.username FROM records r
        JOIN users u ON r.user_id = u.id
        ORDER BY r.timestamp DESC LIMIT 500
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/tags")
def create_tag(data: CreateTag, admin=Depends(require_admin)):
    sig = sign_tag_uid(data.uid)
    conn = get_db()
    try:
        conn.execute("INSERT INTO nfc_tags (uid, location_name) VALUES (?, ?)", (data.uid, data.location_name))
        conn.commit()
        nfc_url = f"https://TU-DOMINIO.com/?uid={data.uid}&sig={sig}"
        return {"message": "Tag creado", "nfc_url": nfc_url, "uid": data.uid, "sig": sig}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="UID ya registrado")
    finally:
        conn.close()

@app.get("/tags")
def list_tags(admin=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM nfc_tags").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/users")
def list_users(admin=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT id, username, full_name, is_admin FROM users").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/health")
def health():
    return {"status": "ok"}

init_db()
