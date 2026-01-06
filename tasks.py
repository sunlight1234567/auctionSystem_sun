from datetime import datetime, timedelta
import threading
import time
import hashlib
from extensions import db, socketio
from models import Item

def check_auctions(app):
    """后台任务：检查拍卖状态"""
    while True:
        try:
            with app.app_context():
                now = datetime.now()
                
                # 1. 检查已到期的 'active' 拍卖 -> 'ended'
                expired_items = Item.query.filter(Item.status == 'active', Item.end_time <= now).all()
                for item in expired_items:
                    item.status = 'ended'
                    
                    # 如果有获胜者，生成订单哈希
                    if item.highest_bidder_id:
                        # 生成易读的订单编号：ORD + 年月日时分秒 + 4位商品ID (例: ORD202401011200000005)
                        # 这种格式方便后续检索和客服查询
                        timestamp_str = datetime.now().strftime('%Y%m%d%H%M%S')
                        item.order_hash = f"ORD{timestamp_str}{item.id:04d}"

                    db.session.commit()
                    winner_name = item.highest_bidder.username if item.highest_bidder else '无人出价'
                    socketio.emit('auction_ended', {
                        'item_id': item.id, 
                        'winner': winner_name,
                        'order_hash': item.order_hash
                    }, room=f"item_{item.id}")

                # 2. 检查已到开拍时间的 'approved' 拍卖 -> 'active'
                starting_items = Item.query.filter(Item.status == 'approved', Item.start_time <= now).all()
                for item in starting_items:
                    item.status = 'active'
                    db.session.commit()
                    # 可选择通知首页刷新，或在该 Item 的房间里广播
                    print(f"Auction {item.id} started automatically at {now}")

        except Exception as e:
            print(f"Check auction error: {e}")
        time.sleep(10) 
