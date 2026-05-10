import os
from flask import Flask, request
from twilio.rest import Client
from supabase import create_client, Client as SupabaseClient

app = Flask(__name__)

# Credentials from Render Environment Variables
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") # Use Service Role for backend
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_AUTH = os.environ.get("TWILIO_AUTH")
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)

@app.route("/webhook", methods=['POST'])
def webhook():
    # 1. Capture the 'To' and 'From' numbers
    to_number = request.form.get('To')   # The Merchant's Twilio Number
    from_number = request.form.get('From') # The Lead's Phone Number

    # 2. Database Lookup: Who does this number belong to?
    client_data = supabase.table("clients").select("*").eq("twilio_number", to_number).single().execute()
    
    if not client_data.data:
        return "Merchant not found", 404

    merchant = client_data.data
    business_name = merchant['business_name']
    ai_persona = merchant['ai_persona']

    # 3. Create or Update the Lead in the 'leads' table
    # This acts as the 'session' tracker for the AI
    lead_entry = {
        "client_id": merchant['id'],
        "lead_phone": from_number,
        "status": "active"
    }
    supabase.table("leads").upsert(lead_entry).execute()

    # 4. The Response (Elite Message)
    # Eventually, we will plug the AI here. For now, we use the customized name.
    response_text = f"Hi! This is the assistant at {business_name}. We missed your call but want to help immediately. Are you looking for a quote or an emergency booking?"

    twilio_client.messages.create(
        body=response_text,
        from_=to_number,
        to=from_number
    )

    return "OK", 200
    
