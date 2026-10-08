-- ============================================
-- wbSky 光遇私服 - MySQL/MariaDB 数据库初始化脚本
-- 适用于 1Panel 面板的 MySQL 或 MariaDB
-- ============================================

-- 创建数据库（如果不存在）
-- 注意：在 1Panel 中创建数据库后，可以跳过这一步
-- CREATE DATABASE IF NOT EXISTS wbsky DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
-- USE wbsky;

-- ============================================
-- 用户表
-- ============================================
CREATE TABLE IF NOT EXISTS users (
    id              VARCHAR(64)     PRIMARY KEY              COMMENT '用户UUID',
    device_id       VARCHAR(64)     NOT NULL                 COMMENT '设备ID',
    recovery        VARCHAR(128)    NOT NULL                 COMMENT '恢复码',
    achievements    TEXT            DEFAULT NULL             COMMENT '成就数据(JSON)',
    candles         INT             NOT NULL DEFAULT 3       COMMENT '蜡烛数量',
    unlocks         TEXT            DEFAULT NULL             COMMENT '解锁数据(JSON)',
    collects        TEXT            DEFAULT NULL             COMMENT '收集物数据(JSON)',
    wing_buffs      TEXT            DEFAULT NULL             COMMENT '翼buff数据(JSON)',
    checkpoint      BIGINT          DEFAULT NULL             COMMENT '当前关卡ID',
    outfit_wing     INT             DEFAULT 0                COMMENT '披风装扮',
    outfit_prop     INT             DEFAULT 0                COMMENT '道具装扮',
    outfit_face     INT             DEFAULT 0                COMMENT '脸型装扮',
    outfit_mask     INT             DEFAULT 0                COMMENT '面具装扮',
    outfit_hair     INT             DEFAULT 0                COMMENT '发型装扮',
    outfit_body     INT             DEFAULT 0                COMMENT '裤子装扮',
    outfit_arms     INT             DEFAULT 0                COMMENT '手套装扮',
    outfit_feet     INT             DEFAULT 0                COMMENT '鞋子装扮',
    outfit_hat      INT             DEFAULT 0                COMMENT '帽子装扮',
    outfit_horn     INT             DEFAULT 0                COMMENT '头饰装扮',
    outfit_neck     INT             DEFAULT 0                COMMENT '项链装扮',
    outfit_height   FLOAT           DEFAULT 0                COMMENT '身高',
    purchase        TEXT            DEFAULT NULL             COMMENT '购买数据(JSON)',
    user_data       TEXT            DEFAULT NULL             COMMENT '用户存档数据(JSON)',
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    INDEX idx_device_id (device_id),
    INDEX idx_checkpoint (checkpoint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户表';

-- ============================================
-- 可选：聊天消息表（如果以后做服务端聊天存储可以用）
-- ============================================
-- CREATE TABLE IF NOT EXISTS chat_messages (
--     id          BIGINT AUTO_INCREMENT PRIMARY KEY,
--     from_user   VARCHAR(64)  NOT NULL,
--     to_user     VARCHAR(64)  DEFAULT NULL,
--     room_key    VARCHAR(128) DEFAULT NULL,
--     content     TEXT         NOT NULL,
--     created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
--     INDEX idx_from_user (from_user),
--     INDEX idx_to_user (to_user),
--     INDEX idx_room_key (room_key),
--     INDEX idx_created_at (created_at)
-- ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='聊天消息表';

-- ============================================
-- 1Panel 部署说明：
-- 1. 在 1Panel 中创建 MySQL 数据库，命名为 wbsky
-- 2. 创建数据库用户并授权
-- 3. 导入此 SQL 文件初始化表结构
-- 4. 在 config.json 中配置 MySQL 连接信息：
--    "use_mysql": true,
--    "mysql_host": "127.0.0.1",
--    "mysql_port": 3306,
--    "mysql_user": "你的用户名",
--    "mysql_password": "你的密码",
--    "mysql_database": "wbsky"
-- 5. 安装 pymysql: pip install pymysql
-- 6. 重启服务端即可
-- ============================================
