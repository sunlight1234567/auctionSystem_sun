from flask import render_template, request, redirect, url_for, flash, current_app
from flask_login import login_user, login_required, logout_user, current_user
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from decimal import Decimal, ROUND_HALF_UP
import os
import time
from sqlalchemy import text
from extensions import db, socketio
from models import User, Item, ItemImage, Post, Bid
import query
from services import send_system_message

import qrcode
from io import BytesIO
import base64

def register_views(app):

    # --- 全局 Context Processor ---
    @app.context_processor
    def inject_global_vars():
        context = {}
        if current_user.is_authenticated:
            # 管理员审核计数
            if current_user.role == 'admin':
                try:
                    from models import Appeal
                    item_count = Item.query.filter_by(status='pending').count()
                    appeal_count = Appeal.query.filter_by(status='pending').count()
                    context['pending_count'] = item_count + appeal_count
                except:
                    context['pending_count'] = 0
            
            # 未读私信计数
            try:
                # 需在函数内部导入避免循环依赖，或者假设 models 已加载
                from models import ChatSession
                from sqlalchemy import or_
                
                # 计算我作为 buyer 的未读 + 我作为 seller 的未读
                unread = 0
                buyer_sessions = ChatSession.query.filter_by(buyer_id=current_user.id).filter(ChatSession.buyer_unread > 0).count()
                seller_sessions = ChatSession.query.filter_by(seller_id=current_user.id).filter(ChatSession.seller_unread > 0).count()
                context['unread_chats_count'] = buyer_sessions + seller_sessions
            except:
                context['unread_chats_count'] = 0
                
        return context

    @app.route('/')
    @login_required
    def index():
        try:
            search_q = request.args.get('q', '')
            
            # 使用 query 模块获取数据，传入 Model 类以避免循环导入
            active_items, upcoming_items, ended_items = query.get_index_items(Item, User, search_q)
            
            # 搜索卖家
            matched_sellers = query.get_search_users(User, search_q) if search_q else []

            return render_template('index.html', 
                                active_items=active_items, 
                                upcoming_items=upcoming_items, 
                                ended_items=ended_items,
                                matched_sellers=matched_sellers,
                                search_query=search_q)
        except Exception as e:
            return f"<h3>数据库连接失败</h3><p>请检查 app.py 中的数据库密码配置。</p><p>错误详情: {e}</p>"

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            user = User.query.filter_by(username=username).first()
            # 注意：实际生产中应使用 werkzeug.security.check_password_hash
            # 这里为了演示，直接比对（数据库里存明文）
            if user and user.password_hash == password:
                login_user(user)
                return redirect(url_for('index'))
            flash('用户名或密码错误')
        return render_template('login.html')

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            confirm_password = request.form.get('confirm_password')
            phone = request.form.get('phone') # 获取电话
            role = request.form.get('role')
            
            if password != confirm_password:
                flash('两次输入的密码不一致')
            elif User.query.filter_by(username=username).first():
                flash('用户名已存在')
            elif role not in ['buyer', 'seller']:
                flash('无效的角色选择')
            else:
                # 同样，这里存入 password_hash 字段的是明文，正式项目请加密
                new_user = User(username=username, password_hash=password, role=role, phone=phone)
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
            start_price_val = request.form.get('start_price')
            increment_val = request.form.get('increment', '10')
            duration_val = request.form.get('duration')

            if not name or not description or not start_price_val or not duration_val:
                flash('请填写所有必填字段（名称、描述、起拍价、时长）')
                return redirect(request.url)

            try:
                # 强制保留两位小数，通过 ROUND_HALF_UP 四舍五入，防止浮点数精度问题
                start_price = Decimal(start_price_val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                increment = Decimal(increment_val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                duration = int(duration_val)
            except ValueError:
                flash('价格或时长格式无效')
                return redirect(request.url) 
            
            start_time_str = request.form.get('start_time')
            if start_time_str:
                try:
                    # datetime-local format: YYYY-MM-DDTHH:MM
                    start_time = datetime.strptime(start_time_str, '%Y-%m-%dT%H:%M')
                except ValueError:
                    # Fallback if format is wrong
                    start_time = datetime.now()
            else:
                start_time = datetime.now()

            end_time = start_time + timedelta(minutes=duration)
            
            # 验证图片上传
            files = request.files.getlist('images')
            if not files or not any(f.filename for f in files):
                flash('请至少上传一张商品图片')
                return redirect(request.url)

            new_item = Item(
                seller_id=current_user.id,
                name=name,
                description=description,
                start_price=start_price,
                current_price=start_price,
                increment=increment,
                start_time=start_time,
                end_time=end_time,
                status='pending' 
            )
            db.session.add(new_item)
            db.session.flush() # 获取 new_item.id

            # 处理图片上传
            files = request.files.getlist('images')
            for file in files:
                if file and file.filename:
                    filename = secure_filename(file.filename)
                    # 防止重名
                    unique_filename = f"{int(time.time())}_{filename}"
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                    
                    # 确保目录存在
                    if not os.path.exists(app.config['UPLOAD_FOLDER']):
                        os.makedirs(app.config['UPLOAD_FOLDER'])
                        
                    file.save(file_path)
                    
                    # 存相对路径到数据库 (注意 path separator)
                    db_image_path = f"uploads/{unique_filename}"
                    new_image = ItemImage(item_id=new_item.id, image_url=db_image_path)
                    db.session.add(new_image)

            db.session.commit()
            
            # 通知管理员有新审核
            socketio.emit('new_pending_item', {
                'msg': f'新拍品待审核: {new_item.name} (卖家: {current_user.username})',
                'type': 'audit'
            }, room='admin_room')
            
            flash('拍品已提交，等待管理员审核')
            return redirect(url_for('index'))
            
        return render_template('publish.html')

    @app.route('/my_auctions')
    @login_required
    def my_auctions():
        if current_user.role != 'seller':
            flash('只有卖家可以查看发布历史')
            return redirect(url_for('index'))
        
        search_q = request.args.get('q', '')
        # 移至 query.py
        my_items = query.get_seller_items(Item, User, current_user.id, search_q)
        return render_template('my_auctions.html', items=my_items, search_query=search_q)

    @app.route('/my_orders')
    @login_required
    def my_orders():
        search_q = request.args.get('q', '')
        # 移至 query.py
        orders = query.get_buyer_won_items(Item, User, current_user.id, search_q)
        return render_template('my_orders.html', items=orders, search_query=search_q)

    @app.route('/item/<int:item_id>')
    @login_required
    def item_detail(item_id):
        item = Item.query.get_or_404(item_id)
        return render_template('item_detail.html', item=item)

    @app.route('/admin')
    @login_required
    def admin_dashboard():
        return redirect(url_for('admin_audit'))

    @app.route('/admin/audit')
    @login_required
    def admin_audit():
        if current_user.role != 'admin':
            flash('权限不足')
            return redirect(url_for('index'))
        
        # 仅获取待审核
        from models import Item, Appeal
        pending_items = Item.query.filter_by(status='pending').order_by(Item.created_at.desc()).all()
        
        # 为了计算 Badge，也需要其他数量 (或者只计算 audit_count)
        # Context Processor 已经有了 global pending_count (sum)
        # 这里特别传 audit_count 和 appeal_pending_count 给 nav_tabs
        audit_count = len(pending_items)
        appeal_pending_count = Appeal.query.filter_by(status='pending').count()

        return render_template('admin/audit.html', 
                               items=pending_items,
                               active_tab='audit',
                               audit_count=audit_count,
                               appeal_pending_count=appeal_pending_count)

    @app.route('/admin/active')
    @login_required
    def admin_active_items():
        if current_user.role != 'admin':
            return redirect(url_for('index'))
        
        # Active and Approved (Upcoming)
        from models import Item, Appeal
        active_items = Item.query.filter(Item.status.in_(['active', 'approved'])).order_by(Item.start_time).all()
        
        # Counts for tabs
        audit_count = Item.query.filter_by(status='pending').count()
        appeal_pending_count = Appeal.query.filter_by(status='pending').count()

        return render_template('admin/active_items.html', 
                               active_items=active_items,
                               active_tab='active_items',
                               audit_count=audit_count,
                               appeal_pending_count=appeal_pending_count)

    @app.route('/admin/appeals')
    @login_required
    def admin_appeals():
        if current_user.role != 'admin':
            return redirect(url_for('index'))
            
        from models import Appeal, Item
        pending_appeals, history_appeals = query.get_appeal_list(Appeal)
        
        # Counts for tabs
        audit_count = Item.query.filter_by(status='pending').count()
        appeal_pending_count = len(pending_appeals)

        return render_template('admin/appeals.html', 
                               appeal_items=pending_appeals,
                               appeal_history=history_appeals,
                               active_tab='appeals',
                               audit_count=audit_count,
                               appeal_pending_count=appeal_pending_count)

    @app.route('/admin/history')
    @login_required
    def admin_history():
        if current_user.role != 'admin':
            return redirect(url_for('index'))
            
        from models import Item, Appeal
        # Ended items (stopped, rejected, ended)
        ended_items = Item.query.filter(Item.status.in_(['ended', 'stopped', 'rejected'])).order_by(Item.end_time.desc()).limit(50).all()
        
        # Counts for tabs
        audit_count = Item.query.filter_by(status='pending').count()
        appeal_pending_count = Appeal.query.filter_by(status='pending').count()

        return render_template('admin/history.html', 
                               ended_items=ended_items,
                               now=datetime.now(),
                               active_tab='history',
                               audit_count=audit_count,
                               appeal_pending_count=appeal_pending_count)

    @app.route('/approve_action/<int:item_id>', methods=['POST'])
    @login_required
    def approve_item_action(item_id):
        if current_user.role != 'admin':
            return redirect(url_for('index'))
        item = Item.query.get_or_404(item_id)
        
        # Check if this is a future scheduled item
        if item.start_time > datetime.now():
            item.status = 'approved' # Waiting for start time
            flash(f'已批准。拍卖将于 {item.start_time.strftime("%Y-%m-%d %H:%M")} 自动开始')
        else:
            # If immediate or time passed, start now
            item.status = 'active'
            # Reset start time to now to ensure full duration (if desirable for delayed approval)
            # Assuming if user wanted 8:00 but admin approves 9:00, we shift to 9:00
            original_duration = item.end_time - item.start_time
            if original_duration.total_seconds() < 60:
                original_duration = timedelta(hours=1)
            item.start_time = datetime.now()
            item.end_time = item.start_time + original_duration
            flash('已批准并立即开拍')
        
        db.session.commit()
        
        # Notify seller via SocketIO
        msg_content = f'您的拍品 "{item.name}" 已通过审核并上架！'
        socketio.emit('auction_approved', {
            'item_name': item.name,
            'msg': msg_content
        }, room=f"user_{item.seller_id}")
        
        # 发送系统私信 (跳过默认通知，因为已经发送了 auction_approved)
        send_system_message(item.id, item.seller_id, msg_content, skip_notification=True)
        
        return redirect(url_for('admin_audit'))

    @app.route('/reject/<int:item_id>', methods=['POST'])
    @login_required
    def reject_item(item_id):
        if current_user.role != 'admin':
            return redirect(url_for('index'))
        
        item = Item.query.get_or_404(item_id)
        reason = request.form.get('reason', '')
        
        item.status = 'rejected'
        item.rejection_reason = reason
        db.session.commit()
        
        # Notify seller via SocketIO
        msg_content = f'您的拍品 "{item.name}" 已被拒绝。理由: {reason}'
        socketio.emit('auction_rejected', {
            'item_name': item.name,
            'reason': reason,
            'msg': msg_content
        }, room=f"user_{item.seller_id}")
        
        # 发送系统私信 (跳过默认通知，因为已经发送了 auction_rejected)
        send_system_message(item.id, item.seller_id, msg_content, skip_notification=True)
        
        flash('已拒绝并在卖家端发送通知')
        return redirect(url_for('admin_audit'))

    @app.route('/admin/stop/<int:item_id>', methods=['GET', 'POST'])
    @login_required
    def stop_auction(item_id):
        """管理员强制停止拍卖"""
        if current_user.role != 'admin':
            return redirect(url_for('index'))
        
        # 如果是旧的 GET 请求链接，重定向回管理页面并提示
        if request.method == 'GET':
            flash('请刷新页面后，使用“强制下架”按钮填写原因')
            # 兼容性重定向，假设来自活跃列表
            return redirect(url_for('admin_active_items'))
            
        item = Item.query.get_or_404(item_id)
        reason = request.form.get('reason')

        if not reason:
            flash('下架必须填写原因')
            return redirect(url_for('admin_active_items'))
        
        # 允许停止 active 或 approved 状态的商品
        if item.status in ['active', 'approved']:
            item.status = 'stopped' # 强制下架状态
            item.rejection_reason = reason # 下架原因
            db.session.commit()
            
            # 如果正在进行，通知房间内用户
            socketio.emit('error', {'msg': f'管理员已强制终止此拍卖，原因：{reason}'}, room=f"item_{item.id}")
            socketio.emit('auction_ended', {'item_id': item.id, 'winner': '管理员终止'}, room=f"item_{item.id}")
            
            # 生成申诉链接 (指向新的申诉表单页面)
            appeal_url = url_for('submit_appeal', item_id=item.id, _external=True)
            
            # 通知卖家 (Yellow Toast)
            # 使用 HTML <a> 标签包裹链接，配合前端 innerHTML 显示
            msg_content = f'您的拍品 "{item.name}" 已被管理员强制下架。原因：{reason}。如果您对此操作有任何异议，可以<a href="{appeal_url}" class="text-white fw-bold" style="text-decoration: underline;">点击此处</a>进行申诉'
            
            socketio.emit('auction_stopped', {
                'item_name': item.name,
                'reason': reason,
                'msg': msg_content
            }, room=f"user_{item.seller_id}")

            # 发送系统私信给卖家 (跳过通用通知) - 私信中存储完整链接供点击
            chat_msg_content = f'您的拍品 "{item.name}" 已被管理员强制下架。原因：{reason}。如果您对此操作有任何异议，可以点击链接进行申诉: {appeal_url}'
            send_system_message(item.id, item.seller_id, chat_msg_content, skip_notification=True)
            
            flash(f'已强制停止拍品: {item.name}')
        else:
            flash('该拍品当前状态无法停止')
            
        return redirect(url_for('admin_active_items'))

    @app.route('/admin/restore/<int:item_id>', methods=['POST'])
    @login_required
    def restore_auction(item_id):
        """管理员恢复被误操作强制停止的拍卖"""
        if current_user.role != 'admin':
            return redirect(url_for('index'))
            
        item = Item.query.get_or_404(item_id)
        
        if item.status == 'stopped':
            if item.end_time > datetime.now():
                item.status = 'active'
                item.rejection_reason = None 
                
                # Update pending appeals to 'approved' (resolved)
                from models import Appeal
                pending_appeals = Appeal.query.filter_by(item_id=item.id, status='pending').all()
                for appeal in pending_appeals:
                    appeal.status = 'approved'
                    appeal.handled_at = datetime.now()
                    appeal.admin_reply = '管理员主动恢复'

                db.session.commit()
                
                # Notify seller via SocketIO (Green Toast)
                msg_content = f'您的拍品 "{item.name}" 已被管理员恢复上架！'
                socketio.emit('auction_restored', {
                    'item_name': item.name,
                    'msg': msg_content
                }, room=f"user_{item.seller_id}")

                # 发送系统私信 (跳过默认通知)
                send_system_message(item.id, item.seller_id, msg_content, skip_notification=True)

                flash(f'已恢复拍品: {item.name}')
            else:
                flash('该拍品原定结束时间已过，无法恢复')
        else:
            flash('只有被强制下架的拍品才能恢复')
            
        return redirect(url_for('admin_appeals'))

    @app.route('/item/<int:item_id>/appeal', methods=['GET', 'POST'])
    @login_required
    def submit_appeal(item_id):
        item = Item.query.get_or_404(item_id)
        
        if item.seller_id != current_user.id:
            flash('无权操作')
            return redirect(url_for('inbox'))
            
        if item.status != 'stopped':
            flash('该拍品未处于强制下架状态，无需申诉')
            return redirect(url_for('inbox'))
        
        # 处理 GET 请求：显示申诉表单
        if request.method == 'GET':
            return render_template('appeal.html', item=item)
            
        # 处理 POST 请求：提交申诉
        reason = request.form.get('reason')
        if not reason:
            flash('请填写申诉理由')
            return render_template('appeal.html', item=item)
            
        # Create new Appeal record
        from models import Appeal
        new_appeal = Appeal(
            item_id=item.id,
            user_id=current_user.id,
            content=reason,
            status='pending',
            rejection_reason_snapshot=item.rejection_reason
        )
        db.session.add(new_appeal)
        db.session.commit()
        
        flash('申诉提交成功，请等待管理员处理')
        
        # Notify Admin
        socketio.emit('new_pending_item', {
                'msg': f'收到新申诉: {item.name} (卖家: {current_user.username})',
                'type': 'appeal'
            }, room='admin_room')
        
        # 跳转回消息列表 or Chat
        return redirect(url_for('inbox'))

    @app.route('/admin/reject_appeal/<int:item_id>', methods=['POST'])
    @login_required
    def reject_appeal(item_id):
        if current_user.role != 'admin':
            return redirect(url_for('index'))
            
        # item_id is passed, but we should probably reject by appeal_id now?
        # But UI sends item_id currently. Let's find pending appeals for this item.
        # Ideally UI should send appeal_id.
        # Let's support item_id for now to handle "reject all pending appeals for this item" or 
        # modify template to pass appeal_id. modifying template is better.
        
        # But wait, the prompt is to fix bug and support history. 
        # I will update template to iterate appeals, so I should update route to take appeal_id.
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin/reject_appeal_action/<int:appeal_id>', methods=['POST'])
    @login_required
    def reject_appeal_action(appeal_id):
        if current_user.role != 'admin':
            return redirect(url_for('index'))
            
        from models import Appeal
        appeal = Appeal.query.get_or_404(appeal_id)
        
        reason = request.form.get('reason')
        if not reason:
            reason = '申诉被驳回'

        if appeal.status == 'pending':
            appeal.status = 'rejected'
            appeal.handled_at = datetime.now()
            appeal.admin_reply = reason
            
            db.session.commit()
            
            # Notify seller
            item = appeal.item
            msg = f'关于拍品 "{item.name}" 的申诉已被驳回。理由: {reason}。维持下架决定。'
            send_system_message(item.id, item.seller_id, msg)
            socketio.emit('auction_rejected', { 
                'item_name': item.name,
                'reason': reason,
                'msg': msg
            }, room=f"user_{item.seller_id}")
            
            flash('已驳回申诉')
        
        return redirect(url_for('admin_appeals'))

    @app.route('/user/<int:user_id>')
    @login_required
    def user_profile(user_id):
        user = User.query.get_or_404(user_id)
        posts = query.get_user_posts(Post, user_id)
        
        # 获取公开的商品 (如果是卖家)
        public_items = []
        if user.role == 'seller':
            public_items = query.get_user_public_items(Item, user_id)
            
        return render_template('user_profile.html', user=user, posts=posts, items=public_items)

    @app.route('/update_avatar', methods=['POST'])
    @login_required
    def update_avatar():
        if 'avatar' not in request.files:
            flash('没有选择文件')
            return redirect(url_for('user_profile', user_id=current_user.id))
        
        file = request.files['avatar']
        if file.filename == '':
            flash('没有选择文件')
            return redirect(url_for('user_profile', user_id=current_user.id))
            
        if file:
            filename = secure_filename(file.filename)
            # Add timestamp to filename
            extension = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
            if not extension:
                extension = 'jpg' # Default fallback
            unique_filename = f"avatar_{current_user.id}_{int(time.time())}.{extension}"
            
            file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)
            file.save(file_path)
            
            # Update user avatar
            user = User.query.get(current_user.id)
            user.avatar = unique_filename
            db.session.commit()
            
            flash('头像更新成功')
            return redirect(url_for('user_profile', user_id=current_user.id))

    @app.route('/post/create', methods=['POST'])
    @login_required
    def create_post():
        content = request.form.get('content')
        if not content:
            flash('内容不能为空')
            return redirect(url_for('user_profile', user_id=current_user.id))
            
        new_post = Post(user_id=current_user.id, content=content)
        db.session.add(new_post)
        db.session.commit()
        flash('动态发布成功')
        return redirect(url_for('user_profile', user_id=current_user.id))

    @app.route('/item/<int:item_id>/pay', methods=['GET', 'POST'])
    @login_required
    def pay_item(item_id):
        item = Item.query.get_or_404(item_id)
        
        # Security checks
        if item.highest_bidder_id != current_user.id:
            flash('您不是该拍品的获胜者，无法进行支付')
            return redirect(url_for('item_detail', item_id=item_id))
            
        if item.status != 'ended':
            flash('拍卖尚未结束')
            return redirect(url_for('item_detail', item_id=item_id))
            
        if item.payment_status == 'paid':
            flash('该订单已支付')
            return redirect(url_for('item_detail', item_id=item_id))

        show_qr = False
        wx_qr = None
        ali_qr = None

        if request.method == 'POST':
            # Identify if it is Step 1 (Address) or Step 2 (Confirm) - Wait, Step 2 is separate route? 
            # In template I made them separate logic. Step 1 submits to same route.
            
            shipping_name = request.form.get('shipping_name')
            shipping_phone = request.form.get('shipping_phone')
            shipping_address = request.form.get('shipping_address')
            
            if shipping_name and shipping_phone and shipping_address:
                # Save first
                item.shipping_name = shipping_name
                item.shipping_phone = shipping_phone
                item.shipping_address = shipping_address
                db.session.commit()
                
                # Generate QRs
                # Wechat
                qr = qrcode.QRCode(version=1, box_size=10, border=5)
                # Mock Payment URL: weixin://wxpay/bizpayurl?pr=...
                qr_data_wx = f"wxp://f2f09348JS888?oid={item.order_hash}&amt={item.current_price}"
                qr.add_data(qr_data_wx)
                qr.make(fit=True)
                img = qr.make_image(fill='black', back_color='white')
                buffered = BytesIO()
                img.save(buffered, format="PNG")
                wx_qr = base64.b64encode(buffered.getvalue()).decode()
                
                # Alipay
                qr2 = qrcode.QRCode(version=1, box_size=10, border=5)
                qr_data_ali = f"https://qr.alipay.com/bax093?oid={item.order_hash}&amt={item.current_price}"
                qr2.add_data(qr_data_ali)
                qr2.make(fit=True)
                img2 = qr2.make_image(fill='black', back_color='white')
                buffered2 = BytesIO()
                img2.save(buffered2, format="PNG")
                ali_qr = base64.b64encode(buffered2.getvalue()).decode()
                
                show_qr = True
            else:
                flash('请填写所有地址信息')

        return render_template('payment.html', item=item, show_qr=show_qr, wx_qr=wx_qr, ali_qr=ali_qr)

    @app.route('/item/<int:item_id>/confirm_payment', methods=['POST'])
    @login_required
    def confirm_payment(item_id):
        item = Item.query.get_or_404(item_id)
        if item.highest_bidder_id != current_user.id:
            return redirect(url_for('index'))
            
        item.payment_status = 'paid'
        db.session.commit()
        
        # Notify Seller
        send_system_message(item.id, item.seller_id, f"订单 {item.order_hash} 已付款。请尽快安排发货。收货人：{item.shipping_name}，地址：{item.shipping_address}")
        
        flash('支付确认成功！')
        return redirect(url_for('item_detail', item_id=item_id))

    @app.route('/item/<int:item_id>/ship', methods=['POST'])
    @login_required
    def ship_item(item_id):
        item = Item.query.get_or_404(item_id)
        
        # 验证权限：只有卖家能发货
        if item.seller_id != current_user.id:
            flash('您无权操作此订单')
            return redirect(url_for('my_auctions'))
            
        if item.payment_status != 'paid':
            flash('买家尚未付款，无法发货')
            return redirect(url_for('my_auctions'))
            
        tracking_number = request.form.get('tracking_number')
        if not tracking_number:
            flash('请输入快递单号')
            return redirect(url_for('my_auctions'))
            
        item.tracking_number = tracking_number
        item.shipping_status = 'shipped'
        db.session.commit()
        
        # 通知买家
        send_system_message(item.id, item.highest_bidder_id, f"您的订单 {item.order_hash} 已发货！快递单号：{tracking_number}")
        
        flash('发货成功')
        return redirect(url_for('my_auctions'))

    @app.route('/item/<int:item_id>/confirm_receipt', methods=['POST'])
    @login_required
    def confirm_receipt(item_id):
        item = Item.query.get_or_404(item_id)
        
        # 验证权限：只有买家能收货
        if item.highest_bidder_id != current_user.id:
            flash('您无权操作此订单')
            return redirect(url_for('my_orders'))
            
        if item.shipping_status != 'shipped':
            flash('订单状态不正确，无法确认收货')
            return redirect(url_for('my_orders'))
            
        item.shipping_status = 'received'
        db.session.commit()
        
        # 通知卖家
        send_system_message(item.id, item.seller_id, f"买家已确认收货，订单 {item.order_hash} 完成。")
        
        flash('确认收货成功')
        return redirect(url_for('my_orders'))
