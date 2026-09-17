-- EVAL-02 咖啡类混淆样本：供已有开发数据卷幂等升级。
INSERT INTO shop (id, name, type_id, address, avg_price, phone, longitude, latitude)
VALUES
    (7, '纸间咖啡书屋', 2, '学院路 18 号', 42, '010-8888-0007', 116.4020, 39.9010),
    (8, '街角共创咖啡', 2, '创业大街 66 号', 39, '010-8888-0008', 116.4150, 39.9020)
ON DUPLICATE KEY UPDATE
    name = VALUES(name),
    type_id = VALUES(type_id),
    address = VALUES(address),
    avg_price = VALUES(avg_price),
    phone = VALUES(phone),
    longitude = VALUES(longitude),
    latitude = VALUES(latitude);

INSERT INTO review (id, shop_id, content, tags)
VALUES
    ('review-013', 3, '工作日下午通常比较安静，插座集中在靠窗座位；店里没有会议室，也不太适合多人讨论。', '["咖啡","工作日","插座","单人办公"]'),
    ('review-014', 7, '书架旁座位光线柔和，店内要求通话尽量轻声，很适合看书；但全店只有两个插座，长时间用电脑不方便。', '["咖啡","阅读","安静","插座少"]'),
    ('review-015', 7, '手冲和冷萃选择很多，人均约 45 元。双人小桌间距较近，适合安静聊天，不适合开线上会议。', '["咖啡","手冲","聊天","不适合会议"]'),
    ('review-016', 7, '周末下午阅读区经常坐满，店员会在满座时提醒限时两小时；工作日上午座位更充足。', '["咖啡","周末","限时","座位"]'),
    ('review-017', 8, '共享长桌配有很多插座和高速无线网络，工作日早上适合带电脑办公，也能预订四人讨论桌。', '["咖啡","插座","无线网络","团队办公"]'),
    ('review-018', 8, '工作日下午常有创业团队讨论，虽然适合协作和开会，但环境比普通咖啡店嘈杂，不适合需要安静专注的人。', '["咖啡","团队讨论","嘈杂","会议"]'),
    ('review-019', 8, '早上八点营业，有咖啡加三明治套餐，人均约 38 元；周末中午家庭客较多，空位和插座都比较紧张。', '["咖啡","早餐","预算","周末"]')
ON DUPLICATE KEY UPDATE
    shop_id = VALUES(shop_id),
    content = VALUES(content),
    tags = VALUES(tags),
    updated_at = CURRENT_TIMESTAMP;
