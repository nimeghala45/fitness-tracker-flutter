from flask import Flask, request, jsonify
import json
import os
import requests
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
from flask_cors import CORS

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)
CORS(app, origins=["*"], supports_credentials=True)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me")

def _load_google_config():
    path = os.path.join(os.path.dirname(__file__), "client.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "client_id" in data and "client_secret" in data:
        return {
            "clientId": data["client_id"],
            "clientSecret": data["client_secret"],
            "redirectUri": data.get("redirect_uri", ""),
        }

    client_id = None
    if "client" in data and data["client"]:
        for oc in data["client"][0].get("oauth_client", []):
            client_id = oc.get("client_id")
            if oc.get("client_type") == 3:  # web client
                break
    return {
        "clientId": client_id or os.environ.get("GOOGLE_CLIENT_ID", ""),
        "clientSecret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        "redirectUri": os.environ.get("GOOGLE_REDIRECT_URI", ""),
    }

cfg = _load_google_config()

_fitness_collection = None
try:
    from pymongo import MongoClient
    uri = os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI")
    if uri:
        _db = MongoClient(uri)[os.environ.get("MONGODB_DB", "fitness")]
        _fitness_collection = _db["fitness_records"]
except Exception as e:
    print("MongoDB not available:", e)



def _store_fitness_record(source, steps, calories, date_str, user_id=None, email=None, active_calories=None, total_calories=None):
    """Store or upsert a fitness record in MongoDB."""
    if _fitness_collection is None:
        return
    doc = {
        "source": source,
        "steps": int(steps) if steps is not None else 0,
        "calories": float(calories) if calories is not None else 0.0,
        "date": date_str,
        "user_id": user_id,
        "email": email,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    if active_calories is not None:
        doc["active_calories"] = float(active_calories)
    if total_calories is not None:
        doc["total_calories"] = float(total_calories)
    _fitness_collection.update_one(
        {"date": date_str, "source": source, "email": email or user_id or "anonymous"},
        {"$set": doc},
        upsert=True,
    )


def _sum_fit_values(body, use_float=False):
    total = 0.0 if use_float else 0
    for bucket in body.get("bucket", []):
        for dataset in bucket.get("dataset", []):
            for point in dataset.get("point", []):
                for val in point.get("value", []):
                    v = val.get("fpVal") or val.get("intVal")
                    if v is not None:
                        total += float(v) if use_float else int(v)
    return total


@app.route("/api/google-fit/fetch", methods=["POST"])
def api_google_fit_fetch():
    """Fetch current steps and calories from Google Fit (no cache). Body: { "access_token", "date" (optional), "email" (optional) }."""
    data = request.get_json() or {}
    access_token = data.get("access_token")
    if not access_token:
        return jsonify({"error": "access_token required"}), 400

    day = data.get("date") or datetime.utcnow().strftime("%Y-%m-%d")
    try:
        start = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date. Use YYYY-MM-DD."}), 400

    end = start + timedelta(days=1)
    start_ms = int(time.mktime(start.timetuple()) * 1000)
    end_ms = int(time.mktime(end.timetuple()) * 1000)

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    url = "https://www.googleapis.com/fitness/v1/users/me/dataset:aggregate"
    payload = {"startTimeMillis": start_ms, "endTimeMillis": end_ms}

    steps_res = requests.post(url, headers=headers, json={**payload, "aggregateBy": [{"dataTypeName": "com.google.step_count.delta"}]})
    total_steps = _sum_fit_values(steps_res.json()) if steps_res.ok else 0

    cal_res = requests.post(
        url,
        headers=headers,
        json={**payload, "aggregateBy": [{"dataTypeName": "com.google.calories.expended"}]},
    )
    total_calories = _sum_fit_values(cal_res.json(), use_float=True) if cal_res.ok else 0.0

    bmr_res = requests.post(
        url,
        headers=headers,
        json={**payload, "aggregateBy": [{"dataTypeName": "com.google.calories.bmr"}]},
    )
    basal_calories = _sum_fit_values(bmr_res.json(), use_float=True) if bmr_res.ok else 0.0

    active_calories = max(0.0, total_calories - basal_calories) if basal_calories > 0 else 0.0
    calories_display = active_calories if active_calories > 0 else total_calories

    _store_fitness_record(
        "google_fit",
        total_steps,
        calories_display,
        day,
        email=data.get("email"),
        active_calories=active_calories,
        total_calories=total_calories,
    )
    return jsonify({
        "source": "google_fit", "steps": total_steps,
        "calories": round(calories_display, 1),
        "active_calories": round(active_calories, 1),
        "total_calories": round(total_calories, 1),
        "basal_calories": round(basal_calories, 1),
        "date": day,
    })


@app.route("/api/fitness/sync", methods=["POST"])
def api_fitness_sync():
    """Store steps/calories from app. Body: { "source", "steps", "calories", "active_calories" (optional), "total_calories" (optional), "date", "email" }."""
    data = request.get_json() or {}
    source = data.get("source")
    if source not in ("apple_health", "google_fit", "health_connect"):
        return jsonify({"error": "source must be apple_health, google_fit, or health_connect"}), 400
    day = data.get("date") or datetime.utcnow().strftime("%Y-%m-%d")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date. Use YYYY-MM-DD."}), 400
    steps = data.get("steps", 0)
    calories = data.get("calories", 0)
    active_cal = data.get("active_calories")
    total_cal = data.get("total_calories")
    _store_fitness_record(source, steps, calories, day, email=data.get("email"), active_calories=active_cal, total_calories=total_cal)
    out = {"source": source, "steps": int(steps), "calories": round(float(calories), 1), "date": day, "stored": True}
    if active_cal is not None:
        out["active_calories"] = round(float(active_cal), 1)
    if total_cal is not None:
        out["total_calories"] = round(float(total_cal), 1)
    return jsonify(out)


@app.route("/api/fitness/history", methods=["GET"])
def api_fitness_history():
    """Get stored records. Query: date (optional), limit (default 30, max 100)."""
    if _fitness_collection is None:
        return jsonify({"records": [], "error": "MongoDB not configured"})
    q = {"date": request.args.get("date")} if request.args.get("date") else {}
    limit = min(int(request.args.get("limit", 30)), 100)

    # Return only the latest record per day (latest updated_at first).
    by_day = {}
    for doc in _fitness_collection.find(q).sort([("date", -1), ("updated_at", -1)]):
        day = doc.get("date")
        if not day or day in by_day:
            continue
        by_day[day] = doc
        if len(by_day) >= limit:
            break

    records = []
    for day in sorted(by_day.keys(), reverse=True):
        doc = by_day[day]
        doc.pop("_id", None)
        records.append(doc)
    return jsonify({"records": records})


if __name__ == "__main__":
    app.run(debug=True,host="0.0.0.0",port=5000)