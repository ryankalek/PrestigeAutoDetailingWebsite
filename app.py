import os
from datetime import datetime, date, time, timedelta
import json
from dateutil.rrule import rrule, DAILY
import pytz

from flask import Flask, render_template, request, redirect, url_for, jsonify, session, send_file, abort, make_response
from flask_session import Session
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, JSON as SAJSON, select, and_, or_
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session as SASession

import requests
from flask import request

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=5)
    except Exception:
        pass  # don’t crash booking if Telegram is being moody


# ------------ Config ------------
TZ_NAME = os.environ.get("SHOP_TZ", "Asia/Beirut")
SHOP_TZ = pytz.timezone(TZ_NAME)

# Business hours (per weekday 0=Mon ... 6=Sun)
# Close on Sunday by setting both to None
BUSINESS_HOURS = {
    0: (9, 19),
    1: (9, 19),
    2: (9, 19),
    3: (9, 19),
    4: (9, 19),
    5: (9, 17),
    6: (None, None)  # Sunday closed
}

# Capacity per resource type (parallel bays / teams)
RESOURCE_CAPACITY = {
    "wash": 2,
    "detail": 1,
    "tint": 1,
    "polish": 1
}

# Seed services
SEED_SERVICES = [
    # name, code, price, resource_type, duration_minutes, duration_days, is_addon, description
    {"name": "Quick Wash", "code": "quick_wash", "price": 25, "resource_type": "wash", "duration_minutes": 60, "duration_days": 0, "is_addon": False, "description": "Exterior wash, quick dry."},
    {"name": "Signature Wash", "code": "signature_wash", "price": 60, "resource_type": "wash", "duration_minutes": 120, "duration_days": 0, "is_addon": False, "description": "Deep clean with foam, rims, tire shine."},
    {"name": "Interior Detail", "code": "interior_detail", "price": 90, "resource_type": "detail", "duration_minutes": 180, "duration_days": 0, "is_addon": False, "description": "Interior steam, vacuum, plastics and leather."},
    {"name": "Window Tint", "code": "window_tint", "price": 150, "resource_type": "tint", "duration_minutes": 240, "duration_days": 0, "is_addon": False, "description": "Full car tint (laws vary)."},
    {"name": "Full Polish", "code": "full_polish", "price": 400, "resource_type": "polish", "duration_minutes": 0, "duration_days": 4, "is_addon": False, "description": "Multi-stage paint correction and protection. 4 days."},
    # Add-ons
    {"name": "Headlight Polish (add-on)", "code": "addon_headlight", "price": 25, "resource_type": "detail", "duration_minutes": 30, "duration_days": 0, "is_addon": True, "description": "Add-on: headlight restoration."},
    {"name": "Engine Bay Clean (add-on)", "code": "addon_engine", "price": 30, "resource_type": "wash", "duration_minutes": 30, "duration_days": 0, "is_addon": True, "description": "Add-on: engine bay degrease and dress."},
]

ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "admin")

# ------------ App / DB setup ------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this")
app.config["SESSION_TYPE"] = "filesystem"
Session(app)

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///booking.db")
engine = create_engine(DB_URL, future=True, echo=False)

class Base(DeclarativeBase):
    pass

class Service(Base):
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    price: Mapped[int] = mapped_column(Integer)  # store as minor units if you prefer
    resource_type: Mapped[str] = mapped_column(String(40))  # wash, detail, tint, polish
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)
    duration_days: Mapped[int] = mapped_column(Integer, default=0)
    is_addon: Mapped[int] = mapped_column(Integer, default=0)  # 0/1
    description: Mapped[str] = mapped_column(Text, default="")

class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(40))
    car_info: Mapped[str] = mapped_column(String(200))  # make/model/plate
    primary_service_code: Mapped[str] = mapped_column(String(80))
    addon_codes: Mapped[str] = mapped_column(Text, default="[]")  # JSON list
    resource_type: Mapped[str] = mapped_column(String(40))
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    total_price: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(40), default="booked")  # booked, in_progress, done, canceled
    notes: Mapped[str] = mapped_column(Text, default="")

Base.metadata.create_all(engine)

def seed_services():
    with SASession(engine) as s:
        existing = {sv.code for sv in s.scalars(select(Service)).all()}
        for svc in SEED_SERVICES:
            if svc["code"] not in existing:
                s.add(Service(**svc))
        s.commit()

seed_services()

# ------------ Helpers ------------
def is_open_on(dt_local: datetime) -> bool:
    h = BUSINESS_HOURS.get(dt_local.weekday(), (None, None))
    return h != (None, None) and h[0] is not None and h[1] is not None

def business_window(dt_local_date: date):
    wh = BUSINESS_HOURS.get(dt_local_date.weekday(), (None, None))
    if wh[0] is None or wh[1] is None:
        return None
    start_naive = datetime.combine(dt_local_date, time(wh[0], 0))
    end_naive = datetime.combine(dt_local_date, time(wh[1], 0))
    start = SHOP_TZ.localize(start_naive)
    end = SHOP_TZ.localize(end_naive)
    return start, end


def to_local(dt_utc: datetime) -> datetime:
    return dt_utc.astimezone(SHOP_TZ)

def to_utc(dt_local: datetime) -> datetime:
    # If naive, attach shop TZ; if already aware, normalize to shop TZ first
    if dt_local.tzinfo is None or dt_local.tzinfo.utcoffset(dt_local) is None:
        dt_local = SHOP_TZ.localize(dt_local)
    else:
        dt_local = dt_local.astimezone(SHOP_TZ)
    return dt_local.astimezone(pytz.utc)


def overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end

def count_overlaps(resource_type: str, start_utc: datetime, end_utc: datetime) -> int:
    with SASession(engine) as s:
        q = s.scalars(select(Appointment).where(
            and_(Appointment.resource_type == resource_type,
                 Appointment.status != "canceled",
                 Appointment.start_utc < end_utc,
                 Appointment.end_utc > start_utc
            )
        ))
        return len(list(q))

def fits_capacity(resource_type: str, start_utc: datetime, end_utc: datetime) -> bool:
    return count_overlaps(resource_type, start_utc, end_utc) < RESOURCE_CAPACITY.get(resource_type, 1)

def compute_total_and_duration(primary: Service, addons: list[Service]):
    total_price = primary.price + sum(a.price for a in addons)
    # Duration: base = primary, add-ons add their own but only hours add-ons for simplicity
    dur_minutes = primary.duration_minutes
    dur_days = primary.duration_days
    for a in addons:
        dur_minutes += a.duration_minutes
        dur_days += a.duration_days
    return total_price, dur_minutes, dur_days

def next_business_day(d: date) -> date:
    # advance until a day with business window
    for i in range(1, 8):
        nd = d + timedelta(days=1)
        bw = business_window(nd)
        if bw is not None:
            return nd
        d = nd
    return d

def end_time_for_span(start_local: datetime, add_minutes: int, add_days: int) -> datetime:
    cur = start_local
    # Multi-day part: consume whole business days
    if add_days > 0:
        remaining_days = add_days
        while remaining_days > 0:
            bw = business_window(cur.date())
            if not bw:
                nd = next_business_day(cur.date())
                bw = business_window(nd)
            start_day, end_day = bw
            remaining_days -= 1
            if remaining_days == 0:
                cur = end_day
                break
            nd = next_business_day(cur.date())
            cur = business_window(nd)[0]

    # Minute part: walk inside business hours
    remaining = add_minutes
    while remaining > 0:
        bw = business_window(cur.date())
        if not bw:
            nd = next_business_day(cur.date())
            bw = business_window(nd)
        start_day, end_day = bw
        if cur < start_day:
            cur = start_day
        available = int((end_day - cur).total_seconds() // 60)
        if available <= 0:
            nd = next_business_day(cur.date())
            cur = business_window(nd)[0]
            continue
        step = min(remaining, available)
        cur = cur + timedelta(minutes=step)
        remaining -= step
    return cur


def slot_candidates_for_date(service: Service, day: date):
    """Return list of local datetimes that can start on given day"""
    bw = business_window(day)
    if not bw:
        return []
    start_day, end_day = bw
    # step every 30 minutes
    step = timedelta(minutes=30)
    slots = []
    cur = start_day
    # If service has duration_days > 0, only allow starts at open time
    if service.duration_days > 0:
        slots = [start_day]
    else:
        while cur + timedelta(minutes=service.duration_minutes) <= end_day:
            slots.append(cur)
            cur += step
    return slots

def available_slots(service: Service, day: date, addons: list[Service]):
    total_price, add_minutes, add_days = compute_total_and_duration(service, addons)
    slots = []
    for start_local in slot_candidates_for_date(service, day):
        # compute end across business hours
        end_local = end_time_for_span(start_local, add_minutes, add_days)
        start_utc = to_utc(start_local)
        end_utc = to_utc(end_local)
        # Check capacity on primary resource and also on addon resources if different
        ok = fits_capacity(service.resource_type, start_utc, end_utc)
        if ok:
            # For each addon with distinct resource, we must also check capacity
            for a in addons:
                if a.resource_type != service.resource_type or a.duration_days > 0:
                    # naive approach: assume addons happen in parallel starting at same time
                    a_end_local = end_time_for_span(start_local, a.duration_minutes, a.duration_days)
                    if not fits_capacity(a.resource_type, to_utc(start_local), to_utc(a_end_local)):
                        ok = False
                        break
        if ok:
            slots.append((start_local, end_local))
    return slots

# ------------ Routes ------------
@app.get("/")
def index():
    with SASession(engine) as s:
        services = list(s.scalars(select(Service).where(Service.is_addon == 0).order_by(Service.name)).all())
        addons = list(s.scalars(select(Service).where(Service.is_addon == 1).order_by(Service.name)).all())

    services_data = [{
        "id": sv.id, "name": sv.name, "code": sv.code, "price": sv.price,
        "resource_type": sv.resource_type, "duration_minutes": sv.duration_minutes,
        "duration_days": sv.duration_days, "is_addon": bool(sv.is_addon),
        "description": sv.description or ""
    } for sv in services]
    addons_data = [{
        "id": sv.id, "name": sv.name, "code": sv.code, "price": sv.price,
        "resource_type": sv.resource_type, "duration_minutes": sv.duration_minutes,
        "duration_days": sv.duration_days, "is_addon": bool(sv.is_addon),
        "description": sv.description or ""
    } for sv in addons]

    today_local = datetime.now(SHOP_TZ).date()
    return render_template("index.html",
        services=services, addons=addons,
        services_data=services_data, addons_data=addons_data,
        today=today_local, tz=TZ_NAME)


@app.get("/api/services")
def api_services():
    with SASession(engine) as s:
        svcs = list(s.scalars(select(Service)).all())
        return jsonify([{
            "id": x.id, "name": x.name, "code": x.code, "price": x.price,
            "resource_type": x.resource_type, "duration_minutes": x.duration_minutes,
            "duration_days": x.duration_days, "is_addon": bool(x.is_addon), "description": x.description
        } for x in svcs])

@app.get("/api/availability")
def api_availability():
    service_code = request.args.get("service")
    day_str = request.args.get("day")
    addon_codes = request.args.getlist("addons")
    if not service_code or not day_str:
        return jsonify({"error": "Missing service or day"}), 400
    day = datetime.strptime(day_str, "%Y-%m-%d").date()
    with SASession(engine) as s:
        svc = s.scalar(select(Service).where(Service.code == service_code))
        if not svc:
            return jsonify({"error": "Service not found"}), 404
        addons = []
        for code in addon_codes:
            a = s.scalar(select(Service).where(Service.code == code))
            if a:
                addons.append(a)
        slots = available_slots(svc, day, addons)
        # format times in local
        result = [{
            "start": to_utc(st).isoformat(),
            "end": to_utc(en).isoformat(),
            "label": st.strftime("%I:%M %p") + " → " + en.strftime("%I:%M %p")
        } for st, en in slots]

        return jsonify(result)

@app.post("/book")
def book():
    data = request.form
    name = data.get("name", "").strip()
    phone = data.get("phone", "").strip()
    car = data.get("car", "").strip()
    service_code = data.get("service")
    addon_codes = request.form.getlist("addons")

    day_str = data.get("day")
    slot_start_iso = data.get("slot_start_iso")
    if not (name and phone and car and service_code and day_str and slot_start_iso):
        return "Missing fields", 400

    with SASession(engine) as s:
        svc = s.scalar(select(Service).where(Service.code == service_code))
        if not svc:
            return "Service not found", 404
        addons = []
        for code in addon_codes:
            a = s.scalar(select(Service).where(Service.code == code))
            if a: addons.append(a)

        start_utc = datetime.fromisoformat(slot_start_iso)
        # recompute end to be safe
        start_local = to_local(start_utc)
        total_price, add_minutes, add_days = compute_total_and_duration(svc, addons)
        end_local = end_time_for_span(start_local, add_minutes, add_days)
        end_utc = to_utc(end_local)

        # capacity check again
        if not fits_capacity(svc.resource_type, start_utc, end_utc):
            return "Selected time no longer available. Please go back and pick another slot.", 409

        appt = Appointment(
            customer_name=name, phone=phone, car_info=car,
            primary_service_code=svc.code, addon_codes=json.dumps([a.code for a in addons]),
            resource_type=svc.resource_type, start_utc=start_utc, end_utc=end_utc,
            total_price=total_price
        )
        s.add(appt)
        s.commit()
        # Build absolute .ics link
        ics_link = request.url_root.rstrip("/") + url_for("ics_appt", appt_id=appt.id)
        msg = (
            f"✅ New booking\n"
            f"Name: {name}\n"
            f"Phone: {phone}\n"
            f"Car: {car}\n"
            f"Service: {svc.name}\n"
            f"When: {to_local(start_utc).strftime('%Y-%m-%d %I:%M %p')} → "
            f"{to_local(end_utc).strftime('%I:%M %p')} ({TZ_NAME})\n"
            f"Price: ${total_price}\n"
            f"Calendar: {ics_link}"
        )
        send_telegram(msg)
        return redirect(url_for("success", appt_id=appt.id))

@app.get("/success/<int:appt_id>")
def success(appt_id):
    with SASession(engine) as s:
        appt = s.get(Appointment, appt_id)
        if not appt:
            abort(404)
        svc = s.scalar(select(Service).where(Service.code == appt.primary_service_code))
        addons = []
        try:
            addon_codes = json.loads(appt.addon_codes)
        except Exception:
            addon_codes = []
        for code in addon_codes:
            a = s.scalar(select(Service).where(Service.code == code))
            if a: addons.append(a)

    return render_template("success.html", appt=appt, svc=svc, addons=addons, tz=TZ_NAME, to_local=to_local)

# ------------ Admin ------------
def require_admin():
    if session.get("is_admin"):
        return True
    return False

@app.get("/admin/login")
def admin_login_form():
    return render_template("login.html")

@app.post("/admin/login")
def admin_login():
    pw = request.form.get("password", "")
    if pw == ADMIN_PASSWORD:
        session["is_admin"] = True
        return redirect(url_for("admin"))
    return render_template("login.html", error="Wrong password.")

@app.get("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login_form"))

@app.get("/admin")
def admin():
    if not require_admin():
        return redirect(url_for("admin_login_form"))
    # Show a simple calendar list for the next 14 days
    start_local = datetime.now(SHOP_TZ).date()
    end_local = start_local + timedelta(days=14)
    with SASession(engine) as s:
        appts = list(s.scalars(select(Appointment).where(
            and_(Appointment.start_utc >= to_utc(datetime.combine(start_local, time(0,0))),
                 Appointment.start_utc < to_utc(datetime.combine(end_local, time(0,0)))
            )
        ).order_by(Appointment.start_utc)).all())
    # group by day
    grouped = {}
    for a in appts:
        d = to_local(a.start_utc).date().isoformat()
        grouped.setdefault(d, []).append(a)
    return render_template("admin.html", grouped=grouped, tz=TZ_NAME, to_local=to_local)

@app.get("/api/events")
def api_events():
    # FullCalendar-style JSON for admin feeds
    start_iso = request.args.get("start")
    end_iso = request.args.get("end")
    if not start_iso or not end_iso:
        return jsonify([])
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    with SASession(engine) as s:
        appts = list(s.scalars(select(Appointment).where(
            and_(Appointment.start_utc < end, Appointment.end_utc > start)
        )).all())
    events = []
    for a in appts:
        events.append({
            "id": a.id,
            "title": f"{a.customer_name} • {a.primary_service_code}",
            "start": a.start_utc.isoformat(),
            "end": a.end_utc.isoformat()
        })
    return jsonify(events)

# ------------ iCal ------------
def ics_for_appt(a: Appointment, svc_name: str):
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    uid = f"appt-{a.id}@carshop"
    start = a.start_utc.astimezone(SHOP_TZ)
    end = a.end_utc.astimezone(SHOP_TZ)
    # VALARM 1 hour before
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CarShop Booking//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{dtstamp}
SUMMARY:Car service - {svc_name}
DTSTART;TZID={TZ_NAME}:{start.strftime("%Y%m%dT%H%M%S")}
DTEND;TZID={TZ_NAME}:{end.strftime("%Y%m%dT%H%M%S")}
DESCRIPTION:Customer: {a.customer_name}\\nPhone: {a.phone}\\nCar: {a.car_info}\\nService: {svc_name}
BEGIN:VALARM
TRIGGER:-PT60M
ACTION:DISPLAY
DESCRIPTION:Service appointment reminder
END:VALARM
END:VEVENT
END:VCALENDAR
"""
    return ics

@app.get("/ics/<int:appt_id>.ics")
def ics_appt(appt_id):
    with SASession(engine) as s:
        a = s.get(Appointment, appt_id)
        if not a:
            abort(404)
        svc_name = a.primary_service_code
        svc = s.scalar(select(Service).where(Service.code == a.primary_service_code))
        if svc:
            svc_name = svc.name
    content = ics_for_appt(a, svc_name)
    resp = make_response(content)
    resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="appointment-{appt_id}.ics"'
    return resp

@app.get("/feed.ics")
def ics_feed():
    # ICS calendar for all appointments
    with SASession(engine) as s:
        appts = list(s.scalars(select(Appointment)).all())
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//CarShop Booking//EN","CALSCALE:GREGORIAN","METHOD:PUBLISH"]
    for a in appts:
        svc_name = a.primary_service_code
        with SASession(engine) as s:
            svc = s.scalar(select(Service).where(Service.code == a.primary_service_code))
        if svc:
            svc_name = svc.name
        start = a.start_utc.astimezone(SHOP_TZ)
        end = a.end_utc.astimezone(SHOP_TZ)
        lines += [
            "BEGIN:VEVENT",
            f"UID:appt-{a.id}@carshop",
            f"DTSTAMP:{dtstamp}",
            f"SUMMARY:Car service - {svc_name}",
            f"DTSTART;TZID={TZ_NAME}:{start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID={TZ_NAME}:{end.strftime('%Y%m%dT%H%M%S')}",
            f"DESCRIPTION:Customer: {a.customer_name}\\nPhone: {a.phone}\\nCar: {a.car_info}\\nService: {svc_name}",
            "END:VEVENT"
        ]
    lines.append("END:VCALENDAR")
    content = "\n".join(lines)
    resp = make_response(content)
    resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
    resp.headers["Content-Disposition"] = 'attachment; filename="carshop-feed.ics"'
    return resp

@app.get("/privacy")
def privacy():
    return render_template("privacy.html")

@app.get("/terms")
def terms():
    return render_template("terms.html")


# ------------ Run ------------
if __name__ == "__main__":
    # For local testing
    app.run(host="0.0.0.0", port=5000, debug=True)