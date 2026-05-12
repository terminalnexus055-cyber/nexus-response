import os
import logging
import threading
import json
from flask import Flask, request, render_template, jsonify
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from supabase import create_client, Client as SupabaseClient
from groq import Groq
from datetime import datetime, timezone

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
GROQ_API_KEY = clean_env(os.environ.get("GROQ_API_KEY"))
TWILIO_NUMBER = clean_env(os.environ.get("TWILIO_NUMBER", "+17542897608"))

supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)
groq_client = Groq(api_key=GROQ_API_KEY)

# ─── UTILITIES ───────────────────────────────────────────────────────────────

def send_sms(to_num, from_num, body):
    twilio_client.messages.create(body=body, from_=from_num, to=to_num)

def is_trial_active(merchant):
    trial_expires = merchant.get("trial_expires")
    if not trial_expires:
        return True
    expiry = datetime.fromisoformat(trial_expires.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) < expiry

def build_triage_menu(merchant):
    biz_name = merchant.get("business_name", "our team")
    opt1 = merchant.get("triage_option_1", "Emergency")
    opt2 = merchant.get("triage_option_2", "Book Appointment")
    opt3 = merchant.get("triage_option_3", "General Query")
    return (
        f"Hi! You've reached the assistant for {biz_name}. "
        f"We missed your call but we're here to help.\n\n"
        f"Reply with a number:\n"
        f"1. {opt1}\n"
        f"2. {opt2}\n"
        f"3. {opt3}"
    )

def alert_merchant(merchant, lead_phone, reason):
    owner_phone = merchant.get("owner_phone")
    biz_name = merchant.get("business_name", "your business")
    twilio_num = merchant.get("twilio_number")
    if owner_phone:
        send_sms(
            owner_phone, twilio_num,
            f"NEXUS ALERT — {biz_name}\n"
            f"Lead {lead_phone} needs immediate attention.\n"
            f"Reason: {reason}\nCall them back now."
        )
        logger.info(f"Emergency alert sent to merchant for {lead_phone}")

def get_ai_response(merchant, lead_phone, user_message, stage, history):
    biz_name = merchant.get("business_name", "this business")
    opt1 = merchant.get("triage_option_1", "Emergency")
    opt2 = merchant.get("triage_option_2", "Book Appointment")
    opt3 = merchant.get("triage_option_3", "General Query")

    system_prompt = f"""You are a professional AI receptionist for {biz_name}.
Your job is to assist leads who missed their call.
Current stage: {stage}
Services: {opt1}, {opt2}, {opt3}

Rules:
- Keep responses SHORT — this is SMS, max 2-3 sentences
- Never invent business details
- Booking: collect name then preferred date/time
- Emergency: confirm urgency, say team is alerted, end with "Someone will call you immediately."
- Query: answer helpfully, stay in scope
- Booking complete signal: end with "The team will confirm shortly."
- Never break character
"""
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        try:
            past = json.loads(history) if isinstance(history, str) else history
            messages.extend(past)
        except:
            pass
    messages.append({"role": "user", "content": user_message})

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=150,
        temperature=0.4
    )
    return response.choices[0].message.content.strip()

# ─── CORE LEAD FLOW ──────────────────────────────────────────────────────────

def process_lead(to_num, from_num):
    try:
        result = supabase.table("clients").select("*").eq("twilio_number", to_num).single().execute()
        if not result.data:
            logger.warning(f"No merchant found for: {to_num}")
            return
        merchant = result.data
        if not is_trial_active(merchant):
            logger.info(f"Trial expired for {merchant.get('business_name')}")
            return

        supabase.table("leads").upsert({
            "client_id": merchant["id"],
            "lead_phone": from_num,
            "status": "new_lead",
            "conversation_stage": "triage_sent",
            "chat_history": json.dumps([])
        }).execute()

        send_sms(from_num, to_num, build_triage_menu(merchant))
        logger.info(f"Triage menu sent to {from_num} for {merchant.get('business_name')}")
    except Exception as e:
        logger.error(f"process_lead error: {str(e)}")

def process_reply(to_num, from_num, body):
    try:
        result = supabase.table("clients").select("*").eq("twilio_number", to_num).single().execute()
        if not result.data:
            return
        merchant = result.data
        if not is_trial_active(merchant):
            return

        lead_result = supabase.table("leads").select("*") \
            .eq("lead_phone", from_num) \
            .eq("client_id", merchant["id"]) \
            .order("created_at", desc=True) \
            .limit(1).execute()

        if not lead_result.data:
            process_lead(to_num, from_num)
            return

        lead = lead_result.data[0]
        stage = lead.get("conversation_stage", "triage_sent")
        history = lead.get("chat_history", "[]")
        body_clean = body.strip()

        opt1 = merchant.get("triage_option_1", "Emergency")
        opt2 = merchant.get("triage_option_2", "Book Appointment")
        opt3 = merchant.get("triage_option_3", "General Query")

        if stage == "triage_sent":
            if body_clean == "1":
                new_stage = "emergency"
                triage_selection = opt1
                alert_merchant(merchant, from_num, opt1)
            elif body_clean == "2":
                new_stage = "booking"
                triage_selection = opt2
            elif body_clean == "3":
                new_stage = "query"
                triage_selection = opt3
            else:
                send_sms(from_num, to_num, "Please reply with 1, 2, or 3 to select an option.")
                return

            ai_reply = get_ai_response(merchant, from_num, body_clean, new_stage, history)
            try:
                past = json.loads(history) if isinstance(history, str) else []
            except:
                past = []
            past.append({"role": "user", "content": body_clean})
            past.append({"role": "assistant", "content": ai_reply})

            supabase.table("leads").update({
                "conversation_stage": new_stage,
                "triage_selection": triage_selection,
                "chat_history": json.dumps(past),
                "status": "in_progress"
            }).eq("id", lead["id"]).execute()

            send_sms(from_num, to_num, ai_reply)

        elif stage in ["emergency", "booking", "query"]:
            try:
                past = json.loads(history) if isinstance(history, str) else []
            except:
                past = []

            ai_reply = get_ai_response(merchant, from_num, body_clean, stage, past)
            past.append({"role": "user", "content": body_clean})
            past.append({"role": "assistant", "content": ai_reply})

            new_stage = stage
            new_status = "in_progress"
            if "confirmed shortly" in ai_reply.lower() or "call you immediately" in ai_reply.lower():
                new_stage = "complete"
                new_status = "qualified"

            supabase.table("leads").update({
                "chat_history": json.dumps(past),
                "conversation_stage": new_stage,
                "status": new_status
            }).eq("id", lead["id"]).execute()

            send_sms(from_num, to_num, ai_reply)

        else:
            send_sms(from_num, to_num, f"Thanks for reaching out to {merchant.get('business_name')}. The team will be in touch shortly.")

    except Exception as e:
        logger.error(f"process_reply error: {str(e)}")

# ─── ROUTES ──────────────────────────────────────────────────────────────────

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

@app.route("/sms", methods=["POST"])
def handle_sms():
    to_num = request.form.get("To")
    from_num = request.form.get("From")
    body = request.form.get("Body", "")
    threading.Thread(target=process_reply, args=(to_num, from_num, body)).start()
    return "", 200

@app.route("/webhook", methods=["POST", "GET"])
def heartbeat():
    return "Heartbeat OK", 200

# ─── DASHBOARD ───────────────────────────────────────────────────────────────

@app.route("/dashboard/<token>", methods=["GET"])
def merchant_dashboard(token):
    try:
        result = supabase.table("clients").select("*").eq("dashboard_token", token).single().execute()
        if not result.data:
            return "Dashboard not found.", 404
        merchant = result.data
        leads_result = supabase.table("leads").select("*") \
            .eq("client_id", merchant["id"]) \
            .order("created_at", desc=True).execute()
        leads = leads_result.data or []
        return render_template("dashboard.html", merchant=merchant, leads=leads)
    except Exception as e:
        logger.error(f"Dashboard error: {str(e)}")
        return "Error loading dashboard.", 500

@app.route("/api/leads/<token>", methods=["GET"])
def get_leads_api(token):
    try:
        result = supabase.table("clients").select("*").eq("dashboard_token", token).single().execute()
        if not result.data:
            return jsonify({"error": "Not found"}), 404
        merchant = result.data
        leads_result = supabase.table("leads").select("*") \
            .eq("client_id", merchant["id"]) \
            .order("created_at", desc=True).execute()
        return jsonify(leads_result.data or [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── DEMO ────────────────────────────────────────────────────────────────────

@app.route("/demo", methods=["GET"])
def demo_page():
    return render_template("demo.html")

@app.route("/demo/start", methods=["POST"])
def demo_start():
    try:
        industry = request.form.get("industry")
        phone = request.form.get("phone")

        industries = {
            "dentist": {
                "name": "SmileCare Dental",
                "opt1": "Dental Emergency / Severe Pain",
                "opt2": "Book an Appointment",
                "opt3": "General Question / Pricing"
            },
            "plumber": {
                "name": "QuickFix Plumbing",
                "opt1": "Emergency / Burst Pipe",
                "opt2": "Book a Visit",
                "opt3": "Get a Quote"
            },
            "lawyer": {
                "name": "Elite Legal Group",
                "opt1": "Urgent Legal Matter",
                "opt2": "Schedule Consultation",
                "opt3": "General Inquiry"
            },
            "realestate": {
                "name": "Prime Properties",
                "opt1": "Urgent Property Matter",
                "opt2": "Schedule a Viewing",
                "opt3": "Property Inquiry"
            },
            "medspa": {
                "name": "Luxe MedSpa",
                "opt1": "Urgent Concern / Reaction",
                "opt2": "Book a Treatment",
                "opt3": "Pricing / Services"
            },
            "hvac": {
                "name": "CoolAir HVAC",
                "opt1": "Emergency / No Heat or AC",
                "opt2": "Schedule a Service",
                "opt3": "Get a Quote"
            }
        }

        config = industries.get(industry, industries["dentist"])
        menu = (
            f"Hi! You've reached the assistant for {config['name']}.\n"
            f"We missed your call but we're here to help.\n\n"
            f"Reply with a number:\n"
            f"1. {config['opt1']}\n"
            f"2. {config['opt2']}\n"
            f"3. {config['opt3']}"
        )

        twilio_client.messages.create(body=menu, from_=TWILIO_NUMBER, to=phone)
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Demo start error: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
