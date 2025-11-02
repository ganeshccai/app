from flask import Flask, request, jsonify
from flask_cors import CORS
import time
import os
import base64

app = Flask(__name__)
# Basic CORS setup; we also add explicit headers below to ensure preflight and
# credentialed requests (Authorization header) are permitted from browsers.
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)


@app.before_request
def _handle_options_preflight():
    # If the browser sends an OPTIONS preflight, reply immediately with the
    # appropriate headers so the real request can proceed.
    if request.method == 'OPTIONS':
        from flask import make_response
        resp = make_response(('', 204))
        origin = request.headers.get('Origin', '*')
        resp.headers['Access-Control-Allow-Origin'] = origin
        resp.headers['Access-Control-Allow-Credentials'] = 'true'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With, X-DIAG'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        return resp


@app.after_request
def _add_cors_headers(response):
    # Ensure CORS headers are present on every response. We reflect the
    # Origin header when present which is compatible with credentialed requests.
    origin = request.headers.get('Origin')
    if origin:
        response.headers['Access-Control-Allow-Origin'] = origin
    else:
        response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With, X-DIAG'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

# In-memory stores
messages = {}
typing_status = {}
online_status = {}
session_tokens = {}  # key: (chat_id, sender), value: {token: timestamp}
message_reactions = {}  # key: message_id, value: {reaction: [user_ids]}
unread_counts = {}  # key: chat_id, value: count


def generate_message_id():
    return str(int(time.time() * 1000))


def verify_token(chat_id, sender, token):
    """Verify session token with expiration check."""
    if not token:
        return False

    tokens = session_tokens.get((chat_id, sender), {})
    if token not in tokens:
        return False

    # Check token expiration (1 hour)
    now = time.time()
    if now - tokens[token] > 3600:
        tokens.pop(token)  # Remove expired token
        return False

    # Update token timestamp
    tokens[token] = now
    return True


def format_last_seen(ts):
    if not ts or ts == 0:
        return ""
    delta = int(time.time() - ts)
    if delta < 60:
        return "just now"
    elif delta < 3600:
        mins = delta // 60
        return f"{mins} min ago"
    else:
        # Format timestamp in 12-hour format
        from datetime import datetime

        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%I:%M %p, %b %d").replace(" 0", " ").lstrip("0")


@app.route("/login", methods=["POST"])
def login():
    data = request.json
    chat_id = data["chat_id"]
    password = data["password"]
    sender = data["sender"]

    active_tokens = session_tokens.get((chat_id, sender), {})
    now = time.time()
    # Clean up expired tokens
    expired_tokens = [
        t for t, ts in active_tokens.items() if now - ts > 3600
    ]  # 1 hour expiry
    for t in expired_tokens:
        active_tokens.pop(t)

    if password == "1":
        # Create new token and cleanup old ones if too many
        token = f"{sender}-{int(now)}"
        tokens_dict = session_tokens.setdefault((chat_id, sender), {})
        tokens_dict[token] = now

        # Keep only the 2 most recent tokens to allow some overlap during reconnection
        if len(tokens_dict) > 2:
            oldest_tokens = sorted(tokens_dict.items(), key=lambda x: x[1])[:-2]
            for old_token, _ in oldest_tokens:
                tokens_dict.pop(old_token)

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

    message_id = generate_message_id()
    msg = {
        "id": message_id,
        "sender": sender,
        "timestamp": time.time(),
        "seen_by": None,
        "reactions": {},
        "reply_to": data.get("reply_to"),  # ID of the message being replied to
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

    # Increment unread count for the other party
    other_party = "agent" if sender == "user" else "user"
    unread_counts.setdefault(chat_id, {})
    unread_counts[chat_id][other_party] = unread_counts[chat_id].get(other_party, 0) + 1

    return jsonify(success=True)


@app.route("/react", methods=["POST"])
def react():
    data = request.json
    chat_id = data["chat_id"]
    message_id = data["message_id"]
    reaction = data["reaction"]
    user_id = data["user_id"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, user_id, token):
        return jsonify(error="Unauthorized"), 403

    chat_messages = messages.get(chat_id, [])
    for msg in chat_messages:
        if msg["id"] == message_id:
            if reaction not in msg["reactions"]:
                msg["reactions"][reaction] = []
            if user_id not in msg["reactions"][reaction]:
                msg["reactions"][reaction].append(user_id)
            return jsonify(success=True)

    return jsonify(error="Message not found"), 404


@app.route("/delete_message", methods=["POST"])
def delete_message():
    data = request.json
    chat_id = data["chat_id"]
    message_id = data["message_id"]
    user_id = data["user_id"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, user_id, token):
        return jsonify(error="Unauthorized"), 403

    chat_messages = messages.get(chat_id, [])
    for i, msg in enumerate(chat_messages):
        if msg["id"] == message_id and msg["sender"] == user_id:
            chat_messages.pop(i)
            return jsonify(success=True)

    return jsonify(error="Message not found or unauthorized"), 404


@app.route("/edit_message", methods=["POST"])
def edit_message():
    data = request.json
    chat_id = data["chat_id"]
    message_id = data["message_id"]
    text = data["text"]
    user_id = data["user_id"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, user_id, token):
        return jsonify(error="Unauthorized"), 403

    chat_messages = messages.get(chat_id, [])
    for msg in chat_messages:
        if msg["id"] == message_id and msg["sender"] == user_id:
            msg["text"] = text
            msg["edited"] = True
            msg["edit_timestamp"] = time.time()
            return jsonify(success=True)

    return jsonify(error="Message not found or unauthorized"), 404


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
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    viewer = request.args.get("viewer")

    if not viewer:
        return jsonify(error="Viewer parameter required"), 400

    if not verify_token(chat_id, viewer, token):
        return jsonify(error="Session expired or invalid"), 403

    active = request.args.get("active") == "true"
    chat = messages.get(chat_id, [])

    if active and chat and chat[-1]["sender"] != viewer:
        chat[-1]["seen_by"] = viewer
        # Reset unread count when active
        if viewer in unread_counts.get(chat_id, {}):
            unread_counts[chat_id][viewer] = 0

    return jsonify(chat)


@app.route("/unread_count/<chat_id>/<user_id>")
def get_unread_count(chat_id, user_id):
    return jsonify(count=unread_counts.get(chat_id, {}).get(user_id, 0))


@app.route("/live_typing", methods=["POST"])
def live_typing():
    data = request.json
    chat_id = data["chat_id"]
    sender = data["sender"]
    text = data["text"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, sender, token):
        return jsonify(error="Unauthorized"), 403

    typing_status[chat_id] = {"sender": sender, "text": text, "timestamp": time.time()}
    return jsonify(success=True)


@app.route("/get_live_typing/<chat_id>")
def get_live_typing(chat_id):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    viewer = request.args.get("viewer")

    if not viewer:
        return jsonify(error="Viewer parameter required"), 400

    if not verify_token(chat_id, viewer, token):
        return jsonify(error="Session expired or invalid"), 403

    # Clean up old typing status (after 5 seconds)
    status = typing_status.get(chat_id, {})
    if status and time.time() - status.get("timestamp", 0) > 5:
        typing_status.pop(chat_id, None)
        return jsonify({})

    return jsonify(status)


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
        agent_last_seen=format_last_seen(agent_time),
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
