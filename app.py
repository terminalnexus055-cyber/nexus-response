import os
import logging
from flask import Flask, request
from twilio.rest import Client

# 1. Setup high-visibility logging for the cloud console
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("NexusCloud")

app = Flask(__name__)

# 2. Secure Configuration (Pulling from Cloud Environment Variables)
TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_AUTH = os.environ.get("TWILIO_AUTH")
TWILIO_NUMBER = os.environ.get("TWILIO_NUMBER")
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "our team")

# Initialize Twilio Client
client = Client(TWILIO_SID, TWILIO_AUTH)

@app.route("/", methods=['GET'])
def health_check():
    """Confirms the server is live for Render's monitoring"""
    return "Nexus Response Engine: ACTIVE", 200

@app.route("/webhook", methods=['POST'])
def handle_missed_call():
    """The core logic that catches the webhook and fires the SMS"""
    from_number = request.form.get('From')
    
    if not from_number:
        logger.warning("Webhook received with no 'From' number.")
        return "No number found", 400

    logger.info(f"Incoming call detected from: {from_number}")
    
    # The 'Irrational' Instant Response
    body_text = (
        f"Hi! This is the {BUSINESS_NAME} safety-net. "
        "We missed your call but wanted to reach out immediately. "
        "How can we help you today?"
    )

    try:
        message = client.messages.create(
            body=body_text,
            from_=TWILIO_NUMBER,
            to=from_number
        )
        logger.info(f"SMS Successfully Sent to {from_number}. SID: {message.sid}")
    except Exception as e:
        logger.error(f"Critical Failure: {str(e)}")
        return f"Error: {str(e)}", 500

    return "Success", 200

if __name__ == "__main__":
    # Locally we use port 8000, Render uses whatever PORT they assign
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
          
