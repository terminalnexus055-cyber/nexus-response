import os
import logging
import threading
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from supabase import create_client, Client as SupabaseClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("NexusEngine")

app = Flask(__name__)

def clean_env(value):
    if value is None:
        return value
    return ''.join(char for char in value if ord(char) < 128)

SUPABASE_URL = clean_env(os.environ.get("SUPABASE_URL"))
SUPABASE_KEY = clean_env(os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))
TWILIO_SID = clean_env(os.environ.get("TWILIO_SID"))
TWILIO_AUTH = clean_env(os.environ.get("TWILIO_AUTH"))

supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)

def process_lead(to_num, from_num):
    try:
        result = supabase.table("clients").select("*").eq("twilio_number", to_num).single().execute()
        if result.data:
            merchant = result.data
            biz_name = merchant.get("business_name", "our team")
            supabase.table("leads").upsert({
                "client_id": merchant["id"],
                "lead_phone": from_num,
                "status": "new_lead"
            }).execute()
            twilio_client.messages.create(
                body=f"Hi! You've reached the assistant for {biz_name}. We missed your call — how can we help you today?",
                from_=to_num,
                to=from_num
            )
            logger.info(f"SMS sent to {from_num} for {biz_name}")
        else:
            logger.warning(f"No merchant found for: {to_num}")
    except Exception as e:
        logger.error(f"Lead processing error: {str(e)}")

@app.route("/", methods=["GET"])
def health_check():
    return "Nexus Engine: Online.", 200

@app.route("/test-db", methods=["GET"])
def test_db():
    try:
        res = supabase.table("clients").select("id").limit(1).execute()
        return f"DB OK. Clients found: {len(res.data)}", 200
    except Exception as e:
        return f"DB FAILED: {str(e)}", 500

@app.route("/voice", methods=["POST"])
def handle_voice():
    to_num = request.form.get("To")
    from_num = request.form.get("From")
    response = VoiceResponse()
    response.reject()
    threading.Thread(target=process_lead, args=(to_num, from_num)).start()
    return str(response)

@app.route("/webhook", methods=["POST", "GET"])
def heartbeat():
    return "Heartbeat OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
