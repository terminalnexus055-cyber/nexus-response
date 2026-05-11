import os
import logging
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from supabase import create_client, Client as SupabaseClient

# 1. Logging & Flask Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("NexusEngine")
app = Flask(__name__)

# 2. Infrastructure Credentials
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_AUTH = os.environ.get("TWILIO_AUTH")

# Initialize Clients
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)

@app.route("/", methods=['GET'])
def health_check():
    """Fixes the 404 and allows the GitHub Heartbeat to verify life."""
    return "Nexus System: Online and Guarding.", 200

@app.route("/test-db", methods=['GET'])
def test_db():
    """Diagnostic route to ensure Render can talk to Supabase."""
    try:
        res = supabase.table("clients").select("id").limit(1).execute()
        return f"Database Connection: SUCCESS. Found {len(res.data)} clients.", 200
    except Exception as e:
        logger.error(f"DB Test Failed: {str(e)}")
        return f"Database Connection: FAILED. Check your Render Environment Variables.", 500

@app.route("/voice", methods=['POST'])
def handle_voice():
    """The entry point for a missed call forwarded to Twilio."""
    to_num = request.form.get('To')   # The Merchant's Twilio Number
    from_num = request.form.get('From') # The Lead's Phone Number

    # Reject call to save money; it still triggers this webhook
    response = VoiceResponse()
    response.reject()

    # Database Lookup for the Merchant
    try:
        client_query = supabase.table("clients").select("*").eq("twilio_number", to_num).single().execute()
        
        if client_query.data:
            merchant = client_query.data
            biz_name = merchant.get('business_name', 'our team')
            
            # Log the Lead
            supabase.table("leads").upsert({
                "client_id": merchant['id'],
                "lead_phone": from_num,
                "status": "new_lead"
            }).execute()

            # Send the Intake SMS
            body_text = f"Hi! This is the assistant for {biz_name}. We missed your call. How can we help you today?"
            twilio_client.messages.create(body=body_text, from_=to_num, to=from_num)
            logger.info(f"Sent text to {from_num} for {biz_name}")
        else:
            logger.warning(f"No merchant found for number: {to_num}")

    except Exception as e:
        logger.error(f"Error in Voice Webhook: {str(e)}")

    return str(response)

@app.route("/webhook", methods=['POST'])
def heartbeat_endpoint():
    """Endpoint for the GitHub Action to ping every 10 minutes."""
    return "Heartbeat Received", 200

if __name__ == "__main__":
    # Use Render's default port 10000 or fallback to 8000
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
            
