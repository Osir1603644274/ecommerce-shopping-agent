"""Serialize root-authored query-only decisions; never infer labels or inspect candidates."""
from pathlib import Path
from retrieval_runtime import write_once,sha,fingerprint
from reviews import rows
from prepare_test_contracts import validate_proposal

# Each line is an individually authored decision: query | body | necessary purpose |
# substitutable explicit attributes | interpretation. No candidate data was read.
AUTHORED = '''沙发扶手巾一对|沙发扶手巾|沙发扶手覆盖|一对|不添加尺寸或材质。
人参苦瓜荞麦桑叶片降血糖|人参苦瓜荞麦桑叶片||人参;苦瓜;荞麦;桑叶;片剂;降血糖文本声明|仅判断商品文本声明，不确认医疗效果或安全性。
直播传输器|直播传输器|直播传输用途||不指定音视频接口、协议或无线形式。
金丝绒上衣女款大全|上衣||金丝绒;女款|大全是浏览措辞，不要求单件商品包含全部款式。
xr充电线头|充电线或充电接头|适配xr充电||线头可能指线材端头或充电头，不限定接口；xr保留原型号，不增添品牌证明。
香肠面包批发|香肠面包||批发|不添加数量下限。
居家服女外穿|居家服||女款;可外穿|外穿按文本或普通服装语义判断，不要求指定款式。
短袖女款正肩|短袖上衣||女款;正肩|不添加纯棉等条件。
机械革命极光x白色|笔记本电脑||机械革命;极光x;白色|不添加处理器、显卡或年份。
小刀青蜂侠前置灯泡改装|前置灯泡|适配小刀青蜂侠;前灯改装用途||兼容条件区别于灯泡制造品牌，不猜接口。
卡皮巴拉鞋子|鞋子||卡皮巴拉主题|不把主题自动当制造品牌。
西游记衣服套装|衣服套装||西游记主题|不限定角色、年龄、性别或演出用途。
玉米面批发|玉米面||批发|不添加数量门槛。
球拍儿童|球拍||儿童适用|未指定球种，不补成羽毛球或网球。
独释品牌女装|服装||独释品牌;女款|保留原品牌拼写。
老牌子月饼|月饼||老牌子|要求可见老品牌关联，不自定品牌名单或经营年限。
男士皮卡棉内裤|内裤||男士;皮卡棉|保留皮卡棉原文，不替换成匹马棉或其他材质。
装在电车子上的音响|音响|电车安装使用||不自行限定电动车种类、电压或接口。
面垫包饺子擀面垫子|擀面垫|擀面或包饺子操作||不添加材质、尺寸。
嘎嘎显瘦微胖定制女装|女装||显瘦;微胖适用;定制;嘎嘎|嘎嘎不推定唯一店铺身份；显瘦为可见风格声明，不要求实际减重。
儿童芦荟胶官方旗舰店|芦荟胶||儿童适用;官方旗舰店来源|来源缺证不凭空确认官方身份，不添加品牌。
睡衣冬|睡衣||冬季适用|不强制加绒厚度。
黑色风衣女中长款|风衣||黑色;女款;中长款|仅保留显式款式要求。
连衣裙女款爆款2025新款秋季|连衣裙||女款;爆款声明;2025新款;秋季|爆款按文本声明，不添加销量阈值。
香菜|香菜|||默认食材本体，种子为相关种植物，不直接视为香菜食材。
红薯粉淀粉|红薯淀粉|||不将成品粉条自动视为淀粉本体。
小手机迷你|手机||迷你小型|不添加尺寸数值或智能系统。
宠物蛇|宠物蛇|宠物饲养用途||查询为动物本体，不自动改成玩具、粮食或箱具。
礼炮筒|礼炮筒|||不自行限定动力、填充物或尺寸。
短袖t恤妈妈女|T恤||短袖;女款;妈妈适用|不添加具体年龄。
宿舍电灯|电灯||宿舍使用|不额外规定电压、充电方式或禁用功率。
刺绣棉袄男款|棉袄||刺绣;男款|不把棉袄擅自限定为某填充物比例。
盘扣风衣外套|风衣外套||盘扣|不添加性别和季节。
猪鼻子耳饰|耳饰||猪鼻子造型或主题|不把主题当材质或品牌。
水溜鸟园丁吧唧|吧唧徽章||水溜鸟;园丁|保留名称，不推定作者授权或唯一角色身份；吧唧按徽章理解。
女式家居短裤新款|短裤||女式;家居;新款|不指定年份。
哦哦三妹裙子|裙子||哦哦三妹关联|保留名称，不猜唯一主播、品牌或店铺。
持久留香的香水|香水||持久留香声明|不规定小时数，不鉴定实际留香。
秋冬包|包袋||秋冬风格或使用|不限定性别、大小或材料。
80电瓶|电瓶||80规格|80单位未说明，不补成80伏或80安时；单位歧义如影响等级须保留不确定。
澳大利亚插头转换器|插头转换器|澳大利亚插头或插座适配||不添加电压转换功能。
化妆镜镜前灯|镜前灯|化妆镜照明||不要求含镜子或特定安装方式。
便秘神器工具|便秘用途工具|便秘相关辅助用途||不指定治疗机制，不确认医疗效果、安全性或适用人群。
英睿达铂胜3600|内存条||英睿达;铂胜;3600规格|不添加容量、代数或时序。
美的控温饮水机|饮水机||美的;控温|不要求指定温度档位。
小儿竹板|小儿竹板||小儿;竹板|歧义：竹板用途及本体无法稳定确定，保留原文，不猜药材或乐器。
八品电磁炉|电磁炉||八品|八品保留原文，不认定是品牌或八成新。
佳能6d保护套|保护套|适配佳能6d||不要求佳能制造。
海宝电动三轮车配件大全脚垫|脚垫|适配海宝电动三轮车||大全是检索措辞，目标为脚垫，不要求全部配件。
黑珍珠美容仪|美容仪||黑珍珠|保留名称，不假定制造商或功效机制。
北欧ins星空卧室灯简约现代护眼儿童|卧室灯||北欧风格;ins风格;星空;简约;现代;护眼声明;儿童适用|不鉴定医学护眼效果或添加照度指标。
河南地菜新鲜|地菜||河南;新鲜|保留地菜原词，不擅自替换为特定野菜品种。
手绳磁铁扣|磁铁扣|手绳连接用途||不指定直径或材质。
厨爽大刀芛干|笋干||厨爽;大刀|芛干按普通笋干词形理解，保留原query，不增添产地。
手摇机械计算器|计算器|机械计算;手摇操作||不将普通电子计算器当同用途满足品。
衣柜门合叶|合页|衣柜门安装用途||合叶按合页普通同义理解，不添加尺寸。
hape kristin美瞳|美瞳||hape kristin|保留原拼写，不自动纠正为其他品牌。
u盘1t|U盘||1t容量|按容量标示判断，不鉴定真实容量，不把硬盘当U盘。
费列罗巧克力蓝色盒子|巧克力||费列罗;蓝色盒装|空包装盒不是巧克力本体。
lojel行李箱|行李箱||lojel|不添加尺寸、颜色或材质。
红油肥肠|肥肠食品||红油口味或做法|不强制即食或特定动物来源。
五菱宏光迷你静电条|静电条|适配五菱宏光迷你||不要求汽车制造商品牌生产配件。
贝亲奶嘴新生婴儿|奶嘴||贝亲;新生婴儿适用|不添加孔径、材料，不作安全鉴定。
柯尔鸭粮|鸭粮|柯尔鸭饲喂用途||不添加年龄、配方，不推断未写营养功效。
老虎车电动|老虎车||电动|歧义：老虎车不能稳定确定具体车型，不能按候选反推玩具或代步工具。
保温材料自粘|保温材料|保温用途|自粘|不指定建筑、管道或车辆部位。
能换墨管的钢笔|钢笔|可更换墨管||不把单独墨管当钢笔。
免擦镀膜水蜡|水蜡||免擦;镀膜|不添加品牌、颜色或用量；仅判断声明。
每天必买谱夹|谱夹|固定乐谱||每天必买为推荐性措辞，不要求订阅或每天购买。
迪士尼旗舰店官方旗舰围栏|围栏||迪士尼;官方旗舰来源|不推定儿童或宠物用途，不凭名称确认授权和官方渠道。
标志设计书籍|书籍|标志设计内容||不自行限定软件或语言。
金钱树花土|花土|金钱树种植用途||不添加配方比例或肥效。
启葆奶粉|奶粉||启葆|不纠正品牌，不添加年龄段。
太阳花呢料裤子|裤子||太阳花;呢料|太阳花保留主题或名称关联，不推定唯一品牌。
圆筒硅胶套|硅胶套||圆筒形|不补适配设备、尺寸或厚度。
天猫精灵ccＨ|天猫精灵设备||ccＨ型号|保留原型号字符，不改为其他型号，不添加屏幕尺寸。
折叠锅铲|锅铲||可折叠|不把可拆卸自动等同折叠。
奔驰s350车牌转换底座|车牌转换底座|适配奔驰s350;车牌转换安装||不要求奔驰制造，不添加年份尺寸。
法丽达吉他|吉他||法丽达|不添加琴体、弦数、尺寸或材料。
欧式风格餐桌垫|餐桌垫||欧式风格|不限定材质和尺寸。'''

def main():
    root=Path('D:/agent-datasets/search-closure-v1');target=root/'test-preparation'
    queries=rows(target/'queries.query-only.jsonl');authored={}
    for line in AUTHORED.splitlines():
        q,core,purpose,attributes,notes=line.split('|')
        assert q not in authored
        authored[q]=(core,purpose.split(';') if purpose else [],attributes.split(';') if attributes else [],notes)
    assert len(authored)==80 and set(authored)=={q['query'] for q in queries}
    proposal=[]
    for q in queries:
        core,purpose,attrs,notes=authored[q['query']]
        proposal.append({**q,'core_product':core,'core_purpose_requirements':purpose,'substitutable_attributes':attrs,
            'required_attribute_keys':['本体',*purpose,*attrs], 'intent_policy':'query_intent_ambiguous' if notes.startswith('歧义：') else 'score_product_relevance',
            'interpretation_notes':notes,'no_added_requirements':True})
    validate_proposal(proposal,queries)
    path=target/'root-authored-intents.jsonl';write_once(path,proposal,jsonl=True)
    write_once(target/'ROOT_QUERY_ONLY_REVIEW.json',{'status':'ROOT_READ_ALL80_ORIGINAL_TEST_QUERIES_BEFORE_CANDIDATES',
        'proposal_sha256':sha(path),'model_selection_sha256':sha(root/'final-selection/SELECTION.json'),
        'query_identity_sha256':fingerprint(queries),'human_gold':False,'author_source_sha256':sha(Path(__file__)),
        'test_candidates_read':False,'all_original_queries_retained':True})
    print({'queries':len(proposal),'proposal_sha256':sha(path),'review_sha256':sha(target/'ROOT_QUERY_ONLY_REVIEW.json')})

if __name__=='__main__':main()
