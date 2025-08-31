# Carshop Booking (Flask)

A simple booking system for a car wash/detail/tint/polish shop.

## Features
- Public booking with service + add-ons
- Availability that respects shop working hours and bay capacities
- Multi-day services (e.g., polish) that block days
- Price and duration estimates
- Admin page with upcoming appointments
- Per-appointment .ics files and a full calendar feed at `/feed.ics`
- Timezone aware (default Asia/Beirut), change via `SHOP_TZ`

## Run locally
```bash
python -m venv .venv
.venv\Scripts\activate # Linux: source .venv/bin/activate
pip install -r requirements.txt
set FLASK_ENV=development  # Linux: export FLASK_ENV=development
set ADMIN_PASS=mysecret
set TELEGRAM_TOKEN=8066177414:AAEhD7umpMWeAtZ8f3ij8J_ZnwaqpF_bjx0
set TELEGRAM_CHAT_ID=5143796509
python app.py
```
Open http://localhost:5000

## Customize
- Edit services, capacities, and business hours inside `app.py` near the top.
- Add more add-ons or services by updating `SEED_SERVICES` and deleting `booking.db` once to reseed.
- Make `/api/availability` smarter for staggering add-ons if needed.

## Deploy
- Works on any host that supports Flask (Railway, Render, Fly, etc.).
- Set `DATABASE_URL` to a persistent database if preferred.
- Set `SECRET_KEY` and `ADMIN_PASS` in environment vars.

## SMS/WhatsApp notifications (optional)
- Hook Twilio, WhatsApp Business, or Telegram in `book()` after saving the appointment.
- Send yourself a message with the appointment details and the `.ics` link.