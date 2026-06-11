from __future__ import annotations

import os
from datetime import datetime, timedelta, date, timezone

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, current_app, send_from_directory

from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, login_user, login_required, logout_user, current_user, UserMixin
)
from sqlalchemy import event, and_, or_, text, func, MetaData
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# UTC helper that returns a naive UTC datetime (keeps storage compatible, avoids deprecation)
def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ------------------------------------------------------------------------------
# App & DB setup
# ------------------------------------------------------------------------------
app = Flask(__name__, instance_relative_config=True)

app.config.setdefault('UPLOAD_FOLDER', os.path.join(app.root_path, 'uploads'))
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ALLOWED_IMAGE_EXTS = {'.png', '.jpg', '.jpeg'}
def _allowed_image(filename):
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_IMAGE_EXTS

print('APP BUILD: daily-replies+taskread v1')
os.makedirs(app.instance_path, exist_ok=True)

# IMPORTANT: set a strong secret in production
app.secret_key = os.environ.get("SECRET_KEY", "dev-key")
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    "DATABASE_URL",
    'sqlite:///' + os.path.join(app.instance_path, 'shop.db')
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['ARCHIVE_RETENTION_DAYS'] = 30

db = SQLAlchemy(app)

with app.app_context():
    db.create_all()

login_manager = LoginManager(app)
login_manager.login_view = "login"


# ==== Role helpers & access control ====
from functools import wraps

def is_admin(u):
    return bool(getattr(u, 'role', '') and getattr(u, 'role', '').lower() in ('admin','manager'))

def is_tech(u):
    return bool(getattr(u, 'role', '').lower() == 'tech')

def is_vendor(u):
    return bool(getattr(u, 'role', '').lower() == 'vendor')

def subscription_required(fn):
    @wraps(fn)
    def _wrap(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))
        if is_admin(current_user):
            plan = (getattr(current_user, 'plan', '') or '').lower()
            lifetime = bool(getattr(current_user, 'is_lifetime', False))
            if not lifetime and (not plan or plan == 'free'):
                return redirect(url_for('billing'))
        return fn(*args, **kwargs)
    return _wrap

def admin_only(fn):
    @wraps(fn)
    def _wrap(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))
        if not is_admin(current_user):
            abort(403)
        return fn(*args, **kwargs)
    return _wrap


@app.context_processor
def inject_time():
    # Make datetime/timedelta available in templates
    return dict(datetime=datetime, timedelta=timedelta)

# Small money formatter for templates
@app.template_filter('money')
def money_filter(v):
    try:
        return f"${int(v):,}"
    except Exception:
        return str(v)

# Ensure SQLite enforces foreign keys
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_conn, conn_record):
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    except Exception:
        pass

def _table_has_column(table_name, column_name):
    res = db.session.execute(db.text(f"PRAGMA table_info({table_name})")).mappings().all()
    return any(r['name'] == column_name for r in res)

def _safe_add_column(table, col_def):
    db.session.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {col_def}"))
    db.session.commit()

# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------
class Shop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    monthly_goal = db.Column(db.Integer, default=200000)

    current_month_revenue = db.Column(db.Integer, default=0)

    tech_reset_password = db.Column(db.String(64), nullable=True)
# Roles: admin, estimator, parts, csr, tech, porter


def is_admin(user) -> bool:
    try:
        return (getattr(user, 'role', '') or '').lower() in ('admin','manager')
    except Exception:
        return False

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), nullable=True)
    password_hash = db.Column(db.String(200), nullable=False)

    role = db.Column(db.String(20), default='estimator')

    shop_id = db.Column(db.Integer, db.ForeignKey('shop.id'), nullable=False)
    shop = db.relationship('Shop', backref=db.backref('users', lazy=True))

    # legacy “last seen” stamps
    last_seen_all = db.Column(db.DateTime, default=datetime(1970,1,1))
    last_seen_incoming = db.Column(db.DateTime, default=datetime(1970,1,1))
    last_seen_outgoing = db.Column(db.DateTime, default=datetime(1970,1,1))

    # Subscription fields
    plan = db.Column(db.String(32), default="free")
    subscription_expires_at = db.Column(db.DateTime, nullable=True)
    is_lifetime = db.Column(db.Boolean, default=False)
    stripe_customer_id = db.Column(db.String(64), nullable=True)
    stripe_subscription_id = db.Column(db.String(64), nullable=True)

    def set_password(self, pw: str) -> None:
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)

# Admin ↔ Shop mapping (optional, kept simple)
class AdminShop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    shop_id = db.Column(db.Integer, db.ForeignKey('shop.id', ondelete='CASCADE'), nullable=False, index=True)



# Daily Updates models
class DailyQuestion(db.Model):
    __tablename__ = 'daily_question'

    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey('shop.id'), nullable=False, index=True)

    # creator (the manager who asked it)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)

    # the person the question is directed to
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)

    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    # ✅ disambiguate both relationships
    creator = db.relationship('User', foreign_keys=[user_id])
    target_user = db.relationship('User', foreign_keys=[target_user_id])

class DailyReply(db.Model):
    __tablename__ = 'daily_reply'
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey('daily_question.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    author_name = db.Column(db.String(80), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    question = db.relationship('DailyQuestion', backref=db.backref('replies_rel', lazy=True, cascade="all, delete-orphan"))
    user = db.relationship('User', backref=db.backref('daily_replies', lazy=True))
class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey('shop.id', ondelete='CASCADE'), nullable=False, index=True)

    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)

    request_type = db.Column(db.String(50))
    assigned_to = db.Column(db.String(120))            # username (individual)
    assigned_group = db.Column(db.String(120))         # 'estimators','parts','csr','tech_pool'
    urgent = db.Column(db.Boolean, default=False)
    comments = db.Column(db.Text)

    status = db.Column(db.String(20), default='pending')    # pending | in-progress | done
    kind = db.Column(db.String(20), default='tech')         # 'tech' for Requests page
    queue = db.Column(db.String(20), default='incoming')

    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=datetime.utcnow)
    due_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)

    # Sublet-related
    sublet_assigned = db.Column(db.Boolean, default=False)
    vendor_name = db.Column(db.String(120), nullable=True)

    # Who submitted (for Outgoing/Self)
    submitted_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), index=True)

    shop = db.relationship('Shop', backref=db.backref('tasks', lazy=True))

class TaskRoleStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    task_id = db.Column(
        db.Integer,
        db.ForeignKey('task.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )

    role = db.Column(db.String(50), nullable=False)

    completed = db.Column(db.Boolean, default=False)
    completed_by = db.Column(db.String(80))
    completed_at = db.Column(db.DateTime)

    task = db.relationship(
        'Task',
        backref=db.backref('role_statuses', cascade="all, delete-orphan")
    )

class TaskComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True)
    user_name = db.Column(db.String(80), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    task = db.relationship('Task', backref=db.backref('comments_rel', cascade="all, delete-orphan", passive_deletes=True))

# Per-user read receipts
class TaskRead(db.Model):
    __tablename__ = 'task_read'
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id', ondelete='CASCADE'), index=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), index=True, nullable=False)
    read_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    __table_args__ = (
        db.UniqueConstraint('task_id', 'user_id', name='uq_task_read_task_user'), {},
    )

# Day End per-day report
class DayEndReport(db.Model):
    __tablename__ = 'day_end_report'
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey('shop.id'), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    report_date = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String(16), default='draft')  # 'draft' | 'final'

    # Dollars/%
    actual_closed        = db.Column(db.Integer, default=0)
    tomorrow_close_goal  = db.Column(db.Integer, default=0)
    daily_gp             = db.Column(db.Integer, default=0)   # 0..100

    # Optional extras
    vehicles_completed   = db.Column(db.Integer, default=0)
    vendor_approvals     = db.Column(db.Integer, default=0)
    all_tech             = db.Column(db.Integer, default=0)
    sublets_completed    = db.Column(db.Integer, default=0)

    issues          = db.Column(db.Text)
    carryover       = db.Column(db.Text)
    tomorrow_focus  = db.Column(db.Text)
    notes           = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('shop_id', 'report_date', name='uq_day_end_shop_date'),)

# Monthly snapshot (auto-archived at month rollover)
class MonthSnapshot(db.Model):
    __tablename__ = 'month_snapshot'
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey('shop.id'), nullable=False, index=True)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)  # 1..12
    goal = db.Column(db.Integer, default=0)
    actual = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (db.UniqueConstraint('shop_id', 'year', 'month', name='uq_month_snapshot'),)

class TaskAttachment(db.Model):
    __tablename__ = 'task_attachments'
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(
        db.Integer, 
        db.ForeignKey('task.id', ondelete='CASCADE'),   # ← This prevents future NULL issues
        nullable=False
    )
    filename = db.Column(db.String(255), nullable=False)
    url_path = db.Column(db.String(400), nullable=False)
    content_type = db.Column(db.String(120))
    size = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    task = db.relationship('Task', backref=db.backref('attachments', lazy='dynamic', cascade="all, delete-orphan"))


# ------------------------------------------------------------------------------
# Helpers / constants
# ------------------------------------------------------------------------------
REQUEST_ROLE_MAP = {
    "Blueprint": ["admin", "parts", "estimator"],
    "Supplement": ["admin", "estimator"],
    "Phase Change": ["admin", "estimator"],
    "Vehicle Complete": ["csr", "estimator", "porter", "admin"],
    "Supplies": ["admin", "parts"],
    "Pull Parts": ["admin", "parts"],
    "Return Parts": ["admin", "parts"],
    "In Process Photos": ["admin", "estimator"],
}

def get_role_status(task):
    return [
        {
            "role": r.role,
            "done": r.completed,
            "by": r.completed_by
        }
        for r in task.role_statuses
    ]

def is_active_subscriber(user: User) -> bool:
    if user.is_lifetime:
        return True
    if user.subscription_expires_at is None:
        return False
    return utcnow() < user.subscription_expires_at

def _table_exists(name: str) -> bool:
    with db.engine.connect() as con:
        result = con.execute(text("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = :name
            )
        """), {"name": name})
        return result.scalar()

def _ensure_columns():
    """Lightweight idempotent migrations for SQLite tables used by the app."""
    from sqlalchemy import text
    with app.app_context():
        # use a single connection so we don't hit ResourceClosedError
        with db.engine.begin() as con:

            def table_exists(name):
                from sqlalchemy import text
                with db.engine.connect() as con:
                    result = con.execute(text("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables 
                            WHERE table_name = :name
                        )
                    """), {"name": name})
                    return result.scalar()

            def cols(table: str):
                res = con.exec_driver_sql(f"PRAGMA table_info('{table}')")
                return {row[1] for row in res.fetchall()}

            def add_col(table: str, ddl: str):
                # ddl is like: "email VARCHAR(120)" or "user_id INTEGER"
                con.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                
            # USER columns
            if table_exists('user'):
                c = cols('user')
                if 'email' not in c:
                    add_col('user', "email VARCHAR(120)")
                if 'shop_id' not in c:
                    add_col('user', "shop_id INTEGER")

            # SHOP columns
            if table_exists('shop'):
                c = cols('shop')
                if 'monthly_goal' not in c:
                    add_col('shop', "monthly_goal INTEGER DEFAULT 0")
                if 'tech_reset_password' not in c:
                    add_col('shop', "tech_reset_password VARCHAR(64)")

                if 'current_month_revenue' not in c:
                    add_col('shop', "current_month_revenue INTEGER DEFAULT 0")

            # DAILY UPDATES tables/columns
            if table_exists('daily_question'):
                c = cols('daily_question')
                
                if 'shop_id' not in c:
                    add_col('daily_question', "shop_id INTEGER")
                if 'user_id' not in c:
                    add_col('daily_question', "user_id INTEGER")
                if 'body' not in c:
                    add_col('daily_question', "body TEXT")
                if 'created_at' not in c:
                    add_col('daily_question', "created_at DATETIME")
                if 'is_active' not in c:
                    add_col('daily_question', "is_active BOOLEAN DEFAULT 1")

            if table_exists('daily_reply'):
                c = cols('daily_reply')
                if 'question_id' not in c:
                    add_col('daily_reply', "question_id INTEGER")
                if 'user_id' not in c:
                    add_col('daily_reply', "user_id INTEGER")
                if 'body' not in c:
                    add_col('daily_reply', "body TEXT")
                if 'created_at' not in c:
                    add_col('daily_reply', "created_at DATETIME")

def _weekday_business_days_left(today: date) -> int:
    count = 0
    d = today
    while d.month == today.month:
        if d.weekday() < 5:  # Mon–Fri
            count += 1
        d += timedelta(days=1)
    return count

def _month_range(dt: datetime):
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year+1, month=1)
    else:
        end = start.replace(month=start.month+1)
    return start, end

# def _incoming_filter_for_user(q, user: User):
#     base = q.outerjoin(TaskRoleStatus).filter(
#         Task.status != 'done'
#     )

#     # 🔴 ADMIN → ONLY unassigned + group tasks (NOT personal assignments)
#     if user.role == 'admin':
#         return base.filter(
#             Task.assigned_to.is_(None)
#         )

#     # 🟢 NORMAL USERS
#     return base.filter(
#         or_(
#             Task.assigned_to == user.username,  # ✅ SHOW personal tasks
#             Task.assigned_group == _role_to_group(user.role),
#             Task.assigned_to.is_(None)
#         )
#     )

# # Works
# def _incoming_filter_for_user(q, user: User):
#     base = q.outerjoin(TaskRoleStatus).filter(
#         Task.status != 'done'
#     )

#     # ADMIN sees everything (you can tweak later)
#     if user.role == 'admin':
#         return base

#     # 🔥 KEY FIX: show tasks where this role exists
#     return base.filter(
#         TaskRoleStatus.role == user.role
#     )
    
def _incoming_filter_for_user(q, user: User):
    base = q.outerjoin(TaskRoleStatus).filter(
        Task.status != 'done',
        Task.kind == 'tech'   # 🔥 ONLY TECH TASKS
    )

    if user.role == 'admin':
        return base

    return base.filter(
        TaskRoleStatus.role == user.role
    )

def _outgoing_filter_for_user(q, user: User):
    return q.filter(
        Task.submitted_by_user_id == user.id,
        Task.status != 'done'
    )

def _self_filter_for_user(q, user: User):
    return q.filter(Task.assigned_to == user.username)

def _mark_read(task_id: int, user_id: int):
    if not TaskRead.query.filter_by(task_id=task_id, user_id=user_id).first():
        db.session.add(TaskRead(task_id=task_id, user_id=user_id, read_at=utcnow()))
        db.session.commit()

@app.post('/shop/revenue')
@login_required
def update_shop_revenue():
    if current_user.role != 'admin':
        abort(403)

    shop = get_active_shop(current_user)

    val_raw = (request.form.get('revenue') or '').replace(',', '').replace('$', '').strip()

    try:
        value = max(0, int(float(val_raw)))
    except Exception:
        flash('Invalid revenue number.', 'error')
        return redirect(url_for('index'))

    shop.current_month_revenue = value
    db.session.commit()

    flash('Revenue updated.')
    return redirect(url_for('index'))

def _ensure_prev_month_snapshot(shop: Shop, today: date):
    """On/after the first of a new month, archive the previous month's goal/actual once."""
    # Determine previous month
    if today.month == 1:
        y, m = today.year - 1, 12
    else:
        y, m = today.year, today.month - 1

    # Only snapshot if previous month actually exists (i.e., not before shop creation – safe to always try)
    existing = MonthSnapshot.query.filter_by(shop_id=shop.id, year=y, month=m).first()
    if existing:
        return

    # Sum previous month's actual_closed
    from calendar import monthrange
    days_in_prev = monthrange(y, m)[1]
    m_start = date(y, m, 1)
    m_end   = date(y, m, days_in_prev)
    actual = (db.session.query(func.coalesce(func.sum(DayEndReport.actual_closed), 0))
              .filter(DayEndReport.shop_id == shop.id,
                      DayEndReport.report_date >= m_start,
                      DayEndReport.report_date <= m_end)
              .scalar()) or 0

    snap = MonthSnapshot(
        shop_id=shop.id,
        year=y, month=m,
        goal=shop.monthly_goal or 0,
        actual=actual
    )
    db.session.add(snap)

    # RESET CURRENT MONTH REVENUE
    shop.current_month_revenue = 0

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


@login_manager.unauthorized_handler
def unauthorized():
    return redirect(url_for("login"))

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

@app.before_request
def require_login_and_subscription():
    open_endpoints = {
        "login", "create_user", "register_shop", "static", "health",
        "billing", "billing_webhook", "billing_success", "billing_cancel",
        "reset_password"
    }
    if request.endpoint in open_endpoints or request.endpoint is None:
        return
    if not current_user.is_authenticated:
        return redirect(url_for("login"))
    # Only admins/managers must be active subscribers
    if is_admin(current_user) and not is_active_subscriber(current_user):
        return redirect(url_for("billing"))

# Active shop helpers (support admin switching)
def get_active_shop_id(user: User) -> int:
    if user.role == 'admin':
        sid = session.get('active_shop_id')
        return int(sid) if sid else user.shop_id
    return user.shop_id

def get_active_shop(user: User) -> Shop:
    return db.session.get(Shop, get_active_shop_id(user))


def _topbar_context():
    """Context used by the top header across all pages so the header stays consistent."""
    shop = get_active_shop(current_user)

    if current_user.role == 'admin':
        allowed_shop_ids = [
            s.shop_id for s in AdminShop.query.filter_by(admin_user_id=current_user.id)
        ]

        if not allowed_shop_ids:
            allowed_shop_ids = [current_user.shop_id]

        all_shops = Shop.query.filter(Shop.id.in_(allowed_shop_ids)).order_by(Shop.name.asc()).all()
    else:
        all_shops = []
        
    return dict(
    is_admin=(current_user.role == 'admin'),
    shop=shop,
    all_shops=all_shops,
    active_shop_id=shop.id if shop else None,
)

def _purge_completed_older_than(days: int):
    """Safe purge for collision repair workflow - deletes children first."""
    cutoff = utcnow() - timedelta(days=days)
    
    stale = Task.query.filter(
        Task.status == 'done',
        Task.completed_at.isnot(None),
        Task.completed_at < cutoff,
        Task.deleted_at.is_(None)
    ).all()

    if not stale:
        print(f"No completed tasks older than {days} days to purge.")
        return

    print(f"Purging {len(stale)} old completed tasks...")

    for t in stale:
        # Explicitly clean ALL child records BEFORE deleting the task
        TaskAttachment.query.filter_by(task_id=t.id).delete(synchronize_session=False)
        TaskComment.query.filter_by(task_id=t.id).delete(synchronize_session=False)
        TaskRead.query.filter_by(task_id=t.id).delete(synchronize_session=False)
        db.session.delete(t)

    try:
        db.session.commit()
        print(f"✅ Successfully purged {len(stale)} old tasks.")
    except Exception as e:
        db.session.rollback()
        print(f"❌ Purge error: {e}")
        # One-by-one fallback (very safe)
        for t in list(stale):
            try:
                db.session.rollback()
                TaskAttachment.query.filter_by(task_id=t.id).delete(synchronize_session=False)
                TaskComment.query.filter_by(task_id=t.id).delete(synchronize_session=False)
                TaskRead.query.filter_by(task_id=t.id).delete(synchronize_session=False)
                db.session.delete(t)
                db.session.commit()
                print(f"Purged task {t.id} safely")
            except Exception as inner:
                print(f"Could not purge task {t.id}: {inner}")
                db.session.rollback()


def _save_task_images(task_id, filelist):
    """
    Saves images to uploads/tasks/<task_id>/ and creates TaskAttachment rows.
    Returns a list of TaskAttachment.
    """
    saved = []
    if not filelist:
        return saved
    base = app.config['UPLOAD_FOLDER']
    task_dir = os.path.join(base, 'tasks', str(task_id))
    os.makedirs(task_dir, exist_ok=True)

    for f in filelist:
        if not f or not getattr(f, 'filename', ''):
            continue
        fname = secure_filename(f.filename)
        if not _allowed_image(fname):
            continue
        dest = os.path.join(task_dir, fname)
        f.save(dest)
        rel = os.path.join('tasks', str(task_id), fname).replace('\\','/')
        att = TaskAttachment(
            task_id=task_id,
            filename=fname,
            url_path=rel,
            content_type=f.mimetype or 'application/octet-stream',
            size=os.path.getsize(dest)
        )
        db.session.add(att)
        saved.append(att)
    db.session.commit()
    return saved

# SHOP columns
# if _table_exists('shop'):
#     c = cols('shop')
#     if 'tech_reset_password' not in c:
#         add_col('shop', "tech_reset_password VARCHAR(64)")
# 
# ------------------------------------------------------------------------------
# Health & Billing
# ------------------------------------------------------------------------------
@app.route('/health')
def health():
    return 'ok', 200

@app.route("/billing", methods=["GET", "POST"])
def billing():
    # Only admins/managers handle subscription
    if not is_admin(current_user):
        if is_tech(current_user):
            return redirect(url_for('tech_dashboard'))
        if is_vendor(current_user):
            return redirect(url_for('vendor_portal')) if 'vendor_portal' in globals() else redirect(url_for('index'))
        return redirect(url_for('index'))
    if request.method == "POST":
        if current_user.is_authenticated:
            current_user.plan = "pro"
            current_user.subscription_expires_at = utcnow() + timedelta(days=7)
            db.session.commit()
            return redirect(url_for("index"))
        return redirect(url_for("login"))
    return render_template("billing.html")

# ------------------------------------------------------------------------------
# Auth & Setup
# ------------------------------------------------------------------------------
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        ident = (request.form.get('username') or '').strip()
        pw = request.form.get('password') or ''
        u = None
        if ident:
            # Try username first
            u = User.query.filter_by(username=ident).first()
            # Fallback to email
            if not u:
                u = User.query.filter_by(email=ident).first()
        if u and u.check_password(pw):
            login_user(u)
            if u.role == 'admin':
                first_shop = AdminShop.query.filter_by(admin_user_id=u.id).first()

                if first_shop:
                    session['active_shop_id'] = first_shop.shop_id
                else:
                    session['active_shop_id'] = u.shop_id  # fallback
            # Role-based landing
            if is_tech(current_user):
                return redirect(url_for('tech_dashboard'))
            if is_vendor(current_user) and 'vendor_portal' in globals():
                return redirect(url_for('vendor_portal'))
            return redirect(url_for('index'))
        flash('Invalid credentials.')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    # End the Flask-Login session
    logout_user()

    # Clean app-specific session keys (don’t *have* to clear everything)
    for k in ('active_shop_id', 'shop_id'):
        session.pop(k, None)

    # If you prefer to nuke everything:
    # session.clear()

    flash('Signed out.', 'info')
    return redirect(url_for('login'))


ROLES = [
    ('admin', 'Admin'),
    ('estimator', 'Estimator'),
    ('parts', 'Parts Coordinator'),
    ('csr', 'Customer Service Representative'),
    ('tech', 'Tech'),
    ('porter', 'Porter'),
]

# @app.route('/register_shop', methods=['GET','POST'])
# @login_required
# def register_shop():
#     if request.method == 'POST':
#         name = request.form['name'].strip()
#         goal = request.form.get('goal', type=int) or 100
#         if not name:
#             flash('Shop name required.')
#             return render_template('register_shop.html')
#         s = Shop(name=name, monthly_goal=goal)
#         db.session.add(s); db.session.commit()
#         db.session.add(AdminShop(
#             admin_user_id=current_user.id,
#             shop_id=s.id
#         ))
#         db.session.commit()
#         flash('Shop registered. Create a user next.')
#         return redirect(url_for('create_user', shop_id=s.id))
#     return render_template('register_shop.html')

@app.route('/register_shop', methods=['GET','POST'])
@login_required
def register_shop():
    if current_user.role != 'admin':
        abort(403)

    if request.method == 'POST':
        name = request.form['name'].strip()
        goal = request.form.get('goal', type=int) or 100

        if not name:
            flash('Shop name required.')
            return render_template('register_shop.html')

        # 🔥 Create shop
        s = Shop(name=name, monthly_goal=goal)
        db.session.add(s)
        db.session.commit()

        # 🔥 Link admin to this shop
        db.session.add(AdminShop(
            admin_user_id=current_user.id,
            shop_id=s.id
        ))
        db.session.commit()

        # 🔥 Switch to it immediately
        session['active_shop_id'] = s.id

        flash('New shop created successfully')
        return redirect(url_for('index'))

    return render_template('register_shop.html')

@app.route('/create_user', methods=['GET','POST'])
@login_required
def create_user():

    # 🔥 ALWAYS define shops first (for both GET + POST)
    if current_user.role == 'admin':
        allowed_shop_ids = [
            s.shop_id for s in AdminShop.query.filter_by(admin_user_id=current_user.id)
        ]

        if not allowed_shop_ids:
            allowed_shop_ids = [current_user.shop_id]

        shops = Shop.query.filter(Shop.id.in_(allowed_shop_ids)).all()
    else:
        shops = []

    if request.method == 'POST':
        username = request.form['username'].strip()
        password = (request.form.get('password') or '').strip()
        role = request.form.get('role','estimator')
        shop_id = request.form.get('shop_id', type=int)

        # 🔥 generate password if missing
        if not password:
            import secrets
            password = secrets.token_urlsafe(8)

        # 🔒 VALIDATE shop_id belongs to admin
        if shop_id not in [s.id for s in shops]:
            abort(403)

        if not all([username, shop_id]):
            flash('All fields required.')
            return render_template('create_user.html', shops=shops, roles=ROLES)

        if User.query.filter_by(username=username).first():
            flash('Username already exists.')
            return render_template('create_user.html', shops=shops, roles=ROLES)

        u = User(username=username, role=role, shop_id=shop_id)
        u.set_password(password)

        db.session.add(u)
        db.session.commit()

        flash(f'User created. Temporary password: {password}')
        return redirect(url_for('login'))

    return render_template('create_user.html', shops=shops, roles=ROLES)

    
# ---- Reset Password ----
@app.route('/reset-password', methods=['GET', 'POST'], endpoint='reset_password')
def reset_password():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip()
        # TODO: send tokenized reset link (don’t disclose whether email exists)
        flash('If that email exists, a reset link has been sent.', 'info')
        return redirect(url_for('login'))
    return render_template('reset_password.html')

def _require_admin():
    if not current_user.is_authenticated or current_user.role != 'admin':
        abort(403)

# ------------------------------------------------------------------------------
# Admin: switch active shop (auto via dropdown change)
# ------------------------------------------------------------------------------
@app.post('/switch-shop')
@login_required
def switch_shop():
    if current_user.role != 'admin':
        abort(403)

    sid = request.form.get('shop_id', type=int)

    allowed = AdminShop.query.filter_by(
        admin_user_id=current_user.id,
        shop_id=sid
    ).first()

    if not allowed:
        abort(403)  # 🚨 BLOCK unauthorized access

    session['active_shop_id'] = sid
    return redirect(request.referrer or url_for('index'))
# ------------------------------------------------------------------------------
# Requests (Tasks)
# ------------------------------------------------------------------------------
GROUP_KEYS = {
    'estimators': 'Estimators',
    'parts': 'Parts Coordinators',
    'csr': 'CSRs',
    'tech_pool': 'Techs (Pool)',
    'porter': 'Porters',
}

def _role_to_group(role: str):
    mapping = {
        'estimator': 'estimators',
        'parts': 'parts',
        'csr': 'csr',
        'tech': 'tech_pool',
        'porter': 'porter'
    }
    return mapping.get(role)

REQUEST_TYPES = [
    "WreckFlow Support",
    "Call Customer",
    "General Task",
    "Order Parts",
    "Return Parts",
    "Scrub File",
    "Store Task",
    "Sublets",
    "Time Adjust",
]

@app.route('/')
@login_required
def index():
    if current_user.is_authenticated:
        if is_tech(current_user):
            return redirect(url_for('tech_dashboard'))
        if is_vendor(current_user) and 'vendor_portal' in globals():
            return redirect(url_for('vendor_portal'))

    _purge_completed_older_than(app.config['ARCHIVE_RETENTION_DAYS'])

    shop = get_active_shop(current_user)
    _ensure_prev_month_snapshot(shop, date.today())

    tab = request.args.get('tab', 'incoming')   # default incoming for non-admins

    # DO NOT filter out "done" here anymore
    # base = Task.query.filter_by(
    #     shop_id=shop.id,
    #     kind='tech',
    #     deleted_at=None
    # )
    base = Task.query.filter_by(
        shop_id=shop.id,
        deleted_at=None
    )

    # === THIS IS THE KEY PART ===
    if tab == 'all' and is_admin(current_user):
        rows_q = base.filter(Task.status != 'done')
    elif tab == 'incoming':
        rows_q = _incoming_filter_for_user(base, current_user)
    elif tab == 'outgoing':
        rows_q = base.filter(
            Task.submitted_by_user_id == current_user.id,
            Task.status != 'done'   # 🔥 THIS FIX
        )
    elif tab == 'self':
        rows_q = base.filter(
        Task.assigned_to == current_user.username,
        Task.status != 'done'
    )
    else:
        rows_q = _incoming_filter_for_user(base, current_user)

    rows = rows_q.distinct().order_by(Task.updated_at.desc()).limit(200).all()
    filtered = []
    for t in rows:
        role_row = next((r for r in t.role_statuses if r.role == current_user.role), None)

        # show only if their role is not completed
        if not role_row or not role_row.completed:
            filtered.append(t)

    rows = filtered

    for t in rows:
        t.all_roles_done = all(r.completed for r in t.role_statuses)

    # KPIs
    if is_admin(current_user):
        total_tech = base.filter(Task.status != 'done').count()
    else:
        total_tech = base.filter(
            Task.status != 'done',
            or_(
                Task.assigned_to == current_user.username,
                Task.assigned_group == _role_to_group(current_user.role)
            )
        ).count()

    start, end = _month_range(utcnow())
    done_this_month = Task.query.filter_by(shop_id=shop.id, kind='tech').filter(
        Task.status == 'done', 
        Task.updated_at >= start, 
        Task.updated_at < end,
        Task.deleted_at.is_(None)
    ).count()
  
    remaining_to_goal = max(
        (shop.monthly_goal or 0) - (shop.current_month_revenue or 0),
        0
    )

    progress_pct = 0
    if shop.monthly_goal:
        progress_pct = int((shop.current_month_revenue / shop.monthly_goal) * 100)
    
    daily_target = 0

    today = date.today()
    workdays_left = _weekday_business_days_left(today)

    if workdays_left > 0:
        daily_target = remaining_to_goal // workdays_left
        
    workdays_left = _weekday_business_days_left(date.today())

    read_ids = {
        r.task_id for r in TaskRead.query.filter_by(user_id=current_user.id).all()
    }

    shop_users = User.query.filter_by(shop_id=shop.id).order_by(User.username.asc()).all()
    if current_user.role == 'admin':
        allowed_shop_ids = [
            s.shop_id for s in AdminShop.query.filter_by(admin_user_id=current_user.id)
        ]

        # 🔥 SAFETY: if no mapping exists, fallback to their own shop
        if not allowed_shop_ids:
            allowed_shop_ids = [current_user.shop_id]
        all_shops = Shop.query.filter(Shop.id.in_(allowed_shop_ids)).order_by(Shop.name.asc()).all()
    else:
        all_shops = []
    self_tasks = base.filter(
    Task.assigned_to == current_user.username,
    Task.status != 'done'
    ).all()

    # self_count = 0
    # for t in self_tasks:
    #     role_row = next((r for r in t.role_statuses if r.role == current_user.role), None)
    #     if not role_row or not role_row.completed:
    #         self_count += 1

    unread_self = base.outerjoin(TaskRoleStatus).filter(
        Task.assigned_to == current_user.username,
        Task.status != 'done',

        or_(
            TaskRoleStatus.role != current_user.role,
            TaskRoleStatus.completed == False
        ),

        ~Task.id.in_(
            db.session.query(TaskRead.task_id)
            .filter(TaskRead.user_id == current_user.id)
        )
    ).count()

    incoming_q = _incoming_filter_for_user(base, current_user)

    #incoming_total = incoming_q.count()
    incoming_total = len(rows)

    incoming_unread = sum(
        1 for t in rows if t.id not in read_ids
    )

    outgoing_rows = _outgoing_filter_for_user(base, current_user) \
    .order_by(Task.updated_at.desc()).all()

    # Outgoing count
    outgoing_filtered = []
    for t in outgoing_rows:
        role_row = next((r for r in t.role_statuses if r.role == current_user.role), None)
        if not role_row or not role_row.completed:
            outgoing_filtered.append(t)

    outgoing_total = len(outgoing_filtered)

    outgoing_unread = sum(
        1 for t in outgoing_filtered if t.id not in read_ids
    )

    self_tasks = base.filter(
        Task.assigned_to == current_user.username,
        Task.status != 'done'
        ).all()

    self_count = 0
    for t in self_tasks:
        role_row = next((r for r in t.role_statuses if r.role == current_user.role), None)
        if not role_row or not role_row.completed:
            self_count += 1
            
    return render_template(
        'index.html',
        current_page='requests',
        tab=tab,
        tasks=rows,
        get_role_status=get_role_status,
        shop=shop,
        kpi=dict(
            all_tech=total_tech,
            remaining=remaining_to_goal,
            workdays=workdays_left,

            incoming_total=incoming_total,
            incoming_unread=incoming_unread,

            outgoing_total=outgoing_total,
            outgoing_unread=0,  # fine for now

            self_total=self_count,
            self_unread=unread_self
        ),
        read_ids=read_ids,
        roles=ROLES,
        group_keys=GROUP_KEYS,
        request_types=REQUEST_TYPES if 'REQUEST_TYPES' in globals() else [],
        shop_users=shop_users,
        is_admin=is_admin(current_user),
        all_shops=all_shops,
        active_shop_id=shop.id
    )

# Create Request
@app.route('/tasks/create', methods=['POST'])
@login_required
def create_task():
    shop_id = get_active_shop_id(current_user)
    request_type = (request.form.get('request_type') or '').strip()
    description = (request.form.get('description') or '').strip() or None
    urgent = bool(request.form.get('urgent'))

    assigned_val = request.form.get('assigned')  # "user:alice" or "group:estimators"
    assigned_to = None
    assigned_group = None
    if assigned_val:
        if assigned_val.startswith('group:'):
            candidate = assigned_val.split(':', 1)[1]
            assigned_group = candidate if candidate in GROUP_KEYS else None
        elif assigned_val.startswith('user:'):
            assigned_to = assigned_val.split(':', 1)[1]

    if not request_type:
        flash("Request type is required.")
        return redirect(url_for('index'))
    if not assigned_to and not assigned_group:
        flash("Please choose an assignee (user or group).")
        return redirect(url_for('index'))

    t = Task(
        shop_id=shop_id,
        title=request_type,
        description=description,
        urgent=urgent,
        assigned_to=assigned_to,
        assigned_group=assigned_group,
        submitted_by_user_id=current_user.id,
        kind='request',
        request_type=request_type,
    )
    db.session.add(t)
    db.session.commit()

    roles = REQUEST_ROLE_MAP.get(t.request_type, [])

    # 🔥 fallback: always include assigned user's role
    if assigned_to:
        user = User.query.filter_by(username=assigned_to).first()
        if user and user.role not in roles:
            roles.append(user.role)

    # assignment still controls who sees it FIRST
    t.assigned_group = assigned_group
    t.assigned_to = assigned_to


    # create role rows
    for role in roles:
        db.session.add(TaskRoleStatus(
            task_id=t.id,
            role=role
        ))

    db.session.commit()

    # Self-assigned should start as UNREAD for the creator (per your spec) -> do NOT mark read here.
    if not (assigned_to and assigned_to == current_user.username):
        _mark_read(t.id, current_user.id)

    flash('Request created.')
    return redirect(url_for('index'))

# Mark read (called when opening the details modal)
@app.route('/tasks/<int:task_id>/read', methods=['POST'])
@login_required
def mark_task_read(task_id: int):
    task = Task.query.get_or_404(task_id)
    if get_active_shop_id(current_user) != task.shop_id:
        return ("Forbidden", 403)
    _mark_read(task.id, current_user.id)
    return ("", 204)

@app.route('/tasks/<int:task_id>/status', methods=['POST'])
@login_required
def change_status(task_id: int):
    task = Task.query.get_or_404(task_id)

    if get_active_shop_id(current_user) != task.shop_id:
        return ("Forbidden", 403)

    new_status = request.form.get('status')
    if new_status not in ('pending', 'in-progress', 'done'):
        flash('Invalid status.')
        return redirect(url_for('index'))

    # -------------------------------
    # NON-DONE STATES (normal updates)
    # -------------------------------
    if new_status != 'done':
        task.status = new_status
        task.completed_at = None
        db.session.commit()
        return redirect(url_for('index', tab=request.args.get('tab','incoming')))

    # -------------------------------
    # DONE CLICKED
    # -------------------------------
    if new_status == 'done':

        # 🟢 NON-ADMIN → mark ONLY their role
        if current_user.role != 'admin':
            role_row = TaskRoleStatus.query.filter_by(
                task_id=task.id,
                role=current_user.role
            ).first()

            # 🔥 fallback if missing
            if not role_row:
                role_row = TaskRoleStatus(
                    task_id=task.id,
                    role=current_user.role
                )
                db.session.add(role_row)

            if not role_row.completed:
                role_row.completed = True
                role_row.completed_by = current_user.username
                role_row.completed_at = utcnow()

            db.session.commit()
            return redirect(url_for('index', tab=request.args.get('tab','incoming')))

        # 🔵 ADMIN → only allowed if all others finished
        all_non_admin_done = all(
            r.completed for r in task.role_statuses if r.role != 'admin'
        )
        
        # 🔵 ADMIN can ALWAYS close
        if current_user.role == 'admin':

            # ensure admin role is marked
            admin_row = TaskRoleStatus.query.filter_by(
                task_id=task.id,
                role='admin'
            ).first()

            if not admin_row:
                admin_row = TaskRoleStatus(task_id=task.id, role='admin')
                db.session.add(admin_row)

            if not admin_row.completed:
                admin_row.completed = True
                admin_row.completed_by = current_user.username
                admin_row.completed_at = utcnow()

            # close task regardless of other roles
            task.status = "done"
            task.completed_at = utcnow()

            db.session.commit()
        
        return redirect(url_for('index', tab=request.args.get('tab','incoming')))
        
@app.route('/tasks/<int:task_id>/delete', methods=['POST'])
@login_required
def delete_task(task_id: int):
    task = Task.query.get_or_404(task_id)
    if get_active_shop_id(current_user) != task.shop_id:
        return ("Forbidden", 403)
    if current_user.role != 'admin':
        flash('Only admins can delete tasks.')
        return redirect(url_for('index'))

    # Soft delete: move to recycle bin
    task.deleted_at = utcnow()
    db.session.commit()
    flash('Task moved to recycle bin.')
    return redirect(request.referrer or url_for('index'))

# --- Comments API ---
@app.get('/tasks/<int:task_id>/comments')
@login_required
def get_comments(task_id):
    t = Task.query.filter_by(id=task_id, shop_id=get_active_shop_id(current_user)).first_or_404()
    items = (TaskComment.query
             .filter_by(task_id=t.id)
             .order_by(TaskComment.created_at.asc())
             .all())
    return jsonify([
        {
            'id': c.id,
            'user': c.user_name,
            'created_at': c.created_at.isoformat() + 'Z',
            'body': c.body
        } for c in items
    ])

@app.post('/tasks/<int:task_id>/comments')
@login_required
def add_comment(task_id):
    t = Task.query.filter_by(id=task_id, shop_id=get_active_shop_id(current_user)).first_or_404()
    body = (request.form.get('body') or '').strip()
    if not body:
        return jsonify({'ok': False, 'error': 'Empty comment'}), 400

    # prevent accidental duplicates within 1 second
    last = TaskComment.query.filter_by(
        task_id=t.id,
        user_id=current_user.id,
        body=body
    ).order_by(TaskComment.created_at.desc()).first()

    if last and (utcnow() - last.created_at).total_seconds() < 1:
        return jsonify({'ok': True})  # ignore duplicate

    c = TaskComment(task_id=t.id, user_id=current_user.id, user_name=current_user.username, body=body)
    db.session.add(c)
    db.session.commit()
    return jsonify({'ok': True, 'comment': {
        'id': c.id,
        'user': c.user_name,
        'created_at': c.created_at.isoformat() + 'Z',
        'body': c.body
    }})

@app.get('/uploads/<path:relpath>')
def serve_upload(relpath):
    # relpath like 'tasks/123/IMG_0001.jpg'
    base = app.config['UPLOAD_FOLDER']
    directory, filename = os.path.split(relpath)
    return send_from_directory(os.path.join(base, directory), filename, as_attachment=False)

@app.get('/api/all-photos')
@login_required
def all_photos():
    attachments = TaskAttachment.query.order_by(TaskAttachment.created_at.desc()).all()
    photos = []
    for a in attachments:
        photos.append({
            'url': url_for('serve_upload', relpath=a.url_path),
            'filename': a.filename,
            'task_id': a.task_id,
            'created_at': a.created_at.isoformat() if a.created_at else None
        })
    return jsonify(photos)

# ------------------------------------------------------------------------------
# Admin placeholders (other pages)
# ------------------------------------------------------------------------------

@app.route('/admin/daily-updates')
@login_required
@subscription_required
@admin_only
def admin_daily_updates():
    if not is_admin(current_user):
        return redirect(url_for('index'))

    shop = get_active_shop(current_user)

    # Preload all active questions and group by target
    all_qs = (DailyQuestion.query
              .filter_by(shop_id=shop.id, is_active=True)
              .order_by(DailyQuestion.created_at.desc())
              .all())

    by_user = {}
    for q in all_qs:
        by_user.setdefault(q.target_user_id, []).append(q)

    user_ids = list(by_user.keys())
    users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    panels = [{
        'user_id': uid,
        'username': (users.get(uid).username if users.get(uid) else f'User {uid}'),
        'count': len(by_user.get(uid, [])),
        'questions': by_user.get(uid, []),
    } for uid in sorted(by_user.keys(), key=lambda x: (users.get(x).username.lower() if users.get(x) else ''))]

    add_users = User.query.filter_by(shop_id=shop.id).order_by(User.username.asc()).all()

    return render_template(
        'admin_daily_updates.html',
        current_page='admin_daily',
        panels=panels,
        add_users=add_users,
        **_topbar_context()
    )
# ---------------- Day End (Admin) COMING SOON----------------
@app.route('/admin/day-end', methods=['GET', 'POST'], endpoint='admin_day_end')
@login_required
@subscription_required
@admin_only
def admin_day_end():
    return render_template(
        'admin_day_end.html',
        coming_soon=True,
        **_topbar_context()
    )


# @app.route('/admin/day-end', methods=['GET', 'POST'], endpoint='admin_day_end')
# @login_required
# @subscription_required
# @admin_only
# def admin_day_end():
#     if not current_user.is_authenticated or current_user.role != 'admin':
#         abort(403)

#     shop = get_active_shop(current_user)
#     if not shop:
#         flash("No active shop selected.", "error")
#         return redirect(url_for('index'))

#     # Ensure last month's snapshot exists (if new month)
#     _ensure_prev_month_snapshot(shop, date.today())

#     # Resolve date for the editor
#     date_str = request.values.get('date')
#     try:
#         rep_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
#     except Exception:
#         rep_date = date.today()

#     report = DayEndReport.query.filter_by(shop_id=shop.id, report_date=rep_date).first()

#     if request.method == 'POST':
#         action = request.form.get('action', 'save')
#         if not report:
#             report = DayEndReport(shop_id=shop.id, author_id=current_user.id, report_date=rep_date)
#             db.session.add(report)

#         def as_int(name):
#             v = (request.form.get(name) or '').replace(',', '').replace('$', '').strip()
#             try:
#                 return int(float(v)) if v else 0
#             except Exception:
#                 return 0

#         report.status               = 'final' if action == 'finalize' else 'draft'
#         report.actual_closed        = as_int('actual_closed')
#         report.tomorrow_close_goal  = as_int('tomorrow_close_goal')
#         report.daily_gp             = max(0, min(100, as_int('daily_gp')))

#         report.issues         = request.form.get('issues') or ''
#         report.carryover      = request.form.get('carryover') or ''
#         report.tomorrow_focus = request.form.get('tomorrow_focus') or ''
#         report.notes          = request.form.get('notes') or ''

#         db.session.commit()
#         flash('Day End saved.' if action != 'finalize' else 'Day End saved & finalized.', 'success')
#         return redirect(url_for('admin_day_end', date=rep_date.isoformat()))

#     # ======= Right-hand metrics / gauge =======
#     mo_goal = (shop.monthly_goal or 0)

#     from calendar import monthrange
#     y, m = rep_date.year, rep_date.month
#     days_in_month = monthrange(y, m)[1]

#     def is_workday(d: date) -> bool:
#         return d.weekday() < 5  # Mon..Fri

#     month_days = [date(y, m, d) for d in range(1, days_in_month + 1)]
#     workdays    = sum(1 for d in month_days if is_workday(d))
#     elapsed_wd  = sum(1 for d in month_days if d <= rep_date and is_workday(d))
#     days_left   = max(0, workdays - elapsed_wd)

#     daily_goal  = int(round(mo_goal / workdays)) if workdays else 0
#     target_to_date = daily_goal * elapsed_wd

#     m_start = date(y, m, 1)
#     m_end   = date(y, m, days_in_month)

#     # --- Build monthly series for chart (cumulative actual vs workday-based goal) ---
#     # Gather each day's actual_closed for the month
#     reports = (DayEndReport.query
#             .filter(DayEndReport.shop_id == shop.id,
#                     DayEndReport.report_date >= m_start,
#                     DayEndReport.report_date <= m_end)
#             .all())
#     by_date = {r.report_date: (r.actual_closed or 0) for r in reports}

#     labels = []
#     daily_actuals = []
#     from calendar import monthrange
#     days_in_month = monthrange(y, m)[1]
#     for d in range(1, days_in_month + 1):
#         dt = date(y, m, d)
#         labels.append(str(d))
#         daily_actuals.append(by_date.get(dt, 0))

#     # Cumulative actuals
#     cum_actuals = []
#     running = 0
#     for v in daily_actuals:
#         running += int(v or 0)
#         cum_actuals.append(running)

#     # Linear goal based on WORKDAYS only (flat on weekends)
#     def is_workday(d: date) -> bool:
#         return d.weekday() < 5  # Mon..Fri

#     goal_cum = []
#     if workdays > 0:
#         wd_so_far = 0
#         for d in range(1, days_in_month + 1):
#             if is_workday(date(y, m, d)):
#                 wd_so_far += 1
#             goal_cum.append(int(round((mo_goal or 0) * (wd_so_far / workdays))))
#     else:
#         goal_cum = [0] * days_in_month


#     mtd_actual = (db.session.query(func.coalesce(func.sum(DayEndReport.actual_closed), 0))
#                   .filter(DayEndReport.shop_id == shop.id,
#                           DayEndReport.report_date >= m_start,
#                           DayEndReport.report_date <= m_end)
#                   .scalar()) or 0

#     remaining_to_close = max(0, mo_goal - mtd_actual)
#     monthly_variance   = mtd_actual - target_to_date
#     todays_goal        = daily_goal
#     todays_variance    = (report.actual_closed if report else 0) - todays_goal

#     days_behind = 0
#     if daily_goal > 0 and mtd_actual < target_to_date:
#         deficit = target_to_date - mtd_actual
#         days_behind = (deficit + daily_goal - 1) // daily_goal  # ceil

#     missed_days = (db.session.query(func.count(DayEndReport.id))
#                    .filter(DayEndReport.shop_id == shop.id,
#                            DayEndReport.report_date >= m_start,
#                            DayEndReport.report_date <= rep_date,
#                            DayEndReport.actual_closed < daily_goal)
#                    .scalar()) or 0

#     pct = 0 if mo_goal <= 0 else max(0, min(100, int(round(mtd_actual * 100 / mo_goal))))

#     all_shops = Shop.query.order_by(Shop.name).all()
#     active_shop_id = shop.id

#     return render_template(
#         'admin_day_end.html',
#         report=report,
#         date_iso=rep_date.isoformat(),
#         mo_goal=mo_goal,
#         mtd_actual=mtd_actual,
#         month_label=rep_date.strftime('%b %Y'),
#         workdays=workdays,
#         days_left=days_left,
#         remaining_to_close=remaining_to_close,
#         days_behind=days_behind,
#         missed_days=missed_days,
#         gauge_pct=pct,
#         todays_goal=todays_goal,
#         todays_variance=todays_variance,
#         monthly_variance=monthly_variance,
#         is_admin=True,
#         all_shops=all_shops,
#         active_shop_id=active_shop_id,
#         shop=shop,
#         current_page='admin',
#         chart_labels=labels,
#         chart_actual_cum=cum_actuals,
#         chart_goal_cum=goal_cum,

#     )

@app.route('/admin/reports')
@login_required
@subscription_required
@admin_only
def admin_reports():
    return render_template('admin_reports.html',
                           current_page='admin_reports',
                           **_topbar_context())  # supplies shop, all_shops, active_shop_id, is_admin


@app.route('/admin/vendor-approvals', methods=['GET', 'POST'])
@login_required
@subscription_required
@admin_only
def admin_vendor_approvals():
    shop = get_active_shop(current_user)

    if request.method == 'POST':
        # Create a vendor-approval record using the existing Task model
        name         = (request.form.get('name') or '').strip()
        address      = (request.form.get('address') or '').strip()
        phone        = (request.form.get('phone') or '').strip()
        service_type = (request.form.get('service_type') or '').strip()
        status       = (request.form.get('status') or 'pending').strip().lower()

        if name:
            t = Task(
                shop_id=shop.id,
                title=f"Vendor: {name}",
                description="\n".join(x for x in [
                    f"Address: {address}" if address else "",
                    f"Phone: {phone}" if phone else "",
                    f"Service Type: {service_type}" if service_type else "",
                ] if x),
                kind='vendor',
                request_type='vendor',
                vendor_name=name,
                status=status if status in ('pending','approved','denied') else 'pending',
                submitted_by_user_id=current_user.id,
            )
            db.session.add(t); db.session.commit()
            flash('Vendor approval created.', 'success')
        else:
            flash('Vendor name is required.', 'error')

        return redirect(url_for('admin_vendor_approvals'))

    # GET: show recent/pending approvals (simple list)
    approvals = (Task.query
                .filter_by(shop_id=shop.id, kind='vendor', status='pending')
                .order_by(Task.created_at.desc())
                .all())


    return render_template('admin_vendor_approvals.html',
                           approvals=approvals,
                           current_page='admin_vendor',
                           **_topbar_context())

@app.route('/admin/vendor/<int:task_id>/decide', methods=['POST'])
@login_required
@subscription_required
@admin_only
def admin_vendor_decide(task_id):
    shop_id = get_active_shop_id(current_user)
    t = Task.query.filter_by(id=task_id, shop_id=shop_id, kind='vendor').first_or_404()
    decision = (request.form.get('decision') or '').strip().lower()
    if decision not in ('approved', 'denied'):
        return jsonify({'ok': False, 'error': 'invalid-decision'}), 400

    if decision == 'approved':
        t.status = 'approved'
        db.session.commit()
        return jsonify({'ok': True, 'id': t.id, 'status': 'approved'})
    else:
        # Denied: remove permanently so it never reappears
        delete_task_cascade(t.id)
        db.session.commit()
        return jsonify({'ok': True, 'id': task_id, 'status': 'deleted'})

# --- Vendor helpers (parse/build description) ---
def _parse_vendor_desc(desc: str) -> dict:
    out = {"address": "", "phone": "", "service_type": ""}
    if not desc:
        return out
    for line in (desc or "").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = (k or "").strip().lower()
        v = (v or "").strip()
        if k == "address":
            out["address"] = v
        elif k == "phone":
            out["phone"] = v
        elif k.startswith("service type"):
            out["service_type"] = v
    return out

def _build_vendor_desc(address: str, phone: str, service_type: str) -> str:
    lines = []
    if address: lines.append(f"Address: {address}")
    if phone: lines.append(f"Phone: {phone}")
    if service_type: lines.append(f"Service Type: {service_type}")
    return "\n".join(lines)

@app.route('/admin/vendor/<int:task_id>.json', methods=['GET'])
@login_required
@subscription_required
@admin_only
def admin_vendor_get(task_id):
    shop_id = get_active_shop_id(current_user)
    t = Task.query.filter_by(id=task_id, shop_id=shop_id, kind='vendor', status='approved').first_or_404()
    parsed = _parse_vendor_desc(t.description or '')
    return jsonify({
        "id": t.id,
        "title": t.title,
        "name": (t.vendor_name or (t.title or '').replace('Vendor:', '').strip()),
        "address": parsed.get("address", ""),
        "phone": parsed.get("phone", ""),
        "service_type": parsed.get("service_type", ""),
        "status": t.status,
        "created_at": t.created_at.isoformat() if t.created_at else None
    })

@app.route('/admin/vendor/<int:task_id>/update', methods=['POST'])
@login_required
@subscription_required
@admin_only
def admin_vendor_update(task_id):
    shop_id = get_active_shop_id(current_user)
    t = Task.query.filter_by(id=task_id, shop_id=shop_id, kind='vendor', status='approved').first_or_404()

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip()
    address = (data.get('address') or '').strip()
    phone = (data.get('phone') or '').strip()
    service_type = (data.get('service_type') or '').strip()

    if not name or not service_type:
        return jsonify({"ok": False, "error": "name and service_type are required"}), 400
    if service_type not in SERVICE_TYPES:
        return jsonify({"ok": False, "error": "invalid service_type"}), 400

    t.title = f"Vendor: {name}"
    t.vendor_name = name
    t.description = _build_vendor_desc(address, phone, service_type)
    # keep status 'approved'
    db.session.commit()

    return jsonify({"ok": True, "id": t.id, "title": t.title})

@app.route('/admin/vendor/<int:task_id>/remove', methods=['POST'])
@login_required
@subscription_required
@admin_only
def admin_vendor_remove(task_id):
    shop_id = get_active_shop_id(current_user)
    t = Task.query.filter_by(id=task_id, shop_id=shop_id, kind='vendor', status='approved').first_or_404()

    # FK-safe manual cascade (SQLite)
    TaskAttachment.query.filter_by(task_id=t.id).delete(synchronize_session=False)
    TaskRead.query.filter_by(task_id=t.id).delete(synchronize_session=False)
    TaskComment.query.filter_by(task_id=t.id).delete(synchronize_session=False)
    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True, "id": task_id, "status": "deleted"})


def delete_task_cascade(task_id):
    metadata = MetaData()
    metadata.reflect(bind=db.engine)

    # find all tables that FK to task.id
    fk_targets = []
    for t in metadata.tables.values():
        for c in t.columns:
            for fk in c.foreign_keys:
                if fk.column.table.name.lower() == 'task' and fk.column.name.lower() == 'id':
                    fk_targets.append((t, c))

    # delete children first
    for table, col in fk_targets:
        db.session.execute(table.delete().where(col == task_id))

    # delete the task last
    t = Task.query.get(task_id)
    if t:
        db.session.delete(t)

    
SERVICE_TYPES = ['Glass', 'Striping', 'Sensor', 'Alignment', 'Misc']

import re as _re_vendor
def _extract_service_type(desc: str) -> str | None:
    if not desc:
        return None
    m = _re_vendor.search(r'(?im)^\s*service\s*type\s*:\s*(.+?)\s*$', desc or '')
    if not m:
        return None
    val = (m.group(1) or '').strip()
    for opt in SERVICE_TYPES:
        if val.lower() == opt.lower():
            return opt
    return None

@app.route('/admin/vendors', endpoint='admin_vendors')
@login_required
@subscription_required
@admin_only
def admin_vendors():
    shop = get_active_shop(current_user)
    q = Task.query.filter_by(shop_id=shop.id, kind='vendor', status='approved')
    rows = q.order_by(Task.created_at.desc()).all()

    buckets = {svc: [] for svc in SERVICE_TYPES}
    for t in rows:
        svc = _extract_service_type(t.description or '') or 'Misc'
        buckets.setdefault(svc, []).append(t)

    return render_template(
        'admin_vendors.html',
        current_page='admin_vendors',
        shop=shop,
        is_admin=True,
        all_shops=Shop.query.order_by(Shop.name.asc()).all(),
        active_shop_id=shop.id,
        buckets=buckets,
        SERVICE_TYPES=SERVICE_TYPES
    )



@app.route('/admin/team')
@login_required
@subscription_required
@admin_only
def admin_team():
    if not is_admin(current_user):
        return redirect(url_for('index'))
    shop = get_active_shop(current_user)
    if not getattr(shop, 'tech_reset_password', None):
        import secrets
        shop.tech_reset_password = secrets.token_urlsafe(8)
        db.session.commit()

    ROLE_CHOICES = ROLES = [
        ('admin', 'Admin'),
        ('estimator', 'Estimator'),
        ('parts', 'Parts'),
        ('csr', 'CSR'),
        ('tech', 'Tech'),
        ('porter', 'Porter'),
    ]
    #ROLE_CHOICES = [('manager','Manager'), ('csr','CSR'), ('porter','Porter'), ('tech','Tech')]
    ROLE_LABELS = {k: v for k, v in ROLE_CHOICES}
    users = User.query.filter_by(shop_id=shop.id).order_by(User.username.asc()).all()
    return render_template('admin_team.html',
        current_page='admin_team',
        users=users,
        roles=ROLE_CHOICES,
        role_labels=ROLE_LABELS,
        tech_reset_password=shop.tech_reset_password,
        **_topbar_context(),
    )

@app.post('/admin/team/add')
@login_required
@subscription_required
@admin_only
def admin_team_add_user():
    if not is_admin(current_user):
        abort(403)
    shop = get_active_shop(current_user)
    username = request.form.get('username','').strip()
    email = request.form.get('email','').strip().lower()
    role = (request.form.get('role') or 'csr').lower()

    shop = get_active_shop(current_user)
    temp_password = shop.tech_reset_password
        
    # prevent duplicate usernames
    if User.query.filter_by(username=username).first():
        flash('Username already exists.', 'error')
        return redirect(url_for('admin_team'))
        
    # AUTO-FIX duplicate usernames
    original_username = username

    base = username
    i = 1
    while User.query.filter_by(username=username).first():
        username = f"{base}{i}"
        i += 1

    # show what happened
    if username != original_username:
        flash(f'Username "{original_username}" taken → created "{username}" instead.', 'info')
    if username != base:
        flash(f'Username taken. Created as "{username}" instead.', 'info')

    u = User(username=username, email=email, role=role, shop_id=shop.id, )
    if hasattr(u, 'set_password'): u.set_password(temp_password)
    else:
        from werkzeug.security import generate_password_hash
        u.password_hash = generate_password_hash(temp_password)
    db.session.add(u); db.session.commit()
    flash(f'User created. Temporary password: {temp_password}', 'info')
    return redirect(url_for('admin_team'))

@app.post('/admin/team/<int:user_id>/update')
@login_required
@subscription_required
@admin_only
def admin_team_update_user(user_id):
    if not is_admin(current_user):
        abort(403)
    shop = get_active_shop(current_user)
    u = User.query.filter_by(id=user_id, shop_id=shop.id).first_or_404()
    u.username = request.form.get('username', u.username).strip()
    email_val = (request.form.get('email') or '').strip().lower()
    try:
        u.email = email_val
    except Exception:
        setattr(u, 'email', email_val)
    u.role = (request.form.get('role') or u.role).lower()
    db.session.commit()
    flash('User updated', 'success')
    return redirect(url_for('admin_team'))

@app.post('/admin/team/<int:user_id>/reset-password')
@login_required
@subscription_required
@admin_only
def admin_team_reset_password(user_id):
    if not is_admin(current_user):
        abort(403)
    shop = get_active_shop(current_user)
    u = User.query.filter_by(id=user_id, shop_id=shop.id).first_or_404()
    import secrets
    shop = Shop.query.get(get_active_shop_id(current_user))

    new_password = shop.tech_reset_password or secrets.token_urlsafe(8)
    if hasattr(u, 'set_password'): u.set_password(new_password)
    else:
        from werkzeug.security import generate_password_hash
        u.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({'ok': True, 'new_password': new_password})

@app.post('/admin/set-shop-password')
@login_required
def set_shop_password():
    if current_user.role != 'admin':
        return ("Forbidden", 403)

    pwd = (request.form.get('shop_password') or '').strip()

    if not pwd or len(pwd) < 4:
        flash("Password must be at least 4 characters", "error")
        return redirect(url_for('admin_team'))

    shop = Shop.query.get(get_active_shop_id(current_user))
    # Store password
    from werkzeug.security import generate_password_hash
    shop.tech_reset_password = pwd

    db.session.commit()

    flash("Default password updated", "success")
    return redirect(url_for('admin_team'))

@app.post('/admin/team/rotate-tech-default')
@login_required
def rotate_shop_default_pw():
    if not is_admin(current_user):
        abort(403)
    shop = get_active_shop(current_user)
    import secrets
    shop.tech_reset_password = secrets.token_urlsafe(8)
    db.session.commit()
    flash('Default technician password rotated.', 'info')
    return redirect(url_for('admin_team'))
# ------------------------------------------------------------------------------
# Archive (Requests & Sublets)

@app.post('/tasks/<int:task_id>/purge')
@login_required
def purge_task(task_id: int):
    _require_admin()
    task = Task.query.get_or_404(task_id)
    if get_active_shop_id(current_user) != task.shop_id:
        return ("Forbidden", 403)
    # Only purge items that are in recycle bin (soft-deleted)
    if task.deleted_at is None:
        flash('Task is not in recycle bin.')
        return redirect(request.referrer or url_for('index'))
    # delete dependent rows explicitly; relationships also have cascade, but be explicit
    TaskComment.query.filter_by(task_id=task.id).delete(synchronize_session=False)
    TaskRead.query.filter_by(task_id=task.id).delete(synchronize_session=False)
    db.session.delete(task)
    db.session.commit()
    flash('Task permanently deleted.')
    return redirect(request.referrer or url_for('recycle_bin'))

@app.post('/tasks/<int:task_id>/restore')
@login_required
def restore_task(task_id: int):
    task = Task.query.get_or_404(task_id)
    if get_active_shop_id(current_user) != task.shop_id:
        return ("Forbidden", 403)
    task.deleted_at = None
    TaskRead.query.filter_by(task_id=task.id).delete(synchronize_session=False)
    db.session.commit()
    flash('Task restored from recycle bin.')
    return redirect(request.referrer or url_for('recycle_bin'))



@app.route('/recycle-bin', endpoint='recycle_bin')
@login_required
def recycle():
    shop = get_active_shop(current_user)
    base = Task.query.filter_by(shop_id=shop.id).filter(Task.deleted_at.isnot(None))
    tasks = base.order_by(Task.deleted_at.desc()).limit(500).all()

    # counts for deleted items in recycle bin
    request_count = base.filter(or_(Task.kind=='tech', Task.request_type=='tech')).count()
    sublets_count = base.filter(or_(Task.kind=='sublet', Task.request_type=='sublet')).count()

    return render_template(
        'recycle_bin.html',
        current_page='recycle',
        tasks=tasks,
        request_count=request_count,
        sublets_count=sublets_count,
        **_topbar_context()
    )

@app.post('/tasks/<int:task_id>/reopen')
@login_required
def reopen_task(task_id: int):
    task = Task.query.get_or_404(task_id)

    if get_active_shop_id(current_user) != task.shop_id:
        return ("Forbidden", 403)

    # reopen task
    task.status = 'pending'
    task.completed_at = None
    task.deleted_at = None

    # 🔥 RESET ADMIN ROLE ONLY
    admin_row = TaskRoleStatus.query.filter_by(
        task_id=task.id,
        role='admin'
    ).first()

    if admin_row:
        admin_row.completed = False
        admin_row.completed_by = None
        admin_row.completed_at = None

    # 🔥 DO NOT TOUCH OTHER ROLES

    # reset read state
    TaskRead.query.filter_by(task_id=task.id).delete(synchronize_session=False)

    db.session.commit()
    flash('Task reopened.')
    return redirect(url_for('index', tab='incoming'))


@app.route('/archive')
@login_required
def archive():

    _purge_completed_older_than(app.config['ARCHIVE_RETENTION_DAYS'])

    shop = get_active_shop(current_user)

    # accept either ?kind= or ?tab= for the section: 'requests' | 'sublets'
    kind = (request.args.get('kind') or request.args.get('tab') or 'requests').lower()
    q = (request.args.get('q') or '').strip()
    assignee = (request.args.get('assignee') or '').strip()
    sort = (request.args.get('sort') or '').strip()

    cutoff = utcnow() - timedelta(days=app.config['ARCHIVE_RETENTION_DAYS'])

    base_done = Task.query.filter_by(shop_id=shop.id, status='done').filter(
        Task.completed_at.isnot(None),
        Task.completed_at >= cutoff,
        Task.deleted_at.is_(None)
    )

    # badge counts
    requests_count = base_done.filter(or_(Task.kind == 'tech', Task.request_type == 'tech')).count()
    sublets_count  = base_done.filter(or_(Task.kind == 'sublet', Task.request_type == 'sublet')).count()

    # scope for this page (used for both listing and option-building)
    if kind == 'sublets':
        scoped = base_done.filter(or_(Task.kind == 'sublet', Task.request_type == 'sublet'))
    else:
        scoped = base_done.filter(or_(Task.kind == 'tech', Task.request_type == 'tech'))

    # ---- Build option lists from the scoped (kind + 30 days) data ----
    # Users that actually appear
    usernames = [
        r[0] for r in db.session.query(Task.assigned_to)
                 .filter(scoped.whereclause)  # reuse filters
                 .filter(Task.assigned_to.isnot(None))
                 .distinct().all()
        if r[0]
    ]
    if usernames:
        shop_users = (User.query
                      .filter(User.shop_id == shop.id, User.username.in_(usernames))
                      .order_by(User.username.asc())
                      .all())
    else:
        shop_users = []

    # Groups that actually appear
    present_groups = [
        g[0] for g in db.session.query(Task.assigned_group)
                .filter(scoped.whereclause)
                .filter(Task.assigned_group.isnot(None))
                .distinct().all()
        if g[0]
    ]
    available_groups = {k: GROUP_KEYS[k] for k in present_groups if k in GROUP_KEYS}

    # ---- Listing: apply search + assignee filter + sort ----
    base = scoped
    if q:
        like = f"%{q}%"
        if kind == 'sublets':
            base = base.filter(or_(
                Task.vendor_name.ilike(like),
                Task.description.ilike(like),
                Task.title.ilike(like),
                Task.assigned_to.ilike(like),
            ))
        else:
            base = base.filter(or_(
                Task.description.ilike(like),
                Task.title.ilike(like),
                Task.assigned_to.ilike(like),
            ))

    if assignee.startswith('user:'):
        username = assignee.split(':', 1)[1]
        base = base.filter(Task.assigned_to == username)
    elif assignee.startswith('group:'):
        group_key = assignee.split(':', 1)[1]
        base = base.filter(Task.assigned_group == group_key)

    if kind == 'sublets':
        if sort == 'vendor':
            base = base.order_by(Task.vendor_name.asc().nulls_last(), Task.completed_at.desc())
        elif sort == '-vendor':
            base = base.order_by(Task.vendor_name.desc().nulls_last(), Task.completed_at.desc())
        elif sort == 'completed':
            base = base.order_by(Task.completed_at.asc())
        else:
            base = base.order_by(Task.completed_at.desc())
    else:
        if sort == 'assignee':
            base = base.order_by(Task.assigned_to.asc().nulls_last(), Task.completed_at.desc())
        elif sort == '-assignee':
            base = base.order_by(Task.assigned_to.desc().nulls_last(), Task.completed_at.desc())
        elif sort == 'completed':
            base = base.order_by(Task.completed_at.asc())
        else:
            base = base.order_by(Task.completed_at.desc())

    tasks = base.all()

    all_shops = []
    if current_user.role == 'admin':
        allowed_ids = [
            s.shop_id for s in AdminShop.query.filter_by(admin_user_id=current_user.id)
        ]
        all_shops = Shop.query.filter(Shop.id.in_(allowed_ids)).order_by(Shop.name.asc()).all()
        
    # shop_users = User.query.filter_by(shop_id=shop.id).order_by(User.username.asc()).all()
    # if current_user.role == 'admin':
    #     allowed_shop_ids = [
    #         s.shop_id for s in AdminShop.query.filter_by(admin_user_id=current_user.id)
    #     ]
    #     all_shops = Shop.query.filter(Shop.id.in_(allowed_shop_ids)).order_by(Shop.name.asc()).all()
    # else:
    #     all_shops = []

    counts = type("Counts", (), {})()
    counts.requests = requests_count
    counts.sublets  = sublets_count

    return render_template(
        'archive.html',
        current_page='archive',
        kind=kind,
        tasks=tasks,
        counts=counts,
        q=q,
        sort=sort,
        assignee=assignee,
        # shop=shop,
        # is_admin=(current_user.role == 'admin'),
        # all_shops=all_shops,
        # active_shop_id=shop.id,
        group_keys=GROUP_KEYS,
        shop_users=shop_users,                # ← filtered users
        available_groups=available_groups,    # ← filtered groups
        **_topbar_context(),
    )


# ------------------------------------------------------------------------------
# Sublets views & actions
# ------------------------------------------------------------------------------
# def _sublets_base_query():
#     return Task.query.filter(
#         Task.shop_id == get_active_shop_id(current_user),
#         Task.deleted_at.is_(None),
#         or_(Task.kind == 'sublet', Task.request_type == 'sublet')
#     )

# def _sublets_context(extra: dict | None = None):
#     shop = get_active_shop(current_user)
#     ctx = dict(
#         current_page='sublets',
#         shop=shop,
#         is_admin=(current_user.role == 'admin'),
#         all_shops=Shop.query.order_by(Shop.name.asc()).all() if current_user.role == 'admin' else [],
#         active_shop_id=shop.id,
#     )
#     if extra:
#         ctx.update(extra)
#     return ctx

# @app.route('/sublets/create', methods=['POST'])
# @login_required
# def create_sublet():
#     shop_id = get_active_shop_id(current_user)
#     title = (request.form.get('title') or '').strip()
#     description = (request.form.get('description') or '').strip() or None
#     vendor_name = (request.form.get('vendor_name') or '').strip() or None
#     urgent = bool(request.form.get('urgent'))
#     if not title:
#         flash('Please enter a title for the sublet.')
#         return redirect(url_for('sublets_unassigned'))

#     t = Task(
#         shop_id=shop_id,
#         title=title,
#         description=description,
#         urgent=urgent,
#         kind='sublet',
#         request_type='sublet',
#         vendor_name=vendor_name if vendor_name else None,
#         sublet_assigned=bool(vendor_name),
#         submitted_by_user_id=current_user.id
#     )
#     db.session.add(t)
#     db.session.commit()

#     # Mark as read for creator (match request creation behavior)
#     try:
#         _mark_read(t.id, current_user.id)
#     except Exception:
#         pass

#     flash('Sublet created.')
#     return redirect(url_for('sublets_assigned' if vendor_name else 'sublets_unassigned'))

# @app.route('/sublets/assigned')
# @login_required
# def sublets_assigned():
#     base = _sublets_base_query()
#     tasks = (base
#              .filter(Task.status != 'done')
#              .filter(or_(Task.vendor_name.isnot(None), Task.sublet_assigned.is_(True)))
#              .order_by(Task.updated_at.desc())
#              .all())

#     counts = type("Counts", (), {})()
#     counts.assigned = len(tasks)
#     counts.unassigned = (_sublets_base_query()
#                          .filter(Task.status != 'done')
#                          .filter(and_(or_(Task.vendor_name.is_(None), Task.vendor_name == ''),
#                                       or_(Task.sublet_assigned.is_(None), Task.sublet_assigned.is_(False))))
#                          .count())

#     return render_template('sublets.html', subtab='assigned', tasks=tasks, counts=counts, **_sublets_context())

# @app.route('/sublets/unassigned')
# @login_required
# def sublets_unassigned():
#     base = _sublets_base_query()
#     tasks = (base
#              .filter(Task.status != 'done')
#              .filter(and_(or_(Task.vendor_name.is_(None), Task.vendor_name == ''),
#                           or_(Task.sublet_assigned.is_(None), Task.sublet_assigned.is_(False))))
#              .order_by(Task.updated_at.desc())
#              .all())

#     counts = type("Counts", (), {})()
#     counts.unassigned = len(tasks)
#     counts.assigned = (_sublets_base_query()
#                        .filter(Task.status != 'done')
#                        .filter(or_(Task.vendor_name.isnot(None), Task.sublet_assigned.is_(True)))
#                        .count())

#     return render_template('sublets.html', subtab='unassigned', tasks=tasks, counts=counts, **_sublets_context())

# # Assign a sublet (action)
# @app.route('/sublets/<int:task_id>/assign', methods=['POST'], endpoint='sublet_assign')
# @login_required
# def sublet_assign(task_id: int):
#     t = Task.query.get_or_404(task_id)
#     if t.shop_id != get_active_shop_id(current_user):
#         abort(403)
#     t.sublet_assigned = True
#     db.session.commit()
#     flash('Sublet marked as assigned.')
#     # After assigning, show the Assigned tab
#     return redirect(url_for('sublets_assigned'))

# # Unassign a sublet (action)
# @app.route('/sublets/<int:task_id>/unassign', methods=['POST'], endpoint='sublet_unassign')
# @login_required
# def sublet_unassign(task_id: int):
#     t = Task.query.get_or_404(task_id)
#     if t.shop_id != get_active_shop_id(current_user):
#         abort(403)
#     t.sublet_assigned = False
#     db.session.commit()
#     flash('Sublet unassigned.')
#     # After unassigning, show the Unassigned tab
#     return redirect(url_for('sublets_unassigned'))

# === SUBLETS REMOVED FOR V1 - Redirect to main Requests ===
@app.route('/sublets')
@app.route('/sublets/assigned')
@app.route('/sublets/unassigned')
@login_required
def sublets():
    flash('Sublets will be added in Version 2.', 'info')
    return redirect(url_for('index'))

# ------------------------------------------------------------------------------
# Shop settings: change monthly goal (opened from KPI on Requests)
# ------------------------------------------------------------------------------
@app.post('/shop/goal')
@login_required
def update_shop_goal():
    if current_user.role != 'admin':
        abort(403)
    shop = get_active_shop(current_user)
    val_raw = (request.form.get('monthly_goal') or '').replace(',', '').replace('$', '').strip()
    try:
        new_goal = max(0, int(float(val_raw))) if val_raw else 0
    except Exception:
        flash('Please enter a valid number for monthly goal.', 'error')
        return redirect(request.referrer or url_for('index'))
    shop.monthly_goal = new_goal
    db.session.commit()
    flash('Monthly goal updated.')
    return redirect(request.referrer or url_for('index'))

# ------------------------------------------------------------------------------
# App start: create tables & run idempotent migrations and purging
# ------------------------------------------------------------------------------
with app.app_context():
    db.create_all()

    if db.engine.url.get_backend_name() == 'sqlite':
        _ensure_columns()

    # Ensure DailyReply.author_name exists (idempotent)
    if db.engine.url.get_backend_name() == 'sqlite':
        if not _table_has_column('daily_reply', 'author_name'):
            _safe_add_column('daily_reply', "author_name VARCHAR(80)")
    # Ensure DayEndReport columns exist (idempotent)
    if db.engine.url.get_backend_name() == 'sqlite':
        if not _table_has_column('day_end_report', 'actual_closed'):
            _safe_add_column('day_end_report', "actual_closed INTEGER DEFAULT 0")
        if not _table_has_column('day_end_report', 'tomorrow_close_goal'):
            _safe_add_column('day_end_report', "tomorrow_close_goal INTEGER DEFAULT 0")
        if not _table_has_column('day_end_report', 'daily_gp'):
            _safe_add_column('day_end_report', "daily_gp INTEGER DEFAULT 0")
    _purge_completed_older_than(app.config['ARCHIVE_RETENTION_DAYS'])  # Disabled until fixed
    #print("Auto-purge DISABLED to prevent startup crash")



# ---------------- Profile & Settings ----------------
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        email = (request.form.get('email') or '').strip()
        if not username:
            flash('Name is required.')
            return redirect(url_for('profile'))
        # Ensure unique username if changed
        if username != current_user.username:
            if User.query.filter_by(username=username).first():
                flash('That name is already taken.')
                return redirect(url_for('profile'))
            current_user.username = username
        current_user.email = email or None
        db.session.commit()
        flash('Profile updated.')
        return redirect(url_for('profile'))
    return render_template('profile.html')

@app.post('/profile/password')
@login_required
def profile_password():
    pw = (request.form.get('password') or '').strip()
    pw2 = (request.form.get('password2') or '').strip()
    if not pw or not pw2:
        flash('Please enter and confirm your password.')
        return redirect(url_for('profile'))
    if pw != pw2:
        flash('Passwords do not match.')
        return redirect(url_for('profile'))
    if len(pw) < 6:
        flash('Password must be at least 6 characters.')
        return redirect(url_for('profile'))
    current_user.set_password(pw)
    db.session.commit()
    flash('Password reset successfully.')
    return redirect(url_for('profile'))

@app.get('/settings')
@login_required
def settings():
    return render_template('settings.html')



# Quiet 404s for favicon in dev
@app.route('/favicon.ico')
def favicon():
    return ('', 204)



# ---- Daily Update Replies API ----
@app.get('/admin/daily-updates/questions/<int:question_id>/replies')
@login_required
def daily_replies_list(question_id):
    q = DailyQuestion.query.get_or_404(question_id)
    replies = DailyReply.query.filter_by(question_id=q.id).order_by(DailyReply.created_at.asc()).all()
    def ser(r):
        return {
            "id": r.id,
            "body": r.body,
            "author_name": r.author_name or (current_user.username if current_user.is_authenticated else "User"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
    return jsonify([ser(r) for r in replies])

@app.post('/admin/daily-updates/questions/<int:question_id>/replies')
@login_required
def daily_replies_add(question_id):
    q = DailyQuestion.query.get_or_404(question_id)
    body = (request.form.get('body') or (request.json.get('body') if request.is_json else '')).strip()
    if not body:
        return jsonify({"error":"Body required"}), 400
    r = DailyReply(question_id=q.id, body=body, author_name=current_user.username)
    db.session.add(r)
    db.session.commit()
    return jsonify({"ok": True, "id": r.id, "author_name": r.author_name, "body": r.body})  

@app.route('/admin/daily-updates/questions', methods=['POST'], endpoint='create_daily_question')
@login_required
def create_daily_question():
    # Only admins can create questions
    if not is_admin(current_user):
        abort(403)

    shop_id = get_active_shop_id(current_user)
    target_user_id = request.form.get('user_id', type=int)   # the person being asked
    body = (request.form.get('body') or '').strip()

    if not target_user_id or not body:
        flash('Please choose a user and enter a question.', 'error')
        return redirect(url_for('admin_daily_updates', tab='add'))

    # Safety: ensure the target user is in the same shop
    target = User.query.filter_by(id=target_user_id, shop_id=shop_id).first()
    if not target:
        flash('Invalid user for this shop.', 'error')
        return redirect(url_for('admin_daily_updates', tab='add'))

    q = DailyQuestion(
        shop_id=shop_id,
        user_id=current_user.id,       # creator
        target_user_id=target_user_id, # person being asked
        body=body,
        is_active=True
    )
    db.session.add(q)
    db.session.commit()
    flash('Question created.', 'success')
    return redirect(url_for('admin_daily_updates', tab='active'))


@app.post('/admin/daily-updates/questions/<int:qid>/delete')
@login_required
def delete_daily_question(qid: int):
    if not is_admin(current_user):
        abort(403)
    q = DailyQuestion.query.get_or_404(qid)
    shop = get_active_shop(current_user)
    if q.shop_id != shop.id:
        abort(403)
    db.session.delete(q); db.session.commit()
    flash('Question deleted.', 'info')
    return redirect(url_for('admin_daily_updates', tab='active'))

@app.get('/tech')
@login_required
def tech_dashboard():
    if (current_user.role or '').lower() != 'tech':
        abort(403)
    return render_template('tech_dashboard.html', **_topbar_context())

@app.post('/tech/requests')
@login_required
def tech_submit_request():
    if (current_user.role or '').lower() != 'tech':
        return jsonify({'ok': False, 'error': 'Forbidden'}), 403

    shop_id = get_active_shop_id(current_user)
    if not shop_id:
        return jsonify({'ok': False, 'error': 'NoShop'}), 400

    # Support both multipart (with files) and JSON
    is_multipart = request.content_type and request.content_type.startswith('multipart')
    if is_multipart:
        req_type = (request.form.get('request_type') or '').strip()
        comments = (request.form.get('comments') or '').strip()
        urgent = bool(request.form.get('urgent'))
        photos = request.files.getlist('photos')   # <-- this gets ALL selected files
    else:
        data = request.get_json(silent=True) or {}
        req_type = (data.get('request_type') or '').strip()
        comments = (data.get('comments') or '').strip()
        urgent = bool(data.get('urgent'))
        photos = []

    if not req_type:
        return jsonify({'ok': False, 'error': 'Missing request_type'}), 400

    # Map to correct group
    req_lower = req_type.lower()
    if req_lower == 'blueprint':
        assigned_group = None

    elif req_lower in ('pull parts', 'return parts', 'supplies'):
        assigned_group = 'parts'

    else:
        assigned_group = 'estimators'



    try:
        t = Task(
            shop_id=shop_id,
            title=req_type,
            request_type=req_type,
            description=comments or None,
            comments=comments or None,
            urgent=urgent,
            status='pending',
            kind='tech',
            queue='incoming',
            assigned_group=assigned_group,
            submitted_by_user_id=current_user.id,
        )
        db.session.add(t)
        db.session.commit()

        if t.request_type == "Blueprint":
            roles = ["admin", "parts", "estimator"]
        else:
            roles = REQUEST_ROLE_MAP.get(t.request_type, [])

        for role in roles:
            db.session.add(TaskRoleStatus(
                task_id=t.id,
                role=role
            ))

        db.session.commit()

        # Save ALL photos
        if photos:
            _save_task_images(t.id, photos)

        print(f"Tech request created: id={t.id}, type={req_type}, photos={len(photos)}")
        return jsonify({'ok': True, 'id': t.id})

    except Exception as e:
        db.session.rollback()
        print("tech_submit_request error:", e)
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/dev/cleanup-test-shops')
def cleanup_test_shops():
    bad_names = ["TEST_SHOP_2", "TEST_SHOP_3"]

    # 1. Find shops to delete
    bad_shops = Shop.query.filter(Shop.name.in_(bad_names)).all()
    bad_ids = [s.id for s in bad_shops]

    print("Deleting MonthSnapshot:", bad_names)
    print("MonthSnapshot Shop IDs:", bad_ids)
    
    MonthSnapshot.query.filter(
        MonthSnapshot.shop_id.in_(bad_ids)
    ).delete(synchronize_session=False)

    # 2. Safety log
    print("Deleting shops:", bad_names)
    print("Shop IDs:", bad_ids)

    # 3. Remove AdminShop links first (IMPORTANT for FK safety)
    if bad_ids:
        AdminShop.query.filter(AdminShop.shop_id.in_(bad_ids)) \
            .delete(synchronize_session=False)

    # 4. Fix any users pointing to deleted shops
    users = User.query.filter(User.shop_id.in_(bad_ids)).all()
    keep_shop = Shop.query.filter(Shop.name == "TEST_SHOP").first()

    for u in users:
        u.shop_id = keep_shop.id

    # 5. Delete shops
    Shop.query.filter(Shop.id.in_(bad_ids)) \
        .delete(synchronize_session=False)

    db.session.commit()

    return {
        "deleted_shops": bad_names,
        "deleted_ids": bad_ids,
        "status": "success"
    }


# ---------------- Reports ----------------
@app.get('/reports')
@login_required
def reports_page():
    if not is_admin(current_user): abort(403)
    return render_template('admin_reports.html', **_topbar_context())

# --- Reports API: per-user submit counts + avg response time in business hours ---
from datetime import datetime, time

def _parse_ymd(s, default):
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return default

def _business_hours_diff(start_dt: datetime, end_dt: datetime) -> float:
    """
    Count hours between start and end but only Mon-Fri, 9:00–17:00.
    Returns float hours (may be fractional).
    """
    if end_dt <= start_dt:
        return 0.0

    # Clamp to business window day-by-day
    biz_start = time(9, 0)
    biz_end   = time(17, 0)

    cur = start_dt
    total_secs = 0

    while cur.date() <= end_dt.date():
        if cur.weekday() < 5:  # 0=Mon .. 4=Fri
            day_start = datetime.combine(cur.date(), biz_start)
            day_end   = datetime.combine(cur.date(), biz_end)

            # For first/last day, clamp window
            win_start = max(day_start, start_dt)
            win_end   = min(day_end, end_dt)

            if win_end > win_start:
                total_secs += (win_end - win_start).total_seconds()

        # next day @ 00:00
        cur = datetime.combine(cur.date(), time(0,0)) + timedelta(days=1)

    return total_secs / 3600.0

@app.get('/api/reports/user-metrics')
@login_required
@subscription_required
@admin_only
def api_reports_user_metrics():
    shop = get_active_shop(current_user)

    # Range (inclusive): defaults to last 30 days
    today = utcnow()
    default_start = (today - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    default_end   = today

    start = _parse_ymd(request.args.get('from', ''), default_start)
    end   = _parse_ymd(request.args.get('to', ''), default_end)
    # normalize end to end-of-day for inclusivity
    end = end.replace(hour=23, minute=59, second=59, microsecond=0)

    # Pull all users in this shop
    users = User.query.filter_by(shop_id=shop.id).all()

    rows = []
    for u in users:
        # tasks submitted by this user in range
        tq = (Task.query
              .filter(Task.shop_id == shop.id,
                      Task.submitted_by_user_id == u.id,
                      Task.created_at >= start,
                      Task.created_at <= end,
                      Task.deleted_at.is_(None)))
        tasks = tq.all()
        count = len(tasks)

        # For response time: first TaskRead by someone else
        hrs_samples = []
        for t in tasks:
            first_other_read = (TaskRead.query
                                .filter(TaskRead.task_id == t.id,
                                        TaskRead.user_id != u.id)
                                .order_by(TaskRead.read_at.asc())
                                .first())
            if first_other_read:
                hrs = _business_hours_diff(t.created_at, first_other_read.read_at)
                if hrs >= 0:
                    hrs_samples.append(hrs)

        avg_hours = round(sum(hrs_samples)/len(hrs_samples), 2) if hrs_samples else None

        rows.append({
            'user': u.username,
            'tasks': count,
            'avg_hours': (avg_hours if avg_hours is not None else '—')
        })

    return jsonify({'rows': rows})



@app.get('/vendor')
@login_required
def vendor_portal():
    if not is_vendor(current_user):
        abort(403)
    subs = []
    try:
        subs = Sublet.query.filter_by(vendor_name=current_user.username).all()
    except Exception:
        pass
    return render_template('vendor_sublets.html', sublets=subs, **_topbar_context())

@app.get('/api/debug/last-tech')
@login_required
def debug_last_tech():
    q = (Task.query
         .filter_by(kind='tech')
         .order_by(Task.created_at.desc())
         .limit(10))
    out = []
    for t in q:
        out.append({
            'id': t.id,
            'shop_id': t.shop_id,
            'title': t.title,
            'status': t.status,
            'queue': t.queue,
            'created_at': t.created_at.isoformat()
        })
    return jsonify(out)

@app.get('/api/tasks/<int:tid>/attachments')
@login_required
def api_task_attachments(tid):
    t = Task.query.get_or_404(tid)
    shop = get_active_shop(current_user)
    # Visibility: same shop or admin/manager
    if t.shop_id != shop.id and (getattr(current_user, 'role','').lower() not in ('admin','manager')):
        abort(403)
    atts = (TaskAttachment.query
            .filter_by(task_id=t.id)
            .order_by(TaskAttachment.created_at.asc())
            .all())
    return jsonify({
        'ok': True,
        'attachments': [
            {
                'id': a.id,
                'name': a.filename,
                'url': url_for('serve_upload', relpath=a.url_path),
                'size': a.size,
                'content_type': a.content_type,
                'created_at': a.created_at.isoformat()
            } for a in atts
        ]
    })


if __name__ == "__main__":
    app.run()

