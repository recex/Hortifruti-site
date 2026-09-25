import os
import json
import random
import string
import sqlite3
import time
from datetime import datetime

import requests
from flask import Flask, request, jsonify, session, render_template, send_from_directory, g, Response
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-essa-chave-em-producao-" + os.urandom(8).hex())

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "campones.db"))

# ---------------------------------------------------------------------------
# BANCO DE DADOS (sqlite3 puro)
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            emoji TEXT DEFAULT '🥕',
            price REAL NOT NULL,
            unit TEXT DEFAULT 'unidade',
            cat TEXT DEFAULT 'outros',
            desc TEXT DEFAULT '',
            stock INTEGER
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            bairro TEXT DEFAULT '',
            addr TEXT NOT NULL,
            slot TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            items_json TEXT NOT NULL,
            coupon TEXT,
            discount REAL DEFAULT 0,
            delivery_fee REAL DEFAULT 0,
            total REAL NOT NULL,
            status TEXT DEFAULT 'recebido',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS coupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            type TEXT NOT NULL,
            value REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS delivery_zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bairro TEXT NOT NULL,
            taxa REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS delivery_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            url TEXT NOT NULL,
            chat_id TEXT,
            events_json TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS admin (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            password_hash TEXT NOT NULL
        );
        """
    )
    conn.commit()
    row = conn.execute("SELECT id FROM admin LIMIT 1").fetchone()
    if not row:
        default_pw = os.environ.get("ADMIN_PASSWORD", "hortifruti123")
        conn.execute("INSERT INTO admin (password_hash) VALUES (?)", (generate_password_hash(default_pw),))
        conn.commit()
    conn.close()


init_db()

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

_login_state = {"fails": 0, "lock_until": 0}


def require_admin():
    return session.get("is_admin") is True


def product_to_dict(row):
    return {
        "id": row["id"], "name": row["name"], "emoji": row["emoji"], "price": row["price"],
        "unit": row["unit"], "cat": row["cat"], "desc": row["desc"] or "", "stock": row["stock"],
    }


def order_to_dict(row):
    return {
        "id": row["id"], "code": row["code"], "name": row["name"], "phone": row["phone"],
        "bairro": row["bairro"], "addr": row["addr"], "slot": row["slot"], "notes": row["notes"],
        "items": json.loads(row["items_json"]), "coupon": row["coupon"], "discount": row["discount"],
        "deliveryFee": row["delivery_fee"], "total": row["total"], "status": row["status"],
        "date": datetime.fromisoformat(row["created_at"]).strftime("%d/%m/%Y %H:%M"),
    }


def get_setting(key, default=""):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def gen_order_code():
    db = get_db()
    while True:
        code = "".join(random.choices(string.digits, k=4))
        if not db.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone():
            return code


EVENT_META = {
    "novo_pedido": ("🧺", "Novo pedido"),
    "pagamento_confirmado": ("✅", "Pagamento confirmado"),
    "ajuda_solicitada": ("💬", "Cliente pediu ajuda"),
    "cupom_usado": ("🏷️", "Cupom usado"),
    "estoque_baixo": ("⚠️", "Estoque baixo"),
    "aviso_manual": ("📢", "Aviso da loja"),
}


def order_text_summary(order: dict):
    lines = "\n".join(f"• {i['qty']}x {i['name']} ({i['unit']}) — R$ {i['subtotal']:.2f}" for i in order["items"])
    notes_line = f"\n📝 {order['notes']}" if order.get("notes") else ""
    return (
        f"🧺 Novo pedido de {order['name']} (#{order['code']})\n{lines}\n\n"
        f"📍 {order['addr']}\n📞 {order['phone']}{notes_line}\n"
        f"💰 Total: R$ {order['total']:.2f}"
    )


def build_payload(hook_type, chat_id, event_type, message, order=None):
    if event_type == "novo_pedido" and order:
        desc = "\n".join(f"• {i['qty']}x {i['name']} ({i['unit']}) — R$ {i['subtotal']:.2f}" for i in order["items"])
        if hook_type == "discord":
            fields = [
                {"name": "Telefone", "value": order["phone"], "inline": True},
                {"name": "Total", "value": f"R$ {order['total']:.2f}", "inline": True},
                {"name": "Endereço", "value": order["addr"]},
            ]
            if order.get("notes"):
                fields.append({"name": "Observações", "value": order["notes"]})
            return {
                "username": "O Camponês — Pedidos",
                "embeds": [{
                    "title": f"Novo pedido de {order['name']} (#{order['code']})",
                    "description": desc, "color": 9425246, "fields": fields,
                }],
            }
        if hook_type == "slack":
            return {"text": order_text_summary(order)}
        if hook_type == "telegram":
            return {"chat_id": chat_id, "text": order_text_summary(order)}
        return order

    emoji, title = EVENT_META.get(event_type, ("📢", "Aviso"))
    if hook_type == "discord":
        return {"username": "O Camponês — Avisos", "content": f"{emoji} **{title}**\n{message}"}
    if hook_type == "slack":
        return {"text": f"{emoji} *{title}*\n{message}"}
    if hook_type == "telegram":
        return {"chat_id": chat_id, "text": f"{emoji} {title}\n{message}"}
    return {"event": event_type, "title": title, "message": message, "timestamp": datetime.utcnow().isoformat()}


def fire_event(event_type, message="", order=None):
    hooks = get_db().execute("SELECT * FROM webhooks").fetchall()
    for hook in hooks:
        events = json.loads(hook["events_json"] or "[]")
        if events and event_type not in events:
            continue
        try:
            payload = build_payload(hook["type"], hook["chat_id"], event_type, message, order)
            requests.post(hook["url"], json=payload, timeout=6)
        except Exception as e:
            app.logger.warning(f"Falha ao enviar webhook {hook['type']}: {e}")

# ---------------------------------------------------------------------------
# ROTAS DE PÁGINA
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

# ---------------------------------------------------------------------------
# API - AUTENTICAÇÃO
# ---------------------------------------------------------------------------

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    if time.time() < _login_state["lock_until"]:
        secs = int(_login_state["lock_until"] - time.time())
        return jsonify({"ok": False, "error": f"Bloqueado por mais {secs}s."}), 429

    data = request.get_json(force=True)
    password = data.get("password", "")
    row = get_db().execute("SELECT * FROM admin LIMIT 1").fetchone()
    if row and check_password_hash(row["password_hash"], password):
        _login_state["fails"] = 0
        session["is_admin"] = True
        session.permanent = True
        return jsonify({"ok": True})

    _login_state["fails"] += 1
    if _login_state["fails"] >= 5:
        _login_state["lock_until"] = time.time() + 60
        _login_state["fails"] = 0
        return jsonify({"ok": False, "error": "Muitas tentativas. Bloqueado por 60s."}), 429
    return jsonify({"ok": False, "error": f"Senha incorreta. ({_login_state['fails']}/5)"}), 401


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    return jsonify({"ok": True})


@app.route("/api/admin/check")
def admin_check():
    return jsonify({"isAdmin": require_admin()})


@app.route("/api/admin/change-password", methods=["POST"])
def change_password():
    if not require_admin():
        return jsonify({"ok": False, "error": "Não autorizado."}), 403
    data = request.get_json(force=True)
    new_pw = data.get("password", "")
    if len(new_pw) < 6:
        return jsonify({"ok": False, "error": "Senha muito curta (mínimo 6 caracteres)."}), 400
    db = get_db()
    db.execute("UPDATE admin SET password_hash = ?", (generate_password_hash(new_pw),))
    db.commit()
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# API - PRODUTOS
# ---------------------------------------------------------------------------

@app.route("/api/products", methods=["GET"])
def list_products():
    rows = get_db().execute("SELECT * FROM products ORDER BY id").fetchall()
    return jsonify([product_to_dict(r) for r in rows])


@app.route("/api/products", methods=["POST"])
def create_product():
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    d = request.get_json(force=True)
    if not d.get("name") or d.get("price") is None:
        return jsonify({"error": "Nome e preço são obrigatórios."}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO products (name, emoji, price, unit, cat, desc, stock) VALUES (?,?,?,?,?,?,?)",
        (d["name"][:120], d.get("emoji", "🥕")[:8], float(d["price"]), d.get("unit", "unidade")[:40],
         d.get("cat", "outros")[:60], d.get("desc", "")[:500], d.get("stock")),
    )
    db.commit()
    row = db.execute("SELECT * FROM products WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(product_to_dict(row)), 201


@app.route("/api/products/<int:pid>", methods=["PUT"])
def update_product(pid):
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    db = get_db()
    row = db.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if not row:
        return jsonify({"error": "Produto não encontrado."}), 404
    d = request.get_json(force=True)
    fields = {"name": row["name"], "emoji": row["emoji"], "unit": row["unit"], "cat": row["cat"],
              "desc": row["desc"], "price": row["price"], "stock": row["stock"]}
    for f in ["name", "emoji", "unit", "cat", "desc"]:
        if f in d:
            fields[f] = d[f]
    if "price" in d:
        fields["price"] = float(d["price"])
    if "stock" in d:
        fields["stock"] = d["stock"]
    db.execute(
        "UPDATE products SET name=?, emoji=?, unit=?, cat=?, desc=?, price=?, stock=? WHERE id=?",
        (fields["name"], fields["emoji"], fields["unit"], fields["cat"], fields["desc"],
         fields["price"], fields["stock"], pid),
    )
    db.commit()
    row = db.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if row["stock"] is not None and row["stock"] <= 3:
        fire_event("estoque_baixo", f'O produto "{row["name"]}" está com estoque baixo — hora de repor!')
    return jsonify(product_to_dict(row))


@app.route("/api/products/<int:pid>", methods=["DELETE"])
def delete_product(pid):
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    db = get_db()
    db.execute("DELETE FROM products WHERE id = ?", (pid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/products/<int:pid>/low-stock-alert", methods=["POST"])
def low_stock_alert(pid):
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    row = get_db().execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if not row:
        return jsonify({"error": "Produto não encontrado."}), 404
    fire_event("estoque_baixo", f'O produto "{row["name"]}" está marcado com estoque baixo — hora de repor!')
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# API - CUPONS
# ---------------------------------------------------------------------------

@app.route("/api/coupons", methods=["GET"])
def list_coupons():
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    rows = get_db().execute("SELECT * FROM coupons").fetchall()
    return jsonify([{"id": r["id"], "code": r["code"], "type": r["type"], "value": r["value"]} for r in rows])


@app.route("/api/coupons", methods=["POST"])
def create_coupon():
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    d = request.get_json(force=True)
    code = d.get("code", "").strip().upper()
    if not code or d.get("value") is None:
        return jsonify({"error": "Código e valor são obrigatórios."}), 400
    db = get_db()
    db.execute(
        "INSERT INTO coupons (code, type, value) VALUES (?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET type = excluded.type, value = excluded.value",
        (code, d.get("type", "percent"), float(d["value"])),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/coupons/<int:cid>", methods=["DELETE"])
def delete_coupon(cid):
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    db = get_db()
    db.execute("DELETE FROM coupons WHERE id = ?", (cid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/coupons/validate", methods=["POST"])
def validate_coupon():
    d = request.get_json(force=True)
    code = d.get("code", "").strip().upper()
    row = get_db().execute("SELECT * FROM coupons WHERE code = ?", (code,)).fetchone()
    if not row:
        return jsonify({"valid": False}), 404
    fire_event("cupom_usado", f'Cupom "{row["code"]}" foi aplicado por um cliente no carrinho.')
    return jsonify({"valid": True, "coupon": {"id": row["id"], "code": row["code"], "type": row["type"], "value": row["value"]}})

# ---------------------------------------------------------------------------
# API - ENTREGA
# ---------------------------------------------------------------------------

@app.route("/api/delivery-zones", methods=["GET"])
def list_zones():
    rows = get_db().execute("SELECT * FROM delivery_zones").fetchall()
    return jsonify([{"id": r["id"], "bairro": r["bairro"], "taxa": r["taxa"]} for r in rows])


@app.route("/api/delivery-zones", methods=["POST"])
def create_zone():
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    d = request.get_json(force=True)
    db = get_db()
    db.execute("INSERT INTO delivery_zones (bairro, taxa) VALUES (?,?)", (d["bairro"][:120], float(d["taxa"])))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/delivery-zones/<int:zid>", methods=["DELETE"])
def delete_zone(zid):
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    db = get_db()
    db.execute("DELETE FROM delivery_zones WHERE id = ?", (zid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/delivery-slots", methods=["GET"])
def list_slots():
    rows = get_db().execute("SELECT * FROM delivery_slots").fetchall()
    return jsonify([{"id": r["id"], "label": r["label"]} for r in rows])


@app.route("/api/delivery-slots", methods=["POST"])
def create_slot():
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    d = request.get_json(force=True)
    db = get_db()
    db.execute("INSERT INTO delivery_slots (label) VALUES (?)", (d["label"][:120],))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/delivery-slots/<int:sid>", methods=["DELETE"])
def delete_slot(sid):
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    db = get_db()
    db.execute("DELETE FROM delivery_slots WHERE id = ?", (sid,))
    db.commit()
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# API - CONFIGURAÇÕES
# ---------------------------------------------------------------------------

PUBLIC_SETTING_KEYS = [
    "pix_key", "pix_name", "pix_city", "help_phone", "min_order",
    "footer_addr", "footer_hours", "footer_insta", "footer_wpp",
]

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify({k: get_setting(k) for k in PUBLIC_SETTING_KEYS})


@app.route("/api/settings", methods=["POST"])
def update_settings():
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    d = request.get_json(force=True)
    for k in PUBLIC_SETTING_KEYS:
        if k in d:
            set_setting(k, str(d[k]))
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# API - WEBHOOKS
# ---------------------------------------------------------------------------

@app.route("/api/webhooks", methods=["GET"])
def list_webhooks():
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    rows = get_db().execute("SELECT * FROM webhooks").fetchall()
    return jsonify([
        {"id": r["id"], "type": r["type"], "url": r["url"], "chatId": r["chat_id"],
         "events": json.loads(r["events_json"] or "[]")} for r in rows
    ])


@app.route("/api/webhooks", methods=["POST"])
def create_webhook():
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    d = request.get_json(force=True)
    db = get_db()
    cur = db.execute(
        "INSERT INTO webhooks (type, url, chat_id, events_json) VALUES (?,?,?,?)",
        (d["type"], d["url"], d.get("chatId"), json.dumps(d.get("events", []))),
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid}), 201


@app.route("/api/webhooks/<int:wid>", methods=["DELETE"])
def delete_webhook(wid):
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    db = get_db()
    db.execute("DELETE FROM webhooks WHERE id = ?", (wid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/webhooks/<int:wid>/test", methods=["POST"])
def test_webhook(wid):
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    row = get_db().execute("SELECT * FROM webhooks WHERE id = ?", (wid,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Webhook não encontrado."}), 404
    fake_order = {
        "code": "0000", "name": "Teste", "phone": "—", "addr": "—", "notes": "",
        "items": [{"qty": 1, "name": "Produto teste", "unit": "un", "subtotal": 0}], "total": 0,
    }
    try:
        payload = build_payload(row["type"], row["chat_id"], "novo_pedido", "", fake_order)
        requests.post(row["url"], json=payload, timeout=6)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/manual-notice", methods=["POST"])
def manual_notice():
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    d = request.get_json(force=True)
    message = d.get("message", "").strip()
    if not message:
        return jsonify({"error": "Mensagem vazia."}), 400
    fire_event("aviso_manual", message)
    return jsonify({"ok": True})


@app.route("/api/help-requested", methods=["POST"])
def help_requested():
    fire_event("ajuda_solicitada", "Um cliente clicou no botão de ajuda do site e foi direcionado ao WhatsApp.")
    return jsonify({"ok": True, "phone": get_setting("help_phone")})

# ---------------------------------------------------------------------------
# API - PEDIDOS
# ---------------------------------------------------------------------------

@app.route("/api/orders", methods=["GET"])
def list_orders():
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    rows = get_db().execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return jsonify([order_to_dict(r) for r in rows])


@app.route("/api/orders", methods=["POST"])
def create_order():
    d = request.get_json(force=True)
    for field in ["name", "phone", "addr", "items"]:
        if not d.get(field):
            return jsonify({"error": f"Campo obrigatório faltando: {field}"}), 400
    if not d.get("consent"):
        return jsonify({"error": "É preciso concordar com o uso dos dados."}), 400

    db = get_db()
    subtotal = 0
    resolved_items = []
    for it in d["items"]:
        row = db.execute("SELECT * FROM products WHERE id = ?", (it["id"],)).fetchone()
        if not row:
            continue
        qty = max(1, int(it["qty"]))
        if row["stock"] is not None:
            qty = min(qty, max(0, row["stock"]))
        if qty <= 0:
            continue
        sub = round(row["price"] * qty, 2)
        subtotal += sub
        resolved_items.append({"id": row["id"], "name": row["name"], "qty": qty, "unit": row["unit"], "subtotal": sub})

    if not resolved_items:
        return jsonify({"error": "Nenhum item disponível no pedido."}), 400

    min_order = float(get_setting("min_order", "0") or 0)

    discount = 0
    coupon_code = d.get("coupon")
    if coupon_code:
        c = db.execute("SELECT * FROM coupons WHERE code = ?", (coupon_code.upper(),)).fetchone()
        if c:
            discount = subtotal * (c["value"] / 100) if c["type"] == "percent" else c["value"]
            discount = min(discount, subtotal)
            coupon_code = c["code"]
        else:
            coupon_code = None

    if (subtotal - discount) < min_order:
        return jsonify({"error": f"Pedido mínimo é R$ {min_order:.2f}."}), 400

    delivery_fee = 0
    bairro = d.get("bairro", "")
    if bairro:
        z = db.execute("SELECT * FROM delivery_zones WHERE bairro = ?", (bairro,)).fetchone()
        if z:
            delivery_fee = z["taxa"]

    total = round(subtotal - discount + delivery_fee, 2)
    code = gen_order_code()
    created_at = datetime.utcnow().isoformat()

    cur = db.execute(
        "INSERT INTO orders (code,name,phone,bairro,addr,slot,notes,items_json,coupon,discount,delivery_fee,total,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, d["name"][:120], d["phone"][:40], bairro[:120], d["addr"][:300], d.get("slot", "")[:120],
         d.get("notes", "")[:300], json.dumps(resolved_items), coupon_code, discount, delivery_fee, total,
         "recebido", created_at),
    )

    for it in resolved_items:
        row = db.execute("SELECT * FROM products WHERE id = ?", (it["id"],)).fetchone()
        if row["stock"] is not None:
            db.execute("UPDATE products SET stock = ? WHERE id = ?", (max(0, row["stock"] - it["qty"]), it["id"]))

    db.commit()

    order_row = db.execute("SELECT * FROM orders WHERE id = ?", (cur.lastrowid,)).fetchone()
    order_dict = order_to_dict(order_row)
    fire_event("novo_pedido", "", order_dict)

    for it in resolved_items:
        row = db.execute("SELECT * FROM products WHERE id = ?", (it["id"],)).fetchone()
        if row["stock"] is not None and row["stock"] <= 3:
            fire_event("estoque_baixo", f'O produto "{row["name"]}" está com estoque baixo — hora de repor!')

    return jsonify(order_dict), 201


@app.route("/api/orders/<int:oid>/status", methods=["PUT"])
def update_order_status(oid):
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    d = request.get_json(force=True)
    db = get_db()
    row = db.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    if not row:
        return jsonify({"error": "Pedido não encontrado."}), 404
    db.execute("UPDATE orders SET status = ? WHERE id = ?", (d.get("status", row["status"]), oid))
    db.commit()
    row = db.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    return jsonify(order_to_dict(row))


@app.route("/api/orders/<int:oid>/paid", methods=["POST"])
def mark_paid(oid):
    row = get_db().execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    if not row:
        return jsonify({"error": "Pedido não encontrado."}), 404
    fire_event("pagamento_confirmado", f"Pedido #{row['code']} de {row['name']} confirmado como pago via Pix. Total: R$ {row['total']:.2f}")
    return jsonify({"ok": True})


@app.route("/api/orders/track/<code>")
def track_order(code):
    row = get_db().execute("SELECT * FROM orders WHERE code = ?", (code,)).fetchone()
    if not row:
        return jsonify({"found": False}), 404
    d = order_to_dict(row)
    return jsonify({"found": True, "status": d["status"], "date": d["date"], "total": d["total"]})


@app.route("/api/orders/export.csv")
def export_orders_csv():
    if not require_admin():
        return jsonify({"error": "Não autorizado."}), 403
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Codigo", "Data", "Cliente", "Telefone", "Bairro", "Endereco", "Horario",
                      "Itens", "Cupom", "Desconto", "Taxa entrega", "Total", "Status"])
    rows = get_db().execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    for r in rows:
        items = json.loads(r["items_json"])
        writer.writerow([
            r["code"], datetime.fromisoformat(r["created_at"]).strftime("%d/%m/%Y %H:%M"), r["name"], r["phone"],
            r["bairro"], r["addr"], r["slot"], " | ".join(f"{i['qty']}x {i['name']}" for i in items),
            r["coupon"] or "", f"{r['discount']:.2f}", f"{r['delivery_fee']:.2f}", f"{r['total']:.2f}", r["status"],
        ])
    return Response(
        "\ufeff" + buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=pedidos-o-camponês.csv"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
