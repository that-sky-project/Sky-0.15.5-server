import os
import logging

# 日志文件路径（默认当前目录下的 nohup.out）
NOHUP_PATH = os.path.join(os.path.dirname(__file__), 'nohup.out')


def clearlog():
    """
    清空 nohup.out 日志文件
    """
    try:
        with open(NOHUP_PATH, 'w', encoding='utf-8') as f:
            f.truncate()
        logging.info("nohup.out 已清空")
        return {"status": "ok", "msg": "日志已清空"}
    except Exception as e:
        logging.exception("清空日志失败")
        return {"status": "error", "msg": str(e)}
