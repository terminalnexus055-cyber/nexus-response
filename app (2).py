import os
import logging
import threading
import json
import smtplib
import requests as http_requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

SUPABASE_URL  = clean_env(os.environ.get("SUPABASE_URL"))
SUPABASE_KEY  = clean_env(os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))
TWILIO_SID    = clean_env(os.environ.get("TWILIO_SID"))
TWILIO_AUTH   = clean_env(os.environ.get("TWILIO_AUTH"))
GROQ_API_KEY  = clean_env(os.environ.get("GROQ_API_KEY"))
TWILIO_NUMBER = clean_env(os.environ.get("TWILIO_NUMBER", "+17542897608"))
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
ADMIN_KEY     = os.environ.get("NEXUS_ADMIN_KEY", "nexus-admin-2025")

supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)
groq_client   = Groq(api_key=GROQ_API_KEY)


# ─── UTILITIES ────────────────────────────────────────────────────────────────

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
    biz_name    = merchant.get("business_name", "your business")
    twilio_num  = merchant.get("twilio_number")
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
- Booking complete: end with "The team will confirm shortly."
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

def log_activity(entity_type, entity_id, event, detail=None):
    """Insert a row into the activities table. Fire-and-forget; never raises."""
    try:
        supabase.table("activities").insert({
            "entity_type": entity_type,
            "entity_id":   str(entity_id) if entity_id else None,
            "event":       event,
            "detail":      detail,
        }).execute()
    except Exception as exc:
        logger.warning(f"log_activity failed: {exc}")


# ─── LEAD FLOW ────────────────────────────────────────────────────────────────

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
            "client_id":          merchant["id"],
            "lead_phone":         from_num,
            "status":             "new_lead",
            "conversation_stage": "triage_sent",
            "chat_history":       json.dumps([])
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

        lead       = lead_result.data[0]
        stage      = lead.get("conversation_stage", "triage_sent")
        history    = lead.get("chat_history", "[]")
        body_clean = body.strip()

        opt1 = merchant.get("triage_option_1", "Emergency")
        opt2 = merchant.get("triage_option_2", "Book Appointment")
        opt3 = merchant.get("triage_option_3", "General Query")

        if stage == "triage_sent":
            if body_clean == "1":
                new_stage        = "emergency"
                triage_selection = opt1
                alert_merchant(merchant, from_num, opt1)
            elif body_clean == "2":
                new_stage        = "booking"
                triage_selection = opt2
            elif body_clean == "3":
                new_stage        = "query"
                triage_selection = opt3
            else:
                send_sms(from_num, to_num, "Please reply with 1, 2, or 3 to select an option.")
                return

            ai_reply = get_ai_response(merchant, from_num, body_clean, new_stage, history)
            try:
                past = json.loads(history) if isinstance(history, str) else []
            except:
                past = []
            past.append({"role": "user",      "content": body_clean})
            past.append({"role": "assistant", "content": ai_reply})

            supabase.table("leads").update({
                "conversation_stage": new_stage,
                "triage_selection":   triage_selection,
                "chat_history":       json.dumps(past),
                "status":             "in_progress"
            }).eq("id", lead["id"]).execute()
            send_sms(from_num, to_num, ai_reply)

        elif stage in ["emergency", "booking", "query"]:
            try:
                past = json.loads(history) if isinstance(history, str) else []
            except:
                past = []

            ai_reply = get_ai_response(merchant, from_num, body_clean, stage, past)
            past.append({"role": "user",      "content": body_clean})
            past.append({"role": "assistant", "content": ai_reply})

            new_stage  = stage
            new_status = "in_progress"
            if "confirmed shortly" in ai_reply.lower() or "call you immediately" in ai_reply.lower():
                new_stage  = "complete"
                new_status = "qualified"

            supabase.table("leads").update({
                "chat_history":       json.dumps(past),
                "conversation_stage": new_stage,
                "status":             new_status
            }).eq("id", lead["id"]).execute()
            send_sms(from_num, to_num, ai_reply)

        else:
            send_sms(from_num, to_num, f"Thanks for reaching out to {merchant.get('business_name')}. The team will be in touch shortly.")

    except Exception as e:
        logger.error(f"process_reply error: {str(e)}")


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/test-db", methods=["GET"])
def test_db():
    try:
        res = supabase.table("clients").select("id").limit(1).execute()
        return f"DB OK. Clients found: {len(res.data)}", 200
    except Exception as e:
        return f"DB FAILED: {str(e)}", 500

@app.route("/voice", methods=["POST"])
def handle_voice():
    to_num   = request.form.get("To")
    from_num = request.form.get("From")
    response = VoiceResponse()
    response.reject()
    threading.Thread(target=process_lead, args=(to_num, from_num)).start()
    return str(response)

@app.route("/sms", methods=["POST"])
def handle_sms():
    to_num   = request.form.get("To")
    from_num = request.form.get("From")
    body     = request.form.get("Body", "")
    threading.Thread(target=process_reply, args=(to_num, from_num, body)).start()
    return "", 200

@app.route("/webhook", methods=["POST", "GET"])
def heartbeat():
    return "Heartbeat OK", 200

@app.route("/warmup", methods=["GET"])
def warmup():
    try:
        supabase.table("clients").select("id").limit(1).execute()
        logger.info("Warmup ping received.")
        return {
            "status":    "warm",
            "service":   "nexus-response",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }, 200
    except Exception as e:
        logger.error(f"Warmup failed: {str(e)}")
        return {"status": "error", "message": str(e)}, 500

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


# ─── DASHBOARD ───────────────────────────────────────────────────────────────

@app.route("/dashboard/<token>", methods=["GET"])
def merchant_dashboard(token):
    try:
        result = supabase.table("clients").select("*").eq("dashboard_token", token).single().execute()
        if not result.data:
            return "Dashboard not found.", 404
        merchant     = result.data
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
        merchant     = result.data
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
        phone    = request.form.get("phone")

        industries = {
            "dentist":     {"name": "SmileCare Dental",  "opt1": "Dental Emergency / Severe Pain",  "opt2": "Book an Appointment",    "opt3": "General Question / Pricing"},
            "plumber":     {"name": "QuickFix Plumbing", "opt1": "Emergency / Burst Pipe",           "opt2": "Book a Visit",           "opt3": "Get a Quote"},
            "lawyer":      {"name": "Elite Legal Group", "opt1": "Urgent Legal Matter",              "opt2": "Schedule Consultation",  "opt3": "General Inquiry"},
            "realestate":  {"name": "Prime Properties",  "opt1": "Urgent Property Matter",           "opt2": "Schedule a Viewing",     "opt3": "Property Inquiry"},
            "medspa":      {"name": "Luxe MedSpa",       "opt1": "Urgent Concern / Reaction",        "opt2": "Book a Treatment",       "opt3": "Pricing / Services"},
            "hvac":        {"name": "CoolAir HVAC",      "opt1": "Emergency / No Heat or AC",        "opt2": "Schedule a Service",     "opt3": "Get a Quote"}
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


# ─── INFRA ───────────────────────────────────────────────────────────────────

@app.route("/robots.txt", methods=["GET"])
def robots():
    return app.send_static_file("robots.txt")

@app.route("/sitemap.xml", methods=["GET"])
def sitemap():
    return app.send_static_file("sitemap.xml"), 200, {"Content-Type": "application/xml"}


# ─── CONTACT / BOOKING ───────────────────────────────────────────────────────

@app.route("/contact", methods=["POST"])
def contact():
    try:
        name     = request.form.get("name", "")
        business = request.form.get("business", "")
        phone    = request.form.get("phone", "")
        email    = request.form.get("email", "")
        slot     = request.form.get("slot", "")

        logger.info(f"Setup request: {name} | {business} | {slot}")

        msg            = MIMEMultipart()
        msg["From"]    = "hello@nexusresponse.com.ng"
        msg["To"]      = "hello@nexusresponse.com.ng"
        msg["Subject"] = f"New Setup Request — {name} ({business})"

        body = (
            f"NEW SETUP APPOINTMENT BOOKED\n\n"
            f"Name:     {name}\n"
            f"Business: {business}\n"
            f"Phone:    {phone}\n"
            f"Email:    {email}\n"
            f"Slot:     {slot}\n\n"
            f"Sent from nexusresponse.com.ng/demo"
        )
        msg.attach(MIMEText(body, "plain"))

        smtp_user = os.environ.get("BREVO_SMTP_USER", "")
        smtp_pass = os.environ.get("BREVO_SMTP_PASS", "")

        if smtp_user and smtp_pass:
            with smtplib.SMTP("smtp-relay.brevo.com", 587) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            logger.info(f"Contact email sent for {name}")

        return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(f"Contact error: {str(e)}")
        return jsonify({"success": False}), 200


# ─── ACTIVATION FLOW ─────────────────────────────────────────────────────────

@app.route("/activate", methods=["GET"])
def activate_page():
    return render_template("activate.html")

@app.route("/activate", methods=["POST"])
def activate_submit():
    try:
        name       = request.form.get("name", "")
        email      = request.form.get("email", "")
        phone      = request.form.get("phone", "")
        business   = request.form.get("business", "")
        industry   = request.form.get("industry", "")
        website    = request.form.get("website", "")
        volume     = request.form.get("volume", "")
        followup   = request.form.get("followup", "")
        pain       = request.form.get("pain", "")
        slot_label = request.form.get("slot_label", "")
        slot_date  = request.form.get("slot_date", "")
        slot_time  = request.form.get("slot_time", "")

        logger.info(f"Activation: {name} | {business} | {slot_label}")

        # Insert appointment
        supabase.table("appointments").insert({
            "slot_date":        slot_date,
            "slot_time":        slot_time,
            "slot_label":       slot_label,
            "business_name":    business,
            "contact_name":     name,
            "contact_email":    email,
            "contact_phone":    phone,
            "industry":         industry,
            "call_volume":      volume,
            "follow_up_method": followup,
            "pain_point":       pain,
            "status":           "confirmed"
        }).execute()

        # Insert prospect into outreach pipeline
        try:
            p_res = supabase.table("prospects").insert({
                "business_name": business,
                "contact_name":  name,
                "email":         email,
                "phone":         phone,
                "industry":      industry,
                "website":       website,
                "source":        "activation_form",
                "status":        "demo_booked",
                "notes":         f"Booked slot: {slot_label} | Volume: {volume} | Pain: {pain}",
            }).execute()
            prospect_id = p_res.data[0]["id"] if p_res.data else None
            log_activity("prospect", prospect_id, "Activation form submitted", f"{business} — {slot_label}")
        except Exception as pe:
            logger.warning(f"Prospect insert after activation failed: {pe}")

        # Log appointment activity
        log_activity("appointment", None, "Deployment review booked", f"{business} — {slot_label}")

        # Send confirmation emails
        smtp_user = os.environ.get("BREVO_SMTP_USER", "")
        smtp_pass = os.environ.get("BREVO_SMTP_PASS", "")

        if smtp_user and smtp_pass:
            owner_msg            = MIMEMultipart()
            owner_msg["From"]    = "hello@nexusresponse.com.ng"
            owner_msg["To"]      = "hello@nexusresponse.com.ng"
            owner_msg["Subject"] = f"NEW DEPLOYMENT REQUEST — {business} ({slot_label})"
            owner_msg.attach(MIMEText(
                f"NEW ACTIVATION REQUEST\n\n"
                f"Business:  {business}\nIndustry:  {industry}\n"
                f"Name:      {name}\nEmail:     {email}\nPhone:     {phone}\n"
                f"Website:   {website}\n\nSLOT: {slot_label} (WAT)\n"
                f"Date: {slot_date} · Time: {slot_time}\n\n"
                f"Volume: {volume} · Follow-up: {followup} · Pain: {pain}\n\n"
                f"Admin: https://nexusresponse.com.ng/admin/slots?key={ADMIN_KEY}",
                "plain"
            ))

            prospect_msg            = MIMEMultipart()
            prospect_msg["From"]    = "hello@nexusresponse.com.ng"
            prospect_msg["To"]      = email
            prospect_msg["Subject"] = f"Your Nexus Response Deployment Review — {slot_label}"
            prospect_msg.attach(MIMEText(
                f"Hi {name},\n\n"
                f"Your deployment review is confirmed.\n\n"
                f"APPOINTMENT\n{slot_label} (West Africa Time)\n\n"
                f"During this session we will:\n"
                f"  1. Configure your missed-call response workflows\n"
                f"  2. Set up lead qualification for {business}\n"
                f"  3. Forward your business line (takes 2 minutes)\n"
                f"  4. Go live — Nexus starts catching missed calls immediately\n\n"
                f"No preparation needed. We handle everything on the call.\n\n"
                f"To reschedule, reply to this email.\n\n"
                f"— Nexus Response\nnexusresponse.com.ng",
                "plain"
            ))

            with smtplib.SMTP("smtp-relay.brevo.com", 587) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(owner_msg)
                server.send_message(prospect_msg)

            logger.info(f"Activation emails sent for {name}")

        return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(f"Activation error: {str(e)}")
        return jsonify({"success": False}), 200


# ─── SLOTS API ────────────────────────────────────────────────────────────────

@app.route("/api/slots", methods=["GET"])
def get_slots():
    try:
        from datetime import date, timedelta
        today = date.today()
        end   = today + timedelta(days=14)

        booked_res  = supabase.table("appointments").select("slot_date,slot_time").gte("slot_date", str(today)).lte("slot_date", str(end)).execute()
        blocked_res = supabase.table("blocked_slots").select("slot_date,slot_time").gte("slot_date", str(today)).lte("slot_date", str(end)).execute()
        taken = {(r["slot_date"], r["slot_time"]) for r in (booked_res.data or [])} | \
                {(r["slot_date"], r["slot_time"]) for r in (blocked_res.data or [])}

        weekday_times  = ["09:00", "11:00", "14:00", "16:00"]
        saturday_times = ["10:00", "12:00"]
        days_short     = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        time_labels    = {"09:00": "9:00 AM", "11:00": "11:00 AM", "14:00": "2:00 PM",
                          "16:00": "4:00 PM", "10:00": "10:00 AM", "12:00": "12:00 PM"}

        slots = []
        d = today + timedelta(days=1)
        while d <= end:
            dow = d.weekday()
            if dow < 5:
                for t in weekday_times:
                    slots.append({"date": str(d), "day": days_short[dow + 1], "time": t,
                                  "label": f"{days_short[dow + 1]} {time_labels[t]}",
                                  "taken": (str(d), t) in taken})
            elif dow == 5:
                for t in saturday_times:
                    slots.append({"date": str(d), "day": "Sat", "time": t,
                                  "label": f"Sat {time_labels[t]}",
                                  "taken": (str(d), t) in taken})
            d += timedelta(days=1)

        return jsonify({"slots": slots}), 200
    except Exception as e:
        logger.error(f"Slots API error: {str(e)}")
        return jsonify({"slots": []}), 200


# ─── ADMIN ────────────────────────────────────────────────────────────────────

def check_admin(req):
    return req.args.get("key") == ADMIN_KEY or req.form.get("key") == ADMIN_KEY

@app.route("/admin/slots", methods=["GET"])
def admin_slots_page():
    if not check_admin(request):
        return "Unauthorized", 403
    return render_template("admin_slots.html")

@app.route("/api/appointments", methods=["GET"])
def api_appointments():
    if not check_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        from datetime import date
        res = supabase.table("appointments").select("*") \
            .gte("slot_date", str(date.today())) \
            .order("slot_date").order("slot_time").execute()
        return jsonify({"appointments": res.data or []}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/blocked-slots", methods=["GET"])
def api_blocked_slots():
    if not check_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        from datetime import date
        res = supabase.table("blocked_slots").select("*") \
            .gte("slot_date", str(date.today())) \
            .order("slot_date").execute()
        return jsonify({"blocked": res.data or []}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/block-slot", methods=["POST"])
def api_block_slot():
    if not check_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        supabase.table("blocked_slots").insert({
            "slot_date": request.form.get("date"),
            "slot_time": request.form.get("time")
        }).execute()
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/unblock-slot", methods=["POST"])
def api_unblock_slot():
    if not check_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        supabase.table("blocked_slots").delete().eq("id", request.form.get("id")).execute()
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─── OUTREACH CONSOLE ────────────────────────────────────────────────────────

def check_outreach_admin(req):
    """Accept key via query param OR JSON body."""
    if req.args.get("key") == ADMIN_KEY:
        return True
    try:
        data = req.get_json(silent=True) or {}
        if data.get("key") == ADMIN_KEY:
            return True
    except Exception:
        pass
    return False

@app.route("/outreach", methods=["GET"])
def outreach_console():
    if request.args.get("key") != ADMIN_KEY:
        return render_template("403.html"), 403
    return render_template("outreach.html", admin_key=ADMIN_KEY)

@app.route("/api/prospects", methods=["GET"])
def get_prospects():
    if not check_outreach_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        res = supabase.table("prospects").select("*").order("created_at", desc=True).execute()
        return jsonify({"prospects": res.data or []}), 200
    except Exception as e:
        logger.error(f"get_prospects error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/prospects", methods=["POST"])
def add_prospect():
    if not check_outreach_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        body    = request.get_json(silent=True) or {}
        payload = {
            "business_name": body.get("business_name", ""),
            "contact_name":  body.get("contact_name"),
            "email":         body.get("email", ""),
            "phone":         body.get("phone"),
            "industry":      body.get("industry"),
            "website":       body.get("website"),
            "source":        body.get("source", "manual"),
            "notes":         body.get("notes"),
            "status":        "new",
        }
        res       = supabase.table("prospects").insert(payload).execute()
        prospect  = res.data[0] if res.data else {}
        log_activity("prospect", prospect.get("id"), "Prospect added", prospect.get("business_name"))
        return jsonify({"success": True, "prospect": prospect}), 201
    except Exception as e:
        logger.error(f"add_prospect error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/prospects/<prospect_id>", methods=["PATCH"])
def update_prospect(prospect_id):
    if not check_outreach_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        body    = request.get_json(silent=True) or {}
        allowed = ["status", "notes", "last_contacted_at", "contact_name",
                   "phone", "industry", "website", "business_name"]
        payload = {k: v for k, v in body.items() if k in allowed}
        if not payload:
            return jsonify({"error": "Nothing to update"}), 400
        res = supabase.table("prospects").update(payload).eq("id", prospect_id).execute()
        return jsonify({"success": True, "prospect": res.data[0] if res.data else {}}), 200
    except Exception as e:
        logger.error(f"update_prospect error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/send-email", methods=["POST"])
def send_outreach_email():
    if not check_outreach_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        body        = request.get_json(silent=True) or {}
        prospect_id = body.get("prospect_id")
        subject     = body.get("subject", "")
        email_body  = body.get("body", "")
        template_id = body.get("template_id")

        if not prospect_id or not subject or not email_body:
            return jsonify({"error": "prospect_id, subject, and body are required"}), 400

        p_res = supabase.table("prospects").select("*").eq("id", prospect_id).single().execute()
        if not p_res.data:
            return jsonify({"error": "Prospect not found"}), 404
        prospect = p_res.data

        # Variable substitution
        replacements = {
            "{{business_name}}": prospect.get("business_name", ""),
            "{{contact_name}}":  prospect.get("contact_name") or prospect.get("business_name", ""),
            "{{industry}}":      prospect.get("industry", ""),
        }
        for var, val in replacements.items():
            subject    = subject.replace(var, val)
            email_body = email_body.replace(var, val)

        # Send via Brevo API
        brevo_response = http_requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json={
                "sender":      {"name": "Nexus Response", "email": "hello@nexusresponse.com.ng"},
                "to":          [{"email": prospect["email"], "name": prospect.get("contact_name") or prospect["business_name"]}],
                "subject":     subject,
                "textContent": email_body,
            },
            timeout=10
        )
        brevo_data       = brevo_response.json()
        brevo_message_id = brevo_data.get("messageId", "")

        if brevo_response.status_code not in (200, 201, 202):
            logger.error(f"Brevo API error: {brevo_data}")
            return jsonify({"error": "Brevo delivery failed", "detail": brevo_data}), 502

        # Save email record
        email_res    = supabase.table("emails_sent").insert({
            "prospect_id":      prospect_id,
            "subject":          subject,
            "body":             email_body,
            "template_id":      template_id,
            "brevo_message_id": brevo_message_id,
            "status":           "sent",
        }).execute()
        email_record = email_res.data[0] if email_res.data else {}

        # Update prospect status
        supabase.table("prospects").update({
            "status":            "contacted",
            "last_contacted_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", prospect_id).execute()

        log_activity("prospect", prospect_id, "Email sent", subject)

        return jsonify({"success": True, "email_id": email_record.get("id"), "brevo_message_id": brevo_message_id}), 200

    except Exception as e:
        logger.error(f"send_outreach_email error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/templates", methods=["GET"])
def get_templates():
    if not check_outreach_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        res = supabase.table("templates").select("*").order("created_at", desc=True).execute()
        return jsonify({"templates": res.data or []}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/templates", methods=["POST"])
def save_template():
    if not check_outreach_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        body        = request.get_json(silent=True) or {}
        template_id = body.get("id", "").strip().lower().replace(" ", "-")
        if not template_id or not body.get("subject") or not body.get("body"):
            return jsonify({"error": "id, subject, and body are required"}), 400
        payload = {
            "id":       template_id,
            "name":     body.get("name", template_id),
            "subject":  body.get("subject"),
            "body":     body.get("body"),
            "industry": body.get("industry"),
        }
        res = supabase.table("templates").upsert(payload).execute()
        return jsonify({"success": True, "template": res.data[0] if res.data else {}}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/templates/<template_id>", methods=["DELETE"])
def delete_template(template_id):
    if not check_outreach_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        supabase.table("templates").delete().eq("id", template_id).execute()
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/activities", methods=["GET"])
def get_activities():
    if not check_outreach_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        entity_id = request.args.get("entity_id")
        query     = supabase.table("activities").select("*").order("created_at", desc=True).limit(50)
        if entity_id:
            query = query.eq("entity_id", entity_id)
        res = query.execute()
        return jsonify({"activities": res.data or []}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/pipeline", methods=["GET"])
def get_pipeline():
    if not check_outreach_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        res    = supabase.table("prospects").select("status").execute()
        counts = {"new": 0, "contacted": 0, "replied": 0, "demo_booked": 0, "activated": 0, "lost": 0}
        for row in (res.data or []):
            s = row.get("status", "new")
            if s in counts:
                counts[s] += 1
        return jsonify(counts), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/prospects/<prospect_id>/emails", methods=["GET"])
def get_prospect_emails(prospect_id):
    if not check_outreach_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        res = supabase.table("emails_sent").select("*") \
            .eq("prospect_id", prospect_id) \
            .order("sent_at", desc=True).execute()
        return jsonify({"emails": res.data or []}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/todays-appointments", methods=["GET"])
def todays_appointments():
    if not check_outreach_admin(request):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        from datetime import date
        today = str(date.today())
        res   = supabase.table("appointments").select("*").eq("slot_date", today).order("slot_time").execute()
        return jsonify({"appointments": res.data or []}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
