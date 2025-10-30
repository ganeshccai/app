from flask import Flask, request, jsonify
from flask_cors import CORS
import time
import os
import base64

app = Flask(__name__)
CORS(app)

# In-memory stores
messages = {}
typing_status = {}
online_status = {}
session_tokens = {}  # key: (chat_id, sender), value: {token: timestamp}

def verify_token(chat_id, sender, token):
    return token in session_tokens.get((chat_id, sender), {})

def format_last_seen(ts):
    if not ts or ts == 0:
        return ""
    delta = int(time.time() - ts)
    if delta < 60:
        return f"{delta} sec ago"
    elif delta < 3600:
        return f"{delta // 60} min ago"
    elif delta < 86400:
        return f"{delta // 3600} hr ago"
    else:
        return f"{delta // 86400} days ago"

@app.route("/login", methods=["POST"])
def login():
    data = request.json
    chat_id = data["chat_id"]
    password = data["password"]
    sender = data["sender"]

    active_tokens = session_tokens.get((chat_id, sender), {})
    now = time.time()
    for t, ts in active_tokens.items():
        if now - ts < 10:
            return jsonify(success=False, error="Try after 5 sec")

    if password == "1":
        token = f"{sender}-{int(now)}"
        session_tokens.setdefault((chat_id, sender), {})[token] = now
        return jsonify(success=True, session_token=token)
    return jsonify(success=False, error="Invalid password")

@app.route("/send", methods=["POST"])
def send():
    data = request.json
    chat_id = data["chat_id"]
    sender = data["sender"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, sender, token):
        return jsonify(error="Unauthorized"), 403

    msg = {
        "sender": sender,
        "timestamp": time.time(),
        "seen_by": None
    }

    if data.get("type") == "image" and data.get("url"):
        msg["type"] = "image"
        msg["url"] = data["url"]
        msg["text"] = None
    else:
        text = data.get("text", "").strip()
        if not text:
            return jsonify(error="Empty message"), 400
        msg["text"] = text
        msg["type"] = "text"

    messages.setdefault(chat_id, []).append(msg)
    return jsonify(success=True)

@app.route("/upload", methods=["POST"])
def upload():
    chat_id = request.form.get("chat_id")
    sender = request.form.get("sender")
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    file = request.files.get("file")

    if not verify_token(chat_id, sender, token):
        return jsonify(success=False, error="Unauthorized"), 403
    if not file:
        return jsonify(success=False, error="No file uploaded"), 400

    ext = file.filename.split(".")[-1].lower()
    if ext not in ["jpg", "jpeg", "png", "gif"]:
        return jsonify(success=False, error="Unsupported file type"), 400

    b64 = base64.b64encode(file.read()).decode("utf-8")
    mime = "image/jpeg" if ext in ["jpg", "jpeg"] else f"image/{ext}"
    url = f"data:{mime};base64,{b64}"
    return jsonify(success=True, url=url)

@app.route("/messages/<chat_id>")
def get_messages(chat_id):
    viewer = request.args.get("viewer")
    active = request.args.get("active") == "true"
    chat = messages.get(chat_id, [])

    if active and chat and chat[-1]["sender"] != viewer:
        chat[-1]["seen_by"] = viewer

    return jsonify(chat)

@app.route("/live_typing", methods=["POST"])
def live_typing():
    data = request.json
    chat_id = data["chat_id"]
    sender = data["sender"]
    text = data["text"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, sender, token):
        return jsonify(error="Unauthorized"), 403

    typing_status[chat_id] = {"sender": sender, "text": text}
    return jsonify(success=True)

@app.route("/get_live_typing/<chat_id>")
def get_live_typing(chat_id):
    return jsonify(typing_status.get(chat_id, {}))

@app.route("/mark_online", methods=["POST"])
def mark_online():
    data = request.json
    chat_id = data["chat_id"]
    sender = data["sender"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, sender, token):
        return jsonify(error="Unauthorized"), 403

    session_tokens[(chat_id, sender)][token] = time.time()
    online_status[(chat_id, sender)] = time.time()
    return jsonify(success=True)

@app.route("/is_online/<chat_id>")
def is_online(chat_id):
    now = time.time()
    user_time = online_status.get((chat_id, "user"))
    agent_time = online_status.get((chat_id, "agent"))
    return jsonify(
        user_online=(user_time is not None and now - user_time < 5),
        agent_online=(agent_time is not None and now - agent_time < 30),
        user_last_seen=format_last_seen(user_time),
        agent_last_seen=format_last_seen(agent_time)
    )

@app.route("/clear_chat/<chat_id>", methods=["POST"])
def clear_chat(chat_id):
    data = request.json
    sender = data["sender"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, sender, token):
        return jsonify(error="Unauthorized"), 403

    messages[chat_id] = []
    return jsonify(success=True)

@app.route("/logout_user", methods=["POST"])
@app.route("/logout_agent", methods=["POST"])
def logout():
    data = request.json
    chat_id = data["chat_id"]
    sender = data["sender"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    session_tokens.get((chat_id, sender), {}).pop(token, None)
    return jsonify(success=True)

# Cloud Run entry point
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
