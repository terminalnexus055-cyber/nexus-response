import os
from flask import Flask, request
from twilio.rest import Client
from supabase import create_client

app = Flask(__name__)

# Credentials from Render Env
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))
twilio_client = Client(os.environ.get("TWILIO_SID"), os.environ.get("TWILIO_AUTH"))

@app.route("/webhook", methods=['POST'])
def webhook():
    to_num = request.form.get('To')   # The Merchant's Twilio Number
    from_num = request.form.get('From') # Your Personal Phone Number

    # 1. Pipeline Test: Database Lookup
    try:
        merchant = supabase.table("clients").select("*").eq("twilio_number", to_num).single().execute()
        
        if merchant.data:
            biz_name = merchant.data['business_name']
            # 2. Log the event in the 'leads' table
            supabase.table("leads").upsert({
                "lead_phone": from_num, 
                "status": "pipeline_test_active"
            }).execute()
            
            response_text = f"Nexus System Check: You have reached the elite assistant for {biz_name}. Our pipeline is 100% active."
        else:
            response_text = "Nexus System Check: Number received, but not found in Supabase clients table."

    except Exception as e:
        response_text = f"Nexus System Check: Error connecting to Supabase: {str(e)}"

    # 3. Send the response via Twilio
    twilio_client.messages.create(
        body=response_text,
        from_=to_num,
        to=from_num
    )

    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
    
