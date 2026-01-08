from models import User, Item, ChatSession, Message
from extensions import db, socketio
from datetime import datetime

def send_system_message(item_id, receiver_id, content, skip_notification=False):
    """
    发送系统消息（以管理员身份）到用户的收件箱
    """
    try:
        # 获取管理员账户
        admin = User.query.filter_by(role='admin').first()
        if not admin:
            # 尝试查找任意管理员，或者如果不存则需要手动创建（这里假设至少有一个）
            print("System Message Error: No admin user found.")
            return

        # 如果接收者自己就是管理员，不需要发送系统消息给自己
        if receiver_id == admin.id:
            return

        item = Item.query.get(item_id)
        if not item:
            return

        # 确定会话双方
        # 逻辑：为了让接收者在收件箱看到"Admin"，我们需要创建一个会话
        # 其中一方是 receiver_id, 另一方是 admin.id
        
        # 为了保持一致性：
        # 如果 receiver 是该商品的 seller => session.seller_id=receiver, session.buyer_id=admin
        # 否则 (receiver是买家) => session.buyer_id=receiver, session.seller_id=admin
        
        if receiver_id == item.seller_id:
            s_id = receiver_id # User
            b_id = admin.id    # Admin
        else:
            b_id = receiver_id # User
            s_id = admin.id    # Admin
            
        session = ChatSession.query.filter_by(item_id=item_id, buyer_id=b_id, seller_id=s_id).first()
        
        if not session:
            session = ChatSession(item_id=item_id, buyer_id=b_id, seller_id=s_id)
            db.session.add(session)
            db.session.flush() # 获取 session.id
            
        # 记录消息到数据库
        new_msg = Message(
            chat_session_id=session.id,
            sender_id=admin.id,
            content=content,
            timestamp=datetime.now()
        )
        db.session.add(new_msg)

        # 更新消息内容
        session.last_message = f"[系统通知] {content}"
        session.updated_at = datetime.now()
        
        # 增加未读计数 (Ensure not None)
        if session.buyer_unread is None:
            session.buyer_unread = 0
        if session.seller_unread is None:
            session.seller_unread = 0

        if receiver_id == b_id:
            session.buyer_unread += 1
        else:
            session.seller_unread += 1
            
        db.session.commit()
        
        # 实时推送通知 (如果在线)
        # 注意：socketio event 需要和前端 chat.js 监听的一致
        # 前端 chat.js 有监听 'new_message' 用于当前聊天窗口，和 'new_chat_notification' 用于全局提示
        
        # 1. 全局提示
        if not skip_notification:
            socketio.emit('new_chat_notification', {'msg': '您有一条新系统消息'}, room=f"user_{receiver_id}")
        
        # 2. 如果用户恰好打开了这个对话窗口 (room id规则见 chat.html)
        # room = 'chat_item_{item_id}_{min_uid}_{max_uid}'
        u1 = min(b_id, s_id)
        u2 = max(b_id, s_id)
        room_id = f'chat_item_{item_id}_{u1}_{u2}'
        
        socketio.emit('new_message', {
            'sender': '管理员',
            'sender_id': admin.id,
            'msg': content,
            'timestamp': new_msg.timestamp.isoformat(),
            'item_id': item_id,
            'avatar': admin.avatar
        }, room=room_id)

    except Exception as e:
        print(f"Failed to send system message: {e}")
