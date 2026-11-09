import os, sqlite3, csv, io, re, secrets, html
from functools import wraps
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE)
DB = os.environ.get("DATABASE_PATH", os.path.join(DATA_DIR, "database", "invoice_web.db"))
PDF_DIR = os.path.join(DATA_DIR, "invoices")
os.makedirs(os.path.dirname(DB), exist_ok=True)
os.makedirs(PDF_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=7))
TRIAL_DAYS = 7
TRIAL_INVOICE_LIMIT = 5
DEFAULT_SETTINGS = {"company_name": "MY COMPANY", "company_address": "Your Address, City, State - PIN",
                    "company_phone": "+91 XXXXX XXXXX", "company_email": "your@email.com", "company_gstin": "YOUR GSTIN"}


def get_db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def columns(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    c = get_db()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'User',
      email TEXT, created_at TEXT, trial_started_at TEXT, plan_status TEXT DEFAULT 'trial'
    );
    CREATE TABLE IF NOT EXISTS settings(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT,
      UNIQUE(user_id,key), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS customers(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL,
      phone TEXT,email TEXT,address TEXT, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS products(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL,
      hsn_sac TEXT,rate REAL DEFAULT 0,gst_percent REAL DEFAULT 18,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS invoices(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, invoice_number TEXT NOT NULL,
      customer_id INTEGER, invoice_date TEXT,due_date TEXT,subtotal REAL,discount REAL,taxable_value REAL,
      tax_type TEXT,gst_percent REAL,cgst REAL,sgst REAL,igst REAL,grand_total REAL,
      payment_status TEXT,notes TEXT,pdf_path TEXT,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY(customer_id) REFERENCES customers(id), UNIQUE(user_id,invoice_number)
    );
    CREATE TABLE IF NOT EXISTS invoice_items(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, invoice_id INTEGER NOT NULL,
      item_name TEXT,hsn_sac TEXT,quantity INTEGER,rate REAL,amount REAL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY(invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
    );
    """)
    # Upgrade the uploaded single-user database safely; old records stay with the original admin.
    for table in ("customers", "products", "invoices", "invoice_items"):
        if "user_id" not in columns(c, table):
            c.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
    ucols = columns(c, "users")
    for name, decl in (("email", "TEXT"), ("created_at", "TEXT"), ("trial_started_at", "TEXT"), ("plan_status", "TEXT DEFAULT 'trial'")):
        if name not in ucols:
            c.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
    # Existing installations retain their original admin account; new installs have no public default admin.
    admin = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if admin:
        aid = admin["id"]
        c.execute("UPDATE users SET plan_status='lifetime' WHERE id=? AND (plan_status IS NULL OR plan_status='trial')", (aid,))
        for table in ("customers", "products", "invoices", "invoice_items"):
            c.execute(f"UPDATE {table} SET user_id=? WHERE user_id IS NULL", (aid,))
        # Migrate old global settings to the legacy admin account.
        if "key" in columns(c, "settings"):
            old = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
            scols = columns(c, "settings")
            if "user_id" not in scols:
                c.execute("ALTER TABLE settings RENAME TO settings_legacy")
                c.execute("CREATE TABLE settings(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,key TEXT NOT NULL,value TEXT,UNIQUE(user_id,key),FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)")
                for row in c.execute("SELECT key,value FROM settings_legacy").fetchall():
                    c.execute("INSERT OR REPLACE INTO settings(user_id,key,value) VALUES(?,?,?)", (aid,row["key"],row["value"]))
                c.execute("DROP TABLE settings_legacy")
    # Ensure settings schema exists for databases created by the previous version.
    scols = columns(c, "settings")
    if "user_id" not in scols:
        c.execute("ALTER TABLE settings RENAME TO settings_legacy")
        c.execute("CREATE TABLE settings(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,key TEXT NOT NULL,value TEXT,UNIQUE(user_id,key),FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)")
        if admin:
            for row in c.execute("SELECT key,value FROM settings_legacy").fetchall():
                c.execute("INSERT OR REPLACE INTO settings(user_id,key,value) VALUES(?,?,?)", (admin["id"],row["key"],row["value"]))
        c.execute("DROP TABLE settings_legacy")
    c.execute("CREATE INDEX IF NOT EXISTS idx_customers_user ON customers(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_products_user ON products(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_invoices_user ON invoices(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_items_invoice_user ON invoice_items(user_id,invoice_id)")
    c.commit(); c.close()

init_db()


def current_user_id(): return session.get("user_id")

def setting(k, user_id=None):
    uid = user_id or current_user_id()
    if not uid: return DEFAULT_SETTINGS.get(k, "")
    c=get_db(); r=c.execute("SELECT value FROM settings WHERE user_id=? AND key=?",(uid,k)).fetchone(); c.close()
    return r["value"] if r else DEFAULT_SETTINGS.get(k, "")

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user_id(): return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user_id(): return redirect(url_for("login"))
        if session.get("role") != "Admin": flash("Admin access required.","danger"); return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper

def account_access_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        uid=current_user_id()
        if not uid: return redirect(url_for("login"))
        c=get_db(); u=c.execute("SELECT plan_status,trial_started_at FROM users WHERE id=?",(uid,)).fetchone()
        count=c.execute("SELECT COUNT(*) FROM invoices WHERE user_id=?",(uid,)).fetchone()[0]; c.close()
        if not u: session.clear(); return redirect(url_for("login"))
        if u["plan_status"] == "lifetime": return f(*args, **kwargs)
        if u["plan_status"] == "trial" and u["trial_started_at"]:
            try: started=datetime.fromisoformat(u["trial_started_at"])
            except ValueError: started=datetime.now(timezone.utc)
            if started.tzinfo is None: started=started.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < started+timedelta(days=TRIAL_DAYS) and count < TRIAL_INVOICE_LIMIT:
                return f(*args, **kwargs)
        flash("Your free trial has ended. Purchase a lifetime license to continue.","warning")
        return redirect(url_for("account"))
    return wrapper

@app.before_request
def protect_post_requests():
    if request.method == "POST":
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not supplied or not expected or not secrets.compare_digest(supplied, expected):
            abort(400)

@app.context_processor
def common():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return {"company_name":setting("company_name"), "logged_user":session.get("username"),
            "role":session.get("role"), "csrf_token":session["csrf_token"]}

@app.route("/")
def index(): return redirect(url_for("dashboard") if current_user_id() else url_for("login"))

@app.route("/signup", methods=["GET","POST"])
def signup():
    if request.method=="POST":
        username=request.form.get("username","").strip(); email=request.form.get("email","").strip().lower(); password=request.form.get("password","")
        if len(username)<3 or len(password)<8:
            flash("Username must be at least 3 characters and password at least 8 characters.","danger")
            return render_template("signup.html")
        if email and not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$",email):
            flash("Please enter a valid email address.","danger"); return render_template("signup.html")
        now=datetime.now(timezone.utc).isoformat(); c=get_db()
        try:
            cur=c.execute("INSERT INTO users(username,password_hash,role,email,created_at,trial_started_at,plan_status) VALUES(?,?,?,?,?,?,?)",
                          (username,generate_password_hash(password),"User",email,now,now,"trial"))
            uid=cur.lastrowid
            for k,v in DEFAULT_SETTINGS.items(): c.execute("INSERT INTO settings(user_id,key,value) VALUES(?,?,?)",(uid,k,v))
            c.commit(); session.clear(); session.permanent=True
            session.update(user_id=uid,username=username,role="User")
            flash("Account created. Your free trial includes 7 days or 5 invoices, whichever comes first.","success")
            return redirect(url_for("dashboard"))
        except sqlite3.IntegrityError:
            c.rollback(); flash("That username is already taken.","danger")
        finally: c.close()
    return render_template("signup.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        u=request.form.get("username","").strip(); p=request.form.get("password","")
        c=get_db(); user=c.execute("SELECT * FROM users WHERE username=?",(u,)).fetchone(); c.close()
        if user and check_password_hash(user["password_hash"],p):
            session.clear(); session.permanent=True
            session.update(user_id=user["id"],username=user["username"],role=user["role"])
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.","danger")
    return render_template("login.html")

@app.route("/logout", methods=["POST"])
def logout(): session.clear(); return redirect(url_for("login"))

@app.route("/account")
@login_required
def account():
    c=get_db(); u=c.execute("SELECT username,email,plan_status,trial_started_at FROM users WHERE id=?",(current_user_id(),)).fetchone()
    count=c.execute("SELECT COUNT(*) FROM invoices WHERE user_id=?",(current_user_id(),)).fetchone()[0]; c.close()
    days_left=0
    if u["trial_started_at"]:
        try: days_left=max(0,(datetime.fromisoformat(u["trial_started_at"])+timedelta(days=TRIAL_DAYS)-datetime.now(timezone.utc)).days)
        except Exception: pass
    return render_template("account.html",account=u,invoice_count=count,days_left=days_left,trial_limit=TRIAL_INVOICE_LIMIT)

@app.route("/dashboard")
@login_required
@account_access_required
def dashboard():
    uid=current_user_id(); c=get_db()
    stats={"invoices":c.execute("SELECT COUNT(*) n FROM invoices WHERE user_id=?",(uid,)).fetchone()["n"],
           "sales":c.execute("SELECT COALESCE(SUM(grand_total),0) n FROM invoices WHERE user_id=?",(uid,)).fetchone()["n"],
           "paid":c.execute("SELECT COALESCE(SUM(grand_total),0) n FROM invoices WHERE user_id=? AND payment_status='Paid'",(uid,)).fetchone()["n"],
           "pending":c.execute("SELECT COALESCE(SUM(grand_total),0) n FROM invoices WHERE user_id=? AND payment_status!='Paid'",(uid,)).fetchone()["n"]}
    recent=c.execute("SELECT i.*,c.name customer FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id AND c.user_id=i.user_id WHERE i.user_id=? ORDER BY i.id DESC LIMIT 10",(uid,)).fetchall(); c.close()
    return render_template("dashboard.html",stats=stats,recent=recent)

@app.route("/customers", methods=["GET","POST"])
@login_required
@account_access_required
def customers():
    uid=current_user_id(); c=get_db()
    if request.method=="POST":
        c.execute("INSERT INTO customers(user_id,name,phone,email,address) VALUES(?,?,?,?,?)",(uid,request.form["name"],request.form.get("phone",""),request.form.get("email",""),request.form.get("address","")))
        c.commit(); flash("Customer added.","success")
    rows=c.execute("SELECT * FROM customers WHERE user_id=? ORDER BY name",(uid,)).fetchall(); c.close()
    return render_template("customers.html",rows=rows)

@app.route("/customers/delete/<int:id>",methods=["POST"])
@login_required
@account_access_required
def customer_delete(id):
    c=get_db(); c.execute("DELETE FROM customers WHERE id=? AND user_id=?",(id,current_user_id())); c.commit(); c.close()
    flash("Customer deleted.","success"); return redirect(url_for("customers"))

@app.route("/products", methods=["GET","POST"])
@login_required
@account_access_required
def products():
    uid=current_user_id(); c=get_db()
    if request.method=="POST":
        c.execute("INSERT INTO products(user_id,name,hsn_sac,rate,gst_percent) VALUES(?,?,?,?,?)",(uid,request.form["name"],request.form.get("hsn_sac",""),float(request.form["rate"]),float(request.form["gst_percent"])))
        c.commit(); flash("Product added.","success")
    rows=c.execute("SELECT * FROM products WHERE user_id=? ORDER BY name",(uid,)).fetchall(); c.close()
    return render_template("products.html",rows=rows)

@app.route("/products/delete/<int:id>",methods=["POST"])
@login_required
@account_access_required
def product_delete(id):
    c=get_db(); c.execute("DELETE FROM products WHERE id=? AND user_id=?",(id,current_user_id())); c.commit(); c.close()
    flash("Product deleted.","success"); return redirect(url_for("products"))

@app.route("/users",methods=["GET","POST"])
@admin_required
def users():
    c=get_db(); rows=c.execute("SELECT id,username,role,email,plan_status FROM users ORDER BY id DESC").fetchall(); c.close()
    return render_template("users.html",rows=rows,users=rows)

@app.route("/users/add",methods=["GET","POST"])
@admin_required
def add_user():
    if request.method=="POST":
        username=request.form.get("username","").strip(); password=request.form.get("password",""); role=request.form.get("role","User")
        if len(username)<3 or len(password)<8: flash("Username (3+) and password (8+) are required.","danger"); return redirect(url_for("users"))
        c=get_db()
        try:
            now=datetime.now(timezone.utc).isoformat(); cur=c.execute("INSERT INTO users(username,password_hash,role,created_at,trial_started_at,plan_status) VALUES(?,?,?,?,?,?)",(username,generate_password_hash(password),role,now,now,"trial")); uid=cur.lastrowid
            for k,v in DEFAULT_SETTINGS.items(): c.execute("INSERT INTO settings(user_id,key,value) VALUES(?,?,?)",(uid,k,v))
            c.commit(); flash("User created.","success")
        except sqlite3.IntegrityError: flash("Username already exists.","danger")
        finally: c.close()
        return redirect(url_for("users"))
    return redirect(url_for("users"))

@app.route("/users/delete/<int:id>",methods=["POST"])
@admin_required
def user_delete(id):
    if id==current_user_id(): flash("You cannot delete your own account.","danger")
    else:
        c=get_db(); c.execute("DELETE FROM users WHERE id=? AND role!='Admin'",(id,)); c.commit(); c.close(); flash("User deleted if eligible.","success")
    return redirect(url_for("users"))

@app.route("/settings",methods=["GET","POST"])
@login_required
@account_access_required
def settings():
    uid=current_user_id()
    if request.method=="POST":
        c=get_db()
        for k in DEFAULT_SETTINGS: c.execute("INSERT INTO settings(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",(uid,k,request.form.get(k,"")))
        c.commit(); c.close(); flash("Settings saved.","success")
    return render_template("settings.html",s={k:setting(k,uid) for k in DEFAULT_SETTINGS})

def next_invoice_no(uid):
    c=get_db(); rows=c.execute("SELECT invoice_number FROM invoices WHERE user_id=?",(uid,)).fetchall(); c.close(); nums=[]
    for r in rows:
        try: nums.append(int(r["invoice_number"].split("-")[-1]))
        except Exception: pass
    return f"INV-{uid}-{(max(nums)+1 if nums else 1):03d}"

@app.route("/invoices")
@login_required
@account_access_required
def invoices():
    uid=current_user_id(); q=request.args.get("q","").strip(); c=get_db()
    rows=c.execute("SELECT i.*,c.name customer FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id AND c.user_id=i.user_id WHERE i.user_id=? AND (i.invoice_number LIKE ? OR COALESCE(c.name,'') LIKE ?) ORDER BY i.id DESC",(uid,f"%{q}%",f"%{q}%")).fetchall(); c.close()
    return render_template("invoices.html",rows=rows,q=q)

@app.route("/invoice/new",methods=["GET","POST"])
@login_required
@account_access_required
def new_invoice():
    uid=current_user_id(); c=get_db(); customers=c.execute("SELECT * FROM customers WHERE user_id=? ORDER BY name",(uid,)).fetchall(); products=c.execute("SELECT * FROM products WHERE user_id=? ORDER BY name",(uid,)).fetchall()
    if request.method=="POST":
        customer_id=int(request.form["customer_id"])
        if not c.execute("SELECT id FROM customers WHERE id=? AND user_id=?",(customer_id,uid)).fetchone(): c.close(); abort(400)
        invoice_date=request.form["invoice_date"]; due_date=request.form["due_date"]; tax_type=request.form["tax_type"]; gst=float(request.form.get("gst_percent") or 0); discount=float(request.form.get("discount") or 0); status=request.form["payment_status"]; notes=request.form.get("notes","")
        names=request.form.getlist("item_name"); hsns=request.form.getlist("hsn_sac"); qtys=request.form.getlist("quantity"); rates=request.form.getlist("rate")
        items=[]; subtotal=0
        for name,hsn,qty,rate in zip(names,hsns,qtys,rates):
            if not name.strip(): continue
            q=int(qty); r=float(rate)
            if q<=0 or r<0: c.close(); abort(400)
            amount=q*r; subtotal+=amount; items.append((name,hsn,q,r,amount))
        taxable=max(0,subtotal-discount); cgst=sgst=igst=0
        if tax_type=="CGST + SGST": cgst=sgst=taxable*gst/200
        elif tax_type=="IGST": igst=taxable*gst/100
        total=taxable+cgst+sgst+igst
        if not items: flash("Add at least one item.","danger"); c.close(); return render_template("new_invoice.html",customers=customers,products=[dict(x) for x in products],next_no=next_invoice_no(uid))
        no=next_invoice_no(uid)
        try:
            cur=c.cursor(); cur.execute("INSERT INTO invoices(user_id,invoice_number,customer_id,invoice_date,due_date,subtotal,discount,taxable_value,tax_type,gst_percent,cgst,sgst,igst,grand_total,payment_status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(uid,no,customer_id,invoice_date,due_date,subtotal,discount,taxable,tax_type,gst,cgst,sgst,igst,total,status,notes)); iid=cur.lastrowid
            for x in items: cur.execute("INSERT INTO invoice_items(user_id,invoice_id,item_name,hsn_sac,quantity,rate,amount) VALUES(?,?,?,?,?,?,?)",(uid,iid,*x))
            c.commit()
        except Exception: c.rollback(); c.close(); raise
        c.close(); return redirect(url_for("invoice_pdf",id=iid))
    c.close(); return render_template("new_invoice.html",customers=customers,products=[dict(x) for x in products],next_no=next_invoice_no(uid))

@app.route("/invoice/<int:id>/pdf")
@login_required
def invoice_pdf(id):
    uid=current_user_id(); c=get_db()
    inv=c.execute("SELECT i.*,c.name customer,c.phone,c.email,c.address FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id AND c.user_id=i.user_id WHERE i.id=? AND i.user_id=?",(id,uid)).fetchone()
    items=c.execute("SELECT * FROM invoice_items WHERE invoice_id=? AND user_id=? ORDER BY id",(id,uid)).fetchall(); c.close()
    if not inv: abort(404)
    path=os.path.join(PDF_DIR,str(uid),f"{inv['invoice_number']}.pdf"); os.makedirs(os.path.dirname(path),exist_ok=True)
    doc=SimpleDocTemplate(path,pagesize=A4,rightMargin=13*mm,leftMargin=13*mm,topMargin=12*mm,bottomMargin=12*mm)
    styles=getSampleStyleSheet(); small=ParagraphStyle("small",parent=styles["Normal"],fontSize=8.5,leading=10); right=ParagraphStyle("right",parent=small,alignment=TA_RIGHT)
    esc=lambda v: html.escape(str(v or ""))
    story=[Paragraph(f"<b>{esc(setting('company_name',uid))}</b><br/>{esc(setting('company_address',uid))}<br/>{esc(setting('company_phone',uid))}<br/>{esc(setting('company_email',uid))}<br/>GSTIN: {esc(setting('company_gstin',uid))}",small),Spacer(1,3*mm),Paragraph("<b>TAX INVOICE</b>",styles["Title"]),Spacer(1,3*mm)]
    info=Table([[Paragraph(f"<b>Bill To:</b> {esc(inv['customer'])}<br/>{esc(inv['address'])}<br/>{esc(inv['phone'])}",small),Paragraph(f"<b>Invoice:</b> {esc(inv['invoice_number'])}<br/><b>Date:</b> {esc(inv['invoice_date'])}<br/><b>Due:</b> {esc(inv['due_date'])}<br/><b>Status:</b> {esc(inv['payment_status'])}",right)]],colWidths=[105*mm,70*mm]); info.setStyle(TableStyle([("BOX",(0,0),(-1,-1),.5,colors.grey),("VALIGN",(0,0),(-1,-1),"TOP")]))
    story += [info,Spacer(1,5*mm)]; data=[["#","Item","HSN/SAC","Qty","Rate","Amount"]]
    for n,x in enumerate(items,1): data.append([n,esc(x["item_name"]),esc(x["hsn_sac"]),x["quantity"],f"Rs. {x['rate']:,.2f}",f"Rs. {x['amount']:,.2f}"])
    t=Table(data,colWidths=[8*mm,65*mm,25*mm,15*mm,30*mm,32*mm],repeatRows=1); t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("GRID",(0,0),(-1,-1),.4,colors.grey),("ALIGN",(0,0),(-1,-1),"RIGHT"),("ALIGN",(1,1),(1,-1),"LEFT")]))
    story += [t,Spacer(1,4*mm)]; totals=[["","Subtotal",f"Rs. {inv['subtotal']:,.2f}"],["","Discount",f"Rs. {inv['discount']:,.2f}"],["","Taxable",f"Rs. {inv['taxable_value']:,.2f}"]]
    if inv["tax_type"]=="CGST + SGST": totals += [["","CGST",f"Rs. {inv['cgst']:,.2f}"],["","SGST",f"Rs. {inv['sgst']:,.2f}"]]
    elif inv["tax_type"]=="IGST": totals += [["","IGST",f"Rs. {inv['igst']:,.2f}"]]
    totals += [["","Grand Total",f"Rs. {inv['grand_total']:,.2f}"]]; tt=Table(totals,colWidths=[105*mm,35*mm,35*mm]); tt.setStyle(TableStyle([("ALIGN",(1,0),(-1,-1),"RIGHT"),("GRID",(1,0),(-1,-1),.4,colors.grey)]))
    story += [tt,Spacer(1,5*mm),Paragraph(esc(inv["notes"] or "Thank you for your business."),small)]; doc.build(story)
    c=get_db(); c.execute("UPDATE invoices SET pdf_path=? WHERE id=? AND user_id=?",(path,id,uid)); c.commit(); c.close()
    return send_file(path,as_attachment=True,download_name=os.path.basename(path))

@app.route("/reports")
@login_required
@account_access_required
def reports():
    uid=current_user_id(); c=get_db(); rows=c.execute("SELECT invoice_date,grand_total,payment_status FROM invoices WHERE user_id=?",(uid,)).fetchall(); c.close(); data={}
    for r in rows:
        m=(r["invoice_date"] or "")[:7]; x=data.setdefault(m,{"count":0,"sales":0,"paid":0,"pending":0}); x["count"]+=1; x["sales"]+=r["grand_total"]
        if r["payment_status"]=="Paid": x["paid"]+=r["grand_total"]
        else: x["pending"]+=r["grand_total"]
    return render_template("reports.html",data=sorted(data.items(),reverse=True))

@app.route("/reports.csv")
@login_required
@account_access_required
def reports_csv():
    uid=current_user_id(); c=get_db(); rows=c.execute("SELECT invoice_date,invoice_number,grand_total,payment_status FROM invoices WHERE user_id=? ORDER BY invoice_date DESC",(uid,)).fetchall(); c.close(); out=io.StringIO(); w=csv.writer(out); w.writerow(["Date","Invoice","Grand Total","Status"])
    for r in rows: w.writerow([r["invoice_date"],r["invoice_number"],r["grand_total"],r["payment_status"]])
    return send_file(io.BytesIO(out.getvalue().encode()),as_attachment=True,download_name="invoice_report.csv",mimetype="text/csv")

@app.errorhandler(500)
def server_error(error):
    app.logger.exception("Unhandled application error")
    return "Something went wrong. Please try again or contact support.",500

if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)),debug=False)
