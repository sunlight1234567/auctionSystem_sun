
from app import create_app
from extensions import db
from sqlalchemy import text

app = create_app()

def add_columns():
    with app.app_context():
        # 获取数据库连接
        with db.engine.connect() as conn:
            # 开启事务
            trans = conn.begin()
            try:
                print("尝试添加 tracking_number 字段...")
                try:
                    conn.execute(text("ALTER TABLE items ADD COLUMN tracking_number VARCHAR(100) NULL;"))
                    print("成功添加 tracking_number")
                except Exception as e:
                    print(f"添加 tracking_number 失败 (可能是已存在): {e}")

                print("尝试添加 shipping_status 字段...")
                try:
                    conn.execute(text("ALTER TABLE items ADD COLUMN shipping_status VARCHAR(20) DEFAULT 'unshipped';"))
                    print("成功添加 shipping_status")
                except Exception as e:
                    print(f"添加 shipping_status 失败 (可能是已存在): {e}")
                
                trans.commit()
                print("数据库更新完成！")
            except Exception as e:
                trans.rollback()
                print(f"发生错误，操作回滚: {e}")

if __name__ == "__main__":
    add_columns()
