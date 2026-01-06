from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime, timedelta
import os
import threading
import time

# 初始化应用
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///auction.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*") # 允许跨域，方便局域网访问
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- 数据库模型 ---

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False) # 实际应用应加密
    role = db.Column(db.String(20), nullable=False) # 'buyer', 'seller', 'admin'

class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    start_price = db.Column(db.Float, nullable=False)
    current_price = db.Column(db.Float, nullable=False)
    increment = db.Column(db.Float, default=10.0)
    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    end_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='pending') # pending, active, ended, rejected
    highest_bidder_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    
    seller = db.relationship('User', foreign_keys=[seller_id])
    highest_bidder = db.relationship('User', foreign_keys=[highest_bidder_id])

class Bid(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship('Item')
    user = db.relationship('User')

# --- 辅助函数 ---

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def check_auctions():
    """后台任务：检查拍卖是否结束"""
    while True:
        with app.app_context():
            now = datetime.utcnow()
            # 查找已到期且仍在进行中的拍卖
            expired_items = Item.query.filter(Item.status == 'active', Item.end_time <= now).all()
            for item in expired_items:
                item.status = 'ended'
                db.session.commit()
                # 通知房间内的用户拍卖结束
                socketio.emit('auction_ended', {'item_id': item.id, 'winner': item.highest_bidder.username if item.highest_bidder else '无人出价'}, room=f"item_{item.id}")
        time.sleep(10) # 每10秒检查一次

# --- 路由 ---

@app.route('/')
def index():
    items = Item.query.filter_by(status='active').all()
    return render_template('index.html', items=items)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.password == password:
            login_user(user)
            return redirect(url_for('index'))
        flash('用户名或密码错误')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role')
        if User.query.filter_by(username=username).first():
            flash('用户名已存在')
        else:
            new_user = User(username=username, password=password, role=role)
            db.session.add(new_user)
            db.session.commit()
            login_user(new_user)
            return redirect(url_for('index'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/publish', methods=['GET', 'POST'])
@login_required
def publish():
    if current_user.role != 'seller':
        flash('只有卖家可以发布商品')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description')
        start_price = float(request.form.get('start_price'))
        duration = int(request.form.get('duration')) # 分钟
        
        # 简单起见，图片URL暂略，或让用户输入URL
        end_time = datetime.utcnow() + timedelta(minutes=duration)
        
        new_item = Item(
            seller_id=current_user.id,
            name=name,
            description=description,
            start_price=start_price,
            current_price=start_price,
            end_time=end_time,
            status='pending' # 需要审核
        )
        db.session.add(new_item)
        db.session.commit()
        flash('拍品已提交，等待管理员审核')
        return redirect(url_for('index'))
        
    return render_template('publish.html')

@app.route('/item/<int:item_id>')
def item_detail(item_id):
    item = Item.query.get_or_404(item_id)
    return render_template('item_detail.html', item=item)

@app.route('/admin')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        flash('权限不足')
        return redirect(url_for('index'))
    pending_items = Item.query.filter_by(status='pending').all()
    return render_template('admin_dashboard.html', items=pending_items)

@app.route('/approve/<int:item_id>')
@login_required
def approve_item(item_id):
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    item = Item.query.get_or_404(item_id)
    item.status = 'active'
    item.start_time = datetime.utcnow()
    # 重新计算结束时间（如果是从批准时刻开始算时长）
    # 这里简单处理，假设 duration 是从批准开始算
    # 为了简化，我们假设 publish 设置的是绝对时长，这里加上当前时间
    # 实际项目中可能需要存储 duration 字段而不是 end_time
    # 让我们假设在发布时如果还没根据批准时间更新，这里简单更新一下 end_time
    # 重新计算 end_time = now + (原 end_time - 原 start_time)
    duration = item.end_time - item.start_time
    item.start_time = datetime.utcnow()
    item.end_time = item.start_time + duration
    
    db.session.commit()
    flash('已批准上架')
    return redirect(url_for('admin_dashboard'))

@app.route('/reject/<int:item_id>')
@login_required
def reject_item(item_id):
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    item = Item.query.get_or_404(item_id)
    item.status = 'rejected'
    db.session.commit()
    flash('已拒绝')
    return redirect(url_for('admin_dashboard'))

# --- SocketIO 事件 ---

@socketio.on('join')
def on_join(data):
    room = data['room']
    join_room(room)

@socketio.on('bid')
def on_bid(data):
    # data: {'item_id': 1, 'amount': 100, 'user_id': 2}
    if not current_user.is_authenticated:
        return
        
    item_id = data['item_id']
    amount = float(data['amount'])
    
    item = Item.query.get(item_id)
    
    if not item or item.status != 'active':
        return
        
    # 检查时间是否结束
    if datetime.utcnow() > item.end_time:
        item.status = 'ended'
        db.session.commit()
        emit('error', {'msg': '拍卖已结束'}, room=f"item_{item_id}")
        return

    # 验证金额
    min_bid = item.current_price + (item.increment if item.highest_bidder else 0)
    # 如果没人出过价，出价必须 >= start_price
    if item.highest_bidder is None:
        min_bid = item.start_price
    else:
        min_bid = item.current_price + item.increment

    if amount < min_bid:
        emit('error', {'msg': f'出价必须高于 {min_bid}'}, room=request.sid) # 只发给当前用户
        return

    # 防狙击机制：如果剩余时间小于3分钟，延长5分钟
    time_left = item.end_time - datetime.utcnow()
    extended = False
    if time_left < timedelta(minutes=3):
        item.end_time += timedelta(minutes=5)
        extended = True

    # 更新数据库
    item.current_price = amount
    item.highest_bidder_id = current_user.id
    
    new_bid = Bid(item_id=item.id, user_id=current_user.id, amount=amount)
    db.session.add(new_bid)
    db.session.commit()
    
    # 广播新价格
    response = {
        'new_price': amount,
        'bidder_name': current_user.username,
        'new_end_time': item.end_time.isoformat() + 'Z', # ISO 格式便于前端解析
        'extended': extended
    }
    emit('price_update', response, room=f"item_{item_id}")

# --- 启动 ---

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # 创建一个默认管理员用于测试
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', password='123', role='admin')
            db.session.add(admin)
            db.session.commit()
            
    # 启动后台检查线程
    bg_thread = threading.Thread(target=check_auctions)
    bg_thread.daemon = True
    bg_thread.start()
    
    # host='0.0.0.0' 使其他设备可访问
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)
