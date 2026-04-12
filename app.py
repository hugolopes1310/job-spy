"""Minimal Flask dashboard for the job tracker.

Run with:
    pip install flask
    python app.py
Then open http://127.0.0.1:5000
"""
from __future__ import annotations

from flask import Flask, redirect, render_template_string, request, url_for

from db import DB_PATH, connect, init_db

app = Flask(__name__)

TEMPLATE = """
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Job Tracker — Hugo</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --muted:#64748b; --text:#f1f5f9; --accent:#14b8a6; }
  body { font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 0; background: var(--bg); color: var(--text); }
  header { padding: 16px 24px; border-bottom: 1px solid #334155; display: flex; gap: 24px; align-items: center; }
  header h1 { font-size: 18px; margin: 0; }
  nav a { color: var(--muted); margin-right: 16px; text-decoration: none; }
  nav a.active { color: var(--accent); font-weight: 600; }
  main { padding: 16px 24px; }
  .card { background: var(--card); padding: 14px 16px; border-radius: 8px; margin-bottom: 10px; border-left: 3px solid var(--accent); }
  .card.low { border-left-color: #475569; opacity: 0.75; }
  .row { display: flex; justify-content: space-between; align-items: baseline; }
  .title { font-size: 15px; font-weight: 600; }
  .meta { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .score { background: var(--accent); color: #0f172a; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 700; }
  .actions { margin-top: 8px; }
  .actions form { display: inline; }
  .actions button { background: #334155; color: var(--text); border: none; padding: 4px 10px; border-radius: 4px; font-size: 11px; cursor: pointer; margin-right: 6px; }
  .actions button:hover { background: #475569; }
  a.ext { color: var(--accent); text-decoration: none; }
  .empty { color: var(--muted); padding: 40px; text-align: center; }
</style>
</head>
<body>
<header>
  <h1>🎯 Job Tracker</h1>
  <nav>
    {% for s in statuses %}
      <a href="{{ url_for('index', status=s) }}" class="{{ 'active' if s == current_status else '' }}">{{ s }} ({{ counts.get(s, 0) }})</a>
    {% endfor %}
  </nav>
</header>
<main>
  {% if jobs %}
    {% for j in jobs %}
      <div class="card {{ 'low' if j['score'] < 5 else '' }}">
        <div class="row">
          <div class="title">{{ j['title'] }}</div>
          <div class="score">{{ j['score'] }} pts</div>
        </div>
        <div class="meta">
          🏢 {{ j['company'] or '?' }} • 📍 {{ j['location'] or '?' }} • 🎯 {{ j['axe'] }}
          • <a class="ext" href="{{ j['url'] }}" target="_blank" rel="noopener">Ouvrir l'offre ↗</a>
        </div>
        <div class="actions">
          {% for s in ['applied', 'interview', 'ignored', 'new'] %}
            {% if s != j['status'] %}
              <form method="post" action="{{ url_for('update_status', job_id=j['id']) }}">
                <input type="hidden" name="status" value="{{ s }}">
                <input type="hidden" name="back_to" value="{{ current_status }}">
                <button>→ {{ s }}</button>
              </form>
            {% endif %}
          {% endfor %}
        </div>
      </div>
    {% endfor %}
  {% else %}
    <div class="empty">Aucune offre dans ce statut.</div>
  {% endif %}
</main>
</body>
</html>
"""

STATUSES = ["new", "notified", "applied", "interview", "ignored"]


@app.route("/")
def index():
    status = request.args.get("status", "new")
    with connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY score DESC, first_seen DESC",
            (status,),
        ).fetchall()
        counts_rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM jobs GROUP BY status"
        ).fetchall()
    counts = {r["status"]: r["n"] for r in counts_rows}
    return render_template_string(
        TEMPLATE,
        jobs=[dict(r) for r in rows],
        counts=counts,
        statuses=STATUSES,
        current_status=status,
    )


@app.route("/job/<job_id>/status", methods=["POST"])
def update_status(job_id: str):
    new_status = request.form.get("status", "new")
    back_to = request.form.get("back_to", "new")
    if new_status not in STATUSES:
        return "bad status", 400
    with connect(DB_PATH) as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (new_status, job_id))
    return redirect(url_for("index", status=back_to))


if __name__ == "__main__":
    init_db(DB_PATH)
    app.run(debug=True, host="127.0.0.1", port=5000)
