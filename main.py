from flask import Flask, request, jsonify
from flask_cors import CORS
import time
import os
import uuid  # NEW: For unique message IDs
from datetime import datetime, timezone  # NEW: For standardized timestamps

app = Flask(__name__)
CORS(app)

# In-memory stores
messages = {}  # key: chat_id, value: list of message objects
typing_status = {}
online_status = {}
session_tokens = {}  # key: (chat_id, sender), value: {token: timestamp}

def verify_token(chat_id, sender, token):
    """Checks if a session token is valid."""
    return token in session_tokens.get((chat_id, sender), {})

def format_last_seen(ts):
    """Formats a timestamp into a 'last seen' string."""
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

# --- Helper function to find a message by ID ---
def find_message(chat_id, message_id):
    """Finds a message in the store by its ID."""
    for msg in messages.get(chat_id, []):
        if msg.get('id') == message_id:
            return msg
    return None

@app.route("/login", methods=["POST"])
def login():
    """Handles user and agent login."""
    data = request.json
    chat_id = data["chat_id"]
    password = data["password"]
    sender = data["sender"]

    active_tokens = session_tokens.get((chat_id, sender), {})
    now = time.time()
    # Basic rate limiting
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
    """Handles sending a new message (text or image)."""
    data = request.json
    chat_id = data["chat_id"]
    sender = data["sender"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, sender, token):
        return jsonify(error="Unauthorized"), 403

    # NEW: Create the base message object with new fields
    msg = {
        "id": str(uuid.uuid4()),  # Unique ID for every message
        "sender": sender,
        "timestamp": datetime.now(timezone.utc).isoformat(),  # Standard timestamp
        "seen_by": None,
        "reactions": {},  # For emoji reactions (Req 3)
        "reply_to_id": data.get("reply_to_id"),  # For replies (Req 4)
        "deleted": False  # For deletes (Req 6)
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

@app.route("/messages/<chat_id>")
def get_messages(chat_id):
    """Retrieves and processes all messages for a chat."""
    viewer = request.args.get("viewer")
    active = request.args.get("active") == "true"
    
    chat_messages = messages.get(chat_id, [])
    
    # NEW: Build a map for fast reply lookups
    message_map = {msg['id']: msg for msg in chat_messages if 'id' in msg}
    
    processed_messages = []
    
    for i, msg in enumerate(chat_messages):
        processed_msg = msg.copy()

        # REQ 6: Handle deleted messages
        if msg.get("deleted"):
            processed_msg["text"] = "This message was deleted"
            processed_msg["type"] = "deleted"
            processed_msg["url"] = None
            processed_msg["reactions"] = {} # Clear reactions
            processed_msg["reply_to_id"] = None # Clear reply
        
        # REQ 4: Resolve reply_to_id
        reply_to_id = msg.get("reply_to_id")
        if reply_to_id and reply_to_id in message_map:
            original_msg = message_map[reply_to_id]
            
            # Determine text for the reply preview
            reply_text = "Original message was deleted"
            if not original_msg.get("deleted"):
                reply_text = original_msg.get("text", "Image")

            processed_msg["reply_to_data"] = {
                "sender": original_msg["sender"],
                "text": reply_text
            }

        # REQ 3: Format reactions for frontend
        # From: {"❤️": ["user"], "👍": ["user", "agent"]}
        # To: [{"emoji": "❤️", "count": 1}, {"emoji": "👍", "count": 2}]
        formatted_reactions = []
        for emoji, senders in msg.get("reactions", {}).items():
            if len(senders) > 0:
                formatted_reactions.append({"emoji": emoji, "count": len(senders)})
        processed_msg["reactions"] = formatted_reactions

        # Handle "seen" status
        is_last = (i == len(chat_messages) - 1)
        if active and is_last and msg["sender"] != viewer and not msg.get("seen_by"):
            # IMPORTANT: Update the *original* message in the store
            msg["seen_by"] = viewer
            processed_msg["seen_by"] = viewer # Update the copy as well

        processed_messages.append(processed_msg)

    return jsonify(processed_messages)

@app.route("/live_typing", methods=["POST"])
def live_typing():
    """Updates the typing status for a sender."""
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
    """Gets the current typing status for a chat."""
    return jsonify(typing_status.get(chat_id, {}))

@app.route("/mark_online", methods=["POST"])
def mark_online():
    """Marks a user/agent as online and refreshes their session token."""
    data = request.json
    chat_id = data["chat_id"]
    sender = data["sender"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, sender, token):
        return jsonify(error="Unauthorized"), 403

    # Refresh token timestamp
    session_tokens[(chat_id, sender)][token] = time.time()
    # Update online status timestamp
    online_status[(chat_id, sender)] = time.time()
    return jsonify(success=True)

@app.route("/is_online/<chat_id>")
def is_online(chat_id):
    """Checks the online status of both user and agent."""
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
    """Clears all messages for a chat."""
    data = request.json
    sender = data["sender"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, sender, token):
        return jsonify(error="Unauthorized"), 403

    messages[chat_id] = []
    return jsonify(success=True)

# NEW ENDPOINT: For Reactions (Req 3)
@app.route("/react", methods=["POST"])
def react_to_message():
    """Adds or removes a reaction from a message."""
    data = request.json
    chat_id = data["chat_id"]
    sender = data["sender"]
    message_id = data["message_id"]
    emoji = data["emoji"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, sender, token):
        return jsonify(error="Unauthorized"), 403

    target_msg = find_message(chat_id, message_id)
    if not target_msg:
        return jsonify(error="Message not found"), 404

    if 'reactions' not in target_msg:
        target_msg['reactions'] = {}
        
    if emoji not in target_msg['reactions']:
        target_msg['reactions'][emoji] = []

    # Toggle the reaction: add if not there, remove if it is
    if sender not in target_msg['reactions'][emoji]:
        target_msg['reactions'][emoji].append(sender)
    else:
        target_msg['reactions'][emoji].remove(sender)
        
    return jsonify(success=True)

# NEW ENDPOINT: For Deleting (Req 6)
@app.route("/delete_message", methods=["POST"])
def delete_message():
    """Deletes a single message, if owned by the sender."""
    data = request.json
    chat_id = data["chat_id"]
    sender = data["sender"] # The user requesting the delete
    message_id = data["message_id"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not verify_token(chat_id, sender, token):
        return jsonify(error="Unauthorized"), 403

    target_msg = find_message(chat_id, message_id)
    if not target_msg:
        return jsonify(error="Message not found"), 404

    # Security check: only allow sender to delete their own message
    if target_msg["sender"] != sender:
        return jsonify(error="Forbidden: You can only delete your own messages"), 403
        
    # "Tombstone" the message instead of truly deleting it
    target_msg["deleted"] = True
    target_msg["text"] = None # Clear content
    target_msg["url"] = None # Clear content
    
    return jsonify(success=True)


@app.route("/logout_user", methods=["POST"])
@app.route("/logout_agent", methods=["POST"])
def logout():
    """Logs out a user/agent by invalidating their token."""
    data = request.json
    chat_id = data["chat_id"]
    sender = data["sender"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    # Remove the specific token
    session_tokens.get((chat_id, sender), {}).pop(token, None)
    return jsonify(success=True)

# Cloud Run entry point
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
