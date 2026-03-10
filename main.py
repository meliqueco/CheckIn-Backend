from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from typing import Optional
import hashlib
import hmac
import secrets
import jwt
import datetime
import os
import psycopg2
import psycopg2.extras

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
DATABASE_URL = os.getenv("DATABASE_URL")

# --- DB ---
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

# --- Auth helpers ---
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
    device_id: Optional[str] = None

class CreateTag(BaseModel):
    uid: str
    location_name: str

# --- Endpoints ---

@app.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends()):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE username = %s", (form.username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user or not verify_password(form.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    token = create_jwt(user["id"], user["username"], bool(user["is_admin"]))
    return {"access_token": token, "token_type": "bearer", "full_name": user["full_name"]}

@app.post("/auth/register")
def register(data: RegisterUser, admin=Depends(require_admin)):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (username, password_hash, full_name) VALUES (%s, %s, %s)",
            (data.username, hash_password(data.password), data.full_name)
        )
        conn.commit()
        return {"message": "Usuario creado exitosamente"}
    except psycopg2.IntegrityError:
        raise HTTPException(status_code=400, detail="El usuario ya existe")
    finally:
        cur.close()
        conn.close()

@app.post("/checkin")
def checkin(data: CheckinRequest, user_info: dict = Depends(get_current_user)):
    # 1. Verificar firma del tag
    if not verify_tag_signature(data.tag_uid, data.tag_sig):
        raise HTTPException(status_code=403, detail="Tag NFC no válido o manipulado")

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 2. Verificar que el tag existe
    cur.execute("SELECT * FROM nfc_tags WHERE uid = %s", (data.tag_uid,))
    tag = cur.fetchone()
    if not tag:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Tag no registrado en el sistema")

    # Detectar automáticamente si es entrada o salida según el nombre del tag
    location = tag["location_name"].lower()
    event_type = "out" if "salida" in location else "in"

    # 3. Rate limit: no permitir mismo evento dos veces en 5 minutos
    user_id = int(user_info["sub"])
    cur.execute("""
        SELECT * FROM records
        WHERE user_id = %s AND event_type = %s
        AND timestamp > NOW() - INTERVAL '5 minutes'
    """, (user_id, event_type))
    if cur.fetchone():
        cur.close()
        conn.close()
        raise HTTPException(status_code=429, detail="Ya registraste este evento recientemente")

    # 4. Obtener nombre del usuario
    cur.execute("SELECT full_name FROM users WHERE id = %s", (user_id,))
    user_row = cur.fetchone()
    full_name = user_row["full_name"] if user_row else "—"

    # 5. Actualizar device_id en el usuario si viene uno nuevo
    if data.device_id:
        cur.execute(
            "UPDATE users SET device_id = %s WHERE id = %s",
            (data.device_id, user_id)
        )

    # 6. Guardar registro
    now = datetime.datetime.utcnow()
    cur.execute("""
        INSERT INTO records (user_id, full_name, device_id, tag_uid, event_type, timestamp, latitude, longitude)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (user_id, full_name, data.device_id, data.tag_uid, event_type, now, data.latitude, data.longitude))
    conn.commit()
    cur.close()
    conn.close()

    return {
        "message": f"Check-{'in' if event_type == 'in' else 'out'} registrado correctamente",
        "timestamp": now.isoformat(),
        "location": tag["location_name"]
    }

@app.get("/records/me")
def my_records(user=Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT * FROM records
        WHERE user_id = %s
        ORDER BY timestamp DESC LIMIT 50
    """, (int(user["sub"]),))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/records/all")
def all_records(admin=Depends(require_admin)):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT r.*, u.username, u.device_id as user_device_id
        FROM records r
        JOIN users u ON r.user_id = u.id
        ORDER BY r.timestamp DESC
        LIMIT 500
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/tags")
def create_tag(data: CreateTag, admin=Depends(require_admin)):
    sig = sign_tag_uid(data.uid)
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO nfc_tags (uid, location_name) VALUES (%s, %s)", (data.uid, data.location_name))
        conn.commit()
        nfc_url = f"https://jocular-pasca-d30f94.netlify.app/?uid={data.uid}&sig={sig}"
        return {"message": "Tag creado", "nfc_url": nfc_url, "uid": data.uid, "sig": sig}
    except psycopg2.IntegrityError:
        raise HTTPException(status_code=400, detail="UID ya registrado")
    finally:
        cur.close()
        conn.close()

@app.get("/tags")
def list_tags(admin=Depends(require_admin)):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM nfc_tags")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/users")
def list_users(admin=Depends(require_admin)):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, username, full_name, device_id, is_admin, created_at FROM users")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/health")
def health():
    return {"status": "ok"}
