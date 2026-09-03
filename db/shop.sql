-- 开发期 seed 表：直接重建，保证带上新列 phone（生产环境才用 ALTER 迁移）
DROP TABLE IF EXISTS shop;

CREATE TABLE shop (
    id BIGINT PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    type_id BIGINT NOT NULL,
    address VARCHAR(255) NOT NULL,
    avg_price INT NOT NULL,
    phone VARCHAR(32),
    longitude DOUBLE NOT NULL,
    latitude DOUBLE NOT NULL,
    coordinate_system VARCHAR(16) NOT NULL DEFAULT 'BD-09',
    district VARCHAR(32),
    anchor_place_id VARCHAR(64),
    anchor_place_name VARCHAR(128),
    data_nature VARCHAR(32) NOT NULL DEFAULT 'synthetic_seed',
    source VARCHAR(32) NOT NULL DEFAULT 'seed',
    source_entity_id VARCHAR(128),
    original_name VARCHAR(128),
    original_address VARCHAR(255),
    original_longitude DOUBLE,
    original_latitude DOUBLE,
    localization_version VARCHAR(32),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

INSERT INTO shop (id, name, type_id, address, avg_price, phone, longitude, latitude)
VALUES
    (1, '巷子口火锅', 1, '人民路 12 号', 88, '010-8888-0001', 116.3970, 39.9000),
    (2, '深夜食堂烧烤', 1, '建设街 45 号', 65, '010-8888-0002', 116.3980, 39.9010),
    (3, '清晨手冲咖啡', 2, '文化广场 3 号', 35, '010-8888-0003', 116.4000, 39.9000),
    (4, '城市光影影院', 3, '中央大道 100 号', 45, '010-8888-0004', 116.4100, 39.9050),
    (5, '安睡精品酒店', 4, '湖滨北路 8 号', 320, '010-8888-0005', 116.3900, 39.8950),
    (6, '活力健身工坊', 5, '体育馆东门', 150, '010-8888-0006', 116.4050, 39.8980),
    (7, '纸间咖啡书屋', 2, '学院路 18 号', 42, '010-8888-0007', 116.4020, 39.9010),
    (8, '街角共创咖啡', 2, '创业大街 66 号', 39, '010-8888-0008', 116.4150, 39.9020),
    (9, '河畔家常菜', 1, '沿河路 21 号', 58, '010-8888-0009', 116.3960, 39.9040),
    (10, '星河艺术影城', 3, '大学城西街 9 号', 38, '010-8888-0010', 116.4120, 39.9080),
    (11, '欢乐巨幕影城', 3, '时代商场 6 层', 72, '010-8888-0011', 116.4200, 39.9100),
    (12, '城际商务酒店', 4, '高铁站南广场 2 号', 260, '010-8888-0012', 116.3920, 39.8920),
    (13, '湖畔青年旅舍', 4, '湖滨南路 17 号', 88, '010-8888-0013', 116.3880, 39.8900),
    (14, '静力瑜伽馆', 5, '梧桐街 30 号', 120, '010-8888-0014', 116.4060, 39.8960),
    (15, '全天候综合健身', 5, '新城大道 88 号', 180, '010-8888-0015', 116.4090, 39.8990);
