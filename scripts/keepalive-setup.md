# Keep Render Free Tier Warm — UptimeRobot Setup

Render free web services spin down after **15 minutes of inactivity** and take
~60 seconds to cold-start. This guide sets up a free UptimeRobot monitor that
pings the health endpoint every 5 minutes, preventing spin-down with no code
changes needed.

## Steps

1. **Create a free UptimeRobot account** at https://uptimerobot.com (free tier
   allows up to 50 monitors, 5-minute interval).

2. **Add a new monitor**:
   - Monitor Type: `HTTP(s)`
   - Friendly Name: `ResQVoice API`
   - URL: `https://resqvoice-api.onrender.com/healthz`
   - Monitoring Interval: `5 minutes`
   - Click **Create Monitor**

3. **Verify**: UptimeRobot will show the monitor as "Up" within a few minutes.
   The Render service will no longer spin down as long as UptimeRobot is running.

## What the health endpoint returns

```json
{"status": "ok", "service": "resqvoice-api", "database": "ok"}
```

UptimeRobot marks the monitor as "Up" on any HTTP 2xx response.

## Notes

- Replace `resqvoice-api.onrender.com` with your actual Render URL if different.
- If you upgrade to Render Starter ($7/mo), delete this monitor — always-on
  services do not need external pinging.
- The `/healthz` endpoint also checks PostgreSQL connectivity (Supabase), so the
  monitor doubles as a DB reachability check.
