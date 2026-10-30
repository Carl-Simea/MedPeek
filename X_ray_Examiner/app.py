import os
import requests
from flask import Flask, render_template, request, session, redirect, url_for
from services.analyzer import analyze_xray

app = Flask(__name__)
app.secret_key = "replace_this_with_a_secure_random_string_in_production" 

@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        session["username"] = request.form.get("username", "Guest").strip()
        
        try:
            requests.post(
                "http://localhost:11434/api/generate", 
                json={"model": "llava", "keep_alive": "1h"}, 
                timeout=3
            )
        except requests.exceptions.RequestException:
            pass 
            
        return redirect(url_for("results"))
    
    return render_template("home.html")

@app.route("/results", methods=["GET", "POST"])
def results():
    username = session.get("username", "Guest")
    
    if "chat_history" not in session or not session["chat_history"]:
        session["chat_history"] = [{
            "sender": "bot", 
            "text": f"Hello {username}. Welcome to MedPeek. You can select a sample case below or upload a scan with your symptoms to receive a structured analysis."
        }]
    
    if "image_count" not in session:
        session["image_count"] = 0

    error_message = None

    if request.method == "POST":
        user_text = request.form.get("user_message", "").strip()
        uploaded_files = request.files.getlist("xray_image")
        valid_files = [f for f in uploaded_files if f and f.filename != '']

        if user_text:
            session["chat_history"].append({
                "sender": "user", 
                "text": user_text
            })
            session.modified = True

        if len(valid_files) > 5:
            error_message = "You can only upload up to 5 images at a time."
        
        elif session["image_count"] + len(valid_files) > 20:
            remaining = 20 - session["image_count"]
            error_message = f"Chat limit reached. You only have {remaining} upload(s) remaining."
        
        elif valid_files:
            for file in valid_files:
                session["chat_history"].append({
                    "sender": "user", 
                    "text": f"[Image Uploaded: {file.filename}]"
                })
                
                gemma_response = analyze_xray(file)
                
                structured_html = f"""
                <div class="breakdown-wrapper">
                    <div class="breakdown-card">
                        <div class="breakdown-title">What the image shows</div>
                        <div class="breakdown-content">{gemma_response}</div>
                    </div>
                    <div class="breakdown-card">
                        <div class="breakdown-title d-flex justify-content-between align-items-center">
                            Findings
                            <span class="badge-confidence">Moderate Confidence</span>
                        </div>
                        <div class="breakdown-content">
                            Review indicates potential structural anomalies. Correlate with clinical symptoms.
                        </div>
                    </div>
                    <div class="breakdown-card">
                        <div class="breakdown-title">Next Steps & Referrals</div>
                        <div class="breakdown-content">
                            <ul>
                                <li>Schedule an appointment with an Orthopedic specialist.</li>
                                <li>Do not put weight on the affected area until reviewed.</li>
                                <li>Keep the area immobilized.</li>
                            </ul>
                        </div>
                    </div>
                </div>
                """
                
                session["chat_history"].append({
                    "sender": "bot", 
                    "text": structured_html
                })
                
                session["image_count"] += 1
            
            session.modified = True

        elif user_text:
            session["chat_history"].append({
                "sender": "bot", 
                # Updated fallback response to dynamically use the username
                "text": f"I received your message, {username}. Please attach an X-ray image if you need visual analysis."
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