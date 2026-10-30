import os
from flask import Flask, render_template, request, session, redirect, url_for
from services.analyzer import analyze_xray

app = Flask(__name__)
# A static key prevents chat history from wiping every time the Flask server restarts during development
app.secret_key = "replace_this_with_a_secure_random_string_in_production" 

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/results", methods=["GET", "POST"])
def results():
    if "chat_history" not in session:
        session["chat_history"] = []
    
    if "image_count" not in session:
        session["image_count"] = 0

    error_message = None

    if request.method == "POST":
        # Capture the text message from the chat input
        user_text = request.form.get("user_message", "").strip()
        
        uploaded_files = request.files.getlist("xray_image")
        valid_files = [f for f in uploaded_files if f and f.filename != '']

        # If the user typed something, save it to the chat history immediately
        if user_text:
            session["chat_history"].append({
                "sender": "user", 
                "text": user_text
            })
            session.modified = True

        # Enforce the 5-at-a-time limit
        if len(valid_files) > 5:
            error_message = "You can only upload up to 5 images at a time."
        
        # Enforce the 20-total-per-chat limit
        elif session["image_count"] + len(valid_files) > 20:
            remaining = 20 - session["image_count"]
            error_message = f"Chat limit reached. You only have {remaining} upload(s) remaining."
        
        # Handle file uploads
        elif valid_files:
            for file in valid_files:
                # Add user message to UI indicating upload
                session["chat_history"].append({
                    "sender": "user", 
                    "text": f"[Image Uploaded: {file.filename}]"
                })
                
                # Call the local Ollama model
                gemma_response = analyze_xray(file)
                
                # Add the AI's actual analysis to the chat
                session["chat_history"].append({
                    "sender": "bot", 
                    "text": gemma_response
                })
                
                session["image_count"] += 1
            
            session.modified = True

        # Handle pure text messages (no image attached)
        elif user_text:
            session["chat_history"].append({
                "sender": "bot", 
                "text": "I received your message. Please attach an X-ray image if you need visual analysis."
            })
            session.modified = True

    return render_template(
        "results.html", 
        chat_history=session["chat_history"], 
        error_message=error_message, 
        image_count=session.get("image_count", 0)
    )

@app.route("/clear")
def clear_chat():
    session.pop("chat_history", None)
    session.pop("image_count", None)
    return redirect(url_for("results"))

if __name__ == "__main__":
    app.run(debug=True)
