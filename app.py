"""BanKin — Bank of Kinast.

A boring, single-container, LAN-only kids' ledger. No auth, no PIN,
no interest, no real money movement. Amounts are integer cents.
The SQLite database and uploaded goal pictures live in the data
directory (a persistent volume on the Synology, never in the repo).

Run locally:
    BANKIN_DATA_DIR=./data python app.py
Then open http://127.0.0.1:8080/
"""

import io
import json
import logging
import os
import random
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation

import urllib.error
import urllib.parse
import urllib.request

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename

logger = logging.getLogger("bankin")

ALLOWED_PICTURE_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}


# ---------------------------------------------------------------- money

def parse_amount_to_cents(text):
    """Parse a user-entered amount like '20', '20.00' or '$20.00'.

    Returns integer cents. Raises ValueError on anything that is not
    a positive amount with at most two decimal places.
    """
    if text is None:
        raise ValueError("amount is required")
    cleaned = str(text).strip().replace("$", "").replace(",", "")
    if not cleaned:
        raise ValueError("amount is required")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("amount must be a number") from None
    if value <= 0:
        raise ValueError("amount must be greater than zero")
    if -value.as_tuple().exponent > 2:
        raise ValueError("amount has more than two decimal places")
    return int(value * 100)


def format_cents(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def goal_progress_pct(balance_cents, price_cents):
    """Whole-percent progress toward a goal, capped at 100."""
    if not price_cents or price_cents <= 0:
        return 0
    if balance_cents <= 0:
        return 0
    return min(100, balance_cents * 100 // price_cents)


# ---------------------------------------------------------------- themes

# Skylight-style pastels: harmonious card background, border, and
# balance colour per theme. Plain dict, no CSS generation needed —
# templates inline these three values on the card.
THEMES = {
    "sky": {"label": "Sky", "bg": "#e3f0ff", "border": "#8fb8e8", "balance": "#1d4e89"},
    "mint": {"label": "Mint", "bg": "#dff5e6", "border": "#7fc9a0", "balance": "#14663a"},
    "peach": {"label": "Peach", "bg": "#ffead9", "border": "#f0a878", "balance": "#8a3d12"},
    "lilac": {"label": "Lilac", "bg": "#ece6fb", "border": "#a894e0", "balance": "#4b2e8f"},
    "lemon": {"label": "Lemon", "bg": "#fff6cc", "border": "#e3c95a", "balance": "#6d5700"},
    "rose": {"label": "Rose", "bg": "#ffe3ea", "border": "#e88aa5", "balance": "#7d1f3d"},
}
DEFAULT_THEME = "sky"


def theme_for(kid):
    name = (kid["theme"] if "theme" in kid.keys() else None) or DEFAULT_THEME
    return THEMES.get(name, THEMES[DEFAULT_THEME])


def kid_initials(name):
    parts = (name or "").split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


# ---------------------------------------------------------------- math gate

def new_math_challenge():
    """One random simple math question, server-side verified.

    Returns {"token": ..., "question": "23 + 47 = ?"}. The answer is
    stored in the session under the one-time token and never sent to
    the browser. Two-digit addition/subtraction: simple for an adult,
    a speed bump for a little kid. Only money movement is gated —
    viewing balances, goals, and history never needs a question.
    """
    a = random.randint(10, 50)
    b = random.randint(10, 50)
    op = random.choice(["+", "-"])
    if op == "-" and b > a:
        a, b = b, a
    answer = a + b if op == "+" else a - b
    token = uuid.uuid4().hex
    challenges = session.get("math", {})
    challenges[token] = answer
    while len(challenges) > 20:
        challenges.pop(next(iter(challenges)))
    session["math"] = challenges
    session.modified = True
    return {"token": token, "question": f"{a} {op} {b} = ?"}


def verify_math_challenge():
    """Check the submitted one-time math answer. Consumes the token
    whether the answer is right or wrong, so a challenge cannot be
    replayed. Returns True only on a correct answer."""
    token = (request.form.get("math_token") or "").strip()
    raw = (request.form.get("math_answer") or "").strip()
    challenges = session.get("math", {})
    if token not in challenges:
        return False
    expected = challenges.pop(token)
    session["math"] = challenges
    session.modified = True
    try:
        return int(raw) == expected
    except ValueError:
        return False


# ---------------------------------------------------------------- database

def get_db(data_dir):
    if "db" not in g:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "bankin.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            tv_ip TEXT NOT NULL DEFAULT '',
            avatar_path TEXT,
            theme TEXT NOT NULL DEFAULT 'sky',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kid_id INTEGER NOT NULL REFERENCES kids(id),
            kind TEXT NOT NULL CHECK (kind IN ('deposit', 'withdrawal')),
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            source TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kid_id INTEGER NOT NULL REFERENCES kids(id),
            name TEXT NOT NULL,
            price_cents INTEGER NOT NULL CHECK (price_cents > 0),
            picture_path TEXT,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'completed')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT
        );
        """
    )
    # Looks round 1 migration for databases created by v1: add the
    # avatar and theme columns if they are not there yet.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(kids)")}
    if "avatar_path" not in cols:
        conn.execute("ALTER TABLE kids ADD COLUMN avatar_path TEXT")
    if "theme" not in cols:
        conn.execute("ALTER TABLE kids ADD COLUMN theme TEXT NOT NULL DEFAULT 'sky'")
    conn.commit()


def get_balance(conn, kid_id):
    row = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN kind = 'deposit' THEN amount_cents
                                 ELSE -amount_cents END), 0) AS balance
        FROM transactions WHERE kid_id = ?
        """,
        (kid_id,),
    ).fetchone()
    return row["balance"]


def get_kid_or_404(conn, kid_id):
    kid = conn.execute("SELECT * FROM kids WHERE id = ?", (kid_id,)).fetchone()
    if kid is None:
        abort(404)
    return kid


def get_active_goal(conn, kid_id):
    return conn.execute(
        "SELECT * FROM goals WHERE kid_id = ? AND status = 'active' "
        "ORDER BY id DESC LIMIT 1",
        (kid_id,),
    ).fetchone()


def add_transaction(conn, kid_id, kind, amount_cents, source, note):
    conn.execute(
        "INSERT INTO transactions (kid_id, kind, amount_cents, source, note)"
        " VALUES (?, ?, ?, ?, ?)",
        (kid_id, kind, amount_cents, source or "", note or ""),
    )
    conn.commit()


# ---------------------------------------------------------------- TV card

def render_tv_card(name, balance_cents, goal_name=None, goal_price_cents=None):
    """Render the 240x240 JPEG balance card for a GeekMagic SmallTV.

    Returns JPEG bytes. The image is exactly 240x240, as required
    for the stock SmallTV full-screen photo slot.
    """
    img = Image.new("RGB", (240, 240), (18, 28, 54))
    draw = ImageDraw.Draw(img)
    try:
        font_small = ImageFont.load_default(size=18)
        font_name = ImageFont.load_default(size=22)
        font_big = ImageFont.load_default(size=40)
    except TypeError:  # older Pillow without size support
        font_small = ImageFont.load_default()
        font_name = ImageFont.load_default()
        font_big = ImageFont.load_default()

    draw.text((12, 14), (name or "")[:20], fill=(255, 255, 255), font=font_name)
    draw.text((12, 44), "BanKin", fill=(150, 180, 255), font=font_small)
    draw.text((12, 82), format_cents(balance_cents), fill=(255, 220, 90),
              font=font_big)
    if goal_name and goal_price_cents:
        pct = goal_progress_pct(balance_cents, goal_price_cents)
        draw.text((12, 138), (goal_name or "")[:22], fill=(255, 255, 255),
                  font=font_small)
        draw.text((12, 162), f"{format_cents(balance_cents)} of "
                  f"{format_cents(goal_price_cents)}  {pct}%",
                  fill=(200, 210, 230), font=font_small)
        # Progress bar.
        draw.rectangle([12, 192, 228, 214], outline=(255, 255, 255))
        fill_w = int(214 * pct / 100)
        if fill_w > 0:
            draw.rectangle([13, 193, 13 + fill_w, 213], fill=(90, 200, 120))
    else:
        draw.text((12, 138), "No saving goal", fill=(200, 210, 230),
                  font=font_small)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _clean_tv_host(tv_ip):
    host = (tv_ip or "").strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    return host.strip("/")


def push_to_tv(tv_ip, kid_id, card_bytes, urlopen=None):
    """Push a TV card to a stock SmallTV-Ultra over HTTP.

    Pattern (stock firmware, no auth):
      POST multipart file field "file" to
        http://<tv_ip>/doUpload?dir=/image/
        with filename bankin-<kid_id>.jpg
      GET http://<tv_ip>/set?img=/image/bankin-<kid_id>.jpg
      GET http://<tv_ip>/set?theme=3

    Returns (ok, message). A failure here must never roll back the
    ledger: callers push only after the ledger change is committed,
    and surface the failure as a warning.
    """
    opener = urlopen or urllib.request.urlopen
    host = _clean_tv_host(tv_ip)
    if not host:
        return False, "No TV IP set for this kid."
    filename = f"bankin-{kid_id}.jpg"
    boundary = "----bankin-boundary"
    body = (
        f"--{boundary}\r\n".encode()
        + f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        + b"Content-Type: image/jpeg\r\n\r\n"
        + card_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    try:
        req = urllib.request.Request(
            f"http://{host}/doUpload?dir=/image/",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with opener(req, timeout=15) as resp:
            resp.read()
        with opener(f"http://{host}/set?img=/image/{filename}", timeout=15) as resp:
            resp.read()
        with opener(f"http://{host}/set?theme=3", timeout=15) as resp:
            resp.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("TV push to %s failed: %s", host, exc)
        return False, f"TV push failed ({exc}). Ledger was not changed."
    return True, f"Pushed balance card to TV at {host}."


# ---------------------------------------------------------------- app

def create_app(data_dir=None):
    app = Flask(__name__)
    app.secret_key = "bankin-lan-only-no-auth"
    app.config["DATA_DIR"] = data_dir or os.environ.get("BANKIN_DATA_DIR") \
        or os.environ.get("DATA_DIR") or "/data"
    data_dir = app.config["DATA_DIR"]
    os.makedirs(os.path.join(data_dir, "pictures"), exist_ok=True)

    with app.app_context():
        # Initialise the schema once at startup.
        conn = sqlite3.connect(os.path.join(data_dir, "bankin.db"))
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.close()

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def db():
        return get_db(app.config["DATA_DIR"])

    def kid_card(kid, balance, goal):
        return render_tv_card(
            kid["name"], balance,
            goal["name"] if goal else None,
            goal["price_cents"] if goal else None,
        )

    def push_for_kid(kid):
        """Best-effort TV push after a committed ledger change."""
        if not kid["tv_ip"]:
            return
        conn = db()
        balance = get_balance(conn, kid["id"])
        goal = get_active_goal(conn, kid["id"])
        ok, msg = push_to_tv(kid["tv_ip"], kid["id"], kid_card(kid, balance, goal))
        flash(msg, "ok" if ok else "warn")

    @app.route("/")
    def index():
        conn = db()
        kids = conn.execute("SELECT * FROM kids ORDER BY name").fetchall()
        rows = []
        for kid in kids:
            balance = get_balance(conn, kid["id"])
            goal = get_active_goal(conn, kid["id"])
            txns = conn.execute(
                "SELECT * FROM transactions WHERE kid_id = ? "
                "ORDER BY id DESC LIMIT 5",
                (kid["id"],)).fetchall()
            rows.append({
                "kid": kid,
                "theme": theme_for(kid),
                "balance": balance,
                "goal": goal,
                "pct": goal_progress_pct(balance, goal["price_cents"]) if goal else None,
                "txns": txns,
                "deposit_challenge": new_math_challenge(),
                "withdraw_challenge": new_math_challenge(),
            })
        return render_template("index.html", rows=rows, themes=THEMES)

    @app.route("/kids", methods=["POST"])
    def add_kid():
        name = (request.form.get("name") or "").strip()
        tv_ip = _clean_tv_host(request.form.get("tv_ip"))
        if not name:
            flash("A kid needs a name.", "warn")
            return redirect(url_for("index"))
        conn = db()
        cur = conn.execute(
            "INSERT INTO kids (name, tv_ip) VALUES (?, ?)", (name, tv_ip))
        conn.commit()
        return redirect(url_for("kid_detail", kid_id=cur.lastrowid))

    @app.route("/kid/<int:kid_id>")
    def kid_detail(kid_id):
        conn = db()
        kid = get_kid_or_404(conn, kid_id)
        balance = get_balance(conn, kid_id)
        goal = get_active_goal(conn, kid_id)
        txns = conn.execute(
            "SELECT * FROM transactions WHERE kid_id = ? ORDER BY id DESC",
            (kid_id,)).fetchall()
        archived = conn.execute(
            "SELECT * FROM goals WHERE kid_id = ? AND status = 'completed' "
            "ORDER BY completed_at DESC",
            (kid_id,)).fetchall()
        return render_template(
            "kid.html", kid=kid, theme=theme_for(kid), themes=THEMES,
            balance=balance, goal=goal,
            pct=goal_progress_pct(balance, goal["price_cents"]) if goal else None,
            txns=txns, archived=archived,
            deposit_challenge=new_math_challenge(),
            withdraw_challenge=new_math_challenge(),
            format_cents=format_cents)

    @app.route("/kid/<int:kid_id>/tv-ip", methods=["POST"])
    def set_tv_ip(kid_id):
        conn = db()
        get_kid_or_404(conn, kid_id)
        tv_ip = _clean_tv_host(request.form.get("tv_ip"))
        conn.execute("UPDATE kids SET tv_ip = ? WHERE id = ?", (tv_ip, kid_id))
        conn.commit()
        flash("TV address saved.", "ok")
        return redirect(url_for("kid_detail", kid_id=kid_id))

    def _money_redirect(kid_id):
        # Flyout/popover submissions on the home screen stay on home;
        # kid-page submissions stay on the kid page.
        if request.form.get("from_home") == "1":
            return redirect(url_for("index"))
        return redirect(url_for("kid_detail", kid_id=kid_id))

    @app.route("/kid/<int:kid_id>/deposit", methods=["POST"])
    def deposit(kid_id):
        conn = db()
        kid = get_kid_or_404(conn, kid_id)
        if not verify_math_challenge():
            flash("That math answer wasn't right — try a new question.", "warn")
            return _money_redirect(kid_id)
        try:
            cents = parse_amount_to_cents(request.form.get("amount"))
        except ValueError as exc:
            flash(str(exc), "warn")
            return _money_redirect(kid_id)
        add_transaction(conn, kid_id, "deposit", cents,
                        request.form.get("source"), request.form.get("note"))
        flash(f"Deposited {format_cents(cents)}.", "ok")
        push_for_kid(kid)
        return _money_redirect(kid_id)

    @app.route("/kid/<int:kid_id>/withdraw", methods=["POST"])
    def withdraw(kid_id):
        conn = db()
        kid = get_kid_or_404(conn, kid_id)
        if not verify_math_challenge():
            flash("That math answer wasn't right — try a new question.", "warn")
            return _money_redirect(kid_id)
        try:
            cents = parse_amount_to_cents(request.form.get("amount"))
        except ValueError as exc:
            flash(str(exc), "warn")
            return _money_redirect(kid_id)
        if cents > get_balance(conn, kid_id):
            flash("Not enough in the account for that withdrawal.", "warn")
            return _money_redirect(kid_id)
        add_transaction(conn, kid_id, "withdrawal", cents,
                        request.form.get("source"), request.form.get("note"))
        flash(f"Withdrew {format_cents(cents)}.", "ok")
        push_for_kid(kid)
        return _money_redirect(kid_id)

    @app.route("/kid/<int:kid_id>/avatar", methods=["POST"])
    def set_avatar(kid_id):
        conn = db()
        kid = get_kid_or_404(conn, kid_id)
        upload = request.files.get("avatar")
        if not upload or not upload.filename:
            flash("Choose a photo first.", "warn")
            return redirect(url_for("index"))
        ext = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
        if ext not in ALLOWED_PICTURE_EXTS:
            flash("Avatar must be a jpg, png, gif or webp.", "warn")
            return redirect(url_for("index"))
        if kid["avatar_path"]:
            try:
                os.remove(os.path.join(app.config["DATA_DIR"], kid["avatar_path"]))
            except OSError:
                pass
        fname = f"{kid_id}_avatar_{uuid.uuid4().hex}.{ext}"
        rel = os.path.join("pictures", fname)
        upload.save(os.path.join(app.config["DATA_DIR"], rel))
        conn.execute("UPDATE kids SET avatar_path = ? WHERE id = ?", (rel, kid_id))
        conn.commit()
        flash("Avatar saved.", "ok")
        return redirect(url_for("index"))

    @app.route("/kid/<int:kid_id>/theme", methods=["POST"])
    def set_theme(kid_id):
        conn = db()
        get_kid_or_404(conn, kid_id)
        theme = (request.form.get("theme") or "").strip()
        if theme not in THEMES:
            flash("Unknown theme.", "warn")
            return redirect(url_for("index"))
        conn.execute("UPDATE kids SET theme = ? WHERE id = ?", (theme, kid_id))
        conn.commit()
        flash(f"Theme set to {THEMES[theme]['label']}.", "ok")
        return redirect(url_for("index"))

    @app.route("/avatar/<int:kid_id>")
    def avatar(kid_id):
        conn = db()
        kid = get_kid_or_404(conn, kid_id)
        if not kid["avatar_path"]:
            abort(404)
        path = os.path.join(app.config["DATA_DIR"], kid["avatar_path"])
        if not os.path.isfile(path):
            abort(404)
        return send_file(path)

    @app.route("/kid/<int:kid_id>/goal", methods=["POST"])
    def save_goal(kid_id):
        """Create a new goal, or edit the active one. One active goal max."""
        conn = db()
        get_kid_or_404(conn, kid_id)
        name = (request.form.get("goal_name") or "").strip()
        try:
            price = parse_amount_to_cents(request.form.get("goal_price"))
        except ValueError as exc:
            flash(str(exc), "warn")
            return redirect(url_for("kid_detail", kid_id=kid_id))
        if not name:
            flash("A goal needs a name.", "warn")
            return redirect(url_for("kid_detail", kid_id=kid_id))
        goal = get_active_goal(conn, kid_id)
        picture_path = goal["picture_path"] if goal else None
        upload = request.files.get("goal_picture")
        if upload and upload.filename:
            ext = upload.filename.rsplit(".", 1)[-1].lower()
            if ext not in ALLOWED_PICTURE_EXTS:
                flash("Picture must be a jpg, png, gif or webp.", "warn")
                return redirect(url_for("kid_detail", kid_id=kid_id))
            if picture_path:
                try:
                    os.remove(os.path.join(app.config["DATA_DIR"], picture_path))
                except OSError:
                    pass
            fname = f"{kid_id}_{uuid.uuid4().hex}.{ext}"
            rel = os.path.join("pictures", fname)
            upload.save(os.path.join(app.config["DATA_DIR"], rel))
            picture_path = rel
        if goal:
            conn.execute(
                "UPDATE goals SET name = ?, price_cents = ?, picture_path = ?"
                " WHERE id = ?",
                (name, price, picture_path, goal["id"]))
        else:
            conn.execute(
                "INSERT INTO goals (kid_id, name, price_cents, picture_path)"
                " VALUES (?, ?, ?, ?)",
                (kid_id, name, price, picture_path))
        conn.commit()
        flash("Saving goal saved.", "ok")
        return redirect(url_for("kid_detail", kid_id=kid_id))

    @app.route("/kid/<int:kid_id>/goal/complete", methods=["POST"])
    def complete_goal(kid_id):
        """Complete the active goal: record a withdrawal for its price
        and archive the goal. Ledger change commits first; the TV push
        afterwards can never undo it."""
        conn = db()
        kid = get_kid_or_404(conn, kid_id)
        goal = get_active_goal(conn, kid_id)
        if not goal:
            flash("No active goal to complete.", "warn")
            return redirect(url_for("kid_detail", kid_id=kid_id))
        if goal["price_cents"] > get_balance(conn, kid_id):
            flash("Not enough in the account to buy the goal yet.", "warn")
            return redirect(url_for("kid_detail", kid_id=kid_id))
        add_transaction(conn, kid_id, "withdrawal", goal["price_cents"],
                        "Goal purchase", goal["name"])
        conn.execute(
            "UPDATE goals SET status = 'completed', completed_at = datetime('now')"
            " WHERE id = ?",
            (goal["id"],))
        conn.commit()
        flash(f"Goal '{goal['name']}' completed.", "ok")
        push_for_kid(kid)
        return redirect(url_for("kid_detail", kid_id=kid_id))

    @app.route("/kid/<int:kid_id>/tv.jpg")
    def tv_card_route(kid_id):
        conn = db()
        kid = get_kid_or_404(conn, kid_id)
        balance = get_balance(conn, kid_id)
        goal = get_active_goal(conn, kid_id)
        return send_file(
            io.BytesIO(kid_card(kid, balance, goal)),
            mimetype="image/jpeg",
            download_name=f"bankin-{kid_id}.jpg")

    @app.route("/kid/<int:kid_id>/push-tv", methods=["POST"])
    def push_tv(kid_id):
        conn = db()
        kid = get_kid_or_404(conn, kid_id)
        if not kid["tv_ip"]:
            flash("Set a TV address for this kid first.", "warn")
            return redirect(url_for("kid_detail", kid_id=kid_id))
        balance = get_balance(conn, kid_id)
        goal = get_active_goal(conn, kid_id)
        ok, msg = push_to_tv(kid["tv_ip"], kid_id, kid_card(kid, balance, goal))
        flash(msg, "ok" if ok else "warn")
        return redirect(url_for("kid_detail", kid_id=kid_id))

    @app.route("/goal-picture/<int:goal_id>")
    def goal_picture(goal_id):
        conn = db()
        goal = conn.execute(
            "SELECT picture_path FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if not goal or not goal["picture_path"]:
            abort(404)
        path = os.path.join(app.config["DATA_DIR"], goal["picture_path"])
        if not os.path.isfile(path):
            abort(404)
        return send_file(path)

    app.jinja_env.globals["format_cents"] = format_cents
    app.jinja_env.globals["kid_initials"] = kid_initials
    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
